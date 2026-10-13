"""Deterministic stage/scheduler contracts with explicitly synthetic evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.discovery_config import DiscoverySettings
from expansion_hunter.discovery_models import (
    DiscoveryFeature,
    DiscoveryObservation,
    DiscoveryScore,
    DiscoveryStage,
    Freshness,
)
from expansion_hunter.discovery_scheduler import DiscoveryScheduler, transition
from expansion_hunter.models import Asset

T = datetime(2026, 9, 1, tzinfo=UTC)


def feature(value, frame="15m", freshness=Freshness.FRESH, critical=False, when=T):
    return DiscoveryFeature(
        value=value, provider="test-only:synthetic", timeframe=frame,
        source_timestamp=when, retrieved_at=when, freshness=freshness, critical=critical,
    )


def candidate(identity="synthetic-alpha", light=70, when=T):
    return DiscoveryObservation(
        asset=Asset(asset_id=identity, symbol=identity.upper(), name="Synthetic fixture"),
        as_of=when, eligible=True, disposition="COMPLETE", freshness=Freshness.FRESH,
        features={"price": feature(100, critical=True, when=when)},
        scores={"light_discovery": DiscoveryScore(value=light, coverage=1)},
    )


def advanced(observation, energy=80, absorption=75, trigger=80, readiness=80):
    for key, value in (("stored_energy", energy), ("absorption", absorption),
                       ("trigger_sensitivity", trigger), ("expansion_readiness", readiness)):
        observation.scores[key] = DiscoveryScore(value=value, coverage=1)
    return observation


def test_unknown_advanced_scores_cannot_promote_loaded():
    observation, event = transition(candidate(), None, DiscoverySettings(), "synthetic-cycle")
    assert observation.stage == DiscoveryStage.WATCH
    assert event.event_type == DiscoveryStage.WATCH
    assert not event.high_priority
    assert event.details["live_trading"] is False


def test_score_driven_transitions_and_component_coverage():
    settings = DiscoverySettings()
    for score, state in ((40, DiscoveryStage.DORMANT), (60, DiscoveryStage.RADAR),
                         (70, DiscoveryStage.WATCH)):
        result, _ = transition(candidate(light=score), None, settings, "synthetic-cycle")
        assert result.stage == state
    pre = candidate()
    pre.scores["stored_energy"] = DiscoveryScore(value=70, coverage=1)
    assert transition(pre, None, settings, "synthetic-cycle")[0].stage == DiscoveryStage.PRE_IGNITION
    loaded = advanced(candidate())
    result, event = transition(loaded, None, settings, "synthetic-cycle")
    assert result.stage == DiscoveryStage.LOADED and event.high_priority
    loaded.scores["absorption"].coverage = 0.4
    assert transition(loaded, None, settings, "synthetic-cycle")[0].stage == DiscoveryStage.PRE_IGNITION


def test_rapid_rank_velocity_surfaces_before_score_threshold():
    observation = candidate(light=20)
    observation.rank.rank_velocity = 15
    observation.rank.velocity_percentile = 0.95
    result, event = transition(observation, None, DiscoverySettings(), "synthetic-cycle")
    assert result.stage == DiscoveryStage.RADAR
    assert "rank improvement" in event.reason


@pytest.mark.parametrize("state", [Freshness.STALE, Freshness.MISSING])
def test_stale_critical_data_demotes_and_suppresses_high_priority(state):
    previous = advanced(candidate())
    previous.stage = DiscoveryStage.LOADED
    current = advanced(candidate(when=T + timedelta(minutes=1)))
    current.features["price"].freshness = state
    result, event = transition(current, previous, DiscoverySettings(), "synthetic-cycle")
    assert result.stage == DiscoveryStage.DORMANT
    assert event is None
    assert any("expired" in reason for reason in result.reasons)


def test_ignition_requires_micro_observation_not_hourly_rvol_or_extended_price():
    observation = advanced(candidate())
    observation.scores["volume_awakening"] = DiscoveryScore(value=80, coverage=1)
    observation.features["rvol_1h"] = feature(5, frame="1h")
    observation.features["atr_distance"] = feature(1)
    settings = DiscoverySettings()
    assert transition(observation, None, settings, "synthetic-cycle")[0].stage == DiscoveryStage.LOADED
    observation.features["rvol_5m"] = feature(2.5, frame="5m")
    result, event = transition(observation, None, settings, "synthetic-cycle")
    assert result.stage == DiscoveryStage.IGNITION and event.high_priority
    observation.features["atr_distance"].value = 3
    assert transition(observation, None, settings, "synthetic-cycle")[0].stage == DiscoveryStage.LOADED
    observation.too_late = True
    assert transition(observation, None, settings, "synthetic-cycle")[0].stage == DiscoveryStage.WATCH


def test_hard_invalidation_uses_previously_known_support():
    previous = candidate()
    previous.stage = DiscoveryStage.WATCH
    previous.levels["support"] = 99
    current = candidate(when=T + timedelta(minutes=5))
    current.features["price"].value = 98
    current.levels["support"] = 95
    result, event = transition(current, previous, DiscoverySettings(), "synthetic-cycle")
    assert result.stage == DiscoveryStage.INVALIDATED
    assert event.event_type == DiscoveryStage.INVALIDATED
    assert "99" in event.reason
    # An expired snapshot alone cannot claim a real price invalidation.
    current.features["price"].freshness = Freshness.STALE
    assert transition(current, previous, DiscoverySettings(), "synthetic-cycle")[0].stage == DiscoveryStage.DORMANT


def test_demotions_and_invalidated_state_can_recover():
    previous = candidate()
    previous.stage = DiscoveryStage.WATCH
    result, _ = transition(candidate(light=10), previous, DiscoverySettings(), "synthetic-cycle")
    assert result.stage == DiscoveryStage.DORMANT
    assert any("demotion" in reason for reason in result.reasons)
    previous.stage = DiscoveryStage.INVALIDATED
    result, _ = transition(candidate(), previous, DiscoverySettings(), "synthetic-next")
    assert result.stage == DiscoveryStage.WATCH


def test_scheduler_respects_due_time_asset_and_weight_budgets_with_visible_deferral():
    scheduler = DiscoveryScheduler(DiscoverySettings())
    rows = [candidate("synthetic-" + str(i)) for i in range(4)]
    for row in rows:
        row.stage = DiscoveryStage.WATCH
        row.monitoring_due_at = T
    rows[3].monitoring_due_at = T + timedelta(minutes=1)
    selected = scheduler.due(rows, T, limit=2, weight_budget=100, cost_per_asset=60)
    assert [row.asset.asset_id for row in selected] == ["synthetic-0"]
    assert scheduler.used_weight == 60
    assert len(scheduler.deferred) == 2
    assert all(row["reason"] == "provider request-weight budget" for row in scheduler.deferred)


def test_fair_aging_prevents_permanent_low_priority_starvation():
    scheduler = DiscoveryScheduler(DiscoverySettings())
    dormant = candidate("synthetic-old")
    dormant.stage = DiscoveryStage.RADAR
    dormant.monitoring_due_at = T - timedelta(hours=2)
    ignition = candidate("synthetic-fast")
    ignition.stage = DiscoveryStage.IGNITION
    ignition.monitoring_due_at = T
    assert scheduler.due([ignition, dormant], T, 1, 80)[0].asset.asset_id == "synthetic-old"


def test_light_refresh_does_not_postpone_already_due_deep_analysis():
    previous = candidate()
    previous.stage = DiscoveryStage.WATCH
    previous.monitoring_due_at = T
    previous.last_deep_at = T - timedelta(minutes=5)
    current = candidate(when=T + timedelta(seconds=30))
    current.last_deep_at = previous.last_deep_at
    result, _ = transition(current, previous, DiscoverySettings(), "synthetic-cycle")
    assert result.monitoring_due_at == T
    # Successful analysis advances scheduling from its actual completion.
    current.deep_scanned = True
    current.last_deep_at = current.as_of
    result, _ = transition(current, previous, DiscoverySettings(), "synthetic-cycle")
    assert result.monitoring_due_at == current.as_of + timedelta(seconds=300)


def test_failed_deep_refresh_has_bounded_visible_backoff():
    previous = candidate()
    previous.stage = DiscoveryStage.WATCH
    current = candidate(when=T + timedelta(seconds=1))
    current.deep_scanned = True
    current.disposition = "FAILED"
    current.freshness = Freshness.MISSING
    result, _ = transition(current, previous, DiscoverySettings(), "synthetic-cycle")
    assert result.monitoring_due_at > current.as_of
    assert any("backoff" in reason for reason in result.reasons)
    scheduler = DiscoveryScheduler(DiscoverySettings())
    assert scheduler.due([result], current.as_of, 1, 100) == []


def test_event_is_emitted_once_per_stage_transition_and_identity_is_stable():
    row = candidate()
    first, event = transition(row, None, DiscoverySettings(), "synthetic-cycle")
    _, retried = transition(row, None, DiscoverySettings(), "synthetic-cycle")
    assert retried.event_id == event.event_id
    _, next_event = transition(candidate(), first, DiscoverySettings(), "synthetic-next")
    assert next_event is None


def test_future_critical_observation_is_rejected_for_promotion():
    row = advanced(candidate())
    row.features["price"].retrieved_at = T + timedelta(seconds=1)
    result, event = transition(row, None, DiscoverySettings(), "synthetic-cycle")
    assert result.stage == DiscoveryStage.DORMANT and event is None


def test_degraded_critical_data_caps_priority_and_budget_input_validation():
    row = advanced(candidate())
    row.features["price"].freshness = Freshness.DEGRADED
    assert transition(row, None, DiscoverySettings(), "synthetic-cycle")[0].stage == DiscoveryStage.WATCH
    scheduler = DiscoveryScheduler(DiscoverySettings())
    with pytest.raises(ValueError, match="positive"):
        scheduler.due([row], T, 1, 100, 0)
    with pytest.raises(ValueError, match="nonnegative"):
        scheduler.due([row], T, -1, 100)
