"""Causal swing memory and symmetric conditional decisions; fixtures are not live data."""

import copy
import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.models import TIMEFRAMES, Candle
from expansion_hunter.war_room import (
    FRAMES,
    SWING_PHASES,
    _active_fib_context,
    _swing_classification,
    _swing_decision,
    _swing_report,
    _swing_statistics,
    analyze_war_room,
)

WHEN = datetime(2025, 3, 1, 12, tzinfo=UTC)


def structure(side="LONG", *, trend=None, velocity=None, acceleration=0.1, kind=None):
    sign = 1 if side == "LONG" else -1
    return {
        "quality": "DERIVED", "state": "COMPRESSION", "source": "fixture_only",
        "trend": trend or ("BULLISH_TREND" if side == "LONG" else "BEARISH_TREND"),
        "bias": "BULLISH" if side == "LONG" else "BEARISH", "events": [],
        "last_completed_candle": WHEN.isoformat(),
        "known_high_before_candle": {"price": 100.2 if side == "LONG" else 101, "discovered_at": (WHEN - timedelta(hours=1)).isoformat()},
        "known_low_before_candle": {"price": 99 if side == "LONG" else 99.8, "discovered_at": (WHEN - timedelta(hours=1)).isoformat()},
        "pivots": [{"kind": kind or ("low" if side == "LONG" else "high"), "price": 99 if side == "LONG" else 101,
                    "event_timestamp": (WHEN - timedelta(minutes=15)).isoformat(), "discovered_at": (WHEN - timedelta(minutes=5)).isoformat()}],
        "metrics": {"close": 100, "atr": 1, "velocity_percent_per_bar": velocity if velocity is not None else sign * 0.2,
                    "acceleration_percent_per_bar": acceleration, "range_compression_ratio": 0.5, "rvol": 1.5,
                    "range_high": 101, "range_low": 99, "momentum_state": "ACCELERATION", "volatility_state": "COMPRESSION"},
    }


def analysis(side="LONG", *, fibonacci=False):
    prices = [105, 108] if side == "LONG" else [95, 92]
    data = {"as_of": WHEN.isoformat(), "liquidity_map": [
        {"price": price, "discovered_at": (WHEN - timedelta(hours=1)).isoformat(), "quality": "NATIVE",
         "category": "OBSERVED_ORDER_BOOK", "source": "fixture_depth_only", "type": "ASK_WALL" if side == "LONG" else "BID_WALL"}
        for price in prices
    ]}
    if fibonacci:
        anchor_times = [WHEN - timedelta(hours=4), WHEN - timedelta(hours=3)]
        active = {
            "enabled": True, "status": "AVAILABLE", "quality": "DERIVED", "bias": side, "pair_id": "fixture_pair",
            "source": "fixture_only", "discovered_at": (WHEN - timedelta(hours=1)).isoformat(),
            "source_timestamp": WHEN.isoformat(), "received_timestamp": WHEN.isoformat(),
            "anchors": [{"price": price, "anchor_timestamp": point.isoformat(), "discovery_timestamp": (point + timedelta(minutes=10)).isoformat()}
                        for point, price in zip(anchor_times, [98, 102] if side == "LONG" else [102, 98], strict=True)],
            "levels": [{"price": 100, "ratio": 0.618, "kind": "retracement", "confluence_score": 80,
                        "confluence": [{"type": "AVWAP", "price": 100}], "quality": "DERIVED",
                        "discovered_at": (WHEN - timedelta(hours=1)).isoformat(), "behaviour": {"state": "ACCEPTED"}},
                       {"price": prices[0], "ratio": 1.618, "kind": "extension", "confluence_score": 50,
                        "confluence": [], "quality": "DERIVED", "discovered_at": (WHEN - timedelta(hours=1)).isoformat()}],
            "golden_pocket": {"lower": 99.9, "upper": 100.1},
        }
        data["active_fibonacci"] = {frame: copy.deepcopy(active) for frame in FRAMES}
    return data


def candles(frame="5m", count=40, slope=0.4):
    step = TIMEFRAMES[frame]
    result = []
    for index in range(count):
        price = 100 + index * slope + 3 * math.sin(index * 0.9)
        opened = WHEN - timedelta(seconds=step * (count - index))
        close_at = opened + timedelta(seconds=step)
        result.append(Candle(open_time=opened, close_time=close_at, available_at=close_at,
                             open=price, high=price + 0.4, low=price - 0.4, close=price,
                             volume=100, source="fixture_only"))
    return result


def next_candle(previous, price, *, high=None, low=None, gap=0):
    duration = previous.close_time - previous.open_time
    opened = previous.close_time + duration * gap
    closed = opened + duration
    return Candle(open_time=opened, close_time=closed, available_at=closed, open=previous.close,
                  close=price, high=high if high is not None else max(price, previous.close) + 0.2,
                  low=low if low is not None else min(price, previous.close) - 0.2, volume=100, source=previous.source)


@pytest.mark.parametrize(("phase", "frame", "previous"), [
    ("FALLING", structure("SHORT", trend="RANGE", acceleration=-0.1, kind="low"), {}),
    ("BOTTOMING", structure("SHORT", velocity=0, kind="low"), {}),
    ("TURNING_UP", structure("SHORT", velocity=0.2), {}),
    ("EARLY_UPSWING", structure(), {}),
    ("UPSWING", structure(kind="high"), {}),
    ("LOSING_MOMENTUM", structure(acceleration=-0.1), {}),
    ("TOPPING", structure(velocity=-0.2, kind="high"), {}),
    ("TURNING_DOWN", structure(velocity=-0.2, kind="low"), {}),
    ("EARLY_DOWNSWING", structure("SHORT", acceleration=-0.1), {}),
    ("DOWNSWING", structure("SHORT", acceleration=-0.1, kind="low"), {}),
])
def test_all_declared_phases_have_measurable_rules(phase, frame, previous):
    assert _swing_classification(frame, "5m", previous)[0] == phase
    assert phase in SWING_PHASES


@pytest.mark.parametrize(("event", "side", "phase"), [("SELL_SIDE_SWEEP", "LONG", "TURNING_UP"), ("BUY_SIDE_SWEEP", "SHORT", "TURNING_DOWN")])
def test_native_sweep_changes_phase_symmetrically(event, side, phase):
    frame = structure()
    frame["events"] = [event]
    assert _swing_classification(frame, "5m", {}) == (phase, side)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_conditional_decisions_are_symmetric_and_use_real_known_target_levels(side):
    decision = _swing_decision(structure(side), "5m", side, "UPSWING" if side == "LONG" else "DOWNSWING", analysis(side), {}, WHEN)
    assert decision["entry_status"] == side + "_SETUP"
    assert decision["gross_reward_risk"] == pytest.approx(5)
    assert decision["target_1"] == (105 if side == "LONG" else 95)
    assert decision["target_2"] == (108 if side == "LONG" else 92)
    assert decision["entry_quality"]["loss_at_stop_percent"] == pytest.approx(1)
    assert decision["entry_quality"]["net_reward_risk"] is None
    assert decision["probability"] is None
    for path in ("primary_path", "alternative_path", "failure_path"):
        assert len(decision[path]) == 3
        assert all("IF" in step["condition"] for step in decision[path])
        assert not any(key in step for step in decision[path] for key in ("open", "high", "low", "close", "probability"))


def test_setup_and_entry_quality_have_no_long_favoring_default_weights():
    long = _swing_decision(structure(), "5m", "LONG", "UPSWING", analysis(), {}, WHEN)
    short = _swing_decision(structure("SHORT"), "5m", "SHORT", "DOWNSWING", analysis("SHORT"), {}, WHEN)
    assert long["setup_quality"]["value"] == short["setup_quality"]["value"]
    assert long["entry_quality"]["value"] == pytest.approx(short["entry_quality"]["value"])
    assert long["setup_quality"]["coverage"] == short["setup_quality"]["coverage"]


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize(("mutation", "status"), [("poor_rr", "NO_TRADE"), ("wrong_stop", "NO_TRADE"), ("extended", "TOO_LATE"), ("target_passed", "TOO_LATE"), ("far_trigger", "WAIT"), ("no_targets", "INSUFFICIENT_DATA"), ("stale", "INSUFFICIENT_DATA")])
def test_entry_rejection_applies_both_directions(side, mutation, status):
    frame, data = structure(side), analysis(side)
    sign = 1 if side == "LONG" else -1
    if mutation == "poor_rr":
        data["liquidity_map"][0]["price"] = 100 + sign * 1.2
    elif mutation == "wrong_stop":
        frame["known_low_before_candle" if side == "LONG" else "known_high_before_candle"]["price"] = 100 + sign
    elif mutation == "extended":
        frame["metrics"]["close"] = 100 + sign * 2.3
    elif mutation == "target_passed":
        frame["metrics"]["close"] = 100 + sign * 5.1
    elif mutation == "far_trigger":
        frame["known_high_before_candle" if side == "LONG" else "known_low_before_candle"]["price"] = 100 + sign * 2.2
    elif mutation == "no_targets":
        data["liquidity_map"] = []
    else:
        frame["quality"] = "STALE"
    decision = _swing_decision(frame, "5m", side, "UPSWING" if side == "LONG" else "DOWNSWING", data, {}, WHEN)
    assert decision["entry_status"] == status


def test_neutral_is_wait_without_invented_long_bias():
    decision = _swing_decision(structure(), "5m", "NEUTRAL", "INSUFFICIENT_DATA", analysis(), {}, WHEN)
    assert decision["entry_status"] == "WAIT"
    assert decision["trigger"] is decision["target_1"] is decision["setup_quality"]["value"] is None


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_fib_confluence_has_bounded_visible_uncalibrated_actual_contributions(side):
    decision = _swing_decision(structure(side), "5m", side, "UPSWING", analysis(side, fibonacci=True), {}, WHEN)
    for score in ("setup_quality", "entry_quality"):
        component = next(row for row in decision[score]["components"] if row["name"] == "active_fibonacci_confluence")
        assert component["contribution"] == 8
        assert component["weight"] == 0.1
        assert decision[score]["status"] == "UNCALIBRATED"
        assert decision[score]["probability"] is None
    assert decision["fib_context"]["levels"][0]["directional_role"] == ("SUPPORT_CONTEXT" if side == "LONG" else "RESISTANCE_CONTEXT")
    assert decision["fib_context"]["target_confluence"][0]["observed_target"] == decision["target_1"]
    assert decision["fib_context"]["target_confluence"][0]["role"] == "DERIVED_CONTEXT_NOT_OBSERVED_LIQUIDITY"


@pytest.mark.parametrize("mutation", ["stale", "invalidated", "opposite", "future_anchor", "future_received", "future_level", "anchor_discovery_before_event"])
def test_stale_invalid_or_future_fib_never_becomes_evidence(mutation):
    data = analysis(fibonacci=True)
    active = data["active_fibonacci"]["5m"]
    if mutation in {"stale", "invalidated"}:
        active["status"] = mutation.upper()
    elif mutation == "opposite":
        active["bias"] = "SHORT"
    elif mutation == "future_anchor":
        active["anchors"][0]["discovery_timestamp"] = (WHEN + timedelta(seconds=1)).isoformat()
    elif mutation == "future_received":
        active["received_timestamp"] = (WHEN + timedelta(seconds=1)).isoformat()
    elif mutation == "future_level":
        active["levels"][0]["discovered_at"] = (WHEN + timedelta(seconds=1)).isoformat()
    else:
        active["anchors"][0]["discovery_timestamp"] = (WHEN - timedelta(days=1)).isoformat()
    context = _active_fib_context(data, "5m", "LONG", 100, 1, WHEN)
    assert context["value"] in {None, 0}
    assert not context["levels"]


@pytest.mark.parametrize(("state", "expected"), [("ACCEPTED", 80), ("REJECTED", 40), ("SWEPT_AND_RECLAIMED", 80), ("UNTESTED", 40), ("BROKEN", 0)])
def test_fib_response_observed_state_changes_bounded_context(state, expected):
    data = analysis(fibonacci=True)
    data["active_fibonacci"]["5m"]["levels"][0]["behaviour"]["state"] = state
    assert _active_fib_context(data, "5m", "LONG", 100, 1, WHEN)["value"] == expected


def test_isolated_fib_touch_has_no_confluence_bonus_and_cannot_create_targets():
    data = analysis(fibonacci=True)
    data["active_fibonacci"]["5m"]["levels"][0]["confluence"] = []
    data["liquidity_map"] = []
    result = _swing_decision(structure(), "5m", "LONG", "UPSWING", data, {}, WHEN)
    assert result["fib_context"]["value"] == 0
    assert result["target_1"] is result["target_2"] is None
    assert result["entry_status"] == "INSUFFICIENT_DATA"


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_swing_stats_start_when_known_not_at_retrospective_pivot(side):
    source = candles()
    stats = _swing_statistics(source, "5m", side, {}, WHEN, {"event_timestamp": (WHEN - timedelta(days=1)).isoformat(), "discovered_at": (WHEN - timedelta(hours=1)).isoformat()})
    assert stats["high"] == stats["low"] == stats["start_price"] == source[-1].close
    assert stats["mfe_return"] == stats["mae_return"] == 0
    assert stats["duration_bars"] == stats["duration_seconds"] == 0
    assert stats["start_known_at"] == WHEN.isoformat()
    assert stats["anchor_event_timestamp"] != stats["start_known_at"]


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_swing_stats_track_known_completed_intervals_and_restore_json(side):
    source = candles(slope=0)
    previous = {"side": side, "statistics": _swing_statistics(source, "5m", side, {}, WHEN, None)}
    start = previous["statistics"]["start_price"]
    next_bar = next_candle(source[-1], start + 1, high=start + 3, low=start - 2)
    previous = json.loads(json.dumps(previous))
    stats = _swing_statistics(source + [next_bar], "5m", side, previous, next_bar.available_at, None)
    assert stats["duration_bars"] == 1
    assert stats["duration_seconds"] == 300
    assert stats["percent_move"] == pytest.approx(100 / start)
    assert stats["high"] == start + 3
    assert stats["low"] == start - 2
    assert stats["mfe_return"] == pytest.approx((3 if side == "LONG" else 2) / start)
    assert stats["mae_return"] == pytest.approx(-(2 if side == "LONG" else 3) / start)
    assert _swing_statistics(source + [next_bar], "5m", side, {"side": side, "statistics": stats}, next_bar.available_at, None) == stats


def test_partial_first_interval_excludes_pre_start_wicks():
    source = candles(slope=0)
    start_at = WHEN + timedelta(seconds=30)
    old = _swing_statistics(source, "5m", "LONG", {}, start_at, None)
    next_bar = next_candle(source[-1], old["start_price"] + 1, high=500, low=1)
    stats = _swing_statistics(source + [next_bar], "5m", "LONG", {"statistics": old}, next_bar.available_at, None)
    assert stats["high"] == next_bar.close
    assert stats["low"] == old["start_price"]
    assert stats["duration_bars"] == 0
    assert stats["coverage_status"] == "PARTIAL_FIRST_INTERVAL_EXCLUDED"


def test_gap_reports_only_known_excursion_bounds_and_direction_change_resets_leg():
    source = candles()
    initial = _swing_statistics(source, "5m", "LONG", {}, WHEN, None)
    new = next_candle(source[-1], source[-1].close + 1, gap=2)
    stats = _swing_statistics([new], "5m", "LONG", {"statistics": initial}, new.available_at, None)
    assert stats["coverage_status"] == "PARTIAL_HISTORY_GAP"
    reversed_stats = _swing_statistics([new], "5m", "SHORT", {"statistics": stats}, new.available_at, None)
    assert reversed_stats["start_known_at"] == new.available_at.isoformat()
    assert reversed_stats["duration_bars"] == reversed_stats["mfe_return"] == 0


def test_initial_missing_swing_memory_recovers_when_fresh_history_arrives():
    no_history = analyze_war_room("SYNUSDT", {}, WHEN - timedelta(minutes=5), {"as_of": (WHEN - timedelta(minutes=5)).isoformat()})
    assert no_history["swing_phases"]["5m"]["statistics"] is None
    fresh = analyze_war_room("SYNUSDT", {"5m": candles()}, WHEN, analysis(), previous=no_history)
    assert fresh["swing_phases"]["5m"]["statistics"]["coverage_status"] == "KNOWN_START_ONLY"
    assert fresh["swing_phases"]["5m"]["statistics"]["start_known_at"] == WHEN.isoformat()


def test_stale_swing_retains_memory_without_tradable_decision():
    frame = structure()
    previous = _swing_report(frame, "5m", candles(), {}, analysis(), {}, WHEN)
    stale = dict(frame, quality="STALE")
    result = _swing_report(stale, "5m", candles(), previous, analysis(), {}, WHEN + timedelta(hours=1))
    assert result["status"] == "STALE"
    assert result["current_phase"] == "UNAVAILABLE"
    assert result["decision"]["entry_status"] == "INSUFFICIENT_DATA"
    assert result["statistics"] == previous["statistics"]
    assert result["phase_history"] == previous["phase_history"]


def test_repeated_same_candle_does_not_duplicate_phase_history():
    frame = structure()
    previous = _swing_report(frame, "5m", candles(), {}, analysis(), {}, WHEN)
    repeated = _swing_report(frame, "5m", candles(), previous, analysis(), {}, WHEN)
    assert repeated == previous


@pytest.mark.parametrize("stage", ["BREAKDOWN_PRESSURE", "BREAKDOWN_ATTEMPT", "1M_BREAKDOWN_CONFIRMED", "5M_ACCEPTED_DOWN", "15M_EXPANSION_CONFIRMED_DOWN", "5M_ACCEPTED"])
def test_actual_short_fast_state_and_frozen_support_are_never_forced_long(stage):
    source = candles()
    data = analysis("SHORT")
    fast = {"bias": "SHORT", "stage": stage, "stale": False,
            "support": {"price": 112, "discovered_at": (WHEN - timedelta(hours=1)).isoformat()}}
    result = analyze_war_room("SYNUSDT", {"5m": source}, WHEN, data, fast)
    assert result["bias"] == "BEARISH"
    assert result["expected_path"]["primary"] == "bearish"
    assert result["scenarios"]["bearish"]["trigger"] == 112
    assert result["decisions"]["5m"]["trigger"] == 112
    assert result["decisions"]["5m"]["side"] == "SHORT"
    assert result["market_conflict"] is True
    if stage in {"1M_BREAKDOWN_CONFIRMED", "5M_ACCEPTED_DOWN", "15M_EXPANSION_CONFIRMED_DOWN", "5M_ACCEPTED"}:
        assert next(component for component in result["entry_quality"]["components"] if component["name"] == "fast_confirmation")["value"] == 100


@pytest.mark.parametrize("mutation", ["stale", "future", "future_support"])
def test_stale_future_short_fast_context_cannot_override_current_geometry(mutation):
    source = candles()
    fast = {"bias": "SHORT", "stage": "1M_BREAKDOWN_CONFIRMED", "support": {"price": 112, "discovered_at": WHEN.isoformat()}}
    if mutation == "stale":
        fast["stale"] = True
    elif mutation == "future":
        fast["confirmed_at"] = (WHEN + timedelta(minutes=1)).isoformat()
    else:
        fast["support"]["discovered_at"] = (WHEN + timedelta(minutes=1)).isoformat()
    result = analyze_war_room("SYNUSDT", {"5m": source}, WHEN, analysis(), fast)
    assert result["bias"] == "BULLISH"
    assert result["decisions"]["5m"]["side"] == "LONG"


def test_all_timeframes_integrate_decisions_without_changing_nominal_readiness():
    sources = {frame: candles(frame) for frame in FRAMES}
    data = analysis(fibonacci=True)
    original_components = [{"name": "baseline", "weight": 1, "value": 80}]
    data["scores"] = {"expansion_readiness": {"value": 80, "components": original_components, "coverage": 1}}
    report = analyze_war_room("SYNUSDT", sources, WHEN, data)
    assert report["status"] == "AVAILABLE"
    assert report["setup_quality"]["value"] == 80
    assert report["setup_quality"]["components"] == original_components
    assert set(report["decisions"]) == set(FRAMES)
    for frame in FRAMES:
        assert len(report["swing_phases"][frame]["conditional_next_steps"]) == 3
        assert report["timeframes"][frame]["history"][-1]["swing_phase"] == report["swing_phases"][frame]["current_phase"]
    json.dumps(report, allow_nan=False)


def test_future_candle_cannot_change_phase_decision_stats_or_paths():
    source = candles()
    baseline = analyze_war_room("SYNUSDT", {"5m": source}, WHEN, analysis())
    future = next_candle(source[-1], 500)
    frozen = analyze_war_room("SYNUSDT", {"5m": source + [future]}, WHEN, analysis())
    assert frozen == baseline
