"""Synthetic geometric cases prove implementation, never predictive value."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.models import Candle
from expansion_hunter.pattern_engine import (
    PATTERN_NAMES,
    PatternSettings,
    candle_revision,
    detect_patterns,
    known_liquidity_levels,
    structure_sequences,
    swing_phase,
)
from expansion_hunter.war_room import analyze_war_room

START = datetime(2025, 1, 1, tzinfo=UTC)


def make_bars(values, *, frame="5m"):
    minutes = 5 if frame == "5m" else 1
    return [Candle(open_time=START + timedelta(minutes=index * minutes),
                   close_time=START + timedelta(minutes=(index + 1) * minutes),
                   available_at=START + timedelta(minutes=(index + 1) * minutes),
                   open=price, high=price + 0.08, low=price - 0.08,
                   close=price, volume=100, source="synthetic_test_only")
            for index, price in enumerate(values)]


def turns(values):
    prices = []
    for first, second in zip(values, values[1:], strict=False):
        prices.extend(first + (second - first) * step / 4 for step in range(4))
    prices.append(values[-1])
    while len(prices) < 20:
        prices.insert(0, prices[0] + 0.01)
    return make_bars(prices)


def run(source, **kwargs):
    return detect_patterns(source, "5m", source[-1].available_at, **kwargs)


def named(result, name):
    return next(pattern for pattern in result["detections"] if pattern["name"] == name)


@pytest.mark.parametrize(("name", "prices"), [
    ("DOUBLE_TOP", [100, 106, 100, 106, 100]),
    ("DOUBLE_BOTTOM", [106, 100, 106, 100, 106]),
    ("TRIPLE_TOP", [100, 106, 100, 106, 100, 106, 100]),
    ("TRIPLE_BOTTOM", [106, 100, 106, 100, 106, 100, 106]),
    ("HEAD_AND_SHOULDERS", [100, 106, 100, 112, 100, 106, 100]),
    ("INVERSE_HEAD_AND_SHOULDERS", [112, 106, 112, 100, 112, 106, 112]),
    ("ASCENDING_TRIANGLE", [100, 110, 102, 110, 104, 110, 106, 110, 107]),
    ("DESCENDING_TRIANGLE", [110, 100, 108, 100, 106, 100, 104, 100, 103]),
    ("SYMMETRICAL_TRIANGLE", [100, 112, 102, 110, 104, 108, 105, 107, 106]),
    ("RISING_WEDGE", [100, 107, 104, 110, 107, 112, 109, 113, 111]),
    ("FALLING_WEDGE", [114, 107, 110, 104, 107, 102, 105, 101, 103]),
    ("ASCENDING_CHANNEL", [100, 105, 103, 108, 106, 111, 109, 114, 112]),
    ("DESCENDING_CHANNEL", [115, 110, 112, 107, 109, 104, 106, 101, 103]),
])
def test_objective_confirmed_pivot_geometries(name, prices):
    source = turns(prices)
    result = run(source, context="TEST_RANGE")
    pattern = named(result, name)
    assert result["status"] == "DERIVED"
    assert pattern["context"] == "TEST_RANGE"
    assert pattern["trigger"] > 0
    assert pattern["invalidation"] > 0
    assert pattern["targets"] == []
    assert pattern["target_status"] == "UNAVAILABLE"
    assert pattern["research_weight"] == 0
    assert pattern["probability"] is None
    assert len(pattern["expected_path"]) == 3
    assert datetime.fromisoformat(pattern["discovered_at"]) == source[-1].available_at
    for anchor in pattern["anchors"]:
        assert datetime.fromisoformat(anchor["event_timestamp"]) <= datetime.fromisoformat(anchor["discovered_at"]) <= source[-1].available_at
        assert anchor["source"] == "synthetic_test_only"
    json.dumps(pattern, allow_nan=False)


@pytest.mark.parametrize(("name", "formation", "sign"), [
    ("BULL_FLAG", [112, 114, 112, 113.5, 111.5, 113, 111], 1),
    ("BEAR_FLAG", [112, 114, 112, 113.5, 111.5, 113, 111], -1),
    ("BULL_PENNANT", [112, 116, 110, 115, 111, 114, 112], 1),
    ("BEAR_PENNANT", [112, 116, 110, 115, 111, 114, 112], -1),
])
def test_flag_and_pennant_require_preceding_observed_impulse(name, formation, sign):
    formation_prices = [bar.close for bar in turns(formation)]
    prices = [80 + index * 0.5 for index in range(64)] + formation_prices
    source = make_bars(prices if sign > 0 else [200 - value for value in prices])
    pattern = named(run(source), name)
    assert pattern["metrics"]["impulse_atr"] >= 3
    assert pattern["metrics"]["retracement_fraction"] <= 0.5
    assert {"impulse_origin", "impulse_end"} <= {anchor["kind"] for anchor in pattern["anchors"]}
    # Same formation following a flat market is not the same continuation.
    flat_source = make_bars([112] * 64 + formation_prices)
    assert name not in {pattern["name"] for pattern in run(flat_source)["detections"]}


@pytest.mark.parametrize(("sign", "name"), [(1, "ROUNDED_BOTTOM"), (-1, "ROUNDED_TOP")])
def test_fixed_window_rounding_needs_curvature_depth_and_recovery(sign, name):
    source = make_bars([100 + sign * 0.04 * (index - 19.5) ** 2 for index in range(40)])
    pattern = named(run(source), name)
    assert pattern["metrics"]["quadratic_r_squared"] >= 0.85
    assert pattern["metrics"]["depth_atr"] >= 2
    assert pattern["discovered_at"] == source[-1].available_at.isoformat()
    assert not {"ROUNDED_BOTTOM", "ROUNDED_TOP"} & {pattern["name"] for pattern in run(make_bars([100 + index for index in range(40)]))["detections"]}


def test_flat_history_is_not_forced_into_a_named_pattern():
    result = run(make_bars([100] * 64))
    assert result["detections"] == []
    assert result["research_weight"] == 0
    assert result["pattern_availability"]["DOUBLE_TOP"]["status"] == "INSUFFICIENT_DATA"
    assert result["pattern_availability"]["ROUNDED_BOTTOM"]["status"] == "AVAILABLE"


def test_configurable_ATR_equal_tolerance_changes_geometry_not_history():
    source = turns([100, 106, 100, 106.5, 100])
    assert "DOUBLE_TOP" in {pattern["name"] for pattern in run(source)["detections"]}
    narrow = run(source, settings=PatternSettings(equal_tolerance_atr=0.01))
    assert "DOUBLE_TOP" not in {pattern["name"] for pattern in narrow["detections"]}


@pytest.mark.parametrize("bad", ["future", "late", "gap", "duplicate", "mixed"])
def test_point_in_time_integrity_and_bad_histories(bad):
    source = turns([100, 110, 102, 110, 104, 110, 106, 110, 107])
    point = source[-1].available_at
    baseline = run(source)
    if bad == "future":
        future = source[-1].model_copy(update={"open_time": point, "close_time": point + timedelta(minutes=5), "available_at": point + timedelta(minutes=5), "high": 10000})
        assert detect_patterns(source + [future], "5m", point) == baseline
    elif bad == "late":
        late = source[-1].model_copy(update={"available_at": point + timedelta(hours=1)})
        assert detect_patterns(source[:-1] + [late], "5m", point) == detect_patterns(source[:-1], "5m", point)
    else:
        changed = list(source)
        if bad == "gap":
            changed.pop(10)
        elif bad == "duplicate":
            changed.append(changed[-1])
        else:
            changed[-1] = changed[-1].model_copy(update={"source": "foreign_provider"})
        result = detect_patterns(changed, "5m", point)
        assert result["status"] == "INSUFFICIENT_DATA"
        assert result["detections"] == []
        assert result["issues"]


def test_second_touch_unavailable_until_right_hand_confirmation():
    source = turns([100, 106, 100, 106, 100])
    # Right shoulder/second top occurs four bars before the final trough.
    high = max(range(len(source) - 8, len(source)), key=lambda i: source[i].high)
    before = source[:high + 2]
    after = source[:high + 3]
    config = PatternSettings(minimum_bars=16)
    assert "DOUBLE_TOP" not in {pattern["name"] for pattern in run(before, settings=config)["detections"]}
    named(run(after, settings=config), "DOUBLE_TOP")


def test_stale_candles_emit_no_current_patterns():
    source = turns([100, 106, 100, 106, 100])
    result = detect_patterns(source, "5m", source[-1].available_at + timedelta(hours=1))
    assert result["status"] == "STALE"
    assert result["detections"] == []


def test_targets_only_actual_known_fresh_levels_and_correct_side():
    source = turns([100, 106, 100, 106, 100])
    when = source[-1].available_at
    levels = [{"price": price, "source": "synthetic_book", "quality": "NATIVE", "discovered_at": when.isoformat()}
              for price in (95, 90, 115)]
    levels += [{"price": 98, "source": "synthetic_book", "quality": "NATIVE", "discovered_at": (when + timedelta(seconds=1)).isoformat()},
               {"price": 97, "source": "synthetic_book", "quality": "STALE", "discovered_at": when.isoformat()},
               {"price": 96, "source": "synthetic_book", "quality": "NATIVE", "status": "INVALIDATED", "discovered_at": when.isoformat()}]
    pattern = named(run(source, liquidity_levels=levels), "DOUBLE_TOP")
    assert [target["price"] for target in pattern["targets"]] == [95, 90]
    assert all(target["source"] == "synthetic_book" for target in pattern["targets"])
    assert known_liquidity_levels([{"price": 2, "discovered_at": when.isoformat()}], when) == []


def state_for(source, *, label="LIQUIDITY_SWEEP", events=None):
    stamp = source[-1].available_at.isoformat()
    return {"state": label, "events": events or ["SELL_SIDE_SWEEP", "RECLAIM"],
            "quality": "DERIVED", "bias": "BULLISH", "last_completed_candle": stamp,
            "known_high_before_candle": {"price": 106}, "known_low_before_candle": {"price": 100},
            "history": [{"state": label, "events": events or ["SELL_SIDE_SWEEP", "RECLAIM"], "discovered_at": stamp}],
            "metrics": {"volatility_state": "COMPRESSION"}}


@pytest.mark.parametrize(("label", "events", "pattern"), [
    ("LIQUIDITY_SWEEP", ["SELL_SIDE_SWEEP", "RECLAIM"], "RANGE_DEVIATION"),
    ("FAILED_BREAKOUT", ["FAILED_BREAKOUT"], "FAILED_BREAKOUT_REVERSAL"),
    ("FAILED_BREAKDOWN", ["FAILED_BREAKDOWN"], "FAILED_BREAKDOWN_REVERSAL"),
    ("RETEST", ["RETEST", "SUPPORT_RESISTANCE_FLIP"], "BREAKOUT_RETEST"),
])
def test_observed_event_pattern_context(label, events, pattern):
    source = turns([100, 106, 100, 106, 100])
    state = state_for(source, label=label, events=events)
    if label == "RETEST":
        state["active_break"] = {"price": 106, "direction": "BULLISH"}
    result = run(source, structure=state)
    report = named(result, pattern)
    assert report["metrics"]["events"] == events
    assert report["confirmation"]


def test_future_structure_context_cannot_add_a_reversal():
    source = turns([100, 106, 100, 106, 100])
    state = state_for(source, label="FAILED_BREAKOUT", events=["FAILED_BREAKOUT"])
    state["last_completed_candle"] = (source[-1].available_at + timedelta(hours=1)).isoformat()
    assert "FAILED_BREAKOUT_REVERSAL" not in {row["name"] for row in run(source, structure=state)["detections"]}


def test_ordered_sequences_and_future_history_exclusion():
    sequence = ["RANGE", "SELL_SIDE_SWEEP", "RECLAIM", "COMPRESSION"]
    state = {"history": [{"state": label, "events": [label], "discovered_at": (START + timedelta(minutes=index)).isoformat()} for index, label in enumerate(sequence)]}
    result = structure_sequences(state, START + timedelta(minutes=3))
    assert result[0]["name"] == "SWEEP_RECLAIM_COMPRESSION"
    assert result[0]["expected_next_states"] == ["BREAKOUT", "RETEST", "EXPANSION"]
    assert structure_sequences(state, START + timedelta(minutes=2)) == []
    reversed_state = {"history": list(reversed(state["history"]))}
    assert not any(row["name"] == "SWEEP_RECLAIM_COMPRESSION" for row in structure_sequences(reversed_state, START + timedelta(minutes=3)))


def test_confirmed_higher_low_joins_live_state_sequence_without_backdating():
    history = [
        {"state": "LIQUIDITY_SWEEP", "events": ["SELL_SIDE_SWEEP", "RECLAIM"], "discovered_at": START.isoformat()},
        {"state": "RANGE", "events": [], "discovered_at": (START + timedelta(minutes=1)).isoformat(),
         "newly_confirmed_pivots": [{"classification": "HL", "discovered_at": (START + timedelta(minutes=1)).isoformat()}]},
        {"state": "COMPRESSION", "events": [], "discovered_at": (START + timedelta(minutes=2)).isoformat()},
    ]
    result = structure_sequences({"history": history, "state": "COMPRESSION"}, START + timedelta(minutes=2))
    assert "SWEEP_RECLAIM_HIGHER_LOW_COMPRESSION" in {row["name"] for row in result}
    history[1]["newly_confirmed_pivots"][0]["discovered_at"] = (START + timedelta(hours=1)).isoformat()
    result = structure_sequences({"history": history}, START + timedelta(minutes=2))
    assert "SWEEP_RECLAIM_HIGHER_LOW_COMPRESSION" not in {row["name"] for row in result}


def test_accumulation_distribution_proxy_requires_native_flow_evidence():
    source = turns([100, 106, 100, 106, 100])
    stamp = source[-1].available_at
    history = [{"state": "COMPRESSION", "events": [], "discovered_at": (stamp - timedelta(minutes=5 * (4 - i))).isoformat()} for i in range(5)]
    state = {"history": history, "pivots": [{"classification": "HL"}],
             "metrics": {"absorption_hypothesis": "POSSIBLE_BUYER_ABSORPTION"}}
    assert structure_sequences(state, stamp)[0]["name"] == "ACCUMULATION_PROXY"
    state["metrics"]["absorption_hypothesis"] = "UNAVAILABLE"
    assert structure_sequences(state, stamp) == []


def test_failed_move_marks_earlier_bullish_sequence_invalidated():
    history = [{"state": label, "events": [label], "discovered_at": (START + timedelta(minutes=i)).isoformat()}
               for i, label in enumerate(["SELL_SIDE_SWEEP", "RECLAIM", "BOS", "FAILED_BREAKOUT"])]
    state = {"history": history, "state": "FAILED_BREAKOUT"}
    found = structure_sequences(state, START + timedelta(minutes=3))
    assert next(row for row in found if row["name"] == "SWEEP_RECLAIM_BOS")["status"] == "INVALIDATED_SEQUENCE"


def test_swing_phase_three_conditional_steps_not_predicted_candles():
    source = turns([100, 110, 102, 110, 104, 110, 106, 110, 107])
    when = source[-1].available_at
    war_room = analyze_war_room("SYNUSDT", {"5m": source}, when, {"as_of": when.isoformat()})
    phase = war_room["swing_phases"]["5m"]
    assert phase["status"] == "CONDITIONAL_RESEARCH"
    assert phase["latest_swing"]["discovered_at"] <= when.isoformat()
    assert len(phase["conditional_next_steps"]) == 3
    assert all("IF" in step["condition"] for step in phase["conditional_next_steps"])
    assert phase["probability"] is None
    assert swing_phase({"quality": "STALE", "pivots": []}, when)["status"] == "INSUFFICIENT_DATA"


def test_runtime_report_integrates_patterns_library_and_caches_geometry(monkeypatch):
    source = turns([100, 110, 102, 110, 104, 110, 106, 110, 107])
    when = source[-1].available_at
    first = analyze_war_room("SYNUSDT", {"5m": source}, when, {"as_of": when.isoformat()})
    assert {"ASCENDING_TRIANGLE", "TRIPLE_TOP"} <= {pattern["name"] for pattern in first["patterns"]}
    assert len(first["setup_library"]["setups"]) == 10
    # No geometry fit should repeat for a byte-identical closed-candle revision.
    monkeypatch.setattr("expansion_hunter.war_room.detect_patterns", lambda *args, **kwargs: pytest.fail("duplicate geometry computation"))
    again = analyze_war_room("SYNUSDT", {"5m": source}, when, {"as_of": when.isoformat()}, previous=json.loads(json.dumps(first)))
    assert again == first
    assert candle_revision(source) != candle_revision(source[:-1])


def test_cached_geometry_targets_refresh_when_book_changes_without_future_features():
    source = turns([100, 106, 100, 106, 100])
    when = source[-1].available_at
    first_analysis = {"as_of": when.isoformat(), "liquidity_map": [{"price": 95, "source": "synthetic_book", "quality": "NATIVE", "discovered_at": when.isoformat()}]}
    first = analyze_war_room("SYNUSDT", {"5m": source}, when, first_analysis)
    old_copy = copy.deepcopy(first)
    next_time = when + timedelta(seconds=1)
    second_analysis = {"as_of": next_time.isoformat(), "liquidity_map": [{"price": 90, "source": "synthetic_book", "quality": "NATIVE", "discovered_at": next_time.isoformat()}]}
    second = analyze_war_room("SYNUSDT", {"5m": source}, next_time, second_analysis, previous=first)
    assert [target["price"] for target in named({"detections": second["patterns"]}, "DOUBLE_TOP")["targets"]] == [90]
    assert first == old_copy


def test_supported_pattern_catalog_is_declared_without_forcing_all_labels():
    empty = detect_patterns([], "5m", START)
    assert empty["status"] == "INSUFFICIENT_DATA"
    assert empty["supported_patterns"] == list(PATTERN_NAMES)
    assert empty["detections"] == []
    with pytest.raises(ValueError, match="unsupported"):
        detect_patterns([], "7m", START)
