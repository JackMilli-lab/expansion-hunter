"""Measurable setup contracts joined to current War Room evidence.

Contracts are research hypotheses and do not add hidden production score
weights. Missing evidence stays unknown, particularly native order flow.
Personal watchlist membership is deliberately absent from these definitions.
"""

from __future__ import annotations

import math
from datetime import datetime

from .models import aware
from .pattern_engine import known_liquidity_levels

SETUP_DEFINITIONS = {
    "LIQUIDITY_SWEEP_RECLAIM": {
        "requirements": ["known_level_swept", "closed_reclaim"],
        "trigger": "Closed move through the opposite known boundary after reclaim",
        "confirmation": "Retest and acceptance while the sweep extreme remains intact",
        "invalidation": "Loss of the observed sweep extreme",
        "ideal_regime_hypothesis": ["RANGE", "RECOVERY", "RISK_ON"],
    },
    "GOLDEN_POCKET_CONTINUATION": {
        "requirements": ["confirmed_directional_swing", "golden_pocket_reclaimed", "structure_aligned"],
        "trigger": "Closed move through the known continuation boundary after pocket reclaim",
        "confirmation": "Acceptance and actual directional continuation; Fibonacci touch alone is insufficient",
        "invalidation": "Loss of the known controlling swing, not an arbitrary percentage stop",
        "ideal_regime_hypothesis": ["BULLISH_TREND", "BEARISH_TREND"],
    },
    "COMPRESSION_BREAKOUT": {
        "requirements": ["volatility_compressed", "known_directional_boundary"],
        "trigger": "Closed 1m crossing of the predefined 5m boundary plus existing ATR/tick buffer",
        "confirmation": "Independent later 5m acceptance and 15m structural review",
        "invalidation": "Loss of opposing known structure boundary or failed acceptance",
        "ideal_regime_hypothesis": ["LOW_VOLATILITY", "RANGE", "RISK_ON"],
    },
    "FAILED_BREAKOUT_REVERSAL": {
        "requirements": ["previous_breakout_observed", "breakout_lost_acceptance"],
        "trigger": "Separate closed breakdown through a previously known low",
        "confirmation": "Acceptance below that low; losing an upside break alone does not confirm a short",
        "invalidation": "Reclaim of the known failed-break boundary",
        "ideal_regime_hypothesis": ["RANGE", "RISK_OFF", "BEARISH_TREND"],
    },
    "FAILED_BREAKDOWN_REVERSAL": {
        "requirements": ["previous_breakdown_observed", "breakdown_reclaimed"],
        "trigger": "Separate closed breakout through a previously known high",
        "confirmation": "Acceptance above that high",
        "invalidation": "Loss of the known recovered-breakdown boundary",
        "ideal_regime_hypothesis": ["RANGE", "RECOVERY", "RISK_ON"],
    },
    "BREAKOUT_RETEST": {
        "requirements": ["previous_break_observed", "retest_flip_observed"],
        "trigger": "Closed continuation from a defended former boundary",
        "confirmation": "Level holds as support/resistance; volume and order flow affect quality",
        "invalidation": "Loss of accepted former boundary and controlling structure",
        "ideal_regime_hypothesis": ["BULLISH_TREND", "BEARISH_TREND"],
    },
    "HTF_CONTINUATION": {
        "requirements": ["fresh_htf_direction", "ltf_direction_aligned"],
        "trigger": "Known lower-timeframe boundary in higher-timeframe direction",
        "confirmation": "Closed break and acceptance; HTF trend alone is insufficient",
        "invalidation": "Loss of the controlling LTF swing; separately monitor HTF trend conflict",
        "ideal_regime_hypothesis": ["BULLISH_TREND", "BEARISH_TREND"],
    },
    "RANGE_DEVIATION": {
        "requirements": ["range_context", "boundary_sweep_and_return"],
        "trigger": "Reclaim/rejection of the range boundary after a wick deviation",
        "confirmation": "Closed return inside the prior range; continuation needs subsequent acceptance",
        "invalidation": "Acceptance outside the swept range extreme",
        "ideal_regime_hypothesis": ["RANGE", "CHOP"],
    },
    "ABSORPTION_BREAKOUT": {
        "requirements": ["native_flow_absorption_proxy", "known_directional_boundary", "boundary_pressure"],
        "trigger": "Closed crossing of defended structure in the absorption-hypothesis direction",
        "confirmation": "Independent acceptance; native flow/non-displacement is not proof of participant intent",
        "invalidation": "Defended level lost or failure to accept the break",
        "ideal_regime_hypothesis": ["RANGE", "RISK_ON", "RISK_OFF"],
    },
    "MOMENTUM_IGNITION": {
        "requirements": ["price_accelerating", "volume_awakening", "known_directional_boundary"],
        "trigger": "Known structural boundary approached/crossed while velocity and activity increase",
        "confirmation": "Existing objective close rule and later acceptance; momentum is supporting evidence",
        "invalidation": "Structure lost or immediate failed breakout/breakdown",
        "ideal_regime_hypothesis": ["EXPANSION", "RISK_ON", "RISK_OFF"],
    },
}


def _time(value) -> datetime | None:
    try:
        return aware(value if isinstance(value, datetime) else datetime.fromisoformat(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _finite(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _contains(sequence: list[str], value: str) -> bool:
    return value in sequence


def _fresh(frame: dict, as_of: datetime) -> bool:
    timestamp = _time(frame.get("last_completed_candle"))
    history = frame.get("history", [])
    discovered = _time(history[-1].get("discovered_at")) if history else timestamp
    return frame.get("quality") == "DERIVED" and timestamp is not None and timestamp <= as_of and discovered is not None and discovered <= as_of


def _pocket_context(analysis: dict, frame: str, direction: str, as_of: datetime) -> dict | None:
    fibonacci = analysis.get("fibonacci", {})
    pairs = {pair["id"]: pair for pair in fibonacci.get("anchors", []) if pair.get("id")}
    for pocket in fibonacci.get("golden_pockets", []):
        discovered = _time(pocket.get("discovered_at"))
        if pocket.get("timeframe") != frame or pocket.get("quality") != "DERIVED" or discovered is None or discovered > as_of:
            continue
        pair = pairs.get(pocket.get("pair_id"))
        pair_time = _time(pair.get("discovered_at")) if pair else None
        if not pair or pair.get("quality") != "DERIVED" or pair_time is None or pair_time > as_of:
            continue
        expected = "UP" if direction == "BULLISH" else "DOWN" if direction == "BEARISH" else None
        if expected is None or pair.get("direction") != expected:
            continue
        return {"pocket": pocket, "pair": pair}
    return None


def _condition(value: bool | None, reason: str) -> dict:
    return {"value": value, "status": "UNKNOWN" if value is None else "MET" if value else "NOT_MET", "reason": reason}


def _truth(condition) -> bool | None:
    return condition["value"]


def evaluate_setups(report: dict, analysis: dict, as_of: datetime) -> dict:
    """Evaluate all ten setup contracts against timestamped current evidence."""
    as_of = aware(as_of)
    report_time = _time(report.get("as_of"))
    if report_time is None or report_time > as_of:
        report = {}
    frames = report.get("timeframes", {})
    frame = next((key for key in ("5m", "15m", "1m", "1h") if _fresh(frames.get(key, {}), as_of)), None)
    state = frames.get(frame, {}) if frame else {}
    metrics = state.get("metrics", {})
    events, sequence = state.get("events", []), state.get("sequence", [])
    direction = report.get("bias", "NEUTRAL")
    primary = "bullish" if direction == "BULLISH" else "bearish" if direction == "BEARISH" else "neutral"
    scenarios = report.get("scenarios", {})
    path = scenarios.get(primary, {})
    upper, lower = scenarios.get("bullish", {}).get("trigger"), scenarios.get("bearish", {}).get("trigger")
    levels_known = _finite(upper) is not None and _finite(lower) is not None and upper > lower
    available = frame is not None
    analysis_at = _time(analysis.get("as_of"))
    if analysis_at is None or analysis_at > as_of:
        analysis = {}
    absorption = metrics.get("absorption_hypothesis", "UNAVAILABLE")
    flow_known = metrics.get("absorption_quality") == "DERIVED_NATIVE_CANDLE_DELTA"
    velocity = metrics.get("momentum_state")
    acceleration = metrics.get("volume_acceleration_percent")
    rvol = metrics.get("rvol")
    high_tf = next((frames[key] for key in ("1D", "4h", "1h") if _fresh(frames.get(key, {}), as_of) and frames[key].get("bias") in {"BULLISH", "BEARISH"}), None)
    pocket = _pocket_context(analysis, frame, direction, as_of) if frame else None
    fib_data_known = bool(analysis.get("fibonacci", {}).get("anchors")) and available
    behaviour = pocket["pocket"].get("behaviour", {}) if pocket else {}
    pocket_status = behaviour.get("state", behaviour.get("classification"))
    # Price/zone interaction must actually have occurred; mere ratio presence
    # does not earn a continuation setup.
    pocket_reclaimed = pocket_status in {"ACCEPTED", "SWEPT_AND_RECLAIMED"} if pocket else None
    range_context = state.get("trend") == "RANGE" if available else None
    compress = metrics.get("volatility_state") in {"COMPRESSION", "SQUEEZE"} if available else None
    swept = any(event in events for event in ("SELL_SIDE_SWEEP", "BUY_SIDE_SWEEP")) if available else None
    reclaimed = "RECLAIM" in events or "REJECTION" in events if available else None
    prior_break = _contains(sequence, "BREAKOUT") or _contains(sequence, "BREAKDOWN") if available else None
    boundary_distance = min(abs(metrics["close"] - upper), abs(metrics["close"] - lower)) / metrics["atr"] if levels_known and metrics.get("close") and metrics.get("atr") else None
    checks = {
        "LIQUIDITY_SWEEP_RECLAIM": {
            "known_level_swept": _condition(swept, "Current state contains an observed known-level sweep"),
            "closed_reclaim": _condition(reclaimed, "Closed reclaim or rejection of that known level"),
        },
        "GOLDEN_POCKET_CONTINUATION": {
            "confirmed_directional_swing": _condition(bool(pocket) if fib_data_known else None, "Current confirmed Fib pair has matching direction and availability"),
            "golden_pocket_reclaimed": _condition(pocket_reclaimed, "Observed accepted/reclaimed zone; touch alone is insufficient"),
            "structure_aligned": _condition(state.get("bias") == direction and direction != "NEUTRAL" if available else None, "Current structural direction agrees with controlling swing"),
        },
        "COMPRESSION_BREAKOUT": {
            "volatility_compressed": _condition(compress, "Current bounded ATR/range compression definition"),
            "known_directional_boundary": _condition(levels_known if available else None, "Both controlling known boundaries exist in correct order"),
        },
        "FAILED_BREAKOUT_REVERSAL": {
            "previous_breakout_observed": _condition(_contains(sequence, "BREAKOUT") if available else None, "Persisted observed sequence includes the earlier breakout"),
            "breakout_lost_acceptance": _condition(state.get("state") == "FAILED_BREAKOUT" if available else None, "Current structure state explicitly lost the break level"),
        },
        "FAILED_BREAKDOWN_REVERSAL": {
            "previous_breakdown_observed": _condition(_contains(sequence, "BREAKDOWN") if available else None, "Persisted observed sequence includes the earlier breakdown"),
            "breakdown_reclaimed": _condition(state.get("state") == "FAILED_BREAKDOWN" if available else None, "Current structure state explicitly reclaimed the failed breakdown"),
        },
        "BREAKOUT_RETEST": {
            "previous_break_observed": _condition(prior_break, "Break must precede the current retest in observed sequence"),
            "retest_flip_observed": _condition(state.get("state") == "RETEST" and "SUPPORT_RESISTANCE_FLIP" in events if available else None, "Current closed-bar retest/flip event exists"),
        },
        "HTF_CONTINUATION": {
            "fresh_htf_direction": _condition(high_tf is not None if available else None, "Fresh 1D/4h/1h directional structure, not a stale label"),
            "ltf_direction_aligned": _condition(state.get("bias") == high_tf.get("bias") if high_tf and available else None, "Lower-timeframe direction matches known HTF direction"),
        },
        "RANGE_DEVIATION": {
            "range_context": _condition(range_context, "Confirmed pivot regime is range"),
            "boundary_sweep_and_return": _condition(bool(swept and reclaimed) if available else None, "Observed wick deviation followed by a closed return"),
        },
        "ABSORPTION_BREAKOUT": {
            "native_flow_absorption_proxy": _condition(absorption in {"POSSIBLE_BUYER_ABSORPTION", "POSSIBLE_SELLER_ABSORPTION"} if flow_known else None, "Native taker delta plus low price displacement; no simulated CVD"),
            "known_directional_boundary": _condition(levels_known if available else None, "Known upper/lower structure bounds exist"),
            "boundary_pressure": _condition(boundary_distance <= 0.75 if boundary_distance is not None else None, "Current price within explicit 0.75 ATR of a boundary"),
        },
        "MOMENTUM_IGNITION": {
            "price_accelerating": _condition(velocity in {"ACCELERATING_UP", "ACCELERATING_DOWN"} if available else None, "Current price velocity is accelerating directionally"),
            "volume_awakening": _condition(acceleration > 0 and rvol >= 1.25 if acceleration is not None and rvol is not None else None, "Positive short-window volume acceleration and RVOL >= explicit 1.25 hypothesis"),
            "known_directional_boundary": _condition(levels_known if available else None, "A real known trigger/invalidation pair is available"),
        },
    }
    setups = []
    for name, definition in SETUP_DEFINITIONS.items():
        evidence = checks[name]
        values = [_truth(value) for value in evidence.values()]
        status = "CANDIDATE" if values and all(value is True for value in values) else "NOT_PRESENT" if any(value is False for value in values) else "INSUFFICIENT_DATA"
        setup_direction = "BEARISH" if name == "FAILED_BREAKOUT_REVERSAL" else "BULLISH" if name == "FAILED_BREAKDOWN_REVERSAL" else "BULLISH" if "SELL_SIDE_SWEEP" in events else "BEARISH" if "BUY_SIDE_SWEEP" in events else direction
        if name == "ABSORPTION_BREAKOUT":
            setup_direction = "BULLISH" if absorption == "POSSIBLE_BUYER_ABSORPTION" else "BEARISH" if absorption == "POSSIBLE_SELLER_ABSORPTION" else "NEUTRAL"
        elif name == "MOMENTUM_IGNITION":
            setup_direction = "BULLISH" if velocity == "ACCELERATING_UP" else "BEARISH" if velocity == "ACCELERATING_DOWN" else "NEUTRAL"
        setup_path = scenarios.get("bullish" if setup_direction == "BULLISH" else "bearish" if setup_direction == "BEARISH" else "neutral", path)
        targets = known_liquidity_levels(setup_path.get("targets", []), as_of)
        invalidation = setup_path.get("invalidation")
        if name in {"LIQUIDITY_SWEEP_RECLAIM", "RANGE_DEVIATION"} and swept:
            invalidation = metrics.get("latest_low") if "SELL_SIDE_SWEEP" in events else metrics.get("latest_high")
        discovered = state.get("history", [{}])[-1].get("discovered_at") if state.get("history") else state.get("last_completed_candle")
        setups.append({"name": name, "status": status, "timeframe": frame, "direction_hypothesis": setup_direction,
                       "market_conflict": setup_direction in {"BULLISH", "BEARISH"} and direction in {"BULLISH", "BEARISH"} and setup_direction != direction,
                       "requirements": definition["requirements"], "evidence": evidence,
                       "trigger_condition": definition["trigger"], "confirmation": definition["confirmation"],
                       "trigger": setup_path.get("trigger"), "invalidation": invalidation,
                       "invalidation_condition": definition["invalidation"], "targets": targets,
                       "target_status": "OBSERVED_LEVELS" if targets else "UNAVAILABLE",
                       "ideal_regime_hypothesis": definition["ideal_regime_hypothesis"],
                       "actual_regime": report.get("market_regime", "UNAVAILABLE"),
                       "discovered_at": discovered if available else None,
                       "evaluated_at": as_of.isoformat(), "source": state.get("source", "UNAVAILABLE"),
                       "quality": "DERIVED" if available else "UNAVAILABLE",
                       "research_weight": 0, "historical_performance": "INSUFFICIENT_DATA", "probability": None})
    return {"status": "DERIVED" if available else "INSUFFICIENT_DATA", "setups": setups,
            "active_setups": [row for row in setups if row["status"] == "CANDIDATE"],
            "as_of": as_of.isoformat(), "research_weight": 0,
            "explicit_hypothesis_thresholds": {"absorption_boundary_distance_atr": 0.75, "ignition_minimum_rvol": 1.25},
            "limitations": ["Contracts are predeclared measurable hypotheses, not historically validated profitable strategies.",
                            "Missing native order flow remains UNKNOWN; watchlist membership does not affect evidence or scores.",
                            "A candidate does not replace the fast monitor's completed 1m close and acceptance rules."]}
