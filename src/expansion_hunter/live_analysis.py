"""Causal chart overlays and an explicitly categorized liquidity price map.

Candle analysis is intended for warmup and completed 5m updates, not every
trade. ``order_book_analysis`` is the inexpensive snapshot-only path. Native
resting quantities, inferred structure, and estimated historical traded volume
remain separate. Fibonacci relationships are research hypotheses with zero
production weight; these descriptive scores are not probabilities.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from pydantic import Field, model_validator

from .avwap import anchored_vwap
from .live_models import LiveMarketEvent
from .models import TIMEFRAMES, AssetSnapshot, Candle, Model, aware
from .session_analysis import analyze_sessions
from .technical import _ema, _volume_profile, _wilder, analyze, confirmed_swings

PHI = 1.618033988749895
RETRACEMENTS = (0.236, 0.382, 0.500, 0.618, 0.650, 0.705, 0.786)
EXTENSIONS = (1.000, 1.272, 1.414, 1.618, 2.000, 2.618, 4.236)
FIB_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1D", "1W")
ACTIVE_FIB_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1D")
ACTIVE_FIB_RETRACEMENTS = (0.382, 0.500, 0.618, 0.650, 0.705, 0.786)
ACTIVE_FIB_EXTENSIONS = (1.272, 1.414, 1.618)


class LiveAnalysisSettings(Model):
    auto_fibonacci_enabled: bool = True
    swing_left: int = Field(default=3, ge=1, le=20)
    swing_right: int = Field(default=3, ge=1, le=20)
    minimum_impulse_atr: float = Field(default=1.0, gt=0)
    golden_pocket_low: float = Field(default=0.618, gt=0, lt=1)
    golden_pocket_high: float = Field(default=0.650, gt=0, lt=1)
    confluence_distance_atr: float = Field(default=0.25, gt=0, le=2)
    independent_anchor_tolerance_atr: float = Field(default=0.15, gt=0, le=2)
    zone_width_atr: float = Field(default=0.1, gt=0, le=1)
    book_stale_seconds: float = Field(default=15, gt=0)
    wall_relative_quantity: float = Field(default=3, gt=1)
    approaching_distance_atr: float = Field(default=0.5, gt=0)
    testing_distance_atr: float = Field(default=0.1, gt=0)
    approaching_distance_bps: float = Field(default=25, gt=0)
    testing_distance_bps: float = Field(default=2, gt=0)
    max_structural_levels: int = Field(default=40, ge=1, le=200)
    profile_bins: int = Field(default=48, ge=8, le=256)

    @model_validator(mode="after")
    def ordered_limits(self) -> LiveAnalysisSettings:
        if self.golden_pocket_high <= self.golden_pocket_low:
            raise ValueError("Golden Pocket upper ratio must exceed its lower ratio")
        if self.testing_distance_atr > self.approaching_distance_atr:
            raise ValueError("testing ATR tolerance must not exceed approaching tolerance")
        if self.testing_distance_bps > self.approaching_distance_bps:
            raise ValueError("testing bps tolerance must not exceed approaching tolerance")
        return self


def _stamp(value: datetime) -> str:
    return aware(value).isoformat()


def _known_bars(snapshot: AssetSnapshot, timeframe: str) -> tuple[list[Candle], list[str]]:
    bars = sorted(
        (
            bar
            for bar in snapshot.candles.get(timeframe, [])
            if bar.available_at <= snapshot.as_of and bar.close_time <= snapshot.as_of
        ),
        key=lambda bar: bar.open_time,
    )
    if not bars:
        return [], ["no_closed_available_candles"]
    if len({bar.open_time for bar in bars}) != len(bars):
        return [], ["duplicate_candle_timestamps"]
    if len({bar.source for bar in bars}) != 1:
        return [], ["mixed_candle_sources"]
    seconds = TIMEFRAMES[timeframe]
    if any(abs((bar.close_time - bar.open_time).total_seconds() - seconds) > 1.01 for bar in bars):
        return [], ["candle_duration_does_not_match_timeframe"]
    if any(
        (second.open_time - first.open_time).total_seconds() != seconds
        for first, second in zip(bars, bars[1:], strict=False)
    ):
        return [], ["non_contiguous_candles"]
    return bars, []


def _overlay(
    kind: str,
    price: float | None,
    timeframe: str,
    event_at: datetime,
    discovered_at: datetime,
    *,
    quality: str,
    source: str,
    **extra: Any,
) -> dict | None:
    if price is None or not math.isfinite(price) or price <= 0:
        return None
    return {
        "kind": kind,
        "price": float(price),
        "timeframe": timeframe,
        "event_timestamp": _stamp(event_at),
        "discovered_at": _stamp(discovered_at),
        "quality": quality,
        "source": source,
        **extra,
    }


def _proximity(price: float, current: float | None, atr: float | None, config: LiveAnalysisSettings) -> str:
    if current is None or current <= 0:
        return "RESTING"
    distance = abs(price - current)
    if atr is not None and atr > 0:
        test, approach = config.testing_distance_atr * atr, config.approaching_distance_atr * atr
    else:
        test = config.testing_distance_bps * current / 10000
        approach = config.approaching_distance_bps * current / 10000
    return "TESTING" if distance <= test else "APPROACHING" if distance <= approach else "RESTING"


def _depth_event(depth: LiveMarketEvent | dict | None) -> LiveMarketEvent | None:
    if depth is None or isinstance(depth, LiveMarketEvent):
        return depth
    return LiveMarketEvent.model_validate(depth)


def order_book_analysis(
    depth: LiveMarketEvent | dict | None,
    as_of: datetime,
    *,
    price: float | None = None,
    atr: float | None = None,
    prior_levels: list[dict] | None = None,
    config: LiveAnalysisSettings | None = None,
) -> dict:
    """Report observed TOP-20 prices, never imaginary full-book depth or stops.

    A disappearing resting quote is INVALIDATED, not CONSUMED: snapshot-only
    data cannot distinguish a cancellation, execution, or movement outside the
    visible top 20. A partial depth snapshot cannot prove a liquidity vacuum.
    """
    config = config or LiveAnalysisSettings()
    as_of = aware(as_of)
    missing = {
        "quality": "UNAVAILABLE",
        "category": "OBSERVED_ORDER_BOOK",
        "levels": [],
        "bands": {},
        "imbalance": None,
        "spread_bps": None,
        "mid": None,
        "scope": "TOP_20_SNAPSHOT",
        "reason": "no native depth snapshot",
    }
    try:
        event = _depth_event(depth)
    except (ValueError, TypeError) as exc:
        return {**missing, "reason": f"invalid native depth snapshot: {exc}"}
    if event is None:
        return missing
    if event.kind != "DEPTH":
        return {**missing, "reason": "a depth event is required"}
    if event.received_timestamp > as_of or event.processed_timestamp > as_of:
        return {**missing, "reason": "depth was not yet available at the decision time"}
    if event.exchange_timestamp and event.exchange_timestamp > as_of:
        return {**missing, "reason": "exchange timestamp lies after the decision time"}
    # Exchange age matters independently of receipt age when an event carries it.
    source_time = event.exchange_timestamp or event.received_timestamp
    age = (as_of - source_time).total_seconds()
    quality = "STALE" if age > config.book_stale_seconds or event.sequence_gap else "NATIVE"
    bids = [level for level in event.bids if level.quantity > 0]
    asks = [level for level in event.asks if level.quantity > 0]
    if not bids or not asks:
        return {**missing, "reason": "both nonempty bid and ask sides are required"}
    mid = (bids[0].price + asks[0].price) / 2
    current = price if price is not None and price > 0 else mid
    bid_notional = sum(level.price * level.quantity for level in bids)
    ask_notional = sum(level.price * level.quantity for level in asks)
    total = bid_notional + ask_notional
    old = {}
    for level in prior_levels or []:
        if level.get("category") != "OBSERVED_ORDER_BOOK" or level.get("symbol") != event.symbol:
            continue
        try:
            last_update = aware(datetime.fromisoformat(level["last_update"]))
            discovered = aware(datetime.fromisoformat(level["discovered_at"]))
        except (KeyError, ValueError, TypeError):
            continue
        if last_update <= event.received_timestamp and discovered <= last_update:
            old[level.get("id")] = level
    levels: list[dict] = []
    for side, book in (("BID", bids), ("ASK", asks)):
        median = float(np.median([level.quantity for level in book]))
        for level in book:
            identity = f"{event.source}:{event.symbol}:{side}:{level.price:.15g}"
            previous = old.get(identity)
            relative = level.quantity / median if median > 0 else None
            discovered = previous.get("discovered_at") if previous else None
            if discovered:
                try:
                    if aware(datetime.fromisoformat(discovered)) > event.received_timestamp:
                        discovered = None
                except (ValueError, TypeError):
                    discovered = None
            levels.append(
                {
                    "id": identity,
                    "symbol": event.symbol,
                    "side": side,
                    "type": f"{side}_WALL"
                    if relative and relative >= config.wall_relative_quantity
                    else f"{side}_LEVEL",
                    "category": "OBSERVED_ORDER_BOOK",
                    "lower_price": level.price,
                    "upper_price": level.price,
                    "price": level.price,
                    "size": level.quantity,
                    "notional": level.quantity * level.price,
                    "relative_size": relative,
                    "distance_pct": 100 * (level.price / current - 1),
                    "discovered_at": discovered or _stamp(event.received_timestamp),
                    "event_timestamp": _stamp(source_time),
                    "last_update": _stamp(event.received_timestamp),
                    "source": event.source,
                    "quality": quality,
                    "status": "INVALIDATED"
                    if event.sequence_gap
                    else _proximity(level.price, current, atr, config),
                    "reason": "visible resting quote; cancellation/consumption cannot be inferred from a wall",
                }
            )
    ids = {level["id"] for level in levels}
    if quality == "NATIVE":
        for identity, previous in old.items():
            if identity not in ids and previous.get("status") != "INVALIDATED":
                levels.append(
                    {
                        **previous,
                        "status": "INVALIDATED",
                        "quality": "NATIVE",
                        "last_update": _stamp(event.received_timestamp),
                        "reason": "not visible in new top-20 snapshot; cancellation, execution, or scope change unknown",
                    }
                )
    bands = {}
    for percentage in (0.10, 0.25, 0.50, 1.0, 2.0):
        fraction = percentage / 100
        bid_limit, ask_limit = mid * (1 - fraction), mid * (1 + fraction)
        within_bids = sum(level.price * level.quantity for level in bids if level.price >= bid_limit)
        within_asks = sum(level.price * level.quantity for level in asks if level.price <= ask_limit)
        bands[f"{percentage:g}%"] = {
            "bid_notional": within_bids,
            "ask_notional": within_asks,
            "bid_coverage": "BOUNDARY_OBSERVED" if bids[-1].price <= bid_limit else "PARTIAL_TOP_20",
            "ask_coverage": "BOUNDARY_OBSERVED" if asks[-1].price >= ask_limit else "PARTIAL_TOP_20",
        }
    return {
        "quality": quality,
        "category": "OBSERVED_ORDER_BOOK",
        "symbol": event.symbol,
        "levels": levels,
        "bands": bands,
        "imbalance": (bid_notional - ask_notional) / total if total else None,
        "spread_bps": 10000 * (asks[0].price - bids[0].price) / mid,
        "mid": mid,
        "bid_notional": bid_notional,
        "ask_notional": ask_notional,
        "scope": event.depth_scope,
        "source": event.source,
        "source_timestamp": _stamp(source_time),
        "received_timestamp": _stamp(event.received_timestamp),
        "exchange_timestamp_available": event.exchange_timestamp is not None,
        "age_seconds": age,
        "reason": "sequence gap"
        if event.sequence_gap
        else "stale source timestamp"
        if quality == "STALE"
        else None,
        "liquidity_vacuum_score": None,
        "liquidity_vacuum_reason": "partial top-20 depth cannot establish a complete liquidity vacuum",
    }


def _structural_map(
    bars: list[Candle],
    timeframe: str,
    current: float,
    atr: float | None,
    symbol: str,
    as_of: datetime,
    config: LiveAnalysisSettings,
) -> list[dict]:
    if not bars:
        return []
    swings = confirmed_swings(bars, left=config.swing_left, right=config.swing_right)
    width = atr * config.zone_width_atr if atr and atr > 0 else 0
    quality = (
        "STALE"
        if (as_of - bars[-1].close_time).total_seconds() > TIMEFRAMES[timeframe] * 1.5
        else "ESTIMATED"
    )
    result = []
    for swing in swings[-config.max_structural_levels :]:
        price = swing["price"]
        high = swing["kind"] == "high"
        discovered = datetime.fromisoformat(swing["confirmed_at"])
        later = [bar for bar in bars if bar.open_time >= discovered]
        penetrated = [bar for bar in later if (bar.high > price if high else bar.low < price)]
        status = _proximity(price, current, atr, config)
        if penetrated:
            status = "SWEPT"
            if later and (later[-1].close < price if high else later[-1].close > price):
                status = "RECLAIMED"
        identity = (
            f"structure:{symbol}:{timeframe}:{swing['kind']}:{bars[swing['index']].open_time.isoformat()}"
        )
        result.append(
            {
                "id": identity,
                "symbol": symbol,
                "side": "ABOVE" if high else "BELOW",
                "type": "SWING_HIGH" if high else "DEMAND_ZONE",
                "category": "INFERRED_STRUCTURE",
                "lower_price": max(price - width, price * 1e-9),
                "upper_price": price + width,
                "price": price,
                "size": None,
                "notional": None,
                "relative_size": None,
                "distance_pct": 100 * (price / current - 1),
                "event_timestamp": _stamp(bars[swing["index"]].open_time),
                "discovered_at": _stamp(discovered),
                "last_update": _stamp(bars[-1].available_at),
                "source": bars[-1].source,
                "quality": quality,
                "timeframe": timeframe,
                "status": status,
                "strength": 1,
                "reason": "confirmed pivot; potential supply/demand area, not observed resting orders or proven stops",
            }
        )
    # Equal pivots are clustered with an ATR tolerance, without reusing one pivot
    # as multiple independent touches or allocating invented stop quantities.
    if atr and atr > 0:
        tolerance = atr * config.confluence_distance_atr
        for kind in ("high", "low"):
            groups: list[list[dict]] = []
            for swing in sorted((s for s in swings if s["kind"] == kind), key=lambda s: s["price"]):
                if (
                    groups
                    and abs(swing["price"] - float(np.mean([s["price"] for s in groups[-1]]))) <= tolerance
                ):
                    groups[-1].append(swing)
                else:
                    groups.append([swing])
            for group in groups:
                if len(group) < 2:
                    continue
                price = float(np.mean([s["price"] for s in group]))
                discovered = max(datetime.fromisoformat(s["confirmed_at"]) for s in group)
                later = [bar for bar in bars if bar.open_time >= discovered]
                high = kind == "high"
                status = _proximity(price, current, atr, config)
                if any(
                    (bar.high > price + tolerance if high else bar.low < price - tolerance) for bar in later
                ):
                    status = "SWEPT"
                result.append(
                    {
                        "id": f"equal:{symbol}:{timeframe}:{kind}:{discovered.isoformat()}",
                        "symbol": symbol,
                        "side": "BUY_SIDE_LIQUIDITY" if high else "SELL_SIDE_LIQUIDITY",
                        "type": "EQH" if high else "EQL",
                        "category": "INFERRED_STRUCTURE",
                        "lower_price": max(min(s["price"] for s in group) - width, price * 1e-9),
                        "upper_price": max(s["price"] for s in group) + width,
                        "price": price,
                        "size": None,
                        "notional": None,
                        "relative_size": None,
                        "distance_pct": 100 * (price / current - 1),
                        "strength": len(group),
                        "event_timestamp": _stamp(bars[min(s["index"] for s in group)].open_time),
                        "discovered_at": _stamp(discovered),
                        "last_update": _stamp(bars[-1].available_at),
                        "timeframe": timeframe,
                        "source": bars[-1].source,
                        "quality": quality,
                        "status": status,
                        "reason": "ATR-clustered confirmed pivots; stop liquidity is inferred, size unknown",
                    }
                )
    return result[-config.max_structural_levels :]


def _atr_series(bars: list[Candle]) -> np.ndarray:
    previous = np.r_[bars[0].close, [bar.close for bar in bars[:-1]]]
    high, low = np.array([bar.high for bar in bars]), np.array([bar.low for bar in bars])
    return _wilder(np.maximum(high - low, np.maximum(abs(high - previous), abs(low - previous))), 14)


def _fib_pair(bars: list[Candle], timeframe: str, config: LiveAnalysisSettings) -> dict | None:
    swings = confirmed_swings(bars, left=config.swing_left, right=config.swing_right)
    if len(swings) < 2:
        return None
    atr = _atr_series(bars)
    for end in reversed(swings):
        before = [s for s in swings if s["kind"] != end["kind"] and s["index"] < end["index"]]
        if not before:
            continue
        start = before[-1]
        discovery_index = max(start["confirmed_index"], end["confirmed_index"])
        baseline = float(atr[discovery_index])
        amplitude = abs(end["price"] - start["price"])
        if not math.isfinite(baseline) or baseline <= 0 or amplitude < config.minimum_impulse_atr * baseline:
            continue
        rising = start["kind"] == "low" and end["price"] > start["price"]
        falling = start["kind"] == "high" and end["price"] < start["price"]
        if not (rising or falling):
            continue
        anchors = []
        for swing in (start, end):
            anchors.append(
                {
                    "anchor_timestamp": _stamp(bars[swing["index"]].open_time),
                    "discovery_timestamp": swing["confirmed_at"],
                    "price": swing["price"],
                    "timeframe": timeframe,
                    "anchor_type": f"CONFIRMED_SWING_{swing['kind'].upper()}",
                    "swing_algorithm": f"confirmed_swings(left={config.swing_left},right={config.swing_right})",
                    "confidence": min(1.0, amplitude / baseline / 5),
                    "confidence_meaning": "structural excursion evidence, not predictive probability",
                    "reason_selected": "latest opposite confirmed pivots meeting discovery-time ATR excursion threshold",
                }
            )
        return {
            "id": f"{timeframe}:{anchors[0]['anchor_timestamp']}:{anchors[1]['anchor_timestamp']}",
            "timeframe": timeframe,
            "direction": "UP" if rising else "DOWN",
            "anchors": anchors,
            "discovered_at": max(start["confirmed_at"], end["confirmed_at"]),
            "amplitude": amplitude,
            "atr_at_discovery": baseline,
        }
    return None


def _ratio_basis(ratio: float, kind: str) -> str:
    if ratio in {0.5, 0.65, 0.705, 1.0, 2.0, 1.414}:
        return "TRADING_CONVENTION" if ratio != 1.414 else "SQRT_2_TRADING_CONVENTION"
    if ratio in {0.786, 1.272}:
        return "ROUNDED_SQUARE_ROOT_OF_PHI_RELATIONSHIP"
    return (
        "ROUNDED_PHI_DERIVED_RELATIONSHIP" if kind in {"RETRACEMENT", "EXTENSION"} else "TRADING_CONVENTION"
    )


def _zone_behaviour(
    bars: list[Candle], lower: float, upper: float, discovered_at: datetime, direction: str
) -> dict:
    # A candle opened before discovery cannot locate a historical wick after
    # discovery. Conservatively require all its intrabar action to be later.
    eligible = [bar for bar in bars if bar.open_time >= discovered_at]
    touching = [bar for bar in eligible if bar.high >= lower and bar.low <= upper]

    def supported(bar: Candle) -> bool:
        return bar.close > upper if direction == "UP" else bar.close < lower

    def broken(bar: Candle) -> bool:
        return bar.close < lower if direction == "UP" else bar.close > upper

    def swept(bar: Candle) -> bool:
        return bar.low < lower if direction == "UP" else bar.high > upper

    state = "UNTESTED"
    if touching:
        last = touching[-1]
        state = "SWEPT_AND_RECLAIMED" if swept(last) and supported(last) else "REJECTED"
        if eligible and broken(eligible[-1]):
            state = "BROKEN"
        elif (
            len(eligible) >= 2
            and all(supported(bar) for bar in eligible[-2:])
            and state != "SWEPT_AND_RECLAIMED"
        ):
            state = "ACCEPTED"
    volume = sum(bar.volume for bar in touching) if touching else None
    delta = (
        sum(2 * bar.taker_buy_volume - bar.volume for bar in touching)
        if touching and all(bar.taker_buy_volume is not None for bar in touching)
        else None
    )
    penetration = (
        max(
            (max(0.0, lower - bar.low) if direction == "UP" else max(0.0, bar.high - upper))
            for bar in touching
        )
        if touching
        else None
    )
    return {
        "state": state,
        "tests": len(touching),
        "time_in_overlapping_candles_seconds": sum(
            (bar.close_time - bar.open_time).total_seconds() for bar in touching
        ),
        "penetration_price": penetration,
        "overlapping_candle_volume": volume,
        "volume_quality": "ESTIMATED" if touching else "UNAVAILABLE",
        "volume_reason": "whole overlapping candle volume, not executed volume specifically inside the zone",
        "overlapping_candle_taker_delta": delta,
        "cvd_response_quality": "ESTIMATED" if delta is not None else "UNAVAILABLE",
        "subsequent_expansion": None,
        "outcome_status": "RESEARCH_ONLY_NOT_YET_EVALUATED",
    }


def fibonacci_research(
    bars_by_frame: dict[str, list[Candle]],
    overlays: list[dict],
    *,
    as_of: datetime | None = None,
    current_price: float | None = None,
    atr: float | None = None,
    config: LiveAnalysisSettings | None = None,
) -> dict:
    """Objective swing relationships with deduplicated descriptive confluence.

    Historical calls supply ``as_of`` and use only closed/available contiguous
    candles. Each anchor records when confirmation became
    knowable. No winning future path is used to choose an attractive impulse.
    """
    config = config or LiveAnalysisSettings()
    if as_of is not None:
        as_of = aware(as_of)
        sanitized = AssetSnapshot(
            asset={"asset_id": "research", "symbol": "research", "name": "research"},
            as_of=as_of,
            candles=bars_by_frame,
        )
        bars_by_frame = {frame: _known_bars(sanitized, frame)[0] for frame in bars_by_frame}
        available_overlays = []
        for overlay in overlays:
            try:
                if (
                    overlay.get("discovered_at")
                    and aware(datetime.fromisoformat(overlay["discovered_at"])) > as_of
                ):
                    continue
            except (ValueError, TypeError):
                continue
            available_overlays.append(overlay)
        overlays = available_overlays
    pairs, levels, pockets = [], [], []
    for timeframe in FIB_TIMEFRAMES:
        bars = bars_by_frame.get(timeframe, [])
        pair = _fib_pair(bars, timeframe, config) if bars else None
        if not pair:
            continue
        stale = (
            as_of is not None and (as_of - bars[-1].close_time).total_seconds() > TIMEFRAMES[timeframe] * 1.5
        )
        pair["quality"] = "STALE" if stale else "DERIVED"
        pairs.append(pair)
        first, second = [anchor["price"] for anchor in pair["anchors"]]
        sign = 1 if pair["direction"] == "UP" else -1
        amplitude = pair["amplitude"]
        discovered = datetime.fromisoformat(pair["discovered_at"])
        for kind, ratios in (("RETRACEMENT", RETRACEMENTS), ("EXTENSION", EXTENSIONS)):
            for ratio in ratios:
                price = (
                    second - sign * ratio * amplitude
                    if kind == "RETRACEMENT"
                    else first + sign * ratio * amplitude
                )
                if price <= 0:
                    continue
                matches = []
                categories = set()
                if atr and atr > 0:
                    for overlay in overlays:
                        if overlay.get("quality") in {"STALE", "UNAVAILABLE"}:
                            continue
                        distance = abs(overlay["price"] - price) / atr
                        if distance > config.confluence_distance_atr:
                            continue
                        category = overlay["kind"]
                        if category.startswith("EMA"):
                            category = "EMA"
                        elif category in {"POC", "VAH", "VAL", "HVN", "LVN"}:
                            category = "VOLUME_PROFILE"
                        identity = (category, overlay.get("timeframe"))
                        if identity in categories:
                            continue
                        categories.add(identity)
                        matches.append(
                            {
                                "kind": overlay["kind"],
                                "timeframe": overlay.get("timeframe"),
                                "price": overlay["price"],
                                "distance_atr": distance,
                                "quality": overlay["quality"],
                            }
                        )
                score = min(100.0, 25.0 * len(categories)) if atr and atr > 0 else None
                levels.append(
                    {
                        "pair_id": pair["id"],
                        "kind": kind,
                        "ratio": ratio,
                        "price": price,
                        "timeframe": timeframe,
                        "basis": _ratio_basis(ratio, kind),
                        "event_timestamp": pair["anchors"][-1]["anchor_timestamp"],
                        "discovered_at": pair["discovered_at"],
                        "quality": pair["quality"],
                        "confluence_score": score,
                        "confluence": matches,
                        "target_classification": "HIGH_CONFLUENCE"
                        if score is not None and score >= 75
                        else "MEDIUM_CONFLUENCE"
                        if score is not None and score >= 25
                        else "LOW_CONFLUENCE",
                        "behaviour": _zone_behaviour(bars, price, price, discovered, pair["direction"]),
                        "target_status": "RESEARCH_ZONE_NOT_AUTOMATIC_TAKE_PROFIT",
                    }
                )
        boundaries = [
            second - sign * ratio * amplitude
            for ratio in (config.golden_pocket_low, config.golden_pocket_high)
        ]
        low, high = sorted(boundaries)
        if low > 0:
            pockets.append(
                {
                    "pair_id": pair["id"],
                    "timeframe": timeframe,
                    "lower_price": low,
                    "upper_price": high,
                    "ratios": [config.golden_pocket_low, config.golden_pocket_high],
                    "discovered_at": pair["discovered_at"],
                    "quality": pair["quality"],
                    "behaviour": _zone_behaviour(bars, low, high, discovered, pair["direction"]),
                }
            )
    # A larger-timeframe aggregation of the same price/time impulse is one
    # underlying swing, not a second independent confirmation.
    independent = {}
    for pair in pairs:
        signature = pair["id"]
        for other in pairs:
            if other["id"] in independent and _same_impulse(pair, other, atr, config):
                signature = independent[other["id"]]
                break
        independent[pair["id"]] = signature
    for level in levels:
        level["independent_anchor_id"] = independent[level["pair_id"]]
    for level in levels:
        neighbours = {
            other["independent_anchor_id"]
            for other in levels
            if atr
            and atr > 0
            and abs(other["price"] - level["price"]) / atr <= config.confluence_distance_atr
        }
        level["cluster_score"] = min(100.0, 25 * max(0, len(neighbours) - 1)) if atr and atr > 0 else None
    nearby = [
        level
        for level in levels
        if level["quality"] != "STALE"
        and current_price
        and atr
        and atr > 0
        and abs(level["price"] - current_price) <= atr
    ]
    golden = [
        level
        for level in nearby
        if level["kind"] == "RETRACEMENT"
        and config.golden_pocket_low <= level["ratio"] <= config.golden_pocket_high
    ]

    def best(candidates: list[dict], key: str) -> float | None:
        return max((level[key] for level in candidates if level[key] is not None), default=None)

    research_quality = (
        "DERIVED"
        if any(pair["quality"] == "DERIVED" for pair in pairs)
        else "STALE"
        if pairs
        else "UNAVAILABLE"
    )
    return {
        "phi": PHI,
        "quality": research_quality,
        "anchors": pairs,
        "levels": levels,
        "golden_pockets": pockets,
        "scores": {
            "fib_confluence": best(nearby, "confluence_score"),
            "golden_ratio_confluence": best(golden, "confluence_score"),
            "golden_cluster": best(nearby, "cluster_score"),
        },
        "score_formula": "25 points per independent overlapping category/timeframe, capped at 100; clusters count independent impulses",
        "distance_normalization": "current context ATR",
        "calibration": "UNCALIBRATED",
        "production_weight": 0,
        "research_status": "HYPOTHESIS_NOT_VALIDATED",
        "placebo_validation": "NOT_YET_EVALUATED",
        "reason": None if pairs else "insufficient confirmed opposite swing anchors / discovery-time ATR",
    }


def _same_impulse(first: dict, second: dict, atr: float | None, config: LiveAnalysisSettings) -> bool:
    if first["direction"] != second["direction"] or not atr or atr <= 0:
        return False
    prices = all(
        abs(a["price"] - b["price"]) <= config.independent_anchor_tolerance_atr * atr
        for a, b in zip(first["anchors"], second["anchors"], strict=True)
    )
    tolerance = max(TIMEFRAMES[first["timeframe"]], TIMEFRAMES[second["timeframe"]])
    times = all(
        abs(
            (
                datetime.fromisoformat(a["anchor_timestamp"]) - datetime.fromisoformat(b["anchor_timestamp"])
            ).total_seconds()
        )
        <= tolerance
        for a, b in zip(first["anchors"], second["anchors"], strict=True)
    )
    return prices and times


def active_fibonacci_view(
    bars_by_frame: dict[str, list[Candle]],
    research: dict,
    as_of: datetime,
    *,
    indicators: dict | None = None,
    config: LiveAnalysisSettings | None = None,
) -> dict[str, dict]:
    """Project the existing causal research into each chart's own active swing.

    Selection remains the latest qualified opposite confirmed pivots *on that
    timeframe*. A new unconfirmed extreme cannot replace them. Losing the
    origin explicitly invalidates that geometric orientation; it never causes
    retrospective anchor selection. This projection does not fit new weights.
    """
    as_of = aware(as_of)
    config = config or LiveAnalysisSettings()
    indicators = indicators or {}
    snapshot = AssetSnapshot(
        asset={"asset_id": "active-fibonacci", "symbol": "RESEARCH", "name": "active-fibonacci"},
        as_of=as_of,
        candles={frame: bars_by_frame.get(frame, []) for frame in ACTIVE_FIB_TIMEFRAMES},
    )
    views = {}

    def discovered(value) -> datetime | None:
        try:
            return aware(datetime.fromisoformat(value))
        except (TypeError, ValueError):
            return None

    for frame in ACTIVE_FIB_TIMEFRAMES:
        bars, issues = _known_bars(snapshot, frame)
        view = {
            "enabled": config.auto_fibonacci_enabled,
            "timeframe": frame,
            "status": "INSUFFICIENT_DATA" if config.auto_fibonacci_enabled else "DISABLED",
            "quality": "UNAVAILABLE",
            "bias": "UNAVAILABLE",
            "swing_bias": "UNAVAILABLE",
            "direction": "UNAVAILABLE",
            "pair_id": None,
            "anchors": [],
            "discovered_at": None,
            "source": bars[-1].source if bars else "UNAVAILABLE",
            "source_timestamp": _stamp(bars[-1].close_time) if bars else None,
            "received_timestamp": _stamp(max(bar.available_at for bar in bars)) if bars else None,
            "evaluated_at": _stamp(as_of),
            "age_seconds": (as_of - bars[-1].close_time).total_seconds() if bars else None,
            "retracements": [],
            "extensions": [],
            "levels": [],
            "golden_pocket": None,
            "context": {"current_price": bars[-1].close if bars else None, "phase": "UNAVAILABLE", "retracement_ratio": None},
            "scores": {"fib_confluence": None, "golden_ratio_confluence": None, "golden_cluster": None},
            "confidence_quality": {"status": "INSUFFICIENT_DATA", "anchor_confirmation": "UNAVAILABLE", "probability": None},
            "selection_rule": "latest qualified opposite confirmed pivots on this timeframe; no borrowing of another timeframe's swing",
            "display_ratios": {"retracements": list(ACTIVE_FIB_RETRACEMENTS), "extensions": list(ACTIVE_FIB_EXTENSIONS)},
            "production_weight": 0,
            "calibration": "UNCALIBRATED",
            "research_status": "HYPOTHESIS_NOT_VALIDATED",
            "issues": issues,
            "reason": "insufficient confirmed opposite anchors / discovery-time ATR excursion",
        }
        views[frame] = view
        if not config.auto_fibonacci_enabled:
            view["reason"] = "AutoFib explicitly disabled; full research archive is retained"
            continue
        if issues or not bars:
            continue
        if (as_of - bars[-1].close_time).total_seconds() > TIMEFRAMES[frame] * 1.5:
            view.update(status="STALE", quality="STALE", reason="own-timeframe completed candle context is stale")
            continue
        candidates = []
        for pair in research.get("anchors", []):
            pair_at = discovered(pair.get("discovered_at"))
            if pair.get("timeframe") != frame or pair.get("quality") != "DERIVED" or pair_at is None or pair_at > as_of:
                continue
            anchors = pair.get("anchors", [])
            if len(anchors) != 2 or pair.get("direction") not in {"UP", "DOWN"}:
                continue
            valid = True
            for anchor in anchors:
                event, confirmed = discovered(anchor.get("anchor_timestamp")), discovered(anchor.get("discovery_timestamp"))
                if (anchor.get("timeframe") != frame or event is None or confirmed is None
                        or not event <= confirmed <= as_of):
                    valid = False
                    break
                original = next((bar for bar in bars if bar.open_time == event), None)
                expected_kind = "low" if anchor.get("anchor_type") == "CONFIRMED_SWING_LOW" else "high" if anchor.get("anchor_type") == "CONFIRMED_SWING_HIGH" else None
                if original is None or expected_kind is None or anchor.get("price") != getattr(original, expected_kind):
                    valid = False
                    break
                index = bars.index(original)
                if index < config.swing_left or index + config.swing_right >= len(bars):
                    valid = False
                    break
                neighbours = bars[index - config.swing_left:index] + bars[index + 1:index + config.swing_right + 1]
                actual_confirmed = max(bar.available_at for bar in bars[index - config.swing_left:index + config.swing_right + 1])
                qualified = all(getattr(original, expected_kind) < getattr(bar, expected_kind) for bar in neighbours) if expected_kind == "low" else all(getattr(original, expected_kind) > getattr(bar, expected_kind) for bar in neighbours)
                if not qualified or actual_confirmed > confirmed or confirmed > pair_at:
                    valid = False
                    break
            if valid:
                candidates.append(pair)
        if not candidates:
            continue
        pair = max(candidates, key=lambda value: (discovered(value["discovered_at"]), value["id"]))
        origin, extreme = (float(anchor["price"]) for anchor in pair["anchors"])
        amplitude = abs(extreme - origin)
        baseline = pair.get("atr_at_discovery")
        correct_direction = (pair["direction"] == "UP" and extreme > origin and pair["anchors"][0]["anchor_type"] == "CONFIRMED_SWING_LOW" and pair["anchors"][1]["anchor_type"] == "CONFIRMED_SWING_HIGH") or (pair["direction"] == "DOWN" and extreme < origin and pair["anchors"][0]["anchor_type"] == "CONFIRMED_SWING_HIGH" and pair["anchors"][1]["anchor_type"] == "CONFIRMED_SWING_LOW")
        if amplitude <= 0 or not correct_direction or not isinstance(baseline, (float, int)) or not math.isfinite(baseline) or baseline <= 0:
            continue
        sign = 1 if pair["direction"] == "UP" else -1
        orientation = "LONG" if sign > 0 else "SHORT"
        current = bars[-1].close
        ratio = sign * (extreme - current) / amplitude
        origin_lost = ratio > 1 + 1e-10
        phase = "ORIGIN_LOST" if origin_lost else "AT_SWING_EXTREME" if math.isclose(ratio, 0, abs_tol=1e-10) else "IMPULSE_EXTENSION" if ratio < 0 else "ACTIVE_RETRACEMENT"
        selected = [dict(level) for level in research.get("levels", [])
                    if level.get("pair_id") == pair["id"] and level.get("timeframe") == frame
                    and level.get("quality") == "DERIVED"
                    and discovered(level.get("discovered_at")) is not None
                    and discovered(level["discovered_at"]) <= as_of
                    and ((level.get("kind") == "RETRACEMENT" and level.get("ratio") in ACTIVE_FIB_RETRACEMENTS)
                         or (level.get("kind") == "EXTENSION" and level.get("ratio") in ACTIVE_FIB_EXTENSIONS))]
        pocket = next((dict(zone) for zone in research.get("golden_pockets", [])
                       if zone.get("pair_id") == pair["id"] and zone.get("timeframe") == frame
                       and zone.get("quality") == "DERIVED"
                       and discovered(zone.get("discovered_at")) is not None
                       and discovered(zone["discovered_at"]) <= as_of), None)
        atr = indicators.get(frame, {}).get("metrics", {}).get("atr14")
        if not isinstance(atr, (float, int)) or not math.isfinite(atr) or atr <= 0:
            atr = pair.get("atr_at_discovery")
        near = [level for level in selected if isinstance(atr, (float, int)) and atr > 0 and abs(level["price"] - current) <= atr]
        golden = [level for level in near if level["kind"] == "RETRACEMENT" and config.golden_pocket_low <= level["ratio"] <= config.golden_pocket_high]

        def best(rows, name, atr=atr):
            values = [row[name] for row in rows if isinstance(row.get(name), (int, float)) and math.isfinite(row[name])]
            return max(values, default=0.0) if atr else None

        view.update(
            status="INVALIDATED" if origin_lost else "AVAILABLE",
            quality="DERIVED",
            bias="UNAVAILABLE" if origin_lost else orientation,
            swing_bias=orientation,
            direction=pair["direction"],
            pair_id=pair["id"],
            anchors=[dict(anchor) for anchor in pair["anchors"]],
            discovered_at=pair["discovered_at"],
            retracements=[level for level in selected if level["kind"] == "RETRACEMENT"],
            extensions=[level for level in selected if level["kind"] == "EXTENSION"],
            levels=selected,
            golden_pocket=pocket,
            context={"current_price": current, "phase": phase, "retracement_ratio": ratio,
                     "origin_price": origin, "extreme_price": extreme, "origin_lost": origin_lost,
                     "atr_at_discovery": pair["atr_at_discovery"], "amplitude_atr": amplitude / pair["atr_at_discovery"],
                     "own_timeframe": frame, "anchor_age_seconds": (as_of - discovered(pair["discovered_at"])).total_seconds()},
            scores={"fib_confluence": best(near, "confluence_score"),
                    "golden_ratio_confluence": best(golden, "confluence_score"),
                    "golden_cluster": best(near, "cluster_score")},
            confidence_quality={"status": "UNVALIDATED_GEOMETRY", "anchor_confirmation": "CONFIRMED", "probability": None,
                                "meaning": "Confirmed structure and descriptive confluence; no established predictive edge or win probability"},
            reason="Latest own-timeframe confirmed swing origin lost; wait for new confirmable structure" if origin_lost else "Latest own-timeframe confirmed swing; retracement/extension map is conditional research",
        )
    return views


def _weekly_vwap(bars: list[Candle], as_of: datetime) -> dict:
    if not bars:
        return {"value": None, "quality": "UNAVAILABLE", "reason": "no intraday candles"}
    monday = (as_of - timedelta(days=as_of.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week = [bar for bar in bars if bar.open_time >= monday]
    if not week or week[0].open_time != monday:
        return {
            "value": None,
            "quality": "UNAVAILABLE",
            "reason": "full UTC week-to-date candle coverage required",
        }
    total = sum(bar.volume for bar in week)
    exact = all(bar.quote_volume is not None for bar in week)
    notional = sum(
        bar.quote_volume if exact else (bar.high + bar.low + bar.close) / 3 * bar.volume for bar in week
    )
    return {
        "value": notional / total if total > 0 else None,
        "quality": "DERIVED" if exact else "ESTIMATED",
        "source": week[-1].source,
        "source_timestamp": _stamp(week[-1].close_time),
        "received_timestamp": _stamp(max(bar.available_at for bar in week)),
        "reason": "native quote/base UTC week-to-date VWAP"
        if exact
        else "typical-price UTC week-to-date estimate",
    }


def live_session_view(bars_by_frame: dict[str, list[Candle]], as_of: datetime) -> dict:
    """Measured local-session evidence with explicit coverage and freshness.

    Prefer minute observations. A complete five-minute reference session is
    more useful than a truncated minute window; its coarser source is exposed.
    This descriptive history never supplies a production signal weight.
    """
    now = aware(as_of)
    reports = []
    validation = AssetSnapshot(
        asset={"asset_id": "session_context", "symbol": "SESSION", "name": "Session context"},
        candles={frame: bars_by_frame.get(frame, []) for frame in ("1m", "5m")},
        as_of=now,
    )
    for frame in ("1m", "5m"):
        bars, errors = _known_bars(validation, frame)
        if errors:
            continue
        measured = analyze_sessions(bars, now)
        age = max(0.0, (now - bars[-1].close_time).total_seconds())
        stale = age > TIMEFRAMES[frame] * 1.5
        reports.append({
            **measured,
            "timeframe": frame,
            "sources": sorted({bar.source for bar in bars}),
            "source_timestamp": _stamp(bars[-1].close_time),
            "received_timestamp": _stamp(max(bar.available_at for bar in bars)),
            "age_seconds": age,
            "data_quality": "STALE" if stale else measured["data_quality"],
            "selection_rule": "fresh complete session coverage first; then fresh 1m before 5m",
        })
    if not reports:
        return {
            **analyze_sessions([], now),
            "timeframe": None, "sources": [], "source_timestamp": None,
            "received_timestamp": None, "age_seconds": None,
            "reason": "No coherent closed 1m/5m candle history available at decision time",
        }
    return max(reports, key=lambda report: (
        report["data_quality"] != "STALE",
        any(row["quality"] == "AVAILABLE" and row["completed"] for row in report["sessions"]),
        report["timeframe"] == "1m",
    ))


def analyze_live(
    snapshot: AssetSnapshot,
    depth: LiveMarketEvent | dict | None = None,
    *,
    prior_levels: list[dict] | None = None,
    config: LiveAnalysisSettings | None = None,
) -> dict:
    """JSON-safe indicator/level snapshot; all evidence existed at ``as_of``."""
    config = config or LiveAnalysisSettings()
    overlays, indicators, structural, profile_levels, profiles, issues = [], {}, [], [], {}, []
    bars_by_frame = {}
    context_atr, current = None, None
    symbol = snapshot.asset.exchange_symbol or snapshot.asset.symbol
    frame_order = sorted(snapshot.candles, key=lambda frame: TIMEFRAMES[frame])
    for timeframe in frame_order:
        bars, errors = _known_bars(snapshot, timeframe)
        if errors:
            indicators[timeframe] = {"quality": "UNAVAILABLE", "metrics": {}, "issues": errors}
            issues.extend(f"{timeframe}:{error}" for error in errors)
            continue
        bars_by_frame[timeframe] = bars
        report = analyze(bars, timeframe, snapshot.as_of)
        stale = "stale_latest_candle" in report.issues
        quality = "STALE" if stale else "DERIVED"
        stamp, receipt = bars[-1].close_time, max(bar.available_at for bar in bars)
        indicators[timeframe] = {
            "quality": quality,
            "metrics": report.metrics,
            "patterns": report.patterns,
            "issues": report.issues,
            "source": bars[-1].source,
            "source_timestamp": _stamp(stamp),
            "received_timestamp": _stamp(receipt),
            "age_seconds": (snapshot.as_of - stamp).total_seconds(),
            "bars": len(bars),
        }
        vwap_value = report.metrics.get("vwap")
        indicators[timeframe]["metrics"]["vwap_distance_pct"] = (
            100 * (bars[-1].close / vwap_value - 1) if vwap_value else None
        )
        indicators[timeframe]["anchors"] = [anchor.model_dump(mode="json") for anchor in report.anchors]
        latest = bars[-1]
        has_native_flow = latest.taker_buy_volume is not None
        full_native_flow = all(bar.taker_buy_volume is not None for bar in bars)
        indicators[timeframe]["order_flow"] = {
            "quality": quality if has_native_flow else "UNAVAILABLE",
            "method": "native candle taker-buy base volume; sell volume = total minus taker buy",
            "taker_buy_volume": latest.taker_buy_volume,
            "taker_sell_volume": latest.volume - latest.taker_buy_volume if has_native_flow else None,
            "delta": 2 * latest.taker_buy_volume - latest.volume if has_native_flow else None,
            "cvd": sum(2 * bar.taker_buy_volume - bar.volume for bar in bars) if full_native_flow else None,
            "cvd_start_timestamp": _stamp(bars[0].open_time) if full_native_flow else None,
            "cvd_reason": "finite available candle window; not a complete exchange lifetime CVD"
            if full_native_flow
            else "native taker-buy volume unavailable for some candles",
            "trade_count": None,
            "large_trade_concentration": None,
            "source_timestamp": _stamp(stamp),
            "received_timestamp": _stamp(receipt),
        }
        if current is None and not stale:
            current = bars[-1].close
        if timeframe == "5m" and not stale:
            context_atr = report.metrics.get("atr14")
        close = np.array([bar.close for bar in bars])
        for period in (20, 50, 100, 200):
            curve = _ema(close, period)
            points = [
                {"time": int(bar.open_time.timestamp()), "value": float(value)}
                for bar, value in zip(bars, curve, strict=True)
                if math.isfinite(value)
            ]
            item = _overlay(
                f"EMA{period}",
                report.metrics.get(f"ema{period}"),
                timeframe,
                stamp,
                receipt,
                quality=quality,
                source=bars[-1].source,
                points=points,
            )
            if item:
                overlays.append(item)
        exact = all(bar.quote_volume is not None for bar in bars)
        vwap_quality = "STALE" if stale else "DERIVED" if exact else "ESTIMATED"
        vwap = _overlay(
            "VWAP",
            report.metrics.get("vwap"),
            timeframe,
            stamp,
            receipt,
            quality=vwap_quality,
            source=bars[-1].source,
            method=report.levels.get("vwap_method"),
        )
        if vwap:
            overlays.append(vwap)
        for anchor in report.anchors:
            index = next((i for i, bar in enumerate(bars) if bar.open_time == anchor.anchor_time), None)
            if index is None or anchor.confirmed_at > snapshot.as_of:
                continue
            curve = anchored_vwap(bars, index)
            points = [
                {"time": int(bar.open_time.timestamp()), "value": value}
                for bar, value in zip(bars, curve, strict=True)
                if value is not None and bar.available_at >= anchor.confirmed_at
            ]
            item = _overlay(
                "AVWAP",
                anchor.value,
                timeframe,
                anchor.anchor_time,
                anchor.confirmed_at,
                quality=vwap_quality,
                source=bars[-1].source,
                anchor_type=anchor.kind,
                method=anchor.method,
                points=points,
                support=anchor.support,
                resistance=anchor.resistance,
                reclaim=anchor.reclaim,
                rejection=anchor.rejection,
            )
            if item:
                overlays.append(item)
        atr = report.metrics.get("atr14")
        levels = _structural_map(bars, timeframe, bars[-1].close, atr, symbol, snapshot.as_of, config)
        structural.extend(levels)
        for level in levels:
            overlay = _overlay(
                "RESISTANCE" if level["side"] in {"ABOVE", "BUY_SIDE_LIQUIDITY"} else "SUPPORT",
                level["price"],
                timeframe,
                datetime.fromisoformat(level["event_timestamp"]),
                datetime.fromisoformat(level["discovered_at"]),
                quality=level["quality"],
                source=level["source"],
                liquidity_type=level["type"],
            )
            if overlay:
                overlays.append(overlay)
        profile = _volume_profile(bars[-512:], bins=config.profile_bins)
        profile["quality"] = "STALE" if stale else "ESTIMATED"
        profile["source_timestamp"] = _stamp(stamp)
        profile["received_timestamp"] = _stamp(receipt)
        profile["bars"] = min(len(bars), 512)
        profiles[timeframe] = {"available_window": profile, "rolling": {}}
        previous_profile = {}
        for old in prior_levels or []:
            if old.get("category") != "HISTORICAL_TRADE_VOLUME" or old.get("timeframe") != timeframe:
                continue
            try:
                if aware(datetime.fromisoformat(old["last_update"])) > snapshot.as_of:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            if old.get("type") in {"POC", "VAH", "VAL"}:
                previous_profile[old["type"].lower()] = old.get("price")
        profiles[timeframe]["migration"] = {
            name: profile[name] - previous_profile[name]
            if profile.get(name) is not None and previous_profile.get(name) is not None
            else None
            for name in ("poc", "vah", "val")
        }
        for name, hours in (("24h", 24), ("3d", 72), ("7d", 168), ("14d", 336), ("30d", 720), ("90d", 2160)):
            cutoff = stamp - timedelta(hours=hours)
            if bars[0].open_time <= cutoff and TIMEFRAMES[timeframe] <= hours * 3600:
                selected = [bar for bar in bars if bar.close_time > cutoff]
                profiles[timeframe]["rolling"][name] = {
                    **_volume_profile(selected, bins=config.profile_bins),
                    "quality": "STALE" if stale else "ESTIMATED",
                    "bars": len(selected),
                    "source_timestamp": _stamp(stamp),
                }
            else:
                profiles[timeframe]["rolling"][name] = {
                    "quality": "UNAVAILABLE",
                    "reason": "insufficient full-window history",
                }
        for kind in ("poc", "vah", "val", "hvn", "lvn"):
            raw = profile[kind]
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                item = _overlay(
                    kind.upper(),
                    value,
                    timeframe,
                    stamp,
                    receipt,
                    quality=profile["quality"],
                    source=bars[-1].source,
                    method=profile["method"],
                    category="HISTORICAL_TRADE_VOLUME",
                )
                if item:
                    overlays.append(item)
                    profile_levels.append(
                        {
                            "id": f"profile:{symbol}:{timeframe}:{kind}:{value:.15g}",
                            "symbol": symbol,
                            "side": "ABOVE" if value > bars[-1].close else "BELOW",
                            "type": kind.upper(),
                            "category": "HISTORICAL_TRADE_VOLUME",
                            "lower_price": value,
                            "upper_price": value,
                            "price": value,
                            "size": None,
                            "notional": None,
                            "relative_size": None,
                            "distance_pct": 100 * (value / bars[-1].close - 1),
                            "event_timestamp": _stamp(stamp),
                            "discovered_at": _stamp(receipt),
                            "last_update": _stamp(receipt),
                            "timeframe": timeframe,
                            "source": bars[-1].source,
                            "quality": profile["quality"],
                            "status": _proximity(value, bars[-1].close, atr, config),
                            "reason": "candle-derived traded-volume node; not resting orders or inferred stop quantities",
                        }
                    )
    if context_atr is None:
        context_atr = next(
            (
                item["metrics"].get("atr14")
                for item in indicators.values()
                if item["quality"] != "STALE" and item["metrics"].get("atr14")
            ),
            None,
        )
    book = order_book_analysis(
        depth, snapshot.as_of, price=current, atr=context_atr, prior_levels=prior_levels, config=config
    )
    if book["quality"] == "NATIVE":
        current = book["mid"]
    if current:
        for level in structural + profile_levels:
            level["distance_pct"] = 100 * (level["price"] / current - 1)
    research = fibonacci_research(
        bars_by_frame, overlays, as_of=snapshot.as_of, current_price=current, atr=context_atr, config=config
    )
    for level in research["levels"]:
        overlays.append(
            {
                **level,
                "source": "deterministic-confirmed-swings",
                "kind": "FIBONACCI",
                "category": "RESEARCH_HYPOTHESIS",
                "production_weight": 0,
            }
        )
    intraday_frame, intraday = next(
        (
            (frame, bars_by_frame[frame])
            for frame in frame_order
            if frame in bars_by_frame and TIMEFRAMES[frame] < 86400
        ),
        (None, []),
    )
    weekly = _weekly_vwap(intraday, snapshot.as_of)
    if (
        intraday
        and (snapshot.as_of - intraday[-1].close_time).total_seconds() > TIMEFRAMES[intraday_frame] * 1.5
    ):
        weekly["quality"] = "STALE" if weekly["value"] is not None else "UNAVAILABLE"
    if intraday:
        weekly_overlay = _overlay(
            "WEEKLY_VWAP",
            weekly["value"],
            intraday_frame,
            intraday[-1].close_time,
            max(bar.available_at for bar in intraday),
            quality=weekly["quality"],
            source=intraday[-1].source,
            method=weekly.get("reason"),
        )
        if weekly_overlay:
            overlays.append(weekly_overlay)
    return {
        "symbol": symbol,
        "asset_id": snapshot.asset.asset_id,
        "as_of": _stamp(snapshot.as_of),
        "price": current,
        "quote_asset": snapshot.asset.quote_asset,
        "indicators": indicators,
        "overlays": overlays,
        "liquidity_map": structural + profile_levels + book["levels"],
        "order_book": book,
        "volume_profiles": profiles,
        "weekly_vwap": weekly,
        "fibonacci": research,
        "active_fibonacci": active_fibonacci_view(
            bars_by_frame, research, snapshot.as_of, indicators=indicators, config=config
        ),
        "sessions": live_session_view(bars_by_frame, snapshot.as_of),
        "data_quality": {
            "order_book": book["quality"],
            "spot_candles": "DERIVED"
            if any(value["quality"] == "DERIVED" for value in indicators.values())
            else "STALE"
            if indicators and any(value["quality"] == "STALE" for value in indicators.values())
            else "UNAVAILABLE",
            "derivatives": "UNAVAILABLE" if not snapshot.derivatives else "NOT_EVALUATED_BY_CHART_LAYER",
            "historical_profile": "ESTIMATED" if profiles else "UNAVAILABLE",
            "inferred_stops": "UNAVAILABLE",
            "order_flow": "DERIVED"
            if any(item.get("order_flow", {}).get("quality") == "DERIVED" for item in indicators.values())
            else "UNAVAILABLE",
        },
        "issues": issues,
        "mode": "OBSERVATION",
        "research_weight": 0,
    }
