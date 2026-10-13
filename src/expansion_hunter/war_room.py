"""Bounded point-in-time structure memory and conditional decision-support paths.

This foundation recognizes objective pivot, break, sweep and retest sequences.
Named chart geometries use explicit research definitions. Scores describe
uncalibrated evidence; liquidity destinations are observed levels, not forecasts.
Called on analysis cadence, never on each market-data tick.
"""

from __future__ import annotations

import copy
import math
from datetime import datetime
from statistics import pstdev

from ._numeric import float_mean as mean
from .fast import _atr
from .live_analysis import _known_bars
from .models import TIMEFRAMES, Asset, AssetSnapshot, Candle, aware
from .pattern_engine import (
    PATTERN_NAMES,
    PatternSettings,
    candle_revision,
    detect_patterns,
    refresh_known_targets,
)
from .setup_library import evaluate_setups
from .technical import confirmed_swings

FRAMES = ("1D", "4h", "1h", "15m", "5m", "1m")
FRAME_WEIGHTS = {"1D": 3.0, "4h": 2.5, "1h": 2.0, "15m": 1.5, "5m": 1.0, "1m": 0.75}
CONFIG = {
    "swing_left": 2,
    "swing_right": 2,
    "atr_period": 14,
    "maximum_bars": 256,
    "history_limit": 40,
    "range_window": 20,
    "compression_ratio": 0.75,
    "equal_level_atr_tolerance": 0.15,
    "retest_atr_tolerance": 0.25,
    "failure_atr_buffer": 0.05,
    "minimum_gross_reward_risk": 2.0,
    "swing_velocity_atr_threshold": 0.05,
    "swing_near_boundary_atr": 0.75,
    "swing_early_bars": 5,
    "swing_entry_zone_atr": 0.25,
    "swing_too_late_atr": 2.0,
    "swing_fib_context_weight": 0.10,
}
SWING_PHASES = ("FALLING", "BOTTOMING", "TURNING_UP", "EARLY_UPSWING", "UPSWING",
                "LOSING_MOMENTUM", "TOPPING", "TURNING_DOWN", "EARLY_DOWNSWING", "DOWNSWING")


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _timestamp(value) -> datetime | None:
    try:
        return aware(value if isinstance(value, datetime) else datetime.fromisoformat(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _stamp(value: datetime) -> str:
    return aware(value).isoformat()


def _plain(value) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value if isinstance(value, dict) else {}


def _metrics(bars: list[Candle]) -> dict:
    """Small bounded arithmetic; no pandas/history reconstruction on live ticks."""
    last = bars[-1]
    atr = _atr(bars, CONFIG["atr_period"])
    recent_ranges = [bar.high - bar.low for bar in bars[-5:]]
    prior_ranges = [bar.high - bar.low for bar in bars[-25:-5]]
    prior_range = mean(prior_ranges) if prior_ranges else None
    range_ratio = mean(recent_ranges) / prior_range if prior_range and prior_range > 0 else None
    old_atr = _atr(bars[:-5], CONFIG["atr_period"]) if len(bars) > 20 else None
    atr_ratio = atr / old_atr if atr and old_atr else None
    returns = [100 * (current.close / previous.close - 1) for previous, current in zip(bars[:-1], bars[1:], strict=True)]
    velocity = mean(returns[-3:]) if len(returns) >= 3 else None
    prior_velocity = mean(returns[-6:-3]) if len(returns) >= 6 else None
    acceleration = velocity - prior_velocity if velocity is not None and prior_velocity is not None else None
    true_ranges = [max(bar.high - bar.low, abs(bar.high - prior.close), abs(bar.low - prior.close)) for prior, bar in zip(bars[:-1], bars[1:], strict=True)]
    historical_atrs = [mean(true_ranges[index - 14:index]) for index in range(14, len(true_ranges))]
    historical_atrs = [value for value in historical_atrs if value > 0]
    percentile = (sum(value < atr for value in historical_atrs) + 0.5 * sum(value == atr for value in historical_atrs)) / len(historical_atrs) * 100 if atr and historical_atrs else None
    baseline_volume = mean(bar.volume for bar in bars[-21:-1]) if len(bars) >= 21 else None
    rvol = last.volume / baseline_volume if baseline_volume and baseline_volume > 0 else None
    volume_recent = mean(bar.volume for bar in bars[-3:]) if len(bars) >= 3 else None
    volume_before = mean(bar.volume for bar in bars[-6:-3]) if len(bars) >= 6 else None
    volume_acceleration = (volume_recent / volume_before - 1) * 100 if volume_recent is not None and volume_before and volume_before > 0 else None
    lookback = bars[-21:-1]
    lower = min(bar.low for bar in lookback) if lookback else None
    upper = max(bar.high for bar in lookback) if lookback else None
    window_close = [bar.close for bar in bars[-20:]]
    bb_width = 4 * pstdev(window_close) / mean(window_close) * 100 if len(window_close) == 20 else None
    native_flow = bars[-5:] if len(bars) >= 5 and all(bar.taker_buy_volume is not None for bar in bars[-5:]) else []
    delta = sum(2 * bar.taker_buy_volume - bar.volume for bar in native_flow) if native_flow else None
    flow = "UNAVAILABLE"
    if delta is not None and atr:
        displacement = last.close - native_flow[0].open
        flat = abs(displacement) <= 0.5 * atr
        if flat and delta < 0 and lower is not None and last.close >= lower:
            flow = "POSSIBLE_BUYER_ABSORPTION"
        elif flat and delta > 0 and upper is not None and last.close <= upper:
            flow = "POSSIBLE_SELLER_ABSORPTION"
        else:
            flow = "NATIVE_FLOW_AVAILABLE_NO_ABSORPTION_CONCLUSION"
    if range_ratio is not None and atr_ratio is not None and range_ratio <= CONFIG["compression_ratio"] and atr_ratio <= CONFIG["compression_ratio"]:
        volatility = "SQUEEZE"
    elif range_ratio is not None and range_ratio <= CONFIG["compression_ratio"]:
        volatility = "COMPRESSION"
    elif range_ratio is not None and range_ratio >= 1.5 and atr_ratio is not None and atr_ratio > 1:
        volatility = "EXPANSION"
    elif percentile is not None and percentile >= 80:
        volatility = "HIGH_VOL"
    elif percentile is not None and percentile <= 20:
        volatility = "LOW_VOL"
    else:
        volatility = "NORMAL"
    if velocity is None or acceleration is None:
        momentum = "INSUFFICIENT_DATA"
    elif velocity > 0 and acceleration > 0.001:
        momentum = "ACCELERATING_UP"
    elif velocity > 0 and acceleration < -0.001:
        momentum = "DECELERATING_UP"
    elif velocity < 0 and acceleration < -0.001:
        momentum = "ACCELERATING_DOWN"
    elif velocity < 0 and acceleration > 0.001:
        momentum = "DECELERATING_DOWN"
    else:
        momentum = "STABLE"
    return {
        "close": last.close,
        "latest_high": last.high,
        "latest_low": last.low,
        "atr": atr,
        "atr_compression_ratio": atr_ratio,
        "atr_historical_percentile": percentile,
        "range_compression_ratio": range_ratio,
        "range_low": lower,
        "range_high": upper,
        "range_discovered_at": _stamp(max(bar.available_at for bar in lookback)) if lookback else None,
        "rvol": rvol,
        "volume_acceleration_percent": volume_acceleration,
        "velocity_percent_per_bar": velocity,
        "acceleration_percent_per_bar": acceleration,
        "realized_volatility_percent_per_bar": pstdev(returns[-10:]) if len(returns) >= 10 else None,
        "bollinger_width_percent": bb_width,
        "volatility_state": volatility,
        "momentum_state": momentum,
        "native_taker_delta_5_bars": delta,
        "absorption_hypothesis": flow,
        "absorption_quality": "DERIVED_NATIVE_CANDLE_DELTA" if delta is not None else "UNAVAILABLE",
    }


def _pivots(bars: list[Candle]) -> tuple[list[dict], str]:
    confirmed = confirmed_swings(bars, left=CONFIG["swing_left"], right=CONFIG["swing_right"])
    result, latest = [], {}
    for pivot in confirmed:
        kind = pivot["kind"]
        prior = latest.get(kind)
        classification = "SWING_HIGH" if kind == "high" else "SWING_LOW"
        if prior is not None:
            confirmation_atr = _atr(bars[:pivot["confirmed_index"] + 1], CONFIG["atr_period"])
            tolerance = (confirmation_atr or 0) * CONFIG["equal_level_atr_tolerance"]
            if abs(pivot["price"] - prior) <= tolerance:
                classification = "EQH" if kind == "high" else "EQL"
            else:
                classification = ("HH" if pivot["price"] > prior else "LH") if kind == "high" else ("HL" if pivot["price"] > prior else "LL")
        latest[kind] = pivot["price"]
        result.append({
            "kind": kind,
            "classification": classification,
            "price": pivot["price"],
            "event_timestamp": _stamp(bars[pivot["index"]].close_time),
            "discovered_at": pivot["confirmed_at"],
            "source": bars[pivot["index"]].source,
            "algorithm": "confirmed_swings(left=2,right=2)",
        })
    highs = [pivot for pivot in result if pivot["kind"] == "high"]
    lows = [pivot for pivot in result if pivot["kind"] == "low"]
    trend = "RANGE"
    if highs and lows:
        if highs[-1]["classification"] == "HH" and lows[-1]["classification"] == "HL":
            trend = "BULLISH_TREND"
        elif highs[-1]["classification"] == "LH" and lows[-1]["classification"] == "LL":
            trend = "BEARISH_TREND"
    return result[-40:], trend


def _available_before(pivots: list[dict], kind: str, before: datetime) -> dict | None:
    candidates = [pivot for pivot in pivots if pivot["kind"] == kind and _timestamp(pivot["discovered_at"]) <= before]
    return candidates[-1] if candidates else None


def _state(bars: list[Candle], timeframe: str, as_of: datetime, previous: dict) -> dict:
    last = bars[-1]
    stamp = _stamp(last.close_time)
    prior_completed = _timestamp(previous.get("last_completed_candle"))
    quality = "STALE" if (as_of - last.close_time).total_seconds() > TIMEFRAMES[timeframe] * 1.5 else "DERIVED"
    if prior_completed == last.close_time:
        result = copy.deepcopy(previous)
        result["quality"] = quality
        result["age_seconds"] = (as_of - last.close_time).total_seconds()
        return result
    metrics = _metrics(bars)
    pivots, trend = _pivots(bars)
    known_high = _available_before(pivots, "high", last.open_time)
    known_low = _available_before(pivots, "low", last.open_time)
    earlier = bars[-2]
    atr = metrics["atr"]
    buffer = atr * CONFIG["failure_atr_buffer"] if atr else 0
    tolerance = atr * CONFIG["retest_atr_tolerance"] if atr else 0
    state = "COMPRESSION" if metrics["volatility_state"] in {"COMPRESSION", "SQUEEZE"} else trend
    events = []
    active = copy.deepcopy(previous.get("active_break"))
    pending = copy.deepcopy(previous.get("pending_shift"))
    old_trend = previous.get("trend", trend)
    if active and (_timestamp(active.get("discovered_at")) is None or _timestamp(active["discovered_at"]) > last.open_time):
        active = None
    if active:
        direction, level = active["direction"], active["price"]
        if (direction == "BULLISH" and last.close < level - buffer) or (direction == "BEARISH" and last.close > level + buffer):
            state = "FAILED_BREAKOUT" if direction == "BULLISH" else "FAILED_BREAKDOWN"
            events.extend([state, "REJECTION" if direction == "BULLISH" else "RECLAIM"])
            active = None
        elif (direction == "BULLISH" and last.low <= level + tolerance and last.close > level) or (direction == "BEARISH" and last.high >= level - tolerance and last.close < level):
            state = "RETEST"
            events.extend(["RETEST", "SUPPORT_RESISTANCE_FLIP"])
            active["accepted_at"] = _stamp(last.available_at)
        elif metrics["volatility_state"] == "EXPANSION":
            state = "EXPANSION"
            events.append("EXPANSION")
    # Objectively crossing a pivot takes precedence over a generic volatility label.
    if not events and known_high and earlier.close <= known_high["price"] < last.close:
        countertrend = old_trend == "BEARISH_TREND"
        events = ["CHoCH" if countertrend else "BOS", "BREAKOUT"]
        state = "CHoCH" if countertrend else "BREAKOUT"
        active = {"direction": "BULLISH", "price": known_high["price"], "level_discovered_at": known_high["discovered_at"], "discovered_at": _stamp(last.available_at)}
        if countertrend:
            pending = {"direction": "BULLISH", "price": known_high["price"], "discovered_at": _stamp(last.available_at)}
    elif not events and known_low and last.close < known_low["price"] <= earlier.close:
        countertrend = old_trend == "BULLISH_TREND"
        events = ["CHoCH" if countertrend else "BOS", "BREAKDOWN"]
        state = "CHoCH" if countertrend else "BREAKDOWN"
        active = {"direction": "BEARISH", "price": known_low["price"], "level_discovered_at": known_low["discovered_at"], "discovered_at": _stamp(last.available_at)}
        if countertrend:
            pending = {"direction": "BEARISH", "price": known_low["price"], "discovered_at": _stamp(last.available_at)}
    elif not events and known_low and last.low < known_low["price"] <= last.close and earlier.close >= known_low["price"]:
        state = "LIQUIDITY_SWEEP"
        events = ["SELL_SIDE_SWEEP", "RECLAIM"]
    elif not events and known_high and last.high > known_high["price"] >= last.close and earlier.close <= known_high["price"]:
        state = "LIQUIDITY_SWEEP"
        events = ["BUY_SIDE_SWEEP", "REJECTION"]
    if pending and _timestamp(pending.get("discovered_at")) and _timestamp(pending["discovered_at"]) <= last.open_time:
        shifted = (pending["direction"] == "BULLISH" and trend == "BULLISH_TREND" and last.close > pending["price"]) or (pending["direction"] == "BEARISH" and trend == "BEARISH_TREND" and last.close < pending["price"])
        if shifted and state not in {"FAILED_BREAKOUT", "FAILED_BREAKDOWN", "LIQUIDITY_SWEEP"}:
            state = "MSS"
            events.append("MSS")
            pending = None
    directional = active.get("direction") if active else None
    bias = directional or ("BULLISH" if trend == "BULLISH_TREND" else "BEARISH" if trend == "BEARISH_TREND" else "NEUTRAL")
    if state == "FAILED_BREAKOUT":
        bias = "NEUTRAL"
    elif state == "FAILED_BREAKDOWN":
        bias = "NEUTRAL"
    history = copy.deepcopy(previous.get("history", []))[-CONFIG["history_limit"] + 1:]
    newly_known_pivots = [pivot for pivot in pivots if _timestamp(pivot["discovered_at"]) > prior_completed] if prior_completed else [pivot for pivot in pivots if _timestamp(pivot["discovered_at"]) >= last.available_at]
    history.append({
        "state": state,
        "bias": bias,
        "events": events,
        "event_timestamp": stamp,
        "discovered_at": _stamp(max(bar.available_at for bar in bars)),
        "price": last.close,
        "newly_confirmed_pivots": newly_known_pivots,
        "momentum_state": metrics["momentum_state"],
        "prior_trend": old_trend,
    })
    sequence = []
    for entry in history:
        for label in entry.get("events") or [entry["state"]]:
            if not sequence or sequence[-1] != label:
                sequence.append(label)
    return {
        "state": state,
        "trend": trend,
        "bias": bias,
        "quality": quality,
        "source": last.source,
        "age_seconds": (as_of - last.close_time).total_seconds(),
        "last_completed_candle": stamp,
        "pivots": pivots,
        "known_high_before_candle": known_high,
        "known_low_before_candle": known_low,
        "events": events,
        "history": history,
        "sequence": sequence[-12:],
        "active_break": active,
        "pending_shift": pending,
        "metrics": metrics,
        "reason": "Closed available candles; pivots require two later confirming candles. Break levels were known before the evaluated candle opened.",
    }


def _alignment(frames: dict) -> dict:
    components, weighted, known_weight = [], 0.0, 0.0
    for frame in FRAMES:
        report = frames[frame]
        valid = report["quality"] == "DERIVED"
        direction = {"BULLISH": 1, "BEARISH": -1, "NEUTRAL": 0}.get(report.get("bias")) if valid else None
        weight = FRAME_WEIGHTS[frame]
        components.append({"timeframe": frame, "state": report["state"], "bias": report.get("bias"), "quality": report["quality"], "weight": weight, "direction": direction})
        if direction is not None:
            weighted += direction * weight
            known_weight += weight
    return {
        "value": abs(weighted) / known_weight * 100 if known_weight else None,
        "coverage": known_weight / sum(FRAME_WEIGHTS.values()),
        "direction": "BULLISH" if weighted > 0 else "BEARISH" if weighted < 0 else "NEUTRAL",
        "components": components,
        "status": "UNCALIBRATED" if known_weight else "INSUFFICIENT_DATA",
        "reason": "Weighted directional agreement, not a probability; missing/stale timeframes contribute no evidence.",
    }


def _levels(analysis: dict, as_of: datetime) -> list[dict]:
    result = []
    for level in analysis.get("liquidity_map", []):
        price = _number(level.get("price", level.get("representative_price")))
        discovered = _timestamp(level.get("discovered_at", level.get("discovery_timestamp")))
        if price is None or price <= 0 or discovered is None or discovered > as_of:
            continue
        event_at = _timestamp(level.get("event_timestamp"))
        if event_at and (event_at > as_of or event_at > discovered):
            continue
        if level.get("quality") in {"STALE", "UNAVAILABLE", "MISSING"} or level.get("status") in {"INVALIDATED", "CONSUMED"}:
            continue
        result.append({"price": price, "source": level.get("source", "UNAVAILABLE"), "category": level.get("category", "UNAVAILABLE"), "type": level.get("type", "KNOWN_LEVEL"), "quality": level.get("quality", "ESTIMATED"), "discovered_at": _stamp(discovered)})
    return result


def _targets(levels: list[dict], boundary: float | None, direction: int) -> list[dict]:
    if boundary is None:
        return []
    candidates = sorted((level for level in levels if direction * (level["price"] - boundary) > 0), key=lambda level: direction * level["price"])
    selected = []
    for candidate in candidates:
        if not any(math.isclose(candidate["price"], level["price"], rel_tol=1e-8) for level in selected):
            selected.append(candidate)
        if len(selected) == 3:
            break
    return selected


def _known_fast_boundary(fast: dict, field: str, as_of: datetime) -> dict:
    level = fast.get(field) or {}
    price, discovered = _number(level.get("price")), _timestamp(level.get("discovered_at"))
    event = _timestamp(level.get("event_timestamp"))
    if price is None or price <= 0 or discovered is None or discovered > as_of or event and event > discovered:
        return {}
    return level


def _fast_direction(fast: dict, as_of: datetime) -> str | None:
    """Honor the actual frozen boundary and native long/short lifecycle."""
    stage = fast.get("stage")
    short_stages = {"BREAKDOWN_PRESSURE", "BREAKDOWN_ATTEMPT", "1M_BREAKDOWN_CONFIRMED", "5M_ACCEPTED_DOWN", "15M_EXPANSION_CONFIRMED_DOWN"}
    long_stages = {"BREAKOUT_PRESSURE", "BREAKOUT_ATTEMPT", "1M_BREAKOUT_CONFIRMED", "5M_ACCEPTED", "15M_EXPANSION_CONFIRMED"}
    if stage in short_stages:
        return "BEARISH" if _known_fast_boundary(fast, "support", as_of) else None
    if stage in long_stages:
        if fast.get("bias") == "SHORT":
            return "BEARISH" if _known_fast_boundary(fast, "support", as_of) else None
        return "BULLISH" if _known_fast_boundary(fast, "resistance", as_of) else None
    if stage in {"WATCH", "PRE_IGNITION", "LOADED"}:
        if fast.get("bias") == "SHORT" and _known_fast_boundary(fast, "support", as_of):
            return "BEARISH"
        if fast.get("bias") == "LONG" and _known_fast_boundary(fast, "resistance", as_of):
            return "BULLISH"
    return None


def _paths(frames: dict, analysis: dict, fast_state: dict, as_of: datetime, price: float | None, direction: str) -> tuple[dict, dict]:
    context = next((frames[frame] for frame in ("5m", "15m", "1m", "1h") if frames[frame]["quality"] == "DERIVED"), {})
    metrics = context.get("metrics", {})
    high, low = context.get("known_high_before_candle"), context.get("known_low_before_candle")
    resistance = _known_fast_boundary(fast_state, "resistance", as_of)
    discovered = _timestamp(resistance.get("discovered_at"))
    resistance_price = _number(resistance.get("price"))
    if resistance_price is None or resistance_price <= 0 or discovered is None or discovered > as_of:
        resistance_price = None
    trigger_up = resistance_price or (high["price"] if high else metrics.get("range_high"))
    support = _known_fast_boundary(fast_state, "support", as_of)
    trigger_down = support.get("price") or (low["price"] if low else metrics.get("range_low"))
    levels = _levels(analysis, as_of)
    upward = _targets(levels, max(price, trigger_up) if price is not None and trigger_up is not None else trigger_up, 1)
    downward = _targets(levels, min(price, trigger_down) if price is not None and trigger_down is not None else trigger_down, -1)
    scenarios = {
        "bullish": {
            "condition": "Voltooide 1m sluit boven de vooraf bekende trigger plus de ingestelde ATR/tick-buffer; daarna beoordeel 5m-acceptatie.",
            "trigger": trigger_up,
            "invalidation": trigger_down,
            "targets": upward,
            "status": "CONDITIONAL" if trigger_up is not None and trigger_down is not None else "INSUFFICIENT_DATA",
            "target_status": "OBSERVED_LEVELS" if upward else "UNAVAILABLE",
            "resistance_discovered_at": _stamp(discovered) if resistance_price else high.get("discovered_at") if high else metrics.get("range_discovered_at"),
        },
        "neutral": {
            "condition": "Zolang de vooraf waargenomen range intact blijft, is richting onbevestigd.",
            "lower": metrics.get("range_low"),
            "upper": metrics.get("range_high"),
            "status": "CONDITIONAL" if metrics.get("range_low") is not None else "INSUFFICIENT_DATA",
        },
        "bearish": {
            "condition": "Completed 1m closes below the predefined support minus the configured ATR/tick buffer; separately assess subsequent 5m acceptance.",
            "trigger": trigger_down,
            "invalidation": trigger_up,
            "targets": downward,
            "status": "CONDITIONAL" if trigger_down is not None and trigger_up is not None else "INSUFFICIENT_DATA",
            "target_status": "OBSERVED_LEVELS" if downward else "UNAVAILABLE",
            "support_discovered_at": support.get("discovered_at") or (low.get("discovered_at") if low else metrics.get("range_discovered_at")),
        },
    }
    primary = "bullish" if direction == "BULLISH" else "bearish" if direction == "BEARISH" else "neutral"
    steps = []
    if primary != "neutral":
        path = scenarios[primary]
        if path["trigger"] is not None:
            steps.append({"state": "BREAKOUT" if primary == "bullish" else "BREAKDOWN", "price": path["trigger"], "condition": path["condition"]})
            steps.append({"state": "RETEST", "price": path["trigger"], "condition": "Alleen vervolg als voormalige grens steun/weerstand wordt; 5m-acceptatie is afzonderlijke bevestiging."})
        for target in path["targets"]:
            steps.append({"state": "LIQUIDITY_DESTINATION", "price": target["price"], "condition": "Mogelijke route bij behouden structuur; dit is een waargenomen liquiditeits-/profielniveau, geen gegarandeerd doel."})
    else:
        steps = [{"state": "RANGE", "price": price, "condition": scenarios["neutral"]["condition"]}] if price is not None else []
    alternative = "Uitbraak faalt / reclaim gaat verloren: beoordeel range of tegengesteld scenario; geen vaststaande richting."
    return scenarios, {"label": "EXPECTED / CONDITIONAL PATH", "primary": primary, "steps": steps, "alternative": alternative, "probability": None, "calibration": "NOT_CALIBRATED"}


def _entry_quality(price: float | None, scenarios: dict, primary: str, context: dict, analysis: dict, fast_state: dict) -> dict:
    if primary == "neutral" or price is None:
        return {"value": None, "coverage": 0, "status": "INSUFFICIENT_DATA", "classification": "DIRECTION_UNCONFIRMED", "components": [], "gross_reward_risk": None, "net_reward_risk": None}
    direction = 1 if primary == "bullish" else -1
    scenario = scenarios[primary]
    stop, trigger, targets = scenario["invalidation"], scenario["trigger"], scenario["targets"]
    target = targets[0]["price"] if targets else None
    atr = context.get("metrics", {}).get("atr")
    risk = direction * (price - stop) if stop is not None else None
    reward = direction * (target - price) if target is not None else None
    rr = reward / risk if risk is not None and risk > 0 and reward is not None and reward > 0 else None
    distance = abs(price - trigger) / atr if trigger is not None and atr else None
    book = analysis.get("order_book", {})
    spread = _number(book.get("spread_bps")) if book.get("quality") in {"NATIVE", "AVAILABLE", "DERIVED"} else None
    native_long = primary == "bullish" and fast_state.get("bias", "LONG") == "LONG" and fast_state.get("stage") in {"1M_BREAKOUT_CONFIRMED", "5M_ACCEPTED", "15M_EXPANSION_CONFIRMED"}
    native_short = primary == "bearish" and fast_state.get("bias") == "SHORT" and fast_state.get("stage") in {"1M_BREAKDOWN_CONFIRMED", "5M_ACCEPTED_DOWN", "15M_EXPANSION_CONFIRMED_DOWN", "5M_ACCEPTED", "15M_EXPANSION_CONFIRMED"}
    short_structure = primary == "bearish" and (context.get("active_break") or {}).get("direction") == "BEARISH" and context.get("state") in {"BREAKDOWN", "RETEST", "MSS", "EXPANSION"}
    confirmed = native_long or native_short or short_structure
    values = [
        ("gross_reward_risk", 0.4, min(1, max(0, (rr - 1) / 3)) if rr is not None else None, "Gross R:R; fees/funding/slippage are not known here."),
        ("entry_distance_atr", 0.25, max(0, 1 - distance / 2) if distance is not None else None, "Distance from known trigger normalized by ATR."),
        ("observed_spread", 0.15, max(0, 1 - spread / 50) if spread is not None and spread >= 0 else None, "Observed book spread; no full-book slippage estimate."),
        ("fast_confirmation", 0.2, 1.0 if confirmed else 0.0, "Native directional fast confirmation or separately observed bearish structure; no 5m/15m context delays the actual 1m alert."),
    ]
    coverage = sum(weight for _, weight, value, _ in values if value is not None)
    score = sum(weight * value for _, weight, value, _ in values if value is not None) / coverage * 100 if coverage else None
    classification = "INSUFFICIENT_TARGET_OR_STOP" if rr is None else "POOR_EXECUTION" if rr < CONFIG["minimum_gross_reward_risk"] else "CONDITIONAL_ENTRY_REVIEW" if confirmed else "WAITING_FOR_TRIGGER"
    return {"value": score if rr is not None else None, "coverage": coverage, "status": "UNCALIBRATED", "classification": classification, "gross_reward_risk": rr, "net_reward_risk": None, "entry": price, "stop": stop, "target": target, "components": [{"name": name, "weight": weight, "value": value * 100 if value is not None else None, "reason": reason} for name, weight, value, reason in values], "warning": "Strong structure can still be a poor entry. Net execution quality requires actual costs and depth coverage."}


def _swing_classification(frame: dict, timeframe: str, previous: dict) -> tuple[str, str]:
    """Symmetric, predeclared phase hypotheses from presently observed evidence."""
    if frame.get("quality") != "DERIVED":
        return "INSUFFICIENT_DATA", "NEUTRAL"
    metrics = frame.get("metrics", {})
    velocity, acceleration = _number(metrics.get("velocity_percent_per_bar")), _number(metrics.get("acceleration_percent_per_bar"))
    price, atr = _number(metrics.get("close")), _number(metrics.get("atr"))
    if velocity is None or acceleration is None or price is None or atr is None or atr <= 0:
        return "INSUFFICIENT_DATA", "NEUTRAL"
    threshold = CONFIG["swing_velocity_atr_threshold"] * atr / price * 100
    events, state = frame.get("events", []), frame.get("state")
    active = frame.get("active_break") or frame.get("pending_shift") or {}
    if "SELL_SIDE_SWEEP" in events or state == "FAILED_BREAKDOWN":
        return "TURNING_UP", "LONG"
    if "BUY_SIDE_SWEEP" in events or state == "FAILED_BREAKOUT":
        return "TURNING_DOWN", "SHORT"
    if state in {"CHoCH", "MSS"} and active.get("direction") in {"BULLISH", "BEARISH"}:
        upward = active["direction"] == "BULLISH"
        return ("EARLY_UPSWING" if upward else "EARLY_DOWNSWING") if state == "MSS" else ("TURNING_UP" if upward else "TURNING_DOWN"), "LONG" if upward else "SHORT"
    newest = frame.get("pivots", [])[-1] if frame.get("pivots") else {}
    newest_kind = newest.get("kind")
    confirmed_event = _timestamp(newest.get("event_timestamp"))
    last_event = _timestamp(frame.get("last_completed_candle"))
    age_bars = (last_event - confirmed_event).total_seconds() / TIMEFRAMES[timeframe] - CONFIG["swing_right"] if last_event and confirmed_event else None
    recently_confirmed = age_bars is not None and 0 <= age_bars <= CONFIG["swing_early_bars"]
    high, low = frame.get("known_high_before_candle") or {}, frame.get("known_low_before_candle") or {}
    near_high = _number(high.get("price")) is not None and abs(high["price"] - price) / atr <= CONFIG["swing_near_boundary_atr"]
    near_low = _number(low.get("price")) is not None and abs(low["price"] - price) / atr <= CONFIG["swing_near_boundary_atr"]
    trend = frame.get("trend")
    old_side = previous.get("side")
    if trend == "BULLISH_TREND":
        if velocity < -threshold:
            return ("TOPPING" if newest_kind == "high" and near_high else "TURNING_DOWN"), "LONG" if newest_kind == "high" and near_high else "SHORT"
        if abs(velocity) <= threshold or acceleration < -threshold:
            return ("TOPPING" if newest_kind == "high" and near_high else "LOSING_MOMENTUM"), "LONG"
        return ("EARLY_UPSWING" if newest_kind == "low" and recently_confirmed else "UPSWING"), "LONG"
    if trend == "BEARISH_TREND":
        if velocity > threshold:
            return "TURNING_UP", "LONG"
        if abs(velocity) <= threshold or (acceleration > threshold and near_low):
            return "BOTTOMING", "SHORT"
        if acceleration > threshold:
            return "LOSING_MOMENTUM", "SHORT"
        return ("EARLY_DOWNSWING" if newest_kind == "high" and recently_confirmed else "DOWNSWING"), "SHORT"
    if abs(velocity) <= threshold:
        return ("BOTTOMING", "SHORT") if near_low and newest_kind == "low" else ("TOPPING", "LONG") if near_high and newest_kind == "high" else ("INSUFFICIENT_DATA", "NEUTRAL")
    if velocity > 0:
        return ("TURNING_UP" if old_side == "SHORT" else "EARLY_UPSWING" if newest_kind == "low" and recently_confirmed else "UPSWING"), "LONG"
    return ("TURNING_DOWN" if old_side == "LONG" else "EARLY_DOWNSWING" if newest_kind == "high" and recently_confirmed else "FALLING"), "SHORT"


def _swing_statistics(bars: list[Candle], timeframe: str, side: str, previous: dict, as_of: datetime, newest: dict | None) -> dict:
    """Track only data after the directional leg became known to this engine."""
    last = bars[-1]
    old = _plain(previous.get("statistics"))
    start = _timestamp(old.get("start_known_at"))
    old_prices = [_number(old.get(key)) for key in ("start_price", "high", "low")]
    reusable_prices = all(value is not None and value > 0 for value in old_prices)
    reuse = start is not None and start <= as_of and reusable_prices and old.get("side", previous.get("side")) == side and old.get("source") == last.source
    if not reuse:
        return {"start_timestamp": _stamp(as_of), "start_known_at": _stamp(as_of), "start_event_timestamp": _stamp(last.close_time),
                "start_price": last.close, "current_price": last.close, "high": last.close, "low": last.close,
                "percent_move": 0.0, "directional_move_percent": 0.0, "duration_seconds": 0.0, "duration_bars": 0,
                "mfe_return": 0.0 if side != "NEUTRAL" else None, "mae_return": 0.0 if side != "NEUTRAL" else None, "side": side, "source": last.source,
                "anchor_event_timestamp": newest.get("event_timestamp") if newest else None,
                "anchor_discovered_at": newest.get("discovered_at") if newest else None,
                "last_observed_candle": _stamp(last.close_time), "coverage_status": "KNOWN_START_ONLY",
                "definition": "Measured from this engine's known leg start, not retrospectively from the pivot candle."}
    stats = copy.deepcopy(old)
    prior_candle = _timestamp(old.get("last_observed_candle")) or start
    new = [bar for bar in bars if bar.close_time > prior_candle and bar.available_at <= as_of]
    full = [bar for bar in new if bar.open_time >= start]
    high, low = old["high"], old["low"]
    # A completed post-start close is a known observation. Its earlier wick
    # extrema may predate the start and must not enter this leg's excursions.
    for bar in new:
        high, low = max(high, bar.close), min(low, bar.close)
    for bar in full:
        high, low = max(high, bar.high), min(low, bar.low)
    gap = bool(new and new[0].open_time > prior_candle)
    coverage = "PARTIAL_HISTORY_GAP" if gap or old.get("coverage_status") == "PARTIAL_HISTORY_GAP" else "PARTIAL_FIRST_INTERVAL_EXCLUDED" if len(full) != len(new) or old.get("coverage_status") == "PARTIAL_FIRST_INTERVAL_EXCLUDED" else "COMPLETED_POST_START_INTERVALS" if full or old.get("duration_bars", 0) else "KNOWN_START_ONLY"
    sign = 1 if side == "LONG" else -1 if side == "SHORT" else 0
    entry = old["start_price"]
    favorable, adverse = (high, low) if sign == 1 else (low, high) if sign == -1 else (entry, entry)
    stats.update(current_price=last.close, high=high, low=low,
                 percent_move=(last.close / entry - 1) * 100,
                 directional_move_percent=sign * (last.close / entry - 1) * 100,
                 duration_seconds=max(0.0, (last.close_time - start).total_seconds()),
                 duration_bars=old.get("duration_bars", 0) + len(full),
                 mfe_return=max(0.0, sign * (favorable / entry - 1)) if sign else None,
                 mae_return=min(0.0, sign * (adverse / entry - 1)) if sign else None,
                 last_observed_candle=_stamp(last.close_time), coverage_status=coverage)
    return stats


def _active_fib_context(analysis: dict, timeframe: str, side: str, price: float | None, atr: float | None, as_of: datetime) -> dict:
    active = analysis.get("active_fibonacci", {}).get(timeframe, {})
    unavailable = {"status": "UNAVAILABLE", "value": None, "maximum_score_contribution": 10.0,
                   "weight": CONFIG["swing_fib_context_weight"], "levels": [], "reason": "A fresh causal matching active Fib pair and confluence near price are required.",
                   "calibration": "UNCALIBRATED", "probability": None, "ranking_weight": 0}
    stamps = [active.get("discovered_at"), active.get("source_timestamp"), active.get("received_timestamp")]
    if not active.get("enabled") or active.get("status") != "AVAILABLE" or active.get("quality") != "DERIVED" or active.get("bias") != side or len(active.get("anchors", [])) != 2 or any(_timestamp(stamp) is None or _timestamp(stamp) > as_of for stamp in stamps):
        return unavailable
    for anchor in active["anchors"]:
        if any(_timestamp(anchor.get(field)) is None or _timestamp(anchor[field]) > as_of for field in ("anchor_timestamp", "discovery_timestamp")):
            return unavailable
        if _timestamp(anchor["anchor_timestamp"]) > _timestamp(anchor["discovery_timestamp"]) or _timestamp(anchor["discovery_timestamp"]) > _timestamp(active["discovered_at"]):
            return unavailable
    if price is None or atr is None or atr <= 0:
        return unavailable
    levels, target_candidates = [], []
    for level in active.get("levels", []):
        level_price, score = _number(level.get("price")), _number(level.get("confluence_score"))
        discovered = _timestamp(level.get("discovered_at"))
        if level_price is None or score is None or discovered is None or discovered > as_of or level.get("quality") not in {"DERIVED", "NATIVE"}:
            continue
        distance = abs(level_price - price) / atr
        target_candidates.append({"price": level_price, "ratio": level.get("ratio"), "kind": level.get("kind"), "discovered_at": level["discovered_at"]})
        if distance <= 1 and level.get("confluence"):
            behaviour = copy.deepcopy(level.get("behaviour") or {})
            state = behaviour.get("state", "UNTESTED")
            # REJECTED is the upstream generic touching fallback, including
            # closes inside a zone; it is not confirmed directional rejection.
            response = 0.0 if state == "BROKEN" else 1.0 if state in {"ACCEPTED", "SWEPT_AND_RECLAIMED"} else 0.5
            levels.append({"price": level_price, "ratio": level.get("ratio"), "kind": level.get("kind"),
                           "distance_atr": distance, "confluence_score": max(0.0, min(100.0, score)),
                           "response_multiplier": response, "behaviour": behaviour,
                           "directional_role": "SUPPORT_CONTEXT" if side == "LONG" else "RESISTANCE_CONTEXT",
                           "confluence": copy.deepcopy(level["confluence"]), "discovered_at": level["discovered_at"]})
    value = max((level["confluence_score"] * level["response_multiplier"] for level in levels), default=0.0)
    return {**unavailable, "status": "AVAILABLE", "value": value, "levels": levels,
            "source": active.get("source"), "pair_id": active.get("pair_id"), "golden_pocket": copy.deepcopy(active.get("golden_pocket")),
            "target_candidates": target_candidates,
            "reason": "At most ten uncalibrated contextual points from matching support/resistance confluence: observed acceptance/reclaim x1, generic touch or untested geometry x0.5, broken x0. REJECTED alone does not prove directional rejection; no standalone Fib touch bonus or probability."}


def _symmetric_setup_quality(frame: dict, side: str, fib: dict) -> dict:
    metrics = frame.get("metrics", {})
    direction = "BULLISH" if side == "LONG" else "BEARISH" if side == "SHORT" else None
    velocity = _number(metrics.get("velocity_percent_per_bar"))
    ratio, rvol = _number(metrics.get("range_compression_ratio")), _number(metrics.get("rvol"))
    sign = 1 if side == "LONG" else -1 if side == "SHORT" else 0
    structure = 1.0 if direction and frame.get("trend") == direction + "_TREND" else 0.7 if direction and frame.get("bias") == direction else 0.4 if direction else None
    components = [
        {"name": "structure_direction", "weight": 0.35, "value": structure * 100 if structure is not None else None, "reason": "Same confirmed-trend/bias rule for long and short."},
        {"name": "momentum_direction", "weight": 0.20, "value": 100.0 if velocity is not None and sign * velocity > 0 else 0.0 if velocity is not None and sign else None, "reason": "Observed closed-bar velocity aligned with this side; no future return."},
        {"name": "range_compression", "weight": 0.20, "value": max(0.0, min(100.0, (1 - ratio / 2) * 100)) if ratio is not None else None, "reason": "Direction-neutral compression proxy, not physical energy."},
        {"name": "observed_relative_volume", "weight": 0.15, "value": min(100.0, rvol / 2 * 100) if rvol is not None else None, "reason": "Completed candle RVOL supports context, not directional proof."},
        {"name": "active_fibonacci_confluence", "weight": CONFIG["swing_fib_context_weight"], "value": fib["value"], "reason": fib["reason"], "levels": fib["levels"]},
    ]
    coverage = sum(item["weight"] for item in components if item["value"] is not None)
    for component in components:
        component["contribution"] = component["weight"] * component["value"] if component["value"] is not None else None
    value = sum(item["contribution"] for item in components if item["contribution"] is not None) if coverage >= 0.7 and side != "NEUTRAL" else None
    return {"value": value, "coverage": coverage, "components": components, "status": "UNCALIBRATED" if value is not None else "INSUFFICIENT_DATA",
            "maximum_available_score": coverage * 100,
            "source": "direction_symmetric_swing_context", "probability": None, "ranking_weight": 0,
            "warning": "Predeclared contextual hypotheses; existing nominal Expansion Readiness/ranks are unchanged."}


def _swing_decision(frame: dict, timeframe: str, side: str, phase: str, analysis: dict, fast: dict, as_of: datetime) -> dict:
    metrics = frame.get("metrics", {})
    price, atr = _number(metrics.get("close")), _number(metrics.get("atr"))
    high, low = frame.get("known_high_before_candle") or {}, frame.get("known_low_before_candle") or {}
    upper = _number(high.get("price")) or _number(metrics.get("range_high"))
    lower = _number(low.get("price")) or _number(metrics.get("range_low"))
    fast_direction = _fast_direction(fast, as_of) if timeframe in {"1m", "5m"} else None
    structural_side = side
    if fast_direction:
        side = "LONG" if fast_direction == "BULLISH" else "SHORT"
    if timeframe in {"1m", "5m"}:
        resistance, support = _known_fast_boundary(fast, "resistance", as_of), _known_fast_boundary(fast, "support", as_of)
        upper, lower = resistance.get("price") or upper, support.get("price") or lower
    trigger, invalidation = (upper, lower) if side == "LONG" else (lower, upper) if side == "SHORT" else (None, None)
    sign = 1 if side == "LONG" else -1 if side == "SHORT" else 0
    targets = _targets(_levels(analysis, as_of), trigger, sign) if sign else []
    target = targets[0]["price"] if targets else None
    risk = sign * (price - invalidation) if price is not None and invalidation is not None else None
    reward = sign * (target - price) if price is not None and target is not None else None
    rr = reward / risk if risk is not None and risk > 0 and reward is not None and reward > 0 else None
    extension = sign * (price - trigger) / atr if price is not None and trigger is not None and atr and atr > 0 else None
    fib = _active_fib_context(analysis, timeframe, side, price, atr, as_of)
    fib["target_confluence"] = [{"observed_target": level["price"], "fib_price": candidate["price"], "ratio": candidate["ratio"], "distance_atr": abs(level["price"] - candidate["price"]) / atr,
                                 "role": "DERIVED_CONTEXT_NOT_OBSERVED_LIQUIDITY"}
                                for level in targets for candidate in fib.get("target_candidates", [])
                                if atr and atr > 0 and abs(level["price"] - candidate["price"]) / atr <= 1]
    setup = _symmetric_setup_quality(frame, side, fib)
    status = "WAIT"
    reasons = []
    if frame.get("quality") != "DERIVED" or price is None or atr is None or atr <= 0:
        status, reasons = "INSUFFICIENT_DATA", ["Fresh closed candle and positive ATR are required."]
    elif side == "NEUTRAL":
        status, reasons = "WAIT", ["Direction is not established; no default long or short."]
    elif trigger is None or invalidation is None or target is None:
        status, reasons = "INSUFFICIENT_DATA", ["Known trigger, controlling invalidation and observed target are required; no target is fabricated."]
    elif risk is None or risk <= 0:
        status, reasons = "NO_TRADE", ["Structural invalidation is already lost or lies on the wrong side of the entry."]
    elif extension is not None and (extension > CONFIG["swing_too_late_atr"] or reward is not None and reward <= 0):
        status, reasons = "TOO_LATE", ["Price is overextended from the known trigger or has already reached the nearest observed target."]
    elif rr is None or rr < CONFIG["minimum_gross_reward_risk"]:
        status, reasons = "NO_TRADE", ["Observed target space does not meet the configured minimum gross reward/risk; strong setup is not a good entry."]
    elif phase in {"TOPPING", "BOTTOMING", "LOSING_MOMENTUM"} or extension is not None and extension < -CONFIG["swing_too_late_atr"]:
        status, reasons = "WAIT", ["Momentum/turning evidence or distance from the trigger requires a separate confirmed response."]
    else:
        status, reasons = ("LONG_SETUP" if side == "LONG" else "SHORT_SETUP"), ["Conditional setup only: crossing/acceptance and intact invalidation remain required."]
    zone = {"lower": max(0.0, trigger - atr * CONFIG["swing_entry_zone_atr"]), "upper": trigger + atr * CONFIG["swing_entry_zone_atr"], "source": "known_trigger_plus_current_ATR", "status": "CONDITIONAL_NOT_EXECUTED"} if trigger is not None and atr and atr > 0 else None
    available_levels = _levels(analysis, as_of)
    opposite_targets = _targets(available_levels, invalidation, -sign) if sign else []
    break_state = "BREAKOUT" if side == "LONG" else "BREAKDOWN" if side == "SHORT" else "DIRECTIONAL_BREAK_REQUIRED"
    if frame.get("state") in {"BREAKOUT", "BREAKDOWN", "CHoCH", "MSS", "RETEST", "EXPANSION"}:
        labels = ["RETEST_REVIEW", "ACCEPTANCE_REVIEW", "CONTINUATION_OR_FAILURE_REVIEW"]
    else:
        labels = ["BOUNDARY_TEST", "CLOSED_BREAK_REVIEW", "ACCEPTANCE_OR_FAILURE_REVIEW"]
    primary = [{"step": index + 1, "state": label, "level": trigger if index < 2 else target,
                "condition": f"IF {side.lower()} structure remains intact and {timeframe} context supports {break_state.lower()}, evaluate {label.lower()}; no future candle or outcome is guaranteed."}
               for index, label in enumerate(labels)]
    alternative = [{"step": index + 1, "state": label, "level": (lower, upper, trigger)[index],
                    "condition": "IF the known range survives without directional acceptance, reassess the range instead of assuming continuation."}
                   for index, label in enumerate(["RANGE_REVIEW", "BOUNDARY_TEST", "CLOSED_BREAK_REVIEW"])]
    failure = [{"step": index + 1, "state": label, "level": invalidation if index < 2 else opposite_targets[0]["price"] if opposite_targets else None,
                "condition": "IF the controlling invalidation is lost on a completed candle, cancel this thesis; opposite continuation requires its own trigger and acceptance."}
               for index, label in enumerate(["INVALIDATION_REVIEW", "OPPOSITE_BOUNDARY_RETEST", "OPPOSITE_CONTINUATION_REVIEW"])]
    rr_quality = min(100.0, max(0.0, (rr - 1) / 3 * 100)) if rr is not None else None
    proximity_quality = max(0.0, 100 * (1 - abs(extension) / CONFIG["swing_too_late_atr"])) if extension is not None else None
    entry_components = [{"name": "gross_reward_risk", "weight": 0.70, "value": rr_quality, "raw_value": rr, "reason": "Measured known target/invalidation; execution fees and funding require the manual calculator."},
                        {"name": "trigger_proximity", "weight": 0.20, "value": proximity_quality, "raw_value": extension, "reason": "Direction-symmetric extension/chasing check."},
                        {"name": "active_fibonacci_confluence", "weight": 0.10, "value": fib["value"], "reason": fib["reason"], "levels": fib["levels"]}]
    for component in entry_components:
        component["contribution"] = component["weight"] * component["value"] if component["value"] is not None else None
    entry = {"value": sum(item["contribution"] for item in entry_components if item["contribution"] is not None) if rr is not None else None,
             "coverage": sum(item["weight"] for item in entry_components if item["value"] is not None),
             "status": "UNCALIBRATED" if rr is not None else "INSUFFICIENT_DATA", "classification": status,
             "gross_reward_risk": rr, "net_reward_risk": None, "loss_at_stop_percent": risk / price * 100 if risk is not None and risk > 0 and price else None,
             "components": entry_components, "probability": None}
    return {"timeframe": timeframe, "side": side, "entry_status": status, "entry_zone": zone,
            "trigger": trigger, "invalidation": invalidation, "target_1": target, "target_2": targets[1]["price"] if len(targets) > 1 else None,
            "targets": targets, "gross_reward_risk": rr, "distance_to_trigger_atr": abs(extension) if extension is not None else None,
            "setup_quality": setup, "entry_quality": entry, "fib_context": fib,
            "primary_path": primary, "alternative_path": alternative, "failure_path": failure, "reasons": reasons,
            "structure_side": structural_side, "fast_side": side if fast_direction else "UNAVAILABLE", "direction_conflict": fast_direction is not None and structural_side not in {"NEUTRAL", side},
            "confirmation": "Existing completed 1m close plus predetermined 5m boundary/ATR-tick buffer remains the fast rule; these TF scenarios do not delay it.",
            "fast_short_alert": "NATIVE_DIRECTIONAL_ENGINE", "probability": None, "source": frame.get("source", "UNAVAILABLE"), "as_of": _stamp(as_of)}


def _swing_report(frame: dict, timeframe: str, bars: list[Candle], previous: dict, analysis: dict, fast: dict, as_of: datetime) -> dict:
    if frame.get("quality") != "DERIVED" or not bars:
        return {"status": "STALE" if frame.get("quality") == "STALE" else "INSUFFICIENT_DATA", "current_phase": "UNAVAILABLE", "side": "NEUTRAL",
                "latest_swing": None, "statistics": copy.deepcopy(previous.get("statistics")),
                "phase_history": copy.deepcopy(previous.get("phase_history", [])), "conditional_next_steps": [], "probability": None,
                "decision": {"entry_status": "INSUFFICIENT_DATA", "side": "NEUTRAL", "setup_quality": {"value": None}, "entry_quality": {"value": None}}}
    phase, side = _swing_classification(frame, timeframe, previous)
    newest = frame.get("pivots", [])[-1] if frame.get("pivots") else None
    history = copy.deepcopy(previous.get("phase_history", []))[-CONFIG["history_limit"]:]
    if not history or history[-1]["phase"] != phase or history[-1].get("side") != side:
        history.append({"phase": phase, "side": side, "event_timestamp": frame["last_completed_candle"], "discovered_at": _stamp(as_of)})
        history = history[-CONFIG["history_limit"]:]
    decision = _swing_decision(frame, timeframe, side, phase, analysis, fast, as_of)
    stats = _swing_statistics(bars, timeframe, side, previous, as_of, newest)
    metrics = frame.get("metrics", {})
    stats.update(momentum=metrics.get("momentum_state", "UNAVAILABLE"), volatility=metrics.get("volatility_state", "UNAVAILABLE"),
                 velocity_percent_per_bar=metrics.get("velocity_percent_per_bar"), acceleration_percent_per_bar=metrics.get("acceleration_percent_per_bar"),
                 atr=metrics.get("atr"), realized_volatility_percent_per_bar=metrics.get("realized_volatility_percent_per_bar"))
    return {"status": "CONDITIONAL_RESEARCH", "current_phase": phase, "side": side, "latest_swing": newest,
            "statistics": stats, "phase_history": history, "decision": decision,
            "conditional_next_steps": decision["primary_path"], "trigger": decision["trigger"], "invalidation": decision["invalidation"],
            "entry_status": decision["entry_status"], "entry_zone": decision["entry_zone"], "target_1": decision["target_1"], "target_2": decision["target_2"],
            "warning": "Observed swing phases and three conditional structure steps, never predicted future OHLC candles or probabilities.", "probability": None}


def analyze_war_room(
    symbol: str,
    bars: dict[str, list[Candle]],
    as_of: datetime,
    analysis: dict,
    fast_state=None,
    previous: dict | None = None,
    *,
    pattern_settings: PatternSettings | None = None,
) -> dict:
    """Return a JSON-safe snapshot; ``previous`` is restart-persistable memory.

    Out-of-order decision times are rejected rather than backdating new states.
    Only closed candles with ``available_at <= as_of`` can enter a feature.
    """
    as_of = aware(as_of)
    previous = previous or {}
    if previous.get("symbol") not in {None, symbol}:
        raise ValueError("previous structure state belongs to another asset")
    previous_time = _timestamp(previous.get("as_of"))
    if previous_time and previous_time > as_of:
        raise ValueError("previous structure state belongs to a future decision time")
    snapshot = AssetSnapshot(asset=Asset(asset_id=symbol, symbol=symbol, name=symbol), as_of=as_of, candles=bars)
    frames, issues, closed_by_frame = {}, [], {}
    geometric_config = pattern_settings or PatternSettings()
    input_analysis_time = _timestamp(analysis.get("as_of"))
    current_liquidity = analysis.get("liquidity_map", []) if input_analysis_time is not None and input_analysis_time <= as_of else []
    old_frames = previous.get("timeframes", {})
    for frame in FRAMES:
        known, errors = _known_bars(snapshot, frame)
        known = known[-CONFIG["maximum_bars"]:]
        closed_by_frame[frame] = known
        if errors or len(known) < 20:
            frames[frame] = {"state": "INSUFFICIENT_DATA", "bias": "UNAVAILABLE", "quality": "UNAVAILABLE", "pivots": [], "history": copy.deepcopy(old_frames.get(frame, {}).get("history", []))[-CONFIG["history_limit"]:], "metrics": {}, "events": [], "issues": errors or ["minimum_20_closed_candles_required"]}
            issues.extend(f"{frame}:{error}" for error in frames[frame]["issues"])
            continue
        old = old_frames.get(frame, {})
        previous_candle = _timestamp(old.get("last_completed_candle"))
        if previous_candle and previous_candle > known[-1].close_time:
            frames[frame] = copy.deepcopy(old)
            frames[frame]["quality"] = "STALE"
            issues.append(f"{frame}:out_of_order_or_regressed_history")
        else:
            frames[frame] = _state(known, frame, as_of, old)
        prior_patterns = old.get("pattern_report", {})
        pattern_bars = known[-geometric_config.maximum_bars:]
        revision = candle_revision(pattern_bars)
        if frames[frame]["quality"] == "DERIVED" and prior_patterns.get("revision") == revision and prior_patterns.get("status") == "DERIVED" and prior_patterns.get("config") == geometric_config.model_dump():
            # Geometric anchors/thresholds are immutable for this completed
            # candle revision. Repeated live ticks do not re-fit the history.
            frames[frame]["pattern_report"] = refresh_known_targets(prior_patterns, current_liquidity, as_of, known[-1].close)
        else:
            frames[frame]["pattern_report"] = detect_patterns(
                pattern_bars, frame, as_of, context=frames[frame].get("trend"),
                liquidity_levels=current_liquidity,
                structure=frames[frame], settings=geometric_config,
            )
    alignment = _alignment(frames)
    analysis_at = _timestamp(analysis.get("as_of"))
    if analysis_at is None or analysis_at > as_of:
        analysis = {}
        issues.append("analysis_context_unavailable_or_future")
    fast = _plain(fast_state)
    if any((_timestamp(fast.get(key)) or as_of) > as_of for key in ("last_evaluation_at", "confirmed_at", "accepted_at", "structural_at")):
        fast = {}
        issues.append("fast_context_is_from_future")
    if fast.get("stale") is True:
        fast = {}
        issues.append("fast_context_is_stale")
    price = next((frames[frame].get("metrics", {}).get("close") for frame in ("1m", "5m", "15m", "1h", "4h", "1D") if frames[frame]["quality"] == "DERIVED"), None)
    direction = _fast_direction(fast, as_of) or alignment["direction"]
    scenarios, expected = _paths(frames, analysis, fast, as_of, price, direction)
    context = next((frames[frame] for frame in ("5m", "15m", "1m") if frames[frame]["quality"] == "DERIVED"), {})
    readiness = analysis.get("scores", {}).get("expansion_readiness", {})
    if not isinstance(readiness, dict):
        readiness = {}
    setup = {**readiness, "value": _number(readiness.get("value")), "coverage": readiness.get("coverage", 0), "status": "UNCALIBRATED" if _number(readiness.get("value")) is not None else "INSUFFICIENT_DATA", "source": "existing_expansion_readiness", "components": readiness.get("components", []), "reason": "Reuses current discovery score; structure hypotheses do not silently change production weights."}
    swings = {frame: _swing_report(state, frame, closed_by_frame.get(frame, []), previous.get("swing_phases", {}).get(frame, {}), analysis, fast, as_of)
              for frame, state in frames.items()}
    for frame, state in frames.items():
        state["swing_phase"] = swings[frame]["current_phase"]
        if state.get("history") and state["quality"] == "DERIVED":
            state["history"][-1]["swing_phase"] = swings[frame]["current_phase"]
    fresh = sum(frame["quality"] == "DERIVED" for frame in frames.values())
    report = {
        "symbol": symbol,
        "as_of": _stamp(as_of),
        "status": "AVAILABLE" if fresh == len(FRAMES) else "PARTIAL" if fresh else "INSUFFICIENT_DATA",
        "mode": "DECISION_SUPPORT",
        "bias": direction,
        "market_conflict": direction in {"BULLISH", "BEARISH"} and alignment["direction"] in {"BULLISH", "BEARISH"} and direction != alignment["direction"],
        "price": price,
        "price_source": "LATEST_FRESH_COMPLETED_CANDLE" if price is not None else "UNAVAILABLE",
        "timeframes": frames,
        "alignment": alignment,
        "scenarios": scenarios,
        "expected_path": expected,
        "setup_quality": setup,
        "entry_quality": _entry_quality(price, scenarios, expected["primary"], context, analysis, fast),
        "fast_state": fast.get("stage", "UNAVAILABLE"),
        "market_regime": analysis.get("market_regime", "UNAVAILABLE"),
        "config": CONFIG.copy(),
        "implemented_patterns": ["confirmed_HH_HL_LH_LL", "ATR_equal_highs_lows", "range", "compression", "BOS", "CHoCH", "MSS", "liquidity_sweep_reclaim", "breakout_retest", "failed_breakout", "failed_breakdown", *PATTERN_NAMES],
        "unimplemented_patterns": ["discretionary_cup_handle", "validated_pattern_probabilities"],
        "patterns": [pattern for state in frames.values() if state["quality"] == "DERIVED" for pattern in state.get("pattern_report", {}).get("detections", [])],
        "structure_sequences": [dict(sequence, timeframe=frame) for frame, state in frames.items() if state["quality"] == "DERIVED" for sequence in state.get("pattern_report", {}).get("sequences", [])],
        "swing_phases": swings,
        "decisions": {frame: swing["decision"] for frame, swing in swings.items()},
        "supported_swing_phases": list(SWING_PHASES),
        "research_weight": 0,
        "issues": issues,
        "limitations": ["All new path/structure interpretations are hypotheses; no calibrated probability or profitability claim.", "Liquidity stops are inferred structure; resting orders remain separately categorized native book evidence.", "Missing observed targets remain UNAVAILABLE; no synthetic extension target fills a missing price map.", "Initial startup summarizes available context; earlier state sequences require persisted prior evaluations or chronological replay."],
    }
    report["setup_library"] = evaluate_setups(report, analysis, as_of)
    return report
