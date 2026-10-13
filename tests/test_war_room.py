"""Point-in-time synthetic structure tests; no predictive-performance assertions."""

import copy
import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.models import TIMEFRAMES, Candle
from expansion_hunter.technical import confirmed_swings
from expansion_hunter.war_room import CONFIG, analyze_war_room

START = datetime(2025, 1, 1, tzinfo=UTC)


def bars(count=40, frame="5m", slope=0):
    result = []
    for index in range(count):
        price = 100 + slope * index + 3 * math.sin(index * 0.9)
        opened = START + timedelta(seconds=TIMEFRAMES[frame] * index)
        result.append(Candle(open_time=opened, close_time=opened + timedelta(seconds=TIMEFRAMES[frame]), available_at=opened + timedelta(seconds=TIMEFRAMES[frame]), open=price, high=price + 0.4, low=price - 0.4, close=price, volume=100, source="synthetic_test_only"))
    return result


def candle_after(previous, close, *, high=None, low=None, volume=100, taker=None, frame="5m"):
    opened = previous.close_time
    return Candle(open_time=opened, close_time=opened + timedelta(seconds=TIMEFRAMES[frame]), available_at=opened + timedelta(seconds=TIMEFRAMES[frame]), open=previous.close, high=high if high is not None else max(close, previous.close) + 0.2, low=low if low is not None else min(close, previous.close) - 0.2, close=close, volume=volume, taker_buy_volume=taker, source=previous.source)


def analyze(source, *, frame="5m", previous=None, analysis=None, fast=None, as_of=None):
    point = as_of or source[-1].available_at
    return analyze_war_room("SYNUSDT", {frame: source}, point, analysis or {"as_of": point.isoformat()}, fast, previous)


def test_empty_no_fabricated_levels_scores_or_probabilities():
    result = analyze_war_room("SYNUSDT", {}, START, {"as_of": START.isoformat()})
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["price"] is None
    assert result["alignment"]["value"] is None
    assert result["setup_quality"]["value"] is None
    assert result["entry_quality"]["value"] is None
    for path in ("bullish", "bearish"):
        assert result["scenarios"][path]["trigger"] is None
        assert result["scenarios"][path]["targets"] == []
        assert result["scenarios"][path]["target_status"] == "UNAVAILABLE"
    assert result["expected_path"]["probability"] is None
    assert result["research_weight"] == 0
    json.dumps(result, allow_nan=False)


def test_fully_confirmed_trend_pivots_and_timestamp_order():
    source = bars(slope=0.4)
    result = analyze(source)
    state = result["timeframes"]["5m"]
    classifications = {pivot["classification"] for pivot in state["pivots"]}
    assert {"HH", "HL"} <= classifications
    assert state["trend"] == "BULLISH_TREND"
    assert result["alignment"]["direction"] == "BULLISH"
    assert result["alignment"]["value"] == 100
    assert 0 < result["alignment"]["coverage"] < 1
    for pivot in state["pivots"]:
        assert datetime.fromisoformat(pivot["discovered_at"]) >= datetime.fromisoformat(pivot["event_timestamp"]) + timedelta(minutes=10)


def test_bearish_lower_high_lower_low_structure():
    result = analyze(bars(slope=-0.4))
    state = result["timeframes"]["5m"]
    assert {"LH", "LL"} <= {pivot["classification"] for pivot in state["pivots"]}
    assert state["trend"] == "BEARISH_TREND"
    assert result["alignment"]["direction"] == "BEARISH"


def test_future_and_delayed_bars_cannot_change_historical_state():
    source = bars()
    point = source[-1].available_at
    expected = analyze(source, as_of=point)
    future = candle_after(source[-1], 500)
    assert analyze(source + [future], as_of=point) == expected
    delayed = source[-1].model_copy(update={"available_at": point + timedelta(days=1)})
    assert analyze(source[:-1] + [delayed], as_of=point) == analyze(source[:-1], as_of=point)


@pytest.mark.parametrize("bad", ["gap", "duplicate", "mixed"])
def test_invalid_history_reports_unavailable(bad):
    source = bars()
    if bad == "gap":
        source.pop(10)
    elif bad == "duplicate":
        source.append(source[-1])
    else:
        source[-1] = source[-1].model_copy(update={"source": "unrelated_provider"})
    result = analyze(source)
    assert result["timeframes"]["5m"]["quality"] == "UNAVAILABLE"
    assert result["issues"]


def test_pivot_not_available_before_confirmation():
    source = bars(30)
    partial = analyze(source[:-2])
    complete = analyze(source)
    last_pivots = {pivot["event_timestamp"] for pivot in complete["timeframes"]["5m"]["pivots"]}
    earlier = {pivot["event_timestamp"] for pivot in partial["timeframes"]["5m"]["pivots"]}
    assert earlier <= last_pivots
    for pivot in partial["timeframes"]["5m"]["pivots"]:
        assert datetime.fromisoformat(pivot["discovered_at"]) <= source[-3].available_at


def test_bos_uses_high_confirmed_before_evaluated_candle():
    source = bars()
    previous = analyze(source)
    pivot = [pivot for pivot in previous["timeframes"]["5m"]["pivots"] if pivot["kind"] == "high"][-1]
    assert source[-1].close < pivot["price"]
    trigger = candle_after(source[-1], pivot["price"] + 1)
    result = analyze(source + [trigger], previous=previous)
    state = result["timeframes"]["5m"]
    assert state["state"] == "BREAKOUT"
    assert "BOS" in state["events"]
    assert state["active_break"]["price"] == pivot["price"]
    assert datetime.fromisoformat(state["active_break"]["level_discovered_at"]) <= trigger.open_time


def test_newly_confirmed_high_cannot_be_used_for_same_candle_break():
    source = bars()
    last = source[-1]
    result = analyze(source)
    state = result["timeframes"]["5m"]
    pivot = state["known_high_before_candle"]
    if pivot:
        assert datetime.fromisoformat(pivot["discovered_at"]) <= last.open_time
    expected = [pivot for pivot in confirmed_swings(source, left=2, right=2) if pivot["kind"] == "high" and datetime.fromisoformat(pivot["confirmed_at"]) <= last.open_time]
    assert pivot["price"] == expected[-1]["price"]


def test_wick_above_known_high_is_sweep_not_breakout():
    source = bars()
    previous = analyze(source)
    price = previous["timeframes"]["5m"]["known_high_before_candle"]["price"]
    sweep = candle_after(source[-1], price - 0.2, high=price + 0.8)
    result = analyze(source + [sweep], previous=previous)
    state = result["timeframes"]["5m"]
    assert state["state"] == "LIQUIDITY_SWEEP"
    assert state["events"] == ["BUY_SIDE_SWEEP", "REJECTION"]
    assert state["active_break"] is None


def test_sell_side_sweep_and_reclaim():
    source = bars()
    previous = analyze(source)
    price = previous["timeframes"]["5m"]["known_low_before_candle"]["price"]
    sweep = candle_after(source[-1], price + 0.2, low=price - 0.8)
    result = analyze(source + [sweep], previous=previous)
    assert result["timeframes"]["5m"]["events"] == ["SELL_SIDE_SWEEP", "RECLAIM"]


def test_breakout_retest_and_failed_break_preserve_known_level():
    source = bars()
    initial = analyze(source)
    level = [pivot for pivot in initial["timeframes"]["5m"]["pivots"] if pivot["kind"] == "high"][-1]["price"]
    breakout = candle_after(source[-1], level + 1)
    broken = analyze(source + [breakout], previous=initial)
    retest = candle_after(breakout, level + 0.3, low=level - 0.05)
    retested = analyze(source + [breakout, retest], previous=broken)
    assert retested["timeframes"]["5m"]["state"] == "RETEST"
    assert "SUPPORT_RESISTANCE_FLIP" in retested["timeframes"]["5m"]["events"]
    assert retested["timeframes"]["5m"]["active_break"]["price"] == level
    failed = candle_after(retest, level - 2)
    result = analyze(source + [breakout, retest, failed], previous=retested)
    assert result["timeframes"]["5m"]["state"] == "FAILED_BREAKOUT"
    assert result["timeframes"]["5m"]["active_break"] is None
    assert "RETEST" in result["timeframes"]["5m"]["sequence"]


def test_failed_breakdown_reclaims_level():
    source = bars()
    previous = analyze(source)
    level = previous["timeframes"]["5m"]["known_low_before_candle"]["price"]
    breakdown = candle_after(source[-1], level - 1)
    broken = analyze(source + [breakdown], previous=previous)
    assert broken["timeframes"]["5m"]["state"] == "BREAKDOWN"
    recovered = candle_after(breakdown, level + 2)
    result = analyze(source + [breakdown, recovered], previous=broken)
    assert result["timeframes"]["5m"]["state"] == "FAILED_BREAKDOWN"
    assert "RECLAIM" in result["timeframes"]["5m"]["events"]


def test_countertrend_break_is_choch():
    source = bars(38, slope=-0.4)
    previous = analyze(source)
    level = [pivot for pivot in previous["timeframes"]["5m"]["pivots"] if pivot["kind"] == "high"][-1]["price"]
    changed = candle_after(source[-1], level + 1)
    result = analyze(source + [changed], previous=previous)
    assert result["timeframes"]["5m"]["state"] == "CHoCH"
    assert result["timeframes"]["5m"]["pending_shift"]["direction"] == "BULLISH"


def test_mss_requires_following_confirmed_trend_not_same_choch_candle():
    source = bars(38, slope=-0.4)
    previous = analyze(source)
    level = [pivot for pivot in previous["timeframes"]["5m"]["pivots"] if pivot["kind"] == "high"][-1]["price"]
    changed = candle_after(source[-1], level + 1)
    source.append(changed)
    previous = analyze(source, previous=previous)
    assert previous["timeframes"]["5m"]["state"] == "CHoCH"
    seen_mss = False
    for index in range(1, 31):
        close = changed.close + index * 0.5 + 3 * math.sin(index * 0.9)
        # Distinct native-style wicks prevent an artificial flat two-bar high
        # from being mistaken for a strictly confirmed pivot.
        generated = candle_after(source[-1], close)
        generated = generated.model_copy(update={"high": generated.high + 0.03 * math.sin(index * 1.7), "low": generated.low - 0.03 * math.cos(index * 1.3)})
        source.append(generated)
        previous = analyze(source, previous=previous)
        frame = previous["timeframes"]["5m"]
        if frame["state"] == "MSS":
            assert frame["trend"] == "BULLISH_TREND"
            assert frame["pending_shift"] is None
            assert datetime.fromisoformat(frame["last_completed_candle"]) > changed.close_time
            seen_mss = True
    assert seen_mss


def test_near_equal_levels_remain_range_and_classifications_do_not_repaint():
    source = bars()
    previous = analyze(source)
    assert previous["timeframes"]["5m"]["trend"] == "RANGE"
    assert {"EQH", "EQL"} <= {pivot["classification"] for pivot in previous["timeframes"]["5m"]["pivots"]}
    before = {(pivot["kind"], pivot["event_timestamp"]): pivot["classification"] for pivot in previous["timeframes"]["5m"]["pivots"]}
    huge = candle_after(source[-1], 200)
    after = analyze(source + [huge], previous=previous)
    for pivot in after["timeframes"]["5m"]["pivots"]:
        key = (pivot["kind"], pivot["event_timestamp"])
        if key in before:
            assert pivot["classification"] == before[key]


def test_same_completed_candle_is_not_duplicate_and_restart_json_is_equivalent():
    source = bars()
    original = analyze(source)
    saved = json.loads(json.dumps(original, allow_nan=False))
    result = analyze(source, previous=saved)
    assert result == original
    assert len(result["timeframes"]["5m"]["history"]) == 1
    assert saved == original


def test_history_is_bounded_and_previous_not_mutated():
    source = bars()
    previous = analyze(source)
    first = copy.deepcopy(previous)
    for _ in range(50):
        source.append(candle_after(source[-1], 100, high=max(source[-1].close, 100.4), low=min(source[-1].close, 99.6)))
        previous = analyze(source, previous=previous)
    assert len(previous["timeframes"]["5m"]["history"]) == CONFIG["history_limit"]
    assert len(first["timeframes"]["5m"]["history"]) == 1
    timestamps = [entry["event_timestamp"] for entry in previous["timeframes"]["5m"]["history"]]
    assert len(timestamps) == len(set(timestamps))


def test_future_previous_state_rejected():
    source = bars()
    previous = analyze(source)
    with pytest.raises(ValueError, match="future decision time"):
        analyze(source[:-1], previous=previous)


def test_other_asset_state_cannot_pollute_a_coin():
    source = bars()
    previous = analyze(source)
    with pytest.raises(ValueError, match="another asset"):
        analyze_war_room("OTHERUSDT", {"5m": source}, source[-1].available_at, {"as_of": source[-1].available_at.isoformat()}, previous=previous)


def test_stale_context_excluded_from_alignment_but_history_retained():
    source = bars(slope=0.4)
    fresh = analyze(source)
    stale = analyze(source, previous=fresh, as_of=source[-1].close_time + timedelta(hours=1))
    assert stale["timeframes"]["5m"]["quality"] == "STALE"
    assert stale["timeframes"]["5m"]["history"] == fresh["timeframes"]["5m"]["history"]
    assert stale["alignment"]["value"] is None
    assert stale["alignment"]["coverage"] == 0
    assert stale["price"] is None


def test_native_delta_absorption_hypothesis_no_synthetic_cvd():
    source = bars()
    source = [bar.model_copy(update={"taker_buy_volume": 20}) for bar in source]
    # Last five closes held flat despite native net taker selling.
    for index in range(-5, 0):
        source[index] = source[index].model_copy(update={"open": 100, "close": 100, "high": 100.4, "low": 99.6})
    result = analyze(source)
    metrics = result["timeframes"]["5m"]["metrics"]
    assert metrics["native_taker_delta_5_bars"] == -300
    assert metrics["absorption_hypothesis"] == "POSSIBLE_BUYER_ABSORPTION"
    absent = analyze([bar.model_copy(update={"taker_buy_volume": None}) for bar in source])
    assert absent["timeframes"]["5m"]["metrics"]["native_taker_delta_5_bars"] is None
    assert absent["timeframes"]["5m"]["metrics"]["absorption_quality"] == "UNAVAILABLE"


def test_volume_acceleration_and_compression_are_distinct():
    source = bars(50)
    for index in range(-5, 0):
        source[index] = source[index].model_copy(update={"open": 100, "close": 100, "high": 100.05, "low": 99.95, "volume": 200})
    metrics = analyze(source)["timeframes"]["5m"]["metrics"]
    assert metrics["range_compression_ratio"] < 0.75
    assert metrics["volatility_state"] in {"COMPRESSION", "SQUEEZE"}
    assert metrics["volume_acceleration_percent"] > 0


def test_scenarios_keep_existing_fast_resistance_and_only_known_targets():
    source = bars(slope=0.4)
    when = source[-1].available_at
    analysis = {"as_of": when.isoformat(), "liquidity_map": [
        {"price": 120, "type": "ASK_WALL", "category": "OBSERVED_ORDER_BOOK", "quality": "NATIVE", "source": "synthetic_depth", "discovered_at": when.isoformat()},
        {"price": 125, "type": "SWING_HIGH", "category": "INFERRED_STRUCTURE", "quality": "ESTIMATED", "source": "synthetic_candles", "discovered_at": when.isoformat()},
        {"price": 118, "quality": "NATIVE", "discovered_at": (when + timedelta(hours=1)).isoformat()},
        {"price": 119, "quality": "STALE", "discovered_at": when.isoformat()},
        {"price": 121, "quality": "NATIVE", "status": "INVALIDATED", "discovered_at": when.isoformat()},
    ]}
    fast = {"stage": "BREAKOUT_PRESSURE", "resistance": {"price": 117, "discovered_at": (when - timedelta(hours=1)).isoformat()}}
    result = analyze(source, analysis=analysis, fast=fast)
    assert result["scenarios"]["bullish"]["trigger"] == 117
    assert [target["price"] for target in result["scenarios"]["bullish"]["targets"]] == [120, 125]
    assert result["expected_path"]["primary"] == "bullish"
    assert result["expected_path"]["steps"][0]["price"] == 117
    assert result["expected_path"]["probability"] is None


def test_no_target_is_filled_with_fibonacci_extension_or_random_level():
    source = bars(slope=0.4)
    when = source[-1].available_at
    result = analyze(source, analysis={"as_of": when.isoformat(), "fibonacci": {"levels": [{"price": 150, "ratio": 1.618}]}})
    assert result["scenarios"]["bullish"]["targets"] == []
    assert result["scenarios"]["bullish"]["target_status"] == "UNAVAILABLE"
    assert result["entry_quality"]["value"] is None


def test_future_analysis_and_fast_confirmation_are_excluded():
    source = bars()
    when = source[-1].available_at
    later = when + timedelta(days=1)
    result = analyze(source, analysis={"as_of": later.isoformat(), "scores": {"expansion_readiness": {"value": 99}}}, fast={"stage": "1M_BREAKOUT_CONFIRMED", "confirmed_at": later.isoformat()})
    assert result["setup_quality"]["value"] is None
    assert result["fast_state"] == "UNAVAILABLE"
    assert "analysis_context_unavailable_or_future" in result["issues"]
    assert "fast_context_is_from_future" in result["issues"]


def test_scores_reuse_existing_components_and_entry_costs_remain_unknown():
    source = bars(slope=0.4)
    when = source[-1].available_at
    analysis = {"as_of": when.isoformat(), "scores": {"expansion_readiness": {"value": 80, "coverage": 0.75, "components": [{"name": "compression", "value": 90}]}}, "liquidity_map": [{"price": 125, "quality": "NATIVE", "source": "synthetic_depth", "category": "OBSERVED_ORDER_BOOK", "discovered_at": when.isoformat()}]}
    result = analyze(source, analysis=analysis)
    assert result["setup_quality"]["value"] == 80
    assert result["setup_quality"]["components"] == analysis["scores"]["expansion_readiness"]["components"]
    assert result["entry_quality"]["net_reward_risk"] is None
    assert result["entry_quality"]["status"] == "UNCALIBRATED"
    assert result["research_weight"] == 0


def test_all_six_frames_are_independent_and_have_coverage():
    point = START + timedelta(days=80)
    candles = {}
    for frame in TIMEFRAMES:
        if frame == "1W":
            continue
        source = bars(frame=frame, slope=0.4 if frame in {"1D", "4h", "1h"} else -0.4)
        offset = point - source[-1].available_at
        candles[frame] = [bar.model_copy(update={"open_time": bar.open_time + offset, "close_time": bar.close_time + offset, "available_at": bar.available_at + offset}) for bar in source]
    result = analyze_war_room("SYNUSDT", candles, point, {"as_of": point.isoformat()})
    assert result["status"] == "AVAILABLE"
    assert result["alignment"]["coverage"] == 1
    assert result["timeframes"]["1D"]["bias"] == "BULLISH"
    assert result["timeframes"]["1m"]["bias"] == "BEARISH"
    assert 0 < result["alignment"]["value"] < 100
    json.dumps(result, allow_nan=False)
