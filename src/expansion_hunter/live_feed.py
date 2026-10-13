"""Bounded incremental live state, isolated from slow broad-universe scans."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .live_models import LiveCandle, LiveFeedSettings, LiveMarketEvent, normalize_symbols
from .models import TIMEFRAMES, Candle, aware


@dataclass
class _SymbolState:
    latest: dict[str, LiveMarketEvent] = field(default_factory=dict)
    closed: dict[str, deque[Candle]] = field(default_factory=dict)
    opened: dict[str, LiveCandle] = field(default_factory=dict)
    open_receipt: dict[str, datetime] = field(default_factory=dict)
    sequences: dict[str, tuple[int, int]] = field(default_factory=dict)
    candle_gaps: dict[str, list[tuple[datetime, datetime]]] = field(default_factory=dict)
    history_versions: dict[str, int] = field(default_factory=dict)
    trade_epoch: int | None = None
    cvd_started_at: datetime | None = None
    taker_buy: float = 0.0
    taker_sell: float = 0.0
    quote_buy: float = 0.0
    quote_sell: float = 0.0
    aggregate_trade_count: int = 0
    flow_buckets: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=1440))
    trade_continuity: bool = True


class LiveFeedState:
    """One worker owns this state; callers receive copies of completed history.

    CVD covers only native aggregate trades observed in the current connection
    epoch (or since the latest detected gap). It is never historical exchange CVD.
    """

    def __init__(self, settings: LiveFeedSettings | None = None) -> None:
        self.settings = settings or LiveFeedSettings()
        self._symbols: dict[str, _SymbolState] = {}
        self.events_accepted = 0
        self.events_rejected = 0
        self.sequence_gaps = 0

    def _state(self, symbol: str) -> _SymbolState:
        symbol = normalize_symbols([symbol])[0]
        if symbol not in self._symbols:
            if len(self._symbols) >= self.settings.max_symbols:
                raise ValueError("live state symbol capacity exceeded")
            self._symbols[symbol] = _SymbolState()
        return self._symbols[symbol]

    def remove_symbol(self, symbol: str) -> None:
        self._symbols.pop(symbol.upper(), None)

    def apply(self, event: LiveMarketEvent) -> bool:
        state = self._state(event.symbol)
        key = event.kind if event.candle is None else f"CANDLE:{event.candle.timeframe}"
        previous = state.latest.get(key)
        if previous and (
            event.stream_epoch < previous.stream_epoch
            or event.received_timestamp < previous.received_timestamp
            or (
                event.exchange_timestamp
                and previous.exchange_timestamp
                and event.exchange_timestamp < previous.exchange_timestamp
            )
        ):
            self.events_rejected += 1
            return False
        if event.sequence_id is not None:
            sequence = state.sequences.get(key)
            if sequence and event.stream_epoch == sequence[0] and event.sequence_id <= sequence[1]:
                self.events_rejected += 1
                return False
            if (
                sequence
                and event.stream_epoch == sequence[0]
                and event.sequence_supported
                and event.sequence_id != sequence[1] + 1
            ):
                event = event.model_copy(update={"sequence_gap": True})
        if event.candle:
            candle = event.candle
            timeframe = candle.timeframe
            bars = state.closed.setdefault(timeframe, deque(maxlen=self.settings.candle_capacity))
            if bars and candle.open_time <= bars[-1].open_time:
                # The first finalized candle is immutable; late corrections cannot repaint signals.
                self.events_rejected += 1
                return False
            opened = state.opened.get(timeframe)
            if opened and candle.open_time < opened.open_time:
                self.events_rejected += 1
                return False
            if candle.complete:
                closed = candle.as_closed(event.received_timestamp, event.source)
                if bars and bars[-1].close_time < closed.open_time:
                    gaps = state.candle_gaps.setdefault(timeframe, [])
                    gaps.append((bars[-1].close_time, closed.open_time))
                    del gaps[: -self.settings.candle_capacity]
                bars.append(closed)
                state.history_versions[timeframe] = state.history_versions.get(timeframe, 0) + 1
                state.opened.pop(timeframe, None)
                state.open_receipt.pop(timeframe, None)
            else:
                state.opened[timeframe] = candle.model_copy(deep=True)
                state.open_receipt[timeframe] = event.received_timestamp
        if event.kind == "TRADE":
            if state.trade_epoch != event.stream_epoch or event.sequence_gap:
                state.trade_epoch = event.stream_epoch
                state.cvd_started_at = event.exchange_timestamp or event.received_timestamp
                state.taker_buy = state.taker_sell = state.quote_buy = state.quote_sell = 0
                state.aggregate_trade_count = 0
                state.flow_buckets.clear()
                state.trade_continuity = not event.sequence_gap
            if event.sequence_gap:
                self.sequence_gaps += 1
            quantity = event.quantity
            price = event.price
            assert quantity is not None and price is not None
            buyer_taker = not event.buyer_is_maker
            if buyer_taker:
                state.taker_buy += quantity
                state.quote_buy += quantity * price
            else:
                state.taker_sell += quantity
                state.quote_sell += quantity * price
            state.aggregate_trade_count += 1
            source_time = event.exchange_timestamp or event.received_timestamp
            bucket_time = source_time.replace(second=0, microsecond=0)
            if not state.flow_buckets or state.flow_buckets[-1]["minute"] != bucket_time:
                state.flow_buckets.append({"minute": bucket_time, "buy": 0.0, "sell": 0.0, "count": 0})
            bucket = state.flow_buckets[-1]
            bucket["buy" if buyer_taker else "sell"] += quantity
            bucket["count"] += 1
        if event.sequence_id is not None:
            state.sequences[key] = (event.stream_epoch, event.sequence_id)
        state.latest[key] = event.model_copy(deep=True)
        self.events_accepted += 1
        return True

    def seed_candles(self, symbol: str, timeframe: str, candles: list[Candle], as_of: datetime) -> None:
        """Optional public REST warmup; future/unavailable/open bars are rejected."""
        if timeframe not in TIMEFRAMES:
            raise ValueError("unsupported timeframe")
        as_of = aware(as_of)
        if any(bar.close_time > as_of or bar.available_at > as_of for bar in candles):
            raise ValueError("warmup history includes future or unavailable candles")
        if any((bar.close_time - bar.open_time).total_seconds() != TIMEFRAMES[timeframe] for bar in candles):
            raise ValueError("warmup candle duration differs from timeframe")
        if len({bar.open_time for bar in candles}) != len(candles):
            raise ValueError("duplicate warmup candles")
        state = self._state(symbol)
        existing = {bar.open_time: bar for bar in state.closed.get(timeframe, [])}
        # Finalized live bars win over later-downloaded replacements.
        merged = {bar.open_time: bar.model_copy() for bar in candles}
        merged.update(existing)
        ordered = sorted(merged.values(), key=lambda bar: bar.open_time)[-self.settings.candle_capacity :]
        state.closed[timeframe] = deque(ordered, maxlen=self.settings.candle_capacity)
        state.history_versions[timeframe] = state.history_versions.get(timeframe, 0) + 1
        state.candle_gaps[timeframe] = [
            (left.close_time, right.open_time)
            for left, right in zip(ordered, ordered[1:], strict=False)
            if left.close_time != right.open_time
        ]

    def candles(self, symbol: str, timeframe: str, as_of: datetime | None = None,
                *, limit: int | None = None) -> list[Candle]:
        state = self._symbols.get(symbol.upper())
        bars = state.closed.get(timeframe, []) if state else []
        known = [bar for bar in bars
                 if as_of is None or (bar.close_time <= as_of and bar.available_at <= as_of)]
        if limit is not None:
            if limit < 0:
                raise ValueError("candle limit cannot be negative")
            known = known[-limit:] if limit else []
        # Candle fields are exclusively numbers, strings and immutable datetime
        # values. Separate model objects protect the cache from caller edits
        # without recursively copying every immutable timestamp on every tick.
        return [
            bar.model_copy() for bar in known
        ]

    def history_version(self, symbol: str, timeframe: str) -> int:
        state = self._symbols.get(symbol.upper())
        return state.history_versions.get(timeframe, 0) if state else 0

    def open_candle(self, symbol: str, timeframe: str) -> LiveCandle | None:
        state = self._symbols.get(symbol.upper())
        candle = state.opened.get(timeframe) if state else None
        return candle.model_copy(deep=True) if candle else None

    def latest_event(self, symbol: str, kind: str, timeframe: str | None = None) -> LiveMarketEvent | None:
        state = self._symbols.get(symbol.upper())
        key = kind if timeframe is None else f"{kind}:{timeframe}"
        event = state.latest.get(key) if state else None
        return event.model_copy(deep=True) if event else None

    def _quality(self, event: LiveMarketEvent | None, as_of: datetime) -> str:
        if event is None:
            return "UNAVAILABLE"
        if event.received_timestamp > as_of or (
            event.exchange_timestamp and event.exchange_timestamp > as_of
        ):
            return "UNAVAILABLE"
        ages = [(as_of - event.received_timestamp).total_seconds()]
        if event.exchange_timestamp:
            ages.append((as_of - event.exchange_timestamp).total_seconds())
        return "STALE" if max(ages) > self.settings.stale_after_seconds else "AVAILABLE"

    def snapshot(self, symbol: str, as_of: datetime, *, include_history: bool = True) -> dict[str, Any]:
        as_of = aware(as_of)
        state = self._symbols.get(symbol.upper())
        if state is None:
            return {
                "symbol": symbol.upper(),
                "as_of": as_of.isoformat(),
                "data_quality": {
                    "spot_trades": "UNAVAILABLE",
                    "best_bid_ask": "UNAVAILABLE",
                    "order_book": "UNAVAILABLE",
                    "futures": "UNAVAILABLE",
                    "spot_cvd": "UNAVAILABLE",
                },
            }
        trade = state.latest.get("TRADE")
        ticker = state.latest.get("BOOK_TICKER")
        depth = state.latest.get("DEPTH")
        candidates = [event for event in state.latest.values() if event.received_timestamp <= as_of]
        latest = max(candidates, key=lambda event: event.received_timestamp) if candidates else None
        quality = {
            "spot_trades": self._quality(trade, as_of),
            "best_bid_ask": self._quality(ticker, as_of),
            "order_book": self._quality(depth, as_of),
            "futures": "UNAVAILABLE",
            "spot_cvd": self._quality(trade, as_of),
        }
        quality.update(
            {
                frame: self._quality(state.latest.get(f"CANDLE:{frame}"), as_of)
                for frame in self.settings.timeframes
            }
        )
        price_candidates = [
            event for event in candidates if event.price is not None or event.candle is not None
        ]
        price_event = (
            max(price_candidates, key=lambda event: event.received_timestamp) if price_candidates else None
        )
        price = (
            price_event.price
            if price_event and price_event.price is not None
            else (price_event.candle.close if price_event and price_event.candle else None)
        )
        flow_usable = (
            trade is not None
            and trade.received_timestamp <= as_of
            and (trade.exchange_timestamp is None or trade.exchange_timestamp <= as_of)
        )
        return {
            "symbol": symbol.upper(),
            "as_of": as_of.isoformat(),
            "price": price,
            "price_quality": self._quality(price_event, as_of),
            "price_source": price_event.source if price_event else None,
            "price_source_timestamp": (
                price_event.exchange_timestamp or price_event.received_timestamp
            ).isoformat() if price_event else None,
            "price_received_timestamp": price_event.received_timestamp.isoformat() if price_event else None,
            "last_received_timestamp": latest.received_timestamp.isoformat() if latest else None,
            "last_exchange_timestamp": latest.exchange_timestamp.isoformat()
            if latest and latest.exchange_timestamp
            else None,
            "feed_latency_ms": latest.feed_latency_ms if latest else None,
            "processing_latency_ms": latest.processing_latency_ms if latest else None,
            "data_quality": quality,
            "book_ticker": ticker.model_dump(mode="json")
            if ticker and ticker.received_timestamp <= as_of
            else None,
            "depth": depth.model_dump(mode="json") if depth and depth.received_timestamp <= as_of else None,
            "open_candles": {
                frame: candle.model_dump(mode="json")
                for frame, candle in state.opened.items()
                if state.open_receipt[frame] <= as_of
            },
            "closed_candles": {
                frame: [bar.model_dump(mode="json") for bar in self.candles(symbol, frame, as_of)]
                for frame in state.closed
                if include_history
            },
            "candle_gaps": {
                frame: [[start.isoformat(), end.isoformat()] for start, end in gaps if end <= as_of]
                for frame, gaps in state.candle_gaps.items()
            },
            "order_flow": {
                "source": "NATIVE_OBSERVED_AGGREGATE_TRADES",
                "scope": "CONNECTION_EPOCH_OR_SINCE_LAST_GAP",
                "historical_cvd": "UNAVAILABLE",
                "started_at": state.cvd_started_at.isoformat()
                if flow_usable and state.cvd_started_at
                else None,
                "taker_buy_volume": state.taker_buy if flow_usable else None,
                "taker_sell_volume": state.taker_sell if flow_usable else None,
                "taker_buy_quote_volume": state.quote_buy if flow_usable else None,
                "taker_sell_quote_volume": state.quote_sell if flow_usable else None,
                "delta": state.taker_buy - state.taker_sell if flow_usable else None,
                "cvd": state.taker_buy - state.taker_sell if flow_usable else None,
                "aggregate_trade_count": state.aggregate_trade_count if flow_usable else None,
                "continuous_since_session_start": state.trade_continuity if flow_usable else None,
            },
        }

    def health(self, as_of: datetime) -> dict[str, Any]:
        # Health checks must not serialize every cached candle on the ignition hot path.
        snapshots = {
            symbol: {
                "spot_trades": self._quality(state.latest.get("TRADE"), as_of),
                "best_bid_ask": self._quality(state.latest.get("BOOK_TICKER"), as_of),
                "order_book": self._quality(state.latest.get("DEPTH"), as_of),
                **{
                    frame: self._quality(state.latest.get(f"CANDLE:{frame}"), as_of)
                    for frame in self.settings.timeframes
                },
            }
            for symbol, state in self._symbols.items()
        }
        return {
            "symbols": len(self._symbols),
            "events_accepted": self.events_accepted,
            "events_rejected": self.events_rejected,
            "sequence_gaps": self.sequence_gaps,
            "per_symbol_data_quality": snapshots,
        }

    def flow_window(
        self, symbol: str, as_of: datetime, seconds: int = 300
    ) -> dict[str, float | int | str | None]:
        """Minute-aggregated observations only; a partial first minute is explicit."""
        if seconds < 60:
            raise ValueError("minute buckets cannot support sub-minute flow windows")
        state = self._symbols.get(symbol.upper())
        if not state or state.cvd_started_at is None:
            return {"quality": "UNAVAILABLE", "delta": None, "aggregate_trade_count": None}
        if state.latest["TRADE"].received_timestamp > as_of:
            raise ValueError("mutable live state cannot reconstruct prior flow; use persisted event replay")
        cutoff = as_of - timedelta(seconds=seconds)
        buckets = [bucket for bucket in state.flow_buckets if cutoff <= bucket["minute"] <= as_of]
        return {
            "quality": self._quality(state.latest["TRADE"], as_of),
            "aggregation": "COMPLETE_MINUTE_BUCKETS_WITH_POSSIBLY_PARTIAL_CURRENT_MINUTE",
            "window_seconds": seconds,
            "observed_since": state.cvd_started_at.isoformat(),
            "taker_buy_volume": sum(bucket["buy"] for bucket in buckets),
            "taker_sell_volume": sum(bucket["sell"] for bucket in buckets),
            "delta": sum(bucket["buy"] - bucket["sell"] for bucket in buckets),
            "aggregate_trade_count": sum(bucket["count"] for bucket in buckets),
        }
