"""Deterministic setup evidence, missing-data and causal integration tests."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.setup_library import SETUP_DEFINITIONS, evaluate_setups

POINT = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def context():
    frame = {"quality": "DERIVED", "state": "COMPRESSION", "bias": "BULLISH", "trend": "BULLISH_TREND",
             "source": "synthetic_test_only", "last_completed_candle": POINT.isoformat(),
             "history": [{"state": "COMPRESSION", "events": [], "discovered_at": POINT.isoformat()}],
             "sequence": ["COMPRESSION"], "events": [],
             "metrics": {"close": 105.5, "latest_high": 105.8, "latest_low": 104.5, "atr": 1,
                         "volatility_state": "COMPRESSION", "momentum_state": "ACCELERATING_UP",
                         "volume_acceleration_percent": 30, "rvol": 1.5,
                         "absorption_hypothesis": "POSSIBLE_BUYER_ABSORPTION",
                         "absorption_quality": "DERIVED_NATIVE_CANDLE_DELTA"}}
    targets = [{"price": 110, "source": "synthetic_book", "quality": "NATIVE", "discovered_at": POINT.isoformat()}]
    report = {"as_of": POINT.isoformat(), "bias": "BULLISH", "market_regime": "RISK_ON",
              "timeframes": {"5m": frame},
              "scenarios": {"bullish": {"trigger": 106, "invalidation": 100, "targets": targets},
                            "bearish": {"trigger": 100, "invalidation": 106, "targets": []},
                            "neutral": {"lower": 100, "upper": 106}}}
    return report, {"as_of": POINT.isoformat()}


def result(report, analysis=None):
    return evaluate_setups(report, analysis or {"as_of": POINT.isoformat()}, POINT)


def setup(report, name):
    return next(row for row in report["setups"] if row["name"] == name)


def test_all_ten_contracts_have_measurable_complete_fields():
    report, analysis = context()
    original = copy.deepcopy(report)
    library = result(report, analysis)
    assert len(library["setups"]) == 10
    assert {row["name"] for row in library["setups"]} == set(SETUP_DEFINITIONS)
    for row in library["setups"]:
        assert row["requirements"] == list(row["evidence"])
        assert row["trigger_condition"] and row["confirmation"] and row["invalidation_condition"]
        assert row["ideal_regime_hypothesis"]
        assert row["historical_performance"] == "INSUFFICIENT_DATA"
        assert row["research_weight"] == 0
        assert row["probability"] is None
        assert row["source"] == "synthetic_test_only"
        assert row["quality"] == "DERIVED"
    assert report == original
    json.dumps(library, allow_nan=False)


def test_compression_is_a_candidate_with_known_levels_not_a_breakout():
    report, _ = context()
    loaded = setup(result(report), "COMPRESSION_BREAKOUT")
    assert loaded["status"] == "CANDIDATE"
    assert loaded["trigger"] == 106
    assert loaded["invalidation"] == 100
    assert loaded["targets"][0]["price"] == 110
    assert "Closed 1m" in loaded["trigger_condition"]
    report["timeframes"]["5m"]["metrics"]["volatility_state"] = "EXPANSION"
    assert setup(result(report), "COMPRESSION_BREAKOUT")["status"] == "NOT_PRESENT"


def test_native_absorption_candidate_but_missing_flow_is_unknown():
    report, _ = context()
    assert setup(result(report), "ABSORPTION_BREAKOUT")["status"] == "CANDIDATE"
    report["timeframes"]["5m"]["metrics"]["absorption_quality"] = "UNAVAILABLE"
    report["timeframes"]["5m"]["metrics"]["absorption_hypothesis"] = "UNAVAILABLE"
    unknown = setup(result(report), "ABSORPTION_BREAKOUT")
    assert unknown["status"] == "INSUFFICIENT_DATA"
    assert unknown["evidence"]["native_flow_absorption_proxy"]["value"] is None
    assert unknown["evidence"]["native_flow_absorption_proxy"]["status"] == "UNKNOWN"


def test_momentum_needs_directional_acceleration_and_volume_awakening():
    report, _ = context()
    assert setup(result(report), "MOMENTUM_IGNITION")["status"] == "CANDIDATE"
    report["timeframes"]["5m"]["metrics"]["rvol"] = 0.8
    assert setup(result(report), "MOMENTUM_IGNITION")["status"] == "NOT_PRESENT"
    report["timeframes"]["5m"]["metrics"]["rvol"] = 2
    report["timeframes"]["5m"]["metrics"]["momentum_state"] = "DECELERATING_UP"
    assert setup(result(report), "MOMENTUM_IGNITION")["status"] == "NOT_PRESENT"


def test_momentum_direction_and_market_conflict_do_not_silently_force_long():
    report, _ = context()
    report["timeframes"]["5m"]["metrics"]["momentum_state"] = "ACCELERATING_DOWN"
    found = setup(result(report), "MOMENTUM_IGNITION")
    assert found["status"] == "CANDIDATE"
    assert found["direction_hypothesis"] == "BEARISH"
    assert found["market_conflict"] is True
    assert found["trigger"] == 100


def test_future_outcome_or_target_values_cannot_enter_setup_destinations():
    report, _ = context()
    report["scenarios"]["bullish"]["targets"] += [{"price": 200, "source": "synthetic_book", "quality": "NATIVE", "discovered_at": (POINT + timedelta(days=1)).isoformat()}]
    report["future_return_24h"] = 999
    found = setup(result(report), "COMPRESSION_BREAKOUT")
    assert [level["price"] for level in found["targets"]] == [110]
    assert "future_return_24h" not in found


@pytest.mark.parametrize(("state", "sequence", "name", "direction"), [
    ("FAILED_BREAKOUT", ["BREAKOUT", "FAILED_BREAKOUT"], "FAILED_BREAKOUT_REVERSAL", "BEARISH"),
    ("FAILED_BREAKDOWN", ["BREAKDOWN", "FAILED_BREAKDOWN", "RECLAIM"], "FAILED_BREAKDOWN_REVERSAL", "BULLISH"),
    ("RETEST", ["BREAKOUT", "RETEST"], "BREAKOUT_RETEST", "BULLISH"),
])
def test_failures_and_retests_require_observed_history(state, sequence, name, direction):
    report, _ = context()
    frame = report["timeframes"]["5m"]
    frame.update(state=state, sequence=sequence, events=[state, "SUPPORT_RESISTANCE_FLIP"] if state == "RETEST" else [state])
    found = setup(result(report), name)
    assert found["status"] == "CANDIDATE"
    assert found["direction_hypothesis"] == direction
    frame["sequence"] = [state]
    assert setup(result(report), name)["status"] == "NOT_PRESENT"


@pytest.mark.parametrize(("side", "direction", "invalidation"), [("SELL_SIDE_SWEEP", "BULLISH", 104.5), ("BUY_SIDE_SWEEP", "BEARISH", 105.8)])
def test_sweep_reclaim_has_actual_sweep_extreme_invalidation(side, direction, invalidation):
    report, _ = context()
    frame = report["timeframes"]["5m"]
    frame.update(state="LIQUIDITY_SWEEP", trend="RANGE", events=[side, "RECLAIM" if side == "SELL_SIDE_SWEEP" else "REJECTION"])
    library = result(report)
    reclaim = setup(library, "LIQUIDITY_SWEEP_RECLAIM")
    assert reclaim["status"] == "CANDIDATE"
    assert reclaim["direction_hypothesis"] == direction
    assert reclaim["invalidation"] == invalidation
    assert setup(library, "RANGE_DEVIATION")["status"] == "CANDIDATE"


def test_htf_continuation_needs_fresh_agreeing_context():
    report, _ = context()
    assert setup(result(report), "HTF_CONTINUATION")["status"] == "NOT_PRESENT"
    htf = copy.deepcopy(report["timeframes"]["5m"])
    report["timeframes"]["4h"] = htf
    assert setup(result(report), "HTF_CONTINUATION")["status"] == "CANDIDATE"
    htf["bias"] = "BEARISH"
    assert setup(result(report), "HTF_CONTINUATION")["status"] == "NOT_PRESENT"
    htf["quality"] = "STALE"
    assert setup(result(report), "HTF_CONTINUATION")["status"] == "NOT_PRESENT"


@pytest.mark.parametrize(("behaviour", "expected"), [("UNTESTED", "NOT_PRESENT"), ("REJECTED", "NOT_PRESENT"), ("ACCEPTED", "CANDIDATE"), ("SWEPT_AND_RECLAIMED", "CANDIDATE")])
def test_golden_pocket_requires_actual_behavior_not_ratio_presence(behaviour, expected):
    report, analysis = context()
    analysis["fibonacci"] = {"anchors": [{"id": "pair", "direction": "UP", "quality": "DERIVED", "discovered_at": POINT.isoformat()}],
                             "golden_pockets": [{"pair_id": "pair", "timeframe": "5m", "quality": "DERIVED", "discovered_at": POINT.isoformat(), "behaviour": {"state": behaviour}}]}
    found = setup(result(report, analysis), "GOLDEN_POCKET_CONTINUATION")
    assert found["status"] == expected
    assert found["research_weight"] == 0
    analysis["fibonacci"]["anchors"][0]["discovered_at"] = (POINT + timedelta(seconds=1)).isoformat()
    assert setup(result(report, analysis), "GOLDEN_POCKET_CONTINUATION")["status"] == "NOT_PRESENT"


def test_no_data_no_fabricated_triggers_targets_performance():
    library = result({"as_of": POINT.isoformat()})
    assert library["status"] == "INSUFFICIENT_DATA"
    assert library["active_setups"] == []
    for row in library["setups"]:
        assert row["status"] == "INSUFFICIENT_DATA"
        assert row["trigger"] is None
        assert row["invalidation"] is None
        assert row["targets"] == []
        assert row["target_status"] == "UNAVAILABLE"
        assert row["historical_performance"] == "INSUFFICIENT_DATA"


@pytest.mark.parametrize("bad", ["future_report", "future_frame", "future_receipt", "stale_frame", "future_analysis"])
def test_causal_availability_guards(bad):
    report, analysis = context()
    later = (POINT + timedelta(minutes=1)).isoformat()
    if bad == "future_report":
        report["as_of"] = later
    elif bad == "future_frame":
        report["timeframes"]["5m"]["last_completed_candle"] = later
    elif bad == "future_receipt":
        report["timeframes"]["5m"]["history"][-1]["discovered_at"] = later
    elif bad == "stale_frame":
        report["timeframes"]["5m"]["quality"] = "STALE"
    else:
        analysis["as_of"] = later
        assert setup(result(report, analysis), "GOLDEN_POCKET_CONTINUATION")["status"] == "INSUFFICIENT_DATA"
        return
    assert result(report, analysis)["active_setups"] == []


def test_personal_membership_never_increases_research_evidence():
    report, analysis = context()
    baseline = result(report, analysis)
    report["personal_watchlist"] = True
    report["pinned"] = True
    assert result(report, analysis) == baseline
