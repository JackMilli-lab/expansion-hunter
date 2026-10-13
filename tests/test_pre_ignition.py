"""Causal, direction-neutral Pre-Ignition contracts, using synthetic test-only data.

These checks establish computation and safety behavior, never profitable edge.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.models import Candle
from expansion_hunter.pre_ignition import (
    PreIgnitionEngine,
    PreIgnitionSettings,
    causal_fast_features,
)

T = datetime(2026, 10, 9, 12, tzinfo=UTC)


def bars(*, as_of=T, scale=1.0, future=False):
    """One asset's stable baseline followed by bounded native volume acceleration."""
    history = []
    for index in range(80):
        at = as_of - timedelta(minutes=79 - index)
        close = 100 + 0.002 * index
        volume = (100 + index % 5) * scale
        if index >= 76:
            volume *= (2, 3, 5, 8)[index - 76]
        history.append(Candle(
            open_time=at - timedelta(minutes=1),
            close_time=at,
            available_at=at,
            open=close - 0.001,
            high=close + 0.02,
            low=close - 0.02,
            close=close,
            volume=volume,
            taker_buy_volume=volume * 0.85,
            source="TEST_ONLY_NATIVE_CANDLES",
        ))
    if future:
        at = as_of + timedelta(minutes=1)
        history.append(Candle(open_time=as_of, close_time=at, available_at=at,
            open=100, high=200, low=99, close=190, volume=1_000_000,
            taker_buy_volume=950_000, source="TEST_ONLY_FUTURE"))
    return history


def observation(*, as_of=T, bias="LONG", flow=.9, level=True, strength=.7,
                scale=1.0, unavailable_flow=False):
    direction = -1 if bias == "SHORT" else 1
    data = bars(as_of=as_of, scale=scale)
    if bias == "SHORT":
        data = [bar.model_copy(update={"open": 200 - bar.open,
            "high": 200 - bar.low, "low": 200 - bar.high, "close": 200 - bar.close,
            "taker_buy_volume": bar.volume - bar.taker_buy_volume}) for bar in data]
    price = data[-1].close
    fast = causal_fast_features(data, as_of)
    # Independent core tests supply explicit observed pressure features; separate
    # helper tests above validate their causal calculation from native candles.
    fast.update(range_compression=strength, atr_compression=strength,
        volatility_compression=strength,
        higher_low_strength=.7 if bias == "LONG" else 0,
        lower_high_strength=.7 if bias == "SHORT" else 0)
    metrics = {
        "range_compression": strength,
        "atr_compression": strength,
        "volatility_compression": strength,
        "higher_low_strength" if bias == "LONG" else "lower_high_strength": .7,
        "resistance_test_count" if bias == "LONG" else "support_test_count": 4,
        "relative_volume": 5,
        "volume_acceleration": 1.2,
        "pressure_velocity": .7,
        "pressure_acceleration": .8,
        "rejection_weakening": .8,
        "taker_buy_ratio": None if unavailable_flow else (flow if bias == "LONG" else 1 - flow),
    }
    return {
        "symbol": "UNKNOWNUSDT", "as_of": as_of.isoformat(),
        "status": "EVALUATED", "price": price,
        "data_quality": {"1m": "AVAILABLE", "5m": "AVAILABLE", "futures": "UNAVAILABLE"},
        "btc_context": {"status": "AVAILABLE", "relative_return": direction * .008,
            "btc_return": direction * .002, "regime": "BTC_UP" if bias == "LONG" else "BTC_DOWN"},
        "fast_features": fast,
        "directions": {bias: {
            "level": price + direction * .1 if level else None,
            "level_discovered_at": (as_of - timedelta(minutes=1)).isoformat() if level else None,
            "distance_atr": .1 if level else None,
            "metrics": metrics,
        }},
    }


def row(rows, asset_id="unknown", bias="LONG"):
    return next(item for item in rows if item["asset_id"] == asset_id and item["bias"] == bias)


def update(data, *, engine=None, at=T, bias="LONG"):
    return row((engine or PreIgnitionEngine()).update({"unknown": data}, {}, at), bias=bias)


def test_future_candles_cannot_change_fast_features():
    expected = causal_fast_features(bars(), T)
    assert causal_fast_features(bars(future=True), T) == expected


def test_delayed_receipt_is_excluded_even_after_close():
    data = bars()
    delayed = data[-1].model_copy(update={"available_at": T + timedelta(seconds=1)})
    expected = causal_fast_features(data[:-1], T)
    assert causal_fast_features(data[:-1] + [delayed], T) == expected


def test_duplicate_native_candles_do_not_inflate_volume_or_baseline():
    data = bars()
    assert causal_fast_features(data + [data[-1], data[-2]], T) == causal_fast_features(data, T)


def test_reordered_input_preserves_causal_fast_features():
    data = bars()
    assert causal_fast_features(list(reversed(data)), T) == causal_fast_features(data, T)


def test_candle_features_use_assets_own_volume_baseline():
    small = causal_fast_features(bars(scale=1), T)
    large = causal_fast_features(bars(scale=1000), T)
    # Candle prices are identical. Unit-independent anomaly behavior is required.
    for key in ("relative_volume", "volume_acceleration", "volume_zscore"):
        assert small[key] == pytest.approx(large[key])


def test_no_candle_trade_count_or_derivative_fabrication():
    feature = causal_fast_features(bars(), T)
    assert feature["trade_count_acceleration"] is None
    assert feature.get("open_interest_change") is None
    assert feature.get("funding") is None


def test_future_trade_snapshot_cannot_override_closed_native_features():
    snapshot = {"as_of": (T + timedelta(seconds=1)).isoformat(),
        "data_quality": {"trades": "AVAILABLE"},
        "order_flow_windows": {"1m": {"quality": "AVAILABLE",
            "taker_buy_volume": 99_000, "taker_sell_volume": 1,
            "aggregate_trade_count": 100_000}}}
    assert causal_fast_features(bars(), T, market_snapshot=snapshot) == causal_fast_features(bars(), T)


@pytest.mark.parametrize("missing", [1, 25])
def test_history_gaps_are_visible_instead_of_being_treated_as_contiguous(missing):
    data = bars()
    del data[-missing - 1]
    feature = causal_fast_features(data, T)
    assert feature["quality"] != "AVAILABLE"


def test_stale_history_is_never_reported_fresh():
    feature = causal_fast_features(bars(as_of=T - timedelta(minutes=5)), T)
    assert feature["quality"] == "STALE"


def test_input_observations_are_not_mutated_and_both_directions_are_emitted():
    data = {"unknown": observation()}
    before = copy.deepcopy(data)
    result = PreIgnitionEngine().update(data, {}, T)
    assert data == before
    assert {(item["asset_id"], item["bias"]) for item in result} == {
        ("unknown", "LONG"), ("unknown", "SHORT")}
    assert all(item["execution_enabled"] is False for item in result)


def test_research_weights_are_explicit_and_cannot_claim_probabilities():
    settings = PreIgnitionSettings()
    assert settings.weights == {
        "acceleration": .25, "compression_structure": .20,
        "volume_anomaly": .15, "location": .15,
        "relative_market": .10, "native_order_flow": .10, "context": .05}
    result = update(observation())
    assert result["probability"] is None
    assert 0 <= result["score"] <= 100
    assert "UNCALIBRATED" in result["calibration"]


def test_invalid_weights_are_rejected_instead_of_silently_normalized():
    with pytest.raises(ValueError):
        PreIgnitionSettings(weights={"acceleration": 1.0})


@pytest.mark.parametrize("problem", ["stale", "future", "missing"])
def test_invalid_evidence_cannot_be_ready_or_ranked(problem):
    data = observation(as_of=T - timedelta(minutes=5) if problem == "stale" else
        T + timedelta(seconds=1) if problem == "future" else T)
    if problem == "missing":
        data["data_quality"]["1m"] = "UNAVAILABLE"
    result = update(data)
    assert result["state"] not in {"READY", "TRIGGER", "ACTIVE"}
    assert result["status"] in {"STALE", "UNAVAILABLE"}
    assert result["rank"]["current_rank"] is None


def test_future_trigger_is_excluded_while_valid_fast_evidence_can_remain_radar():
    data = observation()
    data["directions"]["LONG"]["level_discovered_at"] = (T + timedelta(seconds=1)).isoformat()
    result = update(data)
    assert result["trigger"] is None
    assert result["state"] in {"NORMAL", "RADAR", "WATCH"}
    assert result["components"]["location"]["inputs"]["unbroken_boundary_proximity"] is None


def test_missing_native_order_flow_reduces_score_without_reweighting():
    full = observation()
    missing = copy.deepcopy(full)
    missing["directions"]["LONG"]["metrics"]["taker_buy_ratio"] = None
    missing["fast_features"]["taker_buy_ratio"] = None
    present_row, absent_row = update(full), update(missing)
    assert absent_row["score"] < present_row["score"]
    assert absent_row["evidence_coverage"] < present_row["evidence_coverage"]
    assert absent_row["components"]["native_order_flow"]["normalized"] is None
    assert absent_row["components"]["native_order_flow"]["contribution"] in (None, 0)
    assert absent_row["components"]["native_order_flow"]["weight"] == .10


def test_no_known_level_never_becomes_ready_or_invents_a_trigger():
    result = update(observation(level=False))
    assert result["trigger"] is None
    assert result["state"] in {"RADAR", "WATCH"}
    assert result["components"]["location"]["inputs"]["unbroken_boundary_proximity"] is None


def test_decision_clock_must_be_monotonic():
    engine = PreIgnitionEngine()
    engine.update({}, {}, T)
    with pytest.raises(ValueError, match="chronological"):
        engine.update({}, {}, T - timedelta(seconds=1))


def test_restore_is_json_serializable_and_requires_new_market_evidence():
    engine = PreIgnitionEngine()
    data = observation()
    engine.update({"unknown": data}, {}, T)
    restored = PreIgnitionEngine()
    restored.restore_state(json.loads(json.dumps(engine.export_state())))
    old = update(data, engine=restored, at=T + timedelta(seconds=1))
    assert old["status"] == "STALE" and old["state"] == "NORMAL"
    fresh_at = T + timedelta(minutes=1)
    fresh = update(observation(as_of=fresh_at), engine=restored, at=fresh_at)
    assert fresh["status"] == "AVAILABLE"


def test_duplicate_polling_does_not_manufacture_score_velocity():
    engine = PreIgnitionEngine()
    data = observation()
    engine.update({"unknown": data}, {}, T)
    for seconds in (1, 20, 61):
        result = update(data, engine=engine, at=T + timedelta(seconds=seconds))
        assert result["score_velocity"] is None
        assert result["score_acceleration"] is None
        assert result["rank"]["rank_velocity"] is None


def test_score_velocity_uses_genuine_elapsed_market_minutes():
    engine = PreIgnitionEngine()
    first = update(observation(strength=.1), engine=engine)
    at = T + timedelta(minutes=2)
    second = update(observation(as_of=at, strength=.9), engine=engine, at=at)
    assert second["score"] > first["score"]
    assert second["score_velocity"] == pytest.approx((second["score"] - first["score"]) / 2)


def test_distinct_quotes_within_a_minute_do_not_fabricate_score_acceleration():
    engine = PreIgnitionEngine()
    engine.update({"unknown": observation()}, {}, T)
    at = T + timedelta(seconds=10)
    result = update(observation(as_of=at, strength=.9), engine=engine, at=at)
    assert result["score_velocity"] is None
    assert result["score_acceleration"] is None
    assert result["rank"]["rank_velocity"] is None


def test_cross_sectional_rank_improvement_is_measured_against_same_fresh_cohort():
    engine = PreIgnitionEngine()
    first = {"unknown": observation(strength=.05),
        "other": observation(strength=.5), "third": observation(strength=.8)}
    initial = row(engine.update(first, {}, T))
    at = T + timedelta(minutes=1)
    second = {"unknown": observation(as_of=at, strength=.95),
        "other": observation(as_of=at, strength=.5),
        "third": observation(as_of=at, strength=.8)}
    changed = row(engine.update(second, {}, at))
    assert changed["rank"]["current_rank"] < initial["rank"]["current_rank"]
    assert changed["rank"]["rank_velocity"] > 0
    assert changed["score_velocity"] > 0
    assert changed["rank"]["cohort_size"] == 3


def test_cohort_change_does_not_claim_rank_acceleration():
    engine = PreIgnitionEngine()
    engine.update({"unknown": observation(strength=.2), "other": observation(strength=.8)}, {}, T)
    at = T + timedelta(minutes=1)
    values = {"unknown": observation(as_of=at, strength=.9),
        "other": observation(as_of=at, strength=.8), "new": observation(as_of=at)}
    result = row(engine.update(values, {}, at))
    assert result["rank"]["rank_velocity"] is None
    assert result["rank"]["rank_acceleration"] is None


def mirrored_observation(data):
    mirrored = copy.deepcopy(data)
    mirrored["price"] = 200 - data["price"]
    side = mirrored["directions"].pop("LONG")
    side["level"] = 200 - side["level"] if side["level"] is not None else None
    metrics = side["metrics"]
    metrics["lower_high_strength"] = metrics.pop("higher_low_strength")
    metrics["support_test_count"] = metrics.pop("resistance_test_count")
    metrics["taker_buy_ratio"] = 1 - metrics["taker_buy_ratio"]
    if metrics.get("live_taker_buy_ratio") is not None:
        metrics["live_taker_buy_ratio"] = 1 - metrics["live_taker_buy_ratio"]
    mirrored["directions"]["SHORT"] = side
    fast = mirrored["fast_features"]
    fast["higher_low_strength"], fast["lower_high_strength"] = (
        fast["lower_high_strength"], fast["higher_low_strength"])
    fast["taker_buy_ratio"] = 1 - fast["taker_buy_ratio"]
    for key in ("price_return_3m", "price_velocity", "price_acceleration",
                "price_acceleration_normalized", "vwap_distance_atr"):
        fast[key] = -fast[key] if fast.get(key) is not None else None
    for key in ("relative_return", "asset_return", "btc_return"):
        value = mirrored["btc_context"].get(key)
        if value is not None:
            mirrored["btc_context"][key] = -value
    mirrored["btc_context"]["regime"] = "BTC_DOWN"
    return mirrored


def test_mirrored_long_short_have_equal_families_scores_and_pretrigger_gates():
    data = observation(strength=.9)
    long_row = update(data)
    short_row = update(mirrored_observation(data), bias="SHORT")
    assert long_row["score"] == pytest.approx(short_row["score"])
    assert long_row["state"] == short_row["state"] == "READY"
    assert long_row["evidence_coverage"] == short_row["evidence_coverage"]
    for family in long_row["components"]:
        assert long_row["components"][family]["contribution"] == pytest.approx(
            short_row["components"][family]["contribution"])


@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
def test_live_native_flow_overrides_candle_flow_for_both_score_and_ready(bias):
    data = observation(strength=.9, flow=.1)
    data["fast_features"]["taker_buy_ratio"] = .1
    data["directions"]["LONG"]["metrics"]["live_taker_buy_ratio"] = .9
    if bias == "SHORT":
        data = mirrored_observation(data)
    result = update(data, bias=bias)
    assert result["state"] == "READY"
    assert result["directional_taker_ratio"] == pytest.approx(.9)
    assert "OBSERVED_AGGREGATE_TRADES" in result["native_flow_source"]
    assert result["components"]["native_order_flow"]["normalized"] > .5


@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
def test_opposing_live_flow_blocks_ready_despite_supportive_five_minute_candle(bias):
    data = observation(strength=.9, flow=.9)
    data["directions"]["LONG"]["metrics"]["live_taker_buy_ratio"] = .1
    if bias == "SHORT":
        data = mirrored_observation(data)
    result = update(data, bias=bias)
    assert result["state"] not in {"READY", "TRIGGER", "ACTIVE"}
    assert result["directional_taker_ratio"] == pytest.approx(.1)
    assert result["components"]["native_order_flow"]["normalized"] == 0


@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
def test_extension_progress_is_peak_based_and_retracement_cannot_reset_late_state(bias):
    engine = PreIgnitionEngine()
    data = observation(strength=.9)
    data["fast_features"].update(expected_expansion_long_pct=2,
        expected_expansion_short_pct=2, expansion_sample_count=30)
    data["fast_features"].pop("extension_baseline_price", None)
    data["fast_features"].pop("extension_baseline_timestamp", None)
    if bias == "SHORT":
        data = mirrored_observation(data)
    initial = update(data, engine=engine, bias=bias)
    sign = -1 if bias == "SHORT" else 1
    anchor = initial["price"]
    trigger = data["directions"][bias]["level"]
    discovered = data["directions"][bias]["level_discovered_at"]
    at = T + timedelta(minutes=1)
    advanced = copy.deepcopy(data)
    advanced["as_of"] = at.isoformat()
    advanced["fast_features"].update(evidence_timestamp=at.isoformat(),
        available_at=at.isoformat(), fingerprint="NEW_NATIVE_BAR")
    advanced["price"] = anchor * (1 + sign * .01)
    side = advanced["directions"][bias]
    side.update(level=trigger, level_discovered_at=discovered)
    late = update(advanced, engine=engine, at=at, bias=bias)
    assert late["move_consumed_pct"] is None
    assert late["move_consumed_estimate_pct"] == pytest.approx(50)
    assert late["extension_status"] == "EXTENDED"
    assert late["state"] == "EXHAUSTION"
    at += timedelta(minutes=1)
    retraced = copy.deepcopy(advanced)
    retraced["as_of"] = at.isoformat()
    retraced["fast_features"].update(evidence_timestamp=at.isoformat(),
        available_at=at.isoformat(), fingerprint="RETRACED_NATIVE_BAR")
    retraced["price"] = anchor * (1 + sign * .0005)
    result = update(retraced, engine=engine, at=at, bias=bias)
    assert result["candidate_anchor_price"] == anchor
    assert result["move_consumed_estimate_pct"] == late["move_consumed_estimate_pct"]
    assert result["state"] == "EXHAUSTION" and result["move_remaining"] == "LOW"
    assert result["fresh_structure_reset"] is False


def test_insufficient_extension_history_does_not_invent_eventual_move_consumed():
    result = update(observation())
    assert result["move_consumed_pct"] is None
    assert result["move_consumed_estimate_pct"] is None
    assert result["move_remaining"] == "UNKNOWN"
    assert result["extension_status"] == "UNAVAILABLE"


def five_minute_baseline(*, as_of=T, count=90):
    return [Candle(open_time=as_of - timedelta(minutes=(count - index) * 5),
        close_time=as_of - timedelta(minutes=(count - index - 1) * 5),
        available_at=as_of - timedelta(minutes=(count - index - 1) * 5),
        open=100, high=100.5, low=99.5, close=100, volume=100,
        source="TEST_ONLY_PRIOR_NATIVE_5M") for index in range(count)]


def test_expected_extension_uses_completed_prior_windows_and_excludes_active_comparison_region():
    frames = {"1m": bars(), "5m": five_minute_baseline()}
    original = causal_fast_features(frames, T)
    assert original["expansion_sample_count"] >= 20
    assert original["expected_expansion_long_pct"] == pytest.approx(.5)
    assert original["expected_expansion_short_pct"] == pytest.approx(.5)
    # The current comparison region cannot improve its own historical baseline.
    changed = copy.deepcopy(frames)
    changed["5m"][-3:] = [bar.model_copy(update={"high": 150, "low": 50})
        for bar in changed["5m"][-3:]]
    current = causal_fast_features(changed, T)
    assert current["expected_expansion_long_pct"] == original["expected_expansion_long_pct"]
    assert current["expected_expansion_short_pct"] == original["expected_expansion_short_pct"]
    assert current["expansion_sample_count"] == original["expansion_sample_count"]


def test_future_five_minute_winners_cannot_change_prior_expansion_baseline():
    frames = {"1m": bars(), "5m": five_minute_baseline()}
    expected = causal_fast_features(frames, T)
    frames["5m"].append(Candle(open_time=T, close_time=T + timedelta(minutes=5),
        available_at=T + timedelta(minutes=5), open=100, high=200, low=50, close=180,
        volume=10_000_000, source="TEST_ONLY_FUTURE_WINNER"))
    actual = causal_fast_features(frames, T)
    assert actual == expected


def test_native_trade_count_acceleration_requires_observed_counts():
    data = [bar.model_dump(mode="json") for bar in bars()]
    for index, native in enumerate(data):
        native["trade_count"] = 100 + index % 5
    for native, count in zip(data[-3:], (400, 700, 1000), strict=True):
        native["trade_count"] = count
    full = causal_fast_features(data, T)
    assert full["trade_count"] == 1000
    assert full["trade_count_acceleration"] > 1
    assert full["trade_count_acceleration_zscore"] > 0
    del data[-2]["trade_count"]
    partial = causal_fast_features(data, T)
    assert partial["trade_count"] == 1000
    assert partial["trade_count_acceleration"] is None
    assert partial["trade_count_acceleration_zscore"] is None


def test_fast_changing_lower_score_gets_priority_over_static_higher_score():
    engine = PreIgnitionEngine()
    engine.update({"changing": observation(strength=.05), "static": observation(strength=.95)}, {}, T)
    at = T + timedelta(minutes=1)
    result = engine.update({"changing": observation(as_of=at, strength=.75),
        "static": observation(as_of=at, strength=.95)}, {}, at)
    changing, static = row(result, "changing"), row(result, "static")
    assert changing["score"] < static["score"]
    assert changing["score_velocity"] > static["score_velocity"]
    assert changing["early_detection_priority"] > static["early_detection_priority"]


@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
def test_stale_observed_trade_snapshot_falls_back_to_fresh_native_candle_flow(bias):
    data = observation(strength=.9, flow=.1)
    data["fast_features"]["taker_buy_ratio"] = .1
    data["market_snapshot"] = {
        "as_of": (T - timedelta(minutes=5)).isoformat(),
        "order_flow_windows": {"1m": {"quality": "AVAILABLE",
            "taker_buy_volume": 900, "taker_sell_volume": 100}}}
    if bias == "SHORT":
        data = mirrored_observation(data)
        data["market_snapshot"]["order_flow_windows"]["1m"].update(
            taker_buy_volume=100, taker_sell_volume=900)
    result = update(data, bias=bias)
    assert result["directional_taker_ratio"] == pytest.approx(.1)
    assert "COMPLETED_5M" in result["native_flow_source"]
    assert result["state"] != "READY"


def test_cached_poll_keeps_original_velocity_instead_of_changing_elapsed_denominator():
    engine = PreIgnitionEngine()
    engine.update({"unknown": observation(strength=.1)}, {}, T)
    at = T + timedelta(minutes=1)
    data = observation(as_of=at, strength=.9)
    measured = update(data, engine=engine, at=at)
    assert measured["score_velocity"] > 0
    cached = update(data, engine=engine, at=at + timedelta(seconds=20))
    assert cached["score_velocity"] == measured["score_velocity"]
    assert cached["score_acceleration"] == measured["score_acceleration"]
    assert cached["rank"]["rank_velocity"] == measured["rank"]["rank_velocity"]


def test_restart_cannot_be_cleared_by_only_advancing_evaluation_clock():
    engine = PreIgnitionEngine()
    data = observation()
    engine.update({"unknown": data}, {}, T)
    restored = PreIgnitionEngine()
    restored.restore_state(json.loads(json.dumps(engine.export_state())))
    cached = copy.deepcopy(data)
    at = T + timedelta(seconds=1)
    cached["as_of"] = at.isoformat()
    result = update(cached, engine=restored, at=at)
    assert result["status"] == "STALE" and result["state"] == "NORMAL"


def test_first_observation_includes_recent_progress_instead_of_resetting_it_to_zero():
    data = observation(strength=.9)
    data["fast_features"].update(extension_baseline_price=data["price"] / 1.01,
        extension_baseline_timestamp=(T - timedelta(minutes=15)).isoformat(),
        expected_expansion_long_pct=2, expected_expansion_short_pct=2,
        expansion_sample_count=30)
    result = update(data)
    assert result["move_consumed_pct"] is None
    assert result["maximum_observed_progress_pct"] == pytest.approx(1)
    assert result["move_consumed_estimate_pct"] == pytest.approx(50)
    assert result["state"] == "EXHAUSTION"


@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
def test_pre_ignition_never_silently_creates_entry_confirmation(bias):
    data = observation(strength=.9)
    if bias == "SHORT":
        data = mirrored_observation(data)
    result = update(data, bias=bias)
    assert result["state"] == "READY"
    assert result["execution_enabled"] is False
    assert result["probability"] is None
    at = T + timedelta(seconds=10)
    data["as_of"] = at.isoformat()
    sign = -1 if bias == "SHORT" else 1
    data["price"] = data["directions"][bias]["level"] + sign * .01
    crossed = update(data, at=at, bias=bias)
    assert crossed["state"] not in {"READY", "TRIGGER", "ACTIVE"}


@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
def test_native_completed_one_minute_confirmation_remains_authoritative(bias):
    data = observation()
    if bias == "SHORT":
        data = mirrored_observation(data)
    side = data["directions"][bias]
    episode = {"asset_id": "unknown", "bias": bias, "episode_id": "KNOWN_NATIVE_EPISODE",
        "discovered_at": side["level_discovered_at"], "trigger": side["level"],
        "phase": "BREAK_CONFIRMED", "status": "ACTIVE", "confirmed_at": T.isoformat(),
        "last_1m": T.isoformat(), "invalidation": 98 if bias == "LONG" else 102,
        "targets": [103] if bias == "LONG" else [97]}
    result = row(PreIgnitionEngine().update({"unknown": data}, {"known": episode}, T), bias=bias)
    assert result["state"] == "TRIGGER"
    assert result["trigger"] == episode["trigger"]
    assert result["invalidation"] == episode["invalidation"]
    assert result["targets"] == episode["targets"]


@pytest.mark.parametrize("problem", ["native_gap", "native_stale", "aged_native_features"])
def test_cached_fresh_quality_cannot_override_critical_native_data_failure(problem):
    data = observation(strength=.9)
    if problem == "native_gap":
        data["fast_features"]["quality"] = "DATA_GAP"
    elif problem == "native_stale":
        data["fast_features"]["quality"] = "STALE"
    else:
        aged = (T - timedelta(minutes=5)).isoformat()
        data["fast_features"].update(evidence_timestamp=aged, available_at=aged)
    # A fresh evaluation timestamp and cached AVAILABLE flag do not restore
    # broken/stale native evidence or make it eligible for cross-market ranking.
    values = {"unknown": data, "other": observation(strength=.9)}
    result = row(PreIgnitionEngine().update(values, {}, T))
    assert result["status"] in {"STALE", "UNAVAILABLE"}
    assert result["state"] == "NORMAL"
    assert result["rank"]["current_rank"] is None
    assert result["early_detection_priority"] == 0

