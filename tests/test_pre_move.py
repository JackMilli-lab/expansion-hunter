"""Synthetic observation tests; lifecycle/rank rules are not edge evidence."""
import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_fast import ASSET, NOW
from test_trade_alert import histories

from expansion_hunter.pre_move import PreMoveRanker, PreMoveSettings
from expansion_hunter.trade_alert import TradeAlertEngine
from expansion_hunter.trade_alert_models import TradeAlertSettings

T = datetime(2026, 10, 8, 12, tzinfo=UTC)


def observation(score=70, *, now=T, distance=.3, flow=.8, compression=.5, bias="LONG"):
    price = 100
    level = price + distance if bias == "LONG" else price-distance
    return {"symbol": "UNKNOWNUSDT", "as_of": now.isoformat(), "status": "EVALUATED", "price": price,
        "data_quality": {"1m": "AVAILABLE", "5m": "AVAILABLE", "futures": "UNAVAILABLE"},
        "directions": {bias: {"score": score, "distance_atr": distance, "level": level,
            "level_discovered_at": now.isoformat(),
            "components": {"compression": {"weight": .15, "normalized": compression}},
            "metrics": {"range_compression": compression,
                "lower_high_strength" if bias == "SHORT" else "higher_low_strength": .4,
                "support_test_count" if bias == "SHORT" else "resistance_test_count": 3,
                "taker_buy_ratio": 1-flow if bias == "SHORT" and flow is not None else flow}}}}


def select(rows, asset_id="unknown", bias="LONG"):
    return next(row for row in rows if row["asset_id"] == asset_id and row["bias"] == bias)


def cohort(scores, now=T, **values):
    return {asset: observation(score, now=now, **values) for asset, score in scores.items()}


@pytest.mark.parametrize("score,state", [(10, "NORMAL"), (25, "RADAR"), (40, "WATCH"),
                                        (55, "PRE_IGNITION"), (65, "LOADED")])
@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
def test_explicit_direction_neutral_pretrigger_stages(score, state, bias):
    data = {"unknown": observation(score, bias=bias)}
    before = copy.deepcopy(data)
    row = select(PreMoveRanker().update(data, {}, T), bias=bias)
    assert row["state"] == state and row["status"] == "AVAILABLE"
    assert row["score"] == score and row["probability"] is None
    assert row["rank"]["current_rank"] is None
    assert data == before and row["execution_enabled"] is False


@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
def test_imminent_is_volatility_aware_and_not_a_breakout(bias):
    row = select(PreMoveRanker().update({"unknown": observation(distance=.19, bias=bias)}, {}, T), bias=bias)
    assert row["state"] == "TRIGGER_IMMINENT"
    assert all("condition" in step for step in row["conditional_next_steps"])
    assert len(row["conditional_next_steps"]) == 3
    assert not any("open" in step or "high" in step or "low" in step or "close" in step
                   for step in row["conditional_next_steps"])
    assert row["invalidation"] is None and row["targets"] == []


@pytest.mark.parametrize("problem", ["far", "uncompressed", "unknown_flow", "crossed", "price_crossed_cache"])
def test_advanced_stage_requires_pretrigger_structural_and_native_flow_evidence(problem):
    value = observation(distance=2 if problem == "far" else -.1 if problem == "crossed" else .1,
                        compression=.1 if problem == "uncompressed" else .5,
                        flow=None if problem == "unknown_flow" else .8)
    if problem == "price_crossed_cache":
        value["price"] = 101
    row = select(PreMoveRanker().update({"unknown": value}, {}, T))
    assert row["state"] == ("RADAR" if problem == "unknown_flow" else "NORMAL")
    assert row["state"] not in {"TRIGGER", "ACTIVE"}


@pytest.mark.parametrize("problem", ["stale", "future", "unknown_quality", "future_trigger"])
def test_stale_and_future_inputs_cannot_rank_or_alert(problem):
    value = observation(now=T-timedelta(seconds=91) if problem == "stale" else
                        T+timedelta(seconds=1) if problem == "future" else T)
    if problem == "unknown_quality":
        value["data_quality"]["1m"] = "UNAVAILABLE"
    if problem == "future_trigger":
        value["directions"]["LONG"]["level_discovered_at"] = (T+timedelta(seconds=1)).isoformat()
    row = select(PreMoveRanker().update({"unknown": value, "other": observation()}, {}, T))
    assert row["state"] == "NORMAL" and row["status"] in {"STALE", "UNAVAILABLE"}
    assert row["rank"]["current_rank"] is None


def test_rank_migration_uses_actual_minutes_and_positive_improvement():
    ranker = PreMoveRanker()
    first = select(ranker.update(cohort({"unknown": 10, "a": 20, "b": 30, "c": 40}), {}, T))
    assert first["rank"]["current_rank"] == 4 and first["rank"]["rank_velocity"] is None
    second = select(ranker.update(cohort({"unknown": 25, "a": 20, "b": 30, "c": 40}, T+timedelta(minutes=1)), {}, T+timedelta(minutes=1)))
    assert second["rank"]["previous_rank"] == 4 and second["rank"]["rank_delta"] == 1
    assert second["rank"]["rank_velocity"] == 1 and second["rank"]["rank_acceleration"] is None
    third = select(ranker.update(cohort({"unknown": 45, "a": 20, "b": 30, "c": 40}, T+timedelta(minutes=2)), {}, T+timedelta(minutes=2)))
    assert third["rank"]["current_rank"] == 1 and third["rank"]["rank_velocity"] == 2
    assert third["rank"]["rank_acceleration"] == 1


def test_rank_velocity_can_surface_below_watch_asset_without_boosting_its_score():
    ranker = PreMoveRanker()
    ranker.update(cohort({"unknown": 5, "a": 10, "b": 15}), {}, T)
    now = T+timedelta(minutes=1)
    row = select(ranker.update(cohort({"unknown": 20, "a": 10, "b": 15}, now), {}, now))
    assert row["state"] == "RADAR" and row["score"] == 20
    assert row["rank"]["rank_velocity"] == 2 and "rank improvement" in row["reason"]


@pytest.mark.parametrize("problem", ["cohort", "evidence", "gap", "unchanged"])
def test_rank_comparability_is_withheld_instead_of_fabricating_zero(problem):
    ranker = PreMoveRanker()
    values = cohort({"unknown": 20, "other": 30})
    ranker.update(values, {}, T)
    now = T+timedelta(minutes=4 if problem == "gap" else 1)
    after = values if problem == "unchanged" else cohort({"unknown": 40, "other": 30}, now)
    if problem == "cohort":
        after["new"] = observation(now=now)
    if problem == "evidence":
        after["unknown"]["directions"]["LONG"]["components"]["compression"]["normalized"] = None
    row = select(ranker.update(after, {}, now))
    assert row["rank"]["rank_velocity"] is None and row["rank"]["rank_acceleration"] is None
    assert row["rank"]["previous_rank"] is None


def test_long_short_ranks_are_separate_and_ties_get_midrank():
    values = cohort({"unknown": 20, "other": 20})
    for asset, value in values.items():
        value["directions"].update(observation(90 if asset == "unknown" else 30, bias="SHORT")["directions"])
    rows = PreMoveRanker().update(values, {}, T)
    assert select(rows)["rank"]["current_rank"] == 1.5
    assert select(rows, bias="SHORT")["rank"]["current_rank"] == 1
    assert select(rows, "other", "SHORT")["rank"]["current_rank"] == 2


def test_cached_polling_does_not_manufacture_history_and_checkpoint_is_compact():
    ranker = PreMoveRanker()
    data = cohort({"unknown": 20, "other": 30})
    ranker.update(data, {}, T)
    for seconds in (1, 2, 61):
        row = select(ranker.update(data, {}, T+timedelta(seconds=seconds)))
        assert row["rank"]["rank_velocity"] is None
    assert len(ranker.export_state()["samples"]) == 1
    for minutes in range(2, 7):
        now = T+timedelta(minutes=minutes)
        ranker.update(cohort({"unknown": 20, "other": 30}, now), {}, now)
    assert len(ranker.checkpoint_state()["samples"]) == 3
    assert isinstance(ranker.checkpoint_state()["samples"][0]["signatures"]["LONG"], str)


def test_restart_requires_new_observations_and_preserves_comparable_migration():
    ranker = PreMoveRanker()
    ranker.update(cohort({"unknown": 20, "other": 30}), {}, T)
    data = cohort({"unknown": 40, "other": 30}, T+timedelta(minutes=1))
    ranker.update(data, {}, T+timedelta(minutes=1))
    restored = PreMoveRanker()
    restored.restore_state(json.loads(json.dumps(ranker.checkpoint_state())))
    stale = select(restored.update(data, {}, T+timedelta(minutes=1, seconds=1)))
    assert stale["status"] == "STALE" and stale["state"] == "NORMAL"
    now = T+timedelta(minutes=2)
    new = select(restored.update(cohort({"unknown": 40, "other": 30}, now), {}, now))
    assert new["status"] == "AVAILABLE" and new["state"] == "WATCH"
    assert new["rank"]["previous_rank"] == 1 and new["rank"]["rank_velocity"] == 0
    # Zero is valid only because two genuinely new comparable observations tied.
    assert new["rank"]["rank_acceleration"] == -1


@pytest.mark.parametrize("state", ["TRIGGER", "ACTIVE", "FAILED", "EXHAUSTION", "STALE", "FUTURE"])
def test_native_episode_authority_preserves_confirmation_failure_and_restart(state):
    value = observation()
    episode = {"asset_id": "unknown", "bias": "LONG", "episode_id": "known-native",
        "discovered_at": (T-timedelta(minutes=1)).isoformat(), "trigger": 100,
        "phase": "BREAK_CONFIRMED", "status": "ACTIVE", "invalidation": 98, "targets": [101, 102],
        "confirmed_at": T.isoformat(), "last_1m": T.isoformat()}
    if state == "ACTIVE":
        episode["confirmed_at"] = (T-timedelta(minutes=1)).isoformat()
    elif state == "FAILED":
        episode.update(phase="INVALIDATED", status="INVALIDATED")
    elif state == "EXHAUSTION":
        episode["status"] = "LATE"
    elif state == "STALE":
        episode["restart_stale"] = True
    elif state == "FUTURE":
        episode["confirmed_at"] = (T+timedelta(minutes=1)).isoformat()
    row = select(PreMoveRanker().update({"unknown": value}, {"episodes": {"known": episode}}, T))
    assert row["state"] == ("NORMAL" if state in {"STALE", "FUTURE"} else state)
    assert row["status"] == ("STALE" if state == "STALE" else "UNAVAILABLE" if state == "FUTURE" else "AVAILABLE")
    assert row["invalidation"] == 98 and row["targets"] == [101, 102]


def test_production_engine_observation_is_reused_without_threshold_changes():
    settings = TradeAlertSettings()
    engine = TradeAlertEngine(settings)
    engine.evaluate(ASSET, histories(), NOW, NOW, 99.9, histories()["5m"])
    before = engine.settings.model_dump()
    row = select(PreMoveRanker().update(engine.observations, engine.episodes, NOW), ASSET.asset_id)
    assert row["score"] == engine.observations[ASSET.asset_id]["directions"]["LONG"]["score"]
    assert engine.settings.model_dump() == before


def test_active_native_trigger_remains_frozen_when_pressure_candidate_changes():
    value = observation(distance=.1)
    episode = {"asset_id": "unknown", "bias": "LONG", "discovered_at": T.isoformat(),
               "trigger": 99.9, "atr": 1, "status": "ACTIVE", "phase": "TRADE_ALERT"}
    row = select(PreMoveRanker().update({"unknown": value}, {"known": episode}, T))
    assert row["trigger"] == 99.9 and row["distance_atr"] == pytest.approx(-.1)
    assert row["state"] == "NORMAL" and "completed-1m" in row["reason"]


def test_native_confirmation_survives_absent_new_pressure_candidate_without_fake_score():
    value = observation()
    value["directions"] = {}
    episode = {"asset_id": "unknown", "bias": "LONG", "discovered_at": T.isoformat(),
        "trigger": 100, "status": "ACTIVE", "phase": "BREAK_CONFIRMED", "confirmed_at": T.isoformat(),
        "last_1m": T.isoformat(), "setup_type": "LONG_REVERSAL", "score": None}
    row = select(PreMoveRanker().update({"unknown": value}, {"known": episode}, T))
    assert row["state"] == "TRIGGER" and row["status"] == "AVAILABLE"
    assert row["score"] is None and row["rank"]["current_rank"] is None


def test_decision_clock_cannot_move_backwards():
    ranker = PreMoveRanker()
    ranker.update({}, {}, T)
    with pytest.raises(ValueError, match="out-of-order"):
        ranker.update({}, {}, T-timedelta(seconds=1))


def test_configuration_is_explicit_and_does_not_lower_production_thresholds():
    settings = PreMoveSettings(loaded_score=80, minimum_compression=.6)
    row = select(PreMoveRanker(settings).update({"unknown": observation(distance=.1)}, {}, T))
    assert row["state"] == "NORMAL"
    assert settings.loaded_score == 80 and settings.minimum_compression == .6
    with pytest.raises(ValueError, match="ordered"):
        PreMoveSettings(watch_score=90)
