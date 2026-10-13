"""Causal, explicitly defined chart geometries and structure-sequence hypotheses.

Names describe geometry, not an established predictive edge. Every pivot needs
its right-hand confirmation; every level retains event and availability times.
The module runs on completed-candle analysis cadence, outside the trigger path.
No measured-move target or numerical probability is manufactured here.
"""

from __future__ import annotations

import copy
import hashlib
import math
from datetime import datetime
from statistics import mean

from pydantic import Field

from .fast import _atr
from .live_analysis import _known_bars
from .models import TIMEFRAMES, Asset, AssetSnapshot, Candle, Model, aware
from .technical import confirmed_swings

PATTERN_NAMES = (
    "DOUBLE_TOP", "DOUBLE_BOTTOM", "TRIPLE_TOP", "TRIPLE_BOTTOM",
    "HEAD_AND_SHOULDERS", "INVERSE_HEAD_AND_SHOULDERS",
    "ASCENDING_TRIANGLE", "DESCENDING_TRIANGLE", "SYMMETRICAL_TRIANGLE",
    "BULL_FLAG", "BEAR_FLAG", "BULL_PENNANT", "BEAR_PENNANT",
    "RISING_WEDGE", "FALLING_WEDGE", "ASCENDING_CHANNEL", "DESCENDING_CHANNEL",
    "ROUNDED_BOTTOM", "ROUNDED_TOP", "RANGE_DEVIATION",
    "BREAKOUT_RETEST", "FAILED_BREAKOUT_REVERSAL", "FAILED_BREAKDOWN_REVERSAL",
)


class PatternSettings(Model):
    swing_left: int = Field(default=2, ge=1, le=10)
    swing_right: int = Field(default=2, ge=1, le=10)
    atr_period: int = Field(default=14, ge=2, le=100)
    maximum_bars: int = Field(default=128, ge=32, le=512)
    minimum_bars: int = Field(default=20, ge=16, le=100)
    maximum_pivot_age_bars: int = Field(default=60, ge=12, le=512)
    equal_tolerance_atr: float = Field(default=0.4, gt=0, le=3)
    minimum_reaction_atr: float = Field(default=0.75, gt=0, le=10)
    minimum_touch_spacing_bars: int = Field(default=3, ge=2, le=30)
    head_prominence_atr: float = Field(default=0.75, gt=0, le=10)
    minimum_boundary_move_atr: float = Field(default=0.75, gt=0, le=10)
    parallel_tolerance_atr: float = Field(default=0.4, gt=0, le=3)
    minimum_line_r_squared: float = Field(default=0.7, ge=0, le=1)
    minimum_width_contraction: float = Field(default=0.20, gt=0, lt=1)
    formation_window: int = Field(default=24, ge=12, le=64)
    impulse_window: int = Field(default=16, ge=6, le=64)
    minimum_impulse_atr: float = Field(default=3.0, gt=0, le=20)
    maximum_flag_retracement: float = Field(default=0.5, gt=0, lt=1)
    rounded_window: int = Field(default=40, ge=20, le=100)
    rounded_minimum_r_squared: float = Field(default=0.85, ge=0, le=1)
    rounded_minimum_depth_atr: float = Field(default=2.0, gt=0, le=20)
    minimum_rounding_recovery: float = Field(default=0.5, gt=0, le=1)
    maximum_detections: int = Field(default=24, ge=1, le=64)


def _time(value) -> datetime | None:
    try:
        return aware(value if isinstance(value, datetime) else datetime.fromisoformat(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _stamp(value: datetime) -> str:
    return aware(value).isoformat()


def candle_revision(bars: list[Candle]) -> str:
    """Bounded candle revision identity includes OHLCV, source and availability.

    Same close timestamp with changed/corrected prices must not reuse geometry.
    Native flow updates do not change chart anchors but are included for callers
    that reuse the associated setup evidence.
    """
    digest = hashlib.sha256()
    for bar in bars:
        digest.update(repr((bar.open_time, bar.close_time, bar.available_at, bar.open, bar.high,
                            bar.low, bar.close, bar.volume, bar.taker_buy_volume, bar.source)).encode())
    return digest.hexdigest()


def _line(points: list[dict]) -> dict | None:
    if len(points) < 2:
        return None
    xs, ys = [point["index"] for point in points], [point["price"] for point in points]
    x_mean, y_mean = mean(xs), mean(ys)
    variance = sum((x - x_mean) ** 2 for x in xs)
    if variance == 0:
        return None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True)) / variance
    intercept = y_mean - slope * x_mean
    total = sum((y - y_mean) ** 2 for y in ys)
    residual = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    return {"slope": slope, "intercept": intercept,
            "r_squared": 1 - residual / total if total > 1e-12 else 1.0}


def _value(line: dict, index: int) -> float:
    return line["intercept"] + line["slope"] * index


def _anchors(pivots: list[dict], bars: list[Candle]) -> list[dict]:
    return [{"kind": pivot["kind"], "price": pivot["price"], "bar_index": pivot["index"],
             "event_timestamp": _stamp(bars[pivot["index"]].close_time),
             "discovered_at": pivot["confirmed_at"], "source": bars[pivot["index"]].source,
             "algorithm": "strict_confirmed_pivot"} for pivot in pivots]


def known_liquidity_levels(levels: list[dict], as_of: datetime) -> list[dict]:
    """Only real timestamped known levels; missing/stale/cancelled ones excluded."""
    result = []
    for level in levels:
        price = _number(level.get("price", level.get("representative_price")))
        discovered = _time(level.get("discovered_at", level.get("discovery_timestamp")))
        event = _time(level.get("event_timestamp"))
        if price is None or price <= 0 or discovered is None or discovered > as_of:
            continue
        if event is not None and (event > discovered or event > as_of):
            continue
        if level.get("quality") in {"STALE", "UNAVAILABLE", "MISSING"} or level.get("status") in {"INVALIDATED", "CONSUMED"}:
            continue
        if not level.get("source"):
            continue
        result.append({"price": price, "type": level.get("type", "KNOWN_LEVEL"),
                       "category": level.get("category", "UNAVAILABLE"),
                       "quality": level.get("quality", "ESTIMATED"), "source": level["source"],
                       "discovered_at": _stamp(discovered)})
    return result


def _destinations(levels: list[dict], boundary: float | None, direction: str) -> list[dict]:
    if boundary is None or direction == "NEUTRAL":
        return []
    sign = 1 if direction == "BULLISH" else -1
    selected = []
    for level in sorted((row for row in levels if sign * (row["price"] - boundary) > 0),
                        key=lambda row: sign * row["price"]):
        if not any(math.isclose(level["price"], previous["price"], rel_tol=1e-8) for previous in selected):
            selected.append(level)
        if len(selected) == 3:
            break
    return selected


def refresh_known_targets(report: dict, levels: list[dict], as_of: datetime, current_price: float | None) -> dict:
    """Update inexpensive current destinations without re-fitting old geometry."""
    result = copy.deepcopy(report)
    known = known_liquidity_levels(levels, as_of)
    for pattern in result.get("detections", []):
        trigger = pattern.get("trigger")
        direction = pattern.get("direction_hypothesis")
        boundary = max(trigger, current_price) if trigger is not None and current_price is not None and direction == "BULLISH" else min(trigger, current_price) if trigger is not None and current_price is not None and direction == "BEARISH" else trigger
        targets = _destinations(known, boundary, direction)
        pattern["targets"] = targets
        pattern["target_status"] = "OBSERVED_LEVELS" if targets else "UNAVAILABLE"
        pattern["targets_evaluated_at"] = _stamp(as_of)
        if len(pattern.get("expected_path", [])) >= 3:
            pattern["expected_path"][2]["price"] = targets[0]["price"] if targets else None
    return result


def _report(name, direction, pivots, bars, timeframe, as_of, levels, *, trigger, invalidation,
            metrics, reason, context, config, discovered_at=None):
    anchors = _anchors(pivots, bars)
    # Geometry uses the current ATR/context. It must never be backdated to an
    # older pivot's confirmation merely because that pivot is an anchor.
    discovered = discovered_at or max(bars[-1].available_at, *(_time(anchor["discovered_at"]) for anchor in anchors))
    event = max(_time(anchor["event_timestamp"]) for anchor in anchors) if anchors else bars[-1].close_time
    identity = hashlib.sha256(repr((name, timeframe, bars[-1].source, [(anchor["kind"], anchor["event_timestamp"], anchor["price"]) for anchor in anchors])).encode()).hexdigest()[:24]
    destinations = _destinations(levels, max(trigger, bars[-1].close) if direction == "BULLISH" and trigger else
                                 min(trigger, bars[-1].close) if direction == "BEARISH" and trigger else trigger, direction)
    status = "INVALIDATED_GEOMETRY" if (
        invalidation is not None and (
            (direction == "BULLISH" and bars[-1].close < invalidation)
            or (direction == "BEARISH" and bars[-1].close > invalidation)
        )
    ) else "GEOMETRY_CONFIRMED_TRIGGER_CONDITIONAL"
    return {
        "id": f"pattern:{timeframe}:{name}:{identity}", "name": name, "timeframe": timeframe,
        "direction_hypothesis": direction, "status": status,
        "event_timestamp": _stamp(event), "discovered_at": _stamp(discovered),
        "evaluated_at": _stamp(as_of), "source": bars[-1].source, "quality": "DERIVED",
        "anchors": anchors, "context": context or "UNAVAILABLE", "trigger": trigger,
        "confirmation": "Only a closed candle beyond the known level, then acceptance/retest; the fast 1m monitor retains its own ATR/tick buffer.",
        "invalidation": invalidation, "targets": destinations,
        "targets_evaluated_at": _stamp(as_of),
        "target_status": "OBSERVED_LEVELS" if destinations else "UNAVAILABLE",
        "expected_path": [{"state": "BREAKOUT" if direction == "BULLISH" else "BREAKDOWN" if direction == "BEARISH" else "DIRECTIONAL_BREAK_REQUIRED", "price": trigger, "condition": "IF the frozen known boundary is crossed and accepted"},
                          {"state": "RETEST", "price": trigger, "condition": "THEN evaluate support/resistance flip; a retest is not guaranteed"},
                          {"state": "CONTINUATION", "price": destinations[0]["price"] if destinations else None, "condition": "ONLY IF invalidation stays intact; destination is an observed level, not a forecast"}],
        "metrics": metrics, "reason": reason, "research_weight": 0,
        "probability": None, "hypothesis_status": "UNVALIDATED_GEOMETRIC_DEFINITION",
        "swing_algorithm": f"confirmed_swings(left={config.swing_left},right={config.swing_right})",
    }


def _turn_patterns(pivots, bars, timeframe, as_of, levels, context, atr, config):
    found = []
    for kind, direction, suffix in (("high", "BEARISH", "TOP"), ("low", "BULLISH", "BOTTOM")):
        side = [pivot for pivot in pivots if pivot["kind"] == kind]
        opposite = [pivot for pivot in pivots if pivot["kind"] != kind]
        for count, prefix in ((2, "DOUBLE"), (3, "TRIPLE")):
            selected = side[-count:]
            if len(selected) != count or any(b["index"] - a["index"] < config.minimum_touch_spacing_bars for a, b in zip(selected, selected[1:], strict=False)):
                continue
            spread = max(p["price"] for p in selected) - min(p["price"] for p in selected)
            valleys = [pivot for pivot in opposite if selected[0]["index"] < pivot["index"] < selected[-1]["index"]]
            if len(valleys) < count - 1 or spread > config.equal_tolerance_atr * atr:
                continue
            reactions = []
            for first, second in zip(selected, selected[1:], strict=False):
                between = [pivot for pivot in valleys if first["index"] < pivot["index"] < second["index"]]
                if not between:
                    reactions = []
                    break
                opposite_price = min(p["price"] for p in between) if kind == "high" else max(p["price"] for p in between)
                reactions.append(abs(mean([first["price"], second["price"]]) - opposite_price) / atr)
            if not reactions or min(reactions) < config.minimum_reaction_atr:
                continue
            trigger = min(p["price"] for p in valleys) if kind == "high" else max(p["price"] for p in valleys)
            invalidation = max(p["price"] for p in selected) if kind == "high" else min(p["price"] for p in selected)
            found.append(_report(f"{prefix}_{suffix}", direction, sorted(selected + valleys, key=lambda p: p["index"]), bars, timeframe, as_of, levels,
                                 trigger=trigger, invalidation=invalidation,
                                 metrics={"touches": count, "equal_distance_atr": spread / atr, "minimum_reaction_atr": min(reactions)},
                                 reason="Distinct confirmed equal pivots separated by an ATR-sized reaction; no hindsight-selected touch.", context=context, config=config))
        shoulders = side[-3:]
        if len(shoulders) != 3:
            continue
        left, head, right = shoulders
        if abs(left["price"] - right["price"]) > config.equal_tolerance_atr * atr:
            continue
        sign = 1 if kind == "high" else -1
        prominence = min(sign * (head["price"] - left["price"]), sign * (head["price"] - right["price"])) / atr
        first_neck = [p for p in opposite if left["index"] < p["index"] < head["index"]]
        second_neck = [p for p in opposite if head["index"] < p["index"] < right["index"]]
        if prominence < config.head_prominence_atr or not first_neck or not second_neck:
            continue
        neckline = [min(first_neck, key=lambda p: p["price"]), min(second_neck, key=lambda p: p["price"])] if kind == "high" else [max(first_neck, key=lambda p: p["price"]), max(second_neck, key=lambda p: p["price"])]
        # Conservative horizontal boundary is an actual neck pivot, not a
        # hindsight fitted diagonal at a future timestamp.
        trigger = min(p["price"] for p in neckline) if kind == "high" else max(p["price"] for p in neckline)
        found.append(_report("HEAD_AND_SHOULDERS" if kind == "high" else "INVERSE_HEAD_AND_SHOULDERS", direction,
                             sorted(shoulders + neckline, key=lambda p: p["index"]), bars, timeframe, as_of, levels,
                             trigger=trigger, invalidation=head["price"],
                             metrics={"head_prominence_atr": prominence, "shoulder_distance_atr": abs(left["price"] - right["price"]) / atr, "neckline_zone": sorted(p["price"] for p in neckline)},
                             reason="Three confirmed alternating peaks/troughs; middle pivot is ATR-prominent and shoulders overlap. Trigger is the conservative observed neck boundary.", context=context, config=config))
    return found


def _boundary_geometry(pivots, atr, config, minimum_index=0, minimum_touches=3):
    highs = [p for p in pivots if p["kind"] == "high" and p["index"] >= minimum_index][-3:]
    lows = [p for p in pivots if p["kind"] == "low" and p["index"] >= minimum_index][-3:]
    if len(highs) < minimum_touches or len(lows) < minimum_touches:
        return None
    high, low = _line(highs), _line(lows)
    if high is None or low is None:
        return None
    first = max(highs[0]["index"], lows[0]["index"])
    last = min(highs[-1]["index"], lows[-1]["index"])
    span = last - first
    if span <= 0:
        return None
    start_width, final_width = _value(high, first) - _value(low, first), _value(high, last) - _value(low, last)
    if start_width <= 0 or final_width <= 0:
        return None
    high_move, low_move = high["slope"] * span / atr, low["slope"] * span / atr
    high_flat = max(p["price"] for p in highs) - min(p["price"] for p in highs) <= config.equal_tolerance_atr * atr
    low_flat = max(p["price"] for p in lows) - min(p["price"] for p in lows) <= config.equal_tolerance_atr * atr
    if (not high_flat and high["r_squared"] < config.minimum_line_r_squared) or (not low_flat and low["r_squared"] < config.minimum_line_r_squared):
        return None
    return {"highs": highs, "lows": lows, "high_line": high, "low_line": low,
            "high_move_atr": high_move, "low_move_atr": low_move,
            "high_flat": high_flat, "low_flat": low_flat,
            "width_contraction": 1 - final_width / start_width,
            "start_width_atr": start_width / atr, "final_width_atr": final_width / atr}


def _boundary_patterns(pivots, bars, timeframe, as_of, levels, context, atr, config):
    geometry = _boundary_geometry(pivots, atr, config)
    found = []
    if geometry:
        high, low = geometry["high_move_atr"], geometry["low_move_atr"]
        minimum = config.minimum_boundary_move_atr
        converges = geometry["width_contraction"] >= config.minimum_width_contraction
        choices = []
        if geometry["high_flat"] and low >= minimum:
            choices.append(("ASCENDING_TRIANGLE", "BULLISH"))
        if geometry["low_flat"] and high <= -minimum:
            choices.append(("DESCENDING_TRIANGLE", "BEARISH"))
        if high <= -minimum and low >= minimum and converges:
            choices.append(("SYMMETRICAL_TRIANGLE", "NEUTRAL"))
        if high >= minimum and low >= minimum and converges and low > high:
            choices.append(("RISING_WEDGE", "BEARISH"))
        if high <= -minimum and low <= -minimum and converges and high < low:
            choices.append(("FALLING_WEDGE", "BULLISH"))
        if abs(high - low) <= config.parallel_tolerance_atr:
            if min(high, low) >= minimum:
                choices.append(("ASCENDING_CHANNEL", "BULLISH"))
            elif max(high, low) <= -minimum:
                choices.append(("DESCENDING_CHANNEL", "BEARISH"))
        for name, direction in choices:
            # Existing observed extreme bounds, never future extrapolation.
            upper, lower = max(p["price"] for p in geometry["highs"]), min(p["price"] for p in geometry["lows"])
            trigger, invalidation = (lower, upper) if direction == "BEARISH" else (upper, lower)
            evidence = {key: value for key, value in geometry.items() if key not in {"highs", "lows", "high_line", "low_line"}}
            evidence["upper_boundary"] = upper
            evidence["lower_boundary"] = lower
            found.append(_report(name, direction, sorted(geometry["highs"] + geometry["lows"], key=lambda p: p["index"]), bars, timeframe, as_of, levels,
                                 trigger=trigger, invalidation=invalidation, metrics=evidence,
                                 reason="At least three confirmed touches on each side, explicit ATR-normalized slopes and boundary fit/contraction. Direction is a hypothesis requiring a break.", context=context, config=config))
    # A flag/pennant additionally needs an independently observed preceding
    # impulse, and recent formation pivots. A triangle alone is insufficient.
    start = len(bars) - config.formation_window
    impulse_start = start - config.impulse_window
    if impulse_start < 0:
        return found
    recent = _boundary_geometry(pivots, atr, config, minimum_index=start, minimum_touches=2)
    if not recent:
        return found
    impulse = bars[start].close - bars[impulse_start].close
    if abs(impulse) < config.minimum_impulse_atr * atr:
        return found
    sign, direction = (1, "BULLISH") if impulse > 0 else (-1, "BEARISH")
    formation = bars[start:]
    adverse = max(0, bars[start].close - min(bar.low for bar in formation)) if sign > 0 else max(0, max(bar.high for bar in formation) - bars[start].close)
    retracement = adverse / abs(impulse)
    if retracement > config.maximum_flag_retracement:
        return found
    high, low = recent["high_move_atr"], recent["low_move_atr"]
    flag = abs(high - low) <= config.parallel_tolerance_atr and sign * high < 0 and sign * low < 0
    pennant = high < 0 < low and recent["width_contraction"] >= config.minimum_width_contraction
    if not flag and not pennant:
        return found
    upper, lower = max(p["price"] for p in recent["highs"]), min(p["price"] for p in recent["lows"])
    impulse_anchors = [{"kind": "impulse_origin", "index": impulse_start, "price": bars[impulse_start].close, "confirmed_at": _stamp(bars[impulse_start].available_at)},
                       {"kind": "impulse_end", "index": start, "price": bars[start].close, "confirmed_at": _stamp(bars[start].available_at)}]
    found.append(_report(f"{'BULL' if sign > 0 else 'BEAR'}_{'FLAG' if flag else 'PENNANT'}", direction,
                         sorted(impulse_anchors + recent["highs"] + recent["lows"], key=lambda p: p["index"]), bars, timeframe, as_of, levels,
                         trigger=upper if sign > 0 else lower, invalidation=lower if sign > 0 else upper,
                         metrics={"impulse_atr": abs(impulse) / atr, "retracement_fraction": retracement, "formation_bars": config.formation_window, "high_move_atr": high, "low_move_atr": low},
                         reason="Predeclared formation/impulse windows, confirmed countertrend or converging boundaries and controlled observed retracement.", context=context, config=config))
    return found


def _round_pattern(bars, timeframe, as_of, levels, context, atr, config):
    if len(bars) < config.rounded_window:
        return []
    selected = bars[-config.rounded_window:]
    n = len(selected)
    xs = [index - (n - 1) / 2 for index in range(n)]
    ys = [bar.close for bar in selected]
    average = mean(ys)
    s2, s4 = sum(x * x for x in xs), sum(x ** 4 for x in xs)
    a = (sum(x * x * y for x, y in zip(xs, ys, strict=True)) - s2 * average) / (s4 - s2 * s2 / n)
    b = sum(x * y for x, y in zip(xs, ys, strict=True)) / s2
    c = average - a * s2 / n
    if abs(a) < 1e-12:
        return []
    vertex = -b / (2 * a)
    if abs(vertex) > (n - 1) / 4:
        return []
    total = sum((y - average) ** 2 for y in ys)
    residual = sum((y - (a * x * x + b * x + c)) ** 2 for x, y in zip(xs, ys, strict=True))
    fit = 1 - residual / total if total else 0
    quarter = n // 4
    edge = mean(ys[:quarter] + ys[-quarter:])
    middle = mean(ys[n // 2 - quarter // 2:n // 2 + quarter // 2])
    depth = abs(edge - middle) / atr
    recovery = abs(ys[-1] - middle) / abs(edge - middle) if edge != middle else 0
    if fit < config.rounded_minimum_r_squared or depth < config.rounded_minimum_depth_atr or recovery < config.minimum_rounding_recovery:
        return []
    upward = a > 0
    first = len(bars) - n
    extreme = min(range(first, len(bars)), key=lambda i: bars[i].low) if upward else max(range(first, len(bars)), key=lambda i: bars[i].high)
    anchors = [{"kind": "rounded_origin", "index": first, "price": bars[first].close, "confirmed_at": _stamp(bars[first].available_at)},
               {"kind": "observed_low" if upward else "observed_high", "index": extreme, "price": bars[extreme].low if upward else bars[extreme].high, "confirmed_at": _stamp(bars[-1].available_at)},
               {"kind": "rounded_end", "index": len(bars) - 1, "price": bars[-1].close, "confirmed_at": _stamp(bars[-1].available_at)}]
    trigger = max(bar.high for bar in selected) if upward else min(bar.low for bar in selected)
    invalidation = min(bar.low for bar in selected) if upward else max(bar.high for bar in selected)
    return [_report("ROUNDED_BOTTOM" if upward else "ROUNDED_TOP", "BULLISH" if upward else "BEARISH", anchors, bars, timeframe, as_of, levels,
                    trigger=trigger, invalidation=invalidation,
                    metrics={"quadratic_r_squared": fit, "depth_atr": depth, "recovery_fraction": recovery, "window_bars": n},
                    reason="Fixed-window quadratic curvature with central turn, ATR-sized depth and observed recovery. Cup-like geometry is a research description, not proof of accumulation.", context=context, config=config,
                    discovered_at=bars[-1].available_at)]


def _event_patterns(bars, timeframe, as_of, levels, context, structure, config):
    state, events = structure.get("state"), structure.get("events", [])
    upper = structure.get("known_high_before_candle") or {}
    lower = structure.get("known_low_before_candle") or {}
    choices = []
    if "SELL_SIDE_SWEEP" in events:
        choices.append(("RANGE_DEVIATION", "BULLISH", lower.get("price"), bars[-1].low, "Observed sell-side sweep and close reclaim; inferred stop liquidity is not actual orders."))
    if "BUY_SIDE_SWEEP" in events:
        choices.append(("RANGE_DEVIATION", "BEARISH", upper.get("price"), bars[-1].high, "Observed buy-side sweep and close rejection; wick is not a confirmed breakout."))
    active = structure.get("active_break") or {}
    if state == "RETEST" and active.get("price"):
        direction = active.get("direction", "NEUTRAL")
        choices.append(("BREAKOUT_RETEST", direction, active["price"], lower.get("price") if direction == "BULLISH" else upper.get("price"), "Previously known break followed by observed retest/flip; continuation remains conditional."))
    if state == "FAILED_BREAKOUT":
        choices.append(("FAILED_BREAKOUT_REVERSAL", "BEARISH", lower.get("price"), upper.get("price"), "Former break lost acceptance; bearish reversal requires a separate known low to break."))
    if state == "FAILED_BREAKDOWN":
        choices.append(("FAILED_BREAKDOWN_REVERSAL", "BULLISH", upper.get("price"), lower.get("price"), "Former breakdown reclaimed; bullish continuation requires a separate known high to break."))
    result = []
    for name, direction, trigger, invalidation, reason in choices:
        if trigger is None or invalidation is None:
            continue
        synthetic_anchor = [{"kind": "observed_structure_event", "index": len(bars) - 1,
                             "price": bars[-1].close, "confirmed_at": _stamp(bars[-1].available_at)}]
        report = _report(name, direction, synthetic_anchor, bars, timeframe, as_of, levels,
                         trigger=trigger, invalidation=invalidation, metrics={"structure_state": state, "events": events}, reason=reason, context=context, config=config)
        report["status"] = "OBSERVED_STRUCTURE_EVENT_CONDITIONAL_CONTINUATION"
        result.append(report)
    return result


def structure_sequences(structure: dict, as_of: datetime) -> list[dict]:
    """Recognize ordered observed events; no retrospectively invented states."""
    history = [entry for entry in structure.get("history", []) if (_time(entry.get("discovered_at")) or datetime.max.replace(tzinfo=as_of.tzinfo)) <= as_of]
    if any(_time(first["discovered_at"]) > _time(second["discovered_at"]) for first, second in zip(history, history[1:], strict=False)):
        return []
    ordered = []
    for entry in history:
        labels = list(entry.get("events") or [entry.get("state")])
        if entry.get("momentum_state") == "DECELERATING_UP":
            labels.append("MOMENTUM_LOSS")
        for pivot in entry.get("newly_confirmed_pivots", []):
            pivot_time = _time(pivot.get("discovered_at"))
            if pivot_time is None or pivot_time > as_of:
                continue
            classification = pivot.get("classification")
            if classification == "LH" and entry.get("prior_trend") == "BULLISH_TREND":
                labels.append("FAILED_HH")
            labels.append(classification)
        for event in labels:
            if event and (not ordered or event != ordered[-1]):
                ordered.append(event)
    definitions = (
        ("SWEEP_RECLAIM_COMPRESSION", ("SELL_SIDE_SWEEP", "RECLAIM", "COMPRESSION"), "BULLISH", ["BREAKOUT", "RETEST", "EXPANSION"]),
        ("SWEEP_RECLAIM_HIGHER_LOW_COMPRESSION", ("SELL_SIDE_SWEEP", "RECLAIM", "HL", "COMPRESSION"), "BULLISH", ["BREAKOUT", "RETEST", "EXPANSION"]),
        ("SWEEP_RECLAIM_BOS", ("SELL_SIDE_SWEEP", "RECLAIM", "BOS"), "BULLISH", ["RETEST", "ACCEPTANCE", "EXPANSION"]),
        ("BREAKOUT_RETEST_CONTINUATION", ("BREAKOUT", "RETEST"), "BULLISH", ["ACCEPTANCE", "EXPANSION", "TARGET_REVIEW"]),
        ("FAILED_BREAK_REVERSAL", ("BREAKOUT", "FAILED_BREAKOUT"), "BEARISH", ["LOWER_HIGH_REVIEW", "BREAKDOWN", "CONTINUATION_REVIEW"]),
        ("FAILED_BREAKDOWN_RECLAIM", ("BREAKDOWN", "FAILED_BREAKDOWN", "RECLAIM"), "BULLISH", ["HIGHER_LOW_REVIEW", "BREAKOUT", "ACCEPTANCE"]),
        ("TREND_STRUCTURE_SHIFT", ("BULLISH_TREND", "CHoCH", "BREAKDOWN"), "BEARISH", ["LOWER_HIGH_REVIEW", "BEARISH_ACCEPTANCE", "CONTINUATION_REVIEW"]),
        ("MOMENTUM_LOSS_FAILED_HIGH_SHIFT", ("BULLISH_TREND", "MOMENTUM_LOSS", "FAILED_HH", "CHoCH", "LH", "BREAKDOWN"), "BEARISH", ["LOWER_HIGH_RETEST", "BEARISH_ACCEPTANCE", "CONTINUATION_REVIEW"]),
    )
    found = []
    for name, required, direction, future in definitions:
        pointer = 0
        for event in ordered[-20:]:
            if pointer < len(required) and event == required[pointer]:
                pointer += 1
        if pointer != len(required):
            continue
        invalidated = (direction == "BULLISH" and structure.get("state") == "FAILED_BREAKOUT") or (direction == "BEARISH" and structure.get("state") == "FAILED_BREAKDOWN")
        found.append({"name": name, "observed_sequence": list(required), "direction_hypothesis": direction,
                      "discovered_at": history[-1]["discovered_at"], "expected_next_states": future,
                      "condition": "ONLY IF the current reclaim/break level and structure invalidation remain intact; subsequent states are not guaranteed.",
                      "status": "INVALIDATED_SEQUENCE" if invalidated else "RESEARCH_HYPOTHESIS", "research_weight": 0, "probability": None})
    # This is explicitly a proxy, not evidence of institutional accumulation.
    metrics = structure.get("metrics", {})
    compression_count = sum(entry.get("state") in {"RANGE", "COMPRESSION"} for entry in history[-8:])
    pivot_labels = [pivot.get("classification") for pivot in structure.get("pivots", [])[-8:]]
    absorption = metrics.get("absorption_hypothesis")
    if history and compression_count >= 4:
        label = "ACCUMULATION_PROXY" if "HL" in pivot_labels and absorption == "POSSIBLE_BUYER_ABSORPTION" else "DISTRIBUTION_PROXY" if "LH" in pivot_labels and absorption == "POSSIBLE_SELLER_ABSORPTION" else None
        if label:
            found.append({"name": label, "observed_sequence": ordered[-8:],
                          "direction_hypothesis": "BULLISH" if label == "ACCUMULATION_PROXY" else "BEARISH",
                          "discovered_at": history[-1]["discovered_at"], "expected_next_states": ["BOUNDARY_TEST", "DIRECTIONAL_BREAK_REVIEW", "ACCEPTANCE_REVIEW"],
                          "condition": "Compression + confirmed pivot direction + native taker-flow non-displacement; actual accumulation/distribution remains unverified.",
                          "status": "NATIVE_FLOW_STRUCTURE_PROXY", "research_weight": 0, "probability": None})
    return found


def swing_phase(structure: dict, as_of: datetime) -> dict:
    """What the known swing is doing now, followed by three conditional steps."""
    pivots = [pivot for pivot in structure.get("pivots", []) if _time(pivot.get("discovered_at")) and _time(pivot["discovered_at"]) <= as_of]
    if len(pivots) < 2 or structure.get("quality") != "DERIVED":
        return {"status": "INSUFFICIENT_DATA", "current_phase": "UNAVAILABLE", "latest_swing": None, "conditional_next_steps": []}
    newest = pivots[-1]
    metrics = structure.get("metrics", {})
    phase = "AFTER_CONFIRMED_LOW" if newest["kind"] == "low" else "AFTER_CONFIRMED_HIGH"
    if metrics.get("volatility_state") in {"COMPRESSION", "SQUEEZE"}:
        phase = "COMPRESSING_AFTER_CONFIRMED_LOW" if newest["kind"] == "low" else "COMPRESSING_AFTER_CONFIRMED_HIGH"
    known_high, known_low = structure.get("known_high_before_candle") or {}, structure.get("known_low_before_candle") or {}
    direction = structure.get("bias", "NEUTRAL")
    bullish = direction == "BULLISH"
    boundary = known_high.get("price") if bullish else known_low.get("price") if direction == "BEARISH" else None
    invalidation = known_low.get("price") if bullish else known_high.get("price") if direction == "BEARISH" else None
    states = ["BOUNDARY_TEST", "CLOSED_BREAK_REVIEW", "ACCEPTANCE_OR_FAILURE_REVIEW"]
    if structure.get("state") in {"BREAKOUT", "BREAKDOWN", "CHoCH", "MSS"}:
        states = ["RETEST_REVIEW", "ACCEPTANCE_REVIEW", "CONTINUATION_OR_FAILURE_REVIEW"]
    steps = [{"state": state, "level": boundary, "condition": f"IF the known {direction.lower()} structure remains valid and invalidation is not lost"} for state in states]
    return {"status": "CONDITIONAL_RESEARCH", "current_phase": phase, "latest_swing": newest,
            "trigger": boundary, "invalidation": invalidation, "conditional_next_steps": steps,
            "warning": "These are possible structure transitions, not predicted future candles or OHLC values.", "probability": None}


def _pattern_availability(name: str, bars: list[Candle], pivots: list[dict], structure: dict, config: PatternSettings) -> dict:
    counts = {kind: sum(pivot["kind"] == kind for pivot in pivots) for kind in ("high", "low")}
    if name in {"RANGE_DEVIATION", "BREAKOUT_RETEST", "FAILED_BREAKOUT_REVERSAL", "FAILED_BREAKDOWN_REVERSAL"}:
        sufficient = bool(structure)
    elif name.startswith("ROUNDED_"):
        sufficient = len(bars) >= config.rounded_window
    elif "FLAG" in name or "PENNANT" in name:
        start = len(bars) - config.formation_window
        recent_counts = {kind: sum(pivot["kind"] == kind and pivot["index"] >= start for pivot in pivots) for kind in ("high", "low")}
        sufficient = len(bars) >= config.formation_window + config.impulse_window and min(recent_counts.values()) >= 2
    elif "TRIANGLE" in name or "WEDGE" in name or "CHANNEL" in name:
        sufficient = min(counts.values()) >= 3
    elif name == "HEAD_AND_SHOULDERS":
        sufficient = counts["high"] >= 3 and counts["low"] >= 2
    elif name == "INVERSE_HEAD_AND_SHOULDERS":
        sufficient = counts["low"] >= 3 and counts["high"] >= 2
    else:
        kind = "high" if name.endswith("TOP") else "low"
        required = 3 if name.startswith("TRIPLE") else 2
        sufficient = counts[kind] >= required and counts["low" if kind == "high" else "high"] >= required - 1
    return {"status": "AVAILABLE" if sufficient else "INSUFFICIENT_DATA",
            "reason": "required closed-bar window and confirmed anchors exist; geometry may be absent" if sufficient else "required pattern window or confirmed pivot/context count is missing"}


def detect_patterns(
    candles: list[Candle], timeframe: str, as_of: datetime, *, context: str | None = None,
    liquidity_levels: list[dict] | None = None, structure: dict | None = None,
    settings: PatternSettings | None = None,
) -> dict:
    """Only closed available bars can generate a currently usable geometry."""
    config = settings or PatternSettings()
    as_of = aware(as_of)
    if timeframe not in TIMEFRAMES:
        raise ValueError("unsupported pattern timeframe")
    snapshot = AssetSnapshot(asset=Asset(asset_id="pattern-context", symbol="PATTERN", name="pattern-context"), as_of=as_of, candles={timeframe: candles})
    bars, issues = _known_bars(snapshot, timeframe)
    bars = bars[-config.maximum_bars:]
    result = {"timeframe": timeframe, "as_of": _stamp(as_of), "status": "INSUFFICIENT_DATA",
              "detections": [], "sequences": [], "swing_phase": {"status": "INSUFFICIENT_DATA", "current_phase": "UNAVAILABLE", "latest_swing": None, "conditional_next_steps": []},
              "last_completed_candle": _stamp(bars[-1].close_time) if bars else None,
              "source": bars[-1].source if bars else "UNAVAILABLE", "revision": candle_revision(bars),
              "config": config.model_dump(), "supported_patterns": list(PATTERN_NAMES),
              "pattern_availability": {name: {"status": "INSUFFICIENT_DATA", "reason": "closed available bars and confirmed geometry anchors are required"} for name in PATTERN_NAMES},
              "research_weight": 0, "issues": issues}
    if issues or len(bars) < config.minimum_bars:
        if not issues:
            result["issues"] = [f"minimum_{config.minimum_bars}_closed_available_bars_required"]
        return result
    if (as_of - bars[-1].close_time).total_seconds() > TIMEFRAMES[timeframe] * 1.5:
        result["status"] = "STALE"
        result["pattern_availability"] = {name: {"status": "STALE", "reason": "completed candle context is stale"} for name in PATTERN_NAMES}
        result["issues"] = ["completed_candle_context_is_stale"]
        return result
    atr = _atr(bars, config.atr_period)
    if atr is None or atr <= 0:
        result["issues"] = ["positive_ATR_required_for_geometric_tolerance"]
        return result
    pivots = [pivot for pivot in confirmed_swings(bars, left=config.swing_left, right=config.swing_right)
              if pivot["index"] >= len(bars) - config.maximum_pivot_age_bars]
    levels = known_liquidity_levels(liquidity_levels or [], as_of)
    usable_structure = structure or {}
    structure_at = _time(usable_structure.get("last_completed_candle"))
    if usable_structure.get("quality") != "DERIVED" or structure_at is None or structure_at > as_of:
        usable_structure = {}
    detections = _turn_patterns(pivots, bars, timeframe, as_of, levels, context, atr, config)
    detections += _boundary_patterns(pivots, bars, timeframe, as_of, levels, context, atr, config)
    detections += _round_pattern(bars, timeframe, as_of, levels, context, atr, config)
    detections += _event_patterns(bars, timeframe, as_of, levels, context, usable_structure, config)
    availability = {name: _pattern_availability(name, bars, pivots, usable_structure, config) for name in PATTERN_NAMES}
    result.update(status="DERIVED", atr=atr,
                  detections=detections[-config.maximum_detections:],
                  pattern_availability=availability,
                  sequences=structure_sequences(usable_structure, as_of),
                  swing_phase=swing_phase(usable_structure, as_of))
    return result
