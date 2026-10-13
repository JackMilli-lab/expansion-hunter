"""Calendar context and observed large trades; neither implies whale identity."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from .live_models import LiveMarketEvent
from .models import Model, aware


class FlowSettings(Model):
    minimum_quote_notional: float = 100000
    window_seconds: int = 300
    stale_after_seconds: int = 30
    retained_large_trades: int = 256

    def validated(self):
        if (not isfinite(self.minimum_quote_notional) or self.minimum_quote_notional <= 0 or self.window_seconds < 1
                or self.stale_after_seconds < 1 or not 1 <= self.retained_large_trades <= 10000):
            raise ValueError("invalid observed-flow settings")
        return self


def session_context(as_of: datetime) -> dict:
    """Predefined local-clock research windows, with DST handled by IANA rules.

    Crypto trades 24/7. These are calendar windows, not observed liquidity or
    exchange-specific opening hours, and they carry zero production score weight.
    """
    when = aware(as_of)
    definitions = (
        ("TOKYO", "Asia/Tokyo", 9 * 60, 17 * 60),
        ("LONDON", "Europe/London", 8 * 60, 16 * 60 + 30),
        ("NEW_YORK", "America/New_York", 9 * 60 + 30, 16 * 60),
    )
    windows = []
    for name, timezone, start, end in definitions:
        local = when.astimezone(ZoneInfo(timezone))
        minute = local.hour * 60 + local.minute
        active = local.weekday() < 5 and start <= minute < end
        windows.append({"name": name, "timezone": timezone, "local_time": local.isoformat(),
                        "active": active, "starts_local": f"{start // 60:02}:{start % 60:02}",
                        "ends_local": f"{end // 60:02}:{end % 60:02}"})
    return {"as_of": when.isoformat(), "source": "PREDEFINED_CALENDAR_RULES", "quality": "DERIVED",
            "crypto_market": "24_7", "active_windows": [w["name"] for w in windows if w["active"]],
            "windows": windows, "research_weight": 0,
            "warning": "Calendar overlap is not proof of liquidity, institutional activity or predictive value."}


@dataclass
class _ObservedFlow:
    epoch: int
    last_received: datetime
    last_exchange: datetime
    last_sequence: int | None
    source: str
    trades: deque = field(default_factory=deque)
    truncated: bool = False
    continuous: bool = True


class LargeTradeTracker:
    """Bounded native aggregate-trade evidence with explicit unknown identity.

    One aggregate trade may combine exchange fills. It reveals neither a wallet
    nor an institution. No order-book cancellation is counted as a trade.
    """

    def __init__(self, settings: FlowSettings | None = None):
        self.settings = (settings or FlowSettings()).validated()
        self._states: dict[str, _ObservedFlow] = {}

    def observe(self, event: LiveMarketEvent) -> bool:
        if (event.kind != "TRADE" or event.exchange_timestamp is None
                or event.source != "binance-public-ws"):
            return False
        if event.exchange_timestamp > event.received_timestamp:
            return False
        notional = event.price * event.quantity
        if not isfinite(notional):
            return False
        old = self._states.get(event.symbol)
        if old and (event.stream_epoch < old.epoch or event.received_timestamp < old.last_received
                    or (event.stream_epoch == old.epoch and event.sequence_id is not None
                        and old.last_sequence is not None and event.sequence_id <= old.last_sequence)):
            return False
        gap = bool(event.sequence_gap or (
            old and event.stream_epoch == old.epoch and event.sequence_supported
            and event.sequence_id is not None and old.last_sequence is not None
            and event.sequence_id != old.last_sequence + 1))
        if old is None or event.stream_epoch != old.epoch or gap:
            old = _ObservedFlow(event.stream_epoch, event.received_timestamp, event.exchange_timestamp,
                                event.sequence_id, event.source, continuous=not gap)
            self._states[event.symbol] = old
        old.last_received = event.received_timestamp
        old.last_exchange = event.exchange_timestamp
        old.last_sequence = event.sequence_id
        cutoff = event.exchange_timestamp - timedelta(seconds=self.settings.window_seconds)
        while old.trades and old.trades[0]["event_timestamp"] < cutoff:
            old.trades.popleft()
        if notional >= self.settings.minimum_quote_notional:
            if len(old.trades) >= self.settings.retained_large_trades:
                old.trades.popleft()
                old.truncated = True
            old.trades.append({"event_timestamp": event.exchange_timestamp,
                               "received_timestamp": event.received_timestamp,
                               "quote_notional": notional, "price": event.price,
                               "quantity": event.quantity,
                               "aggressor": "SELL" if event.buyer_is_maker else "BUY",
                               "sequence_id": event.sequence_id, "source": event.source})
        return True

    def remove_symbol(self, symbol: str):
        self._states.pop(symbol, None)

    def snapshot(self, symbol: str, as_of: datetime, quote_currency: str = "USDT") -> dict:
        when = aware(as_of)
        state = self._states.get(symbol)
        report = {"symbol": symbol, "as_of": when.isoformat(), "quality": "UNAVAILABLE",
                  "source": "NATIVE_OBSERVED_AGGREGATE_TRADES", "quote_currency": quote_currency,
                  "window_seconds": self.settings.window_seconds,
                  "minimum_quote_notional": self.settings.minimum_quote_notional,
                  "whale_identity": "UNAVAILABLE", "institutional_identity": "UNAVAILABLE",
                  "on_chain_wallet_flow": "UNAVAILABLE", "research_weight": 0,
                  "large_trade_count": None, "buy_quote_notional": None, "sell_quote_notional": None,
                  "delta_quote_notional": None, "trades": [],
                  "warning": "Large exchange trades are observed activity, not identified whale or institutional flow."}
        if state is None:
            return report
        if state.last_received > when or state.last_exchange > when:
            raise ValueError("mutable flow cannot reconstruct an earlier decision; replay persisted observations")
        age = max((when - state.last_received).total_seconds(), (when - state.last_exchange).total_seconds())
        cutoff = when - timedelta(seconds=self.settings.window_seconds)
        trades = [item for item in state.trades if item["event_timestamp"] >= cutoff]
        buy = sum(item["quote_notional"] for item in trades if item["aggressor"] == "BUY")
        sell = sum(item["quote_notional"] for item in trades if item["aggressor"] == "SELL")
        report.update({"quality": "STALE" if age > self.settings.stale_after_seconds else "AVAILABLE",
                       "received_timestamp": state.last_received.isoformat(),
                       "source_timestamp": state.last_exchange.isoformat(), "age_seconds": age,
                       "provider": state.source,
                       "large_trade_count": len(trades), "buy_quote_notional": buy,
                       "sell_quote_notional": sell, "delta_quote_notional": buy - sell,
                       "continuous": state.continuous, "retention_truncated": state.truncated,
                       "aggregation_scope": "RETAINED_OBSERVED_TRADES_ONLY" if state.truncated else "OBSERVED_CONNECTION_EPOCH_WINDOW",
                       "trades": [{k: v.isoformat() if isinstance(v, datetime) else v for k, v in item.items()}
                                  for item in trades]})
        return report
