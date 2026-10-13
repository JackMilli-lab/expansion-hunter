"""Synthetic event-library protocol tests; no historical profitability claims."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from expansion_hunter.analog import FactorQuality, FactorValue
from expansion_hunter.event_learning import (
    EventLearningEngine,
    LearningSettings,
    ResearchEvent,
    calculate_event_outcome,
)
from expansion_hunter.fast_models import FastEvent, FastResistance, FastStage
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.macro_models import MacroMarketObservation, MacroQuality

START = datetime(2025, 1, 1, tzinfo=UTC)
SOURCE = "synthetic_test_only: event library"


def event(*, at=START, kind="LOADED", event_id="signal", asset_id="test-asset", price=100):
    return ResearchEvent(
        event_id=event_id,
        asset_id=asset_id,
        event_type=kind,
        event_timestamp=at,
        data_received_timestamp=at,
        decision_timestamp=at,
        available_at=at,
        reference_price=price,
        source=SOURCE,
        regime="RISK_ON",
        cycle="EARLY_BULL",
    )


def observations(*, start=START, count=5, asset_id="test-asset", expansion=True, baseline=False):
    output = []
    if baseline:
        output.append(
            MacroMarketObservation(
                asset_id=asset_id,
                market_event_timestamp=start,
                data_received_timestamp=start,
                interval_start_timestamp=start - timedelta(minutes=1),
                resolution_seconds=60,
                price=100,
                high=100.5,
                low=99.5,
                volume=100,
                source=SOURCE,
            )
        )
    previous = 100
    for index in range(count):
        price = 100 + (index + 1) * 4 if expansion else 100
        end = start + timedelta(minutes=index + 1)
        output.append(
            MacroMarketObservation(
                asset_id=asset_id,
                market_event_timestamp=end,
                data_received_timestamp=end,
                interval_start_timestamp=end - timedelta(minutes=1),
                resolution_seconds=60,
                price=price,
                high=max(previous, price) + 0.5,
                low=min(previous, price) - 0.5,
                volume=100,
                source=SOURCE,
            )
        )
        previous = price
    return output


def persist_history(store, history):
    for index, observation in enumerate(history):
        store.append(
            "market_observation",
            f"{observation.asset_id}:{observation.market_event_timestamp.isoformat()}:{index}",
            observation.market_event_timestamp,
            observation.data_received_timestamp,
            observation,
            observation.asset_id,
        )


@pytest.fixture
def store(tmp_path):
    instance = IntelligenceStore(str(tmp_path / "test.sqlite3"))
    yield instance
    instance.close()


def test_future_outcome_is_absent_before_maturity():
    result = calculate_event_outcome(
        event(),
        observations(),
        as_of=START + timedelta(minutes=4),
        horizon_seconds=300,
    )
    assert result is None


def test_matured_outcome_measures_extrema_and_closed_expansion():
    result = calculate_event_outcome(
        event(),
        observations(),
        as_of=START + timedelta(minutes=5),
        horizon_seconds=300,
    )
    assert result.status == "MATURED"
    assert result.future_close_return == pytest.approx(0.20)
    assert result.future_max_return == pytest.approx(0.205)
    assert result.future_min_return == pytest.approx(-0.005)
    assert result.mfe == pytest.approx(0.205)
    assert result.mae == pytest.approx(-0.005)
    assert result.time_to_expansion_seconds == 240
    assert result.mfe_interval_start == START + timedelta(minutes=4)
    assert result.mfe_interval_end == START + timedelta(minutes=5)
    assert result.expansion_threshold_hits["0.2"] is True
    assert result.available_at == START + timedelta(minutes=5)


def test_candle_extrema_do_not_invent_exact_subminute_times():
    result = calculate_event_outcome(
        event(),
        observations(),
        as_of=START + timedelta(minutes=5),
        horizon_seconds=300,
    )
    assert result.resolution_seconds == 60
    assert "time_to_mfe_seconds" not in result.model_dump()
    assert result.mfe_interval_start < result.mfe_interval_end


@pytest.mark.parametrize("defect", ["gap", "duplicate", "late", "stale", "mixed_source", "no_extrema"])
def test_bad_market_path_is_censored_not_a_failed_signal(defect):
    history = observations()
    if defect == "gap":
        del history[2]
    elif defect == "duplicate":
        history.append(history[0])
    elif defect == "late":
        history[2].data_received_timestamp = START + timedelta(hours=1)
    elif defect == "stale":
        history[2].data_quality = MacroQuality.STALE
    elif defect == "mixed_source":
        history[2].source = "different_provider"
    else:
        history[2].high = None
    result = calculate_event_outcome(
        event(),
        history,
        as_of=START + timedelta(minutes=5),
        horizon_seconds=300,
    )
    assert result.status == "CENSORED"
    assert result.mfe is None and result.false_positive is None


def test_append_future_observations_cannot_change_matured_short_horizon():
    original = calculate_event_outcome(
        event(),
        observations(),
        as_of=START + timedelta(minutes=5),
        horizon_seconds=300,
    )
    future = observations(start=START + timedelta(minutes=5), count=10)
    after = calculate_event_outcome(
        event(),
        [*observations(), *future],
        as_of=START + timedelta(minutes=5),
        horizon_seconds=300,
    )
    assert original == after


def test_future_features_and_impossible_timestamp_order_are_rejected():
    with pytest.raises(ValidationError, match="future outcomes"):
        ResearchEvent(
            **event().model_dump(exclude={"features"}),
            features={
                "future_return": FactorValue(
                    value=0.2,
                    event_timestamp=START,
                    available_at=START,
                    source=SOURCE,
                    quality=FactorQuality.DERIVED,
                )
            },
        )
    with pytest.raises(ValidationError, match="feature was unavailable"):
        ResearchEvent(
            **event().model_dump(exclude={"features"}),
            features={
                "rvol": FactorValue(
                    value=2,
                    event_timestamp=START,
                    available_at=START + timedelta(minutes=1),
                    source=SOURCE,
                    quality=FactorQuality.DERIVED,
                )
            },
        )
    with pytest.raises(ValidationError, match="ordering"):
        ResearchEvent(
            **event().model_dump(exclude={"available_at"}), available_at=START - timedelta(seconds=1)
        )


def test_false_positive_requires_complete_configured_mature_horizon():
    settings = LearningSettings(false_positive_horizon_seconds=300)
    positive = calculate_event_outcome(
        event(),
        observations(),
        as_of=START + timedelta(minutes=5),
        horizon_seconds=300,
        settings=settings,
    )
    negative = calculate_event_outcome(
        event(),
        observations(expansion=False),
        as_of=START + timedelta(minutes=5),
        horizon_seconds=300,
        settings=settings,
    )
    wrong_horizon = calculate_event_outcome(
        event(),
        observations(expansion=False),
        as_of=START + timedelta(minutes=5),
        horizon_seconds=60,
        settings=settings,
    )
    assert positive.false_positive is False
    assert negative.false_positive is True
    assert wrong_horizon.false_positive is None


def test_exact_return_threshold_is_not_lost_to_float_roundoff():
    history = observations(count=1)
    history[0].price = 120
    history[0].high = 120
    result = calculate_event_outcome(
        event(),
        history,
        as_of=START + timedelta(minutes=1),
        horizon_seconds=60,
        settings=LearningSettings(expansion_return=0.20),
    )
    assert result.time_to_expansion_seconds == 60
    assert result.expansion_threshold_hits["0.2"] is True


def test_failed_breakout_uses_frozen_known_resistance():
    signal = event(kind="1M_BREAKOUT_CONFIRMED", price=101)
    signal.features["resistance"] = FactorValue(
        value=100.5,
        event_timestamp=START - timedelta(minutes=5),
        available_at=START - timedelta(minutes=1),
        source=SOURCE,
        quality=FactorQuality.DERIVED,
    )
    result = calculate_event_outcome(
        signal,
        observations(expansion=False),
        as_of=START + timedelta(minutes=5),
        horizon_seconds=300,
    )
    assert result.failed_breakout is True
    assert result.time_to_failure_seconds == 60


def test_reference_price_zero_excursion_is_part_of_mfe_mae():
    result = calculate_event_outcome(
        event(price=200),
        observations(),
        as_of=START + timedelta(minutes=5),
        horizon_seconds=300,
    )
    assert result.future_max_return < 0
    assert result.mfe == 0
    assert result.mae < 0


def test_outcomes_are_immutable_separate_objects_and_survive_restart(store):
    settings = LearningSettings(horizons_seconds=[60, 300], expansion_window_seconds=300)
    engine = EventLearningEngine(store, settings)
    signal = event()
    assert engine.record_event(signal) is True
    assert engine.record_event(signal) is False
    persist_history(store, observations())
    result = engine.evaluate(START + timedelta(minutes=5))
    assert result["matured_outcomes"] == 2
    assert len(store.objects("research_event", START + timedelta(minutes=5))) == 1
    assert len(store.objects("research_outcome", START + timedelta(minutes=5))) == 2
    assert store.objects("research_outcome", START + timedelta(minutes=4)) == []
    again = engine.evaluate(START + timedelta(minutes=6))
    assert again["matured_outcomes"] == 0
    restored = IntelligenceStore(store.database_path)
    assert len(restored.objects("research_outcome", START + timedelta(minutes=6))) == 2
    restored.close()


def test_censored_outcomes_are_not_persisted_as_negative_evidence(store):
    settings = LearningSettings(horizons_seconds=[300], expansion_window_seconds=300)
    engine = EventLearningEngine(store, settings)
    engine.record_event(event())
    result = engine.evaluate(START + timedelta(minutes=5))
    assert result["censored_outcomes"] == 1
    assert store.objects("research_outcome", START + timedelta(minutes=5)) == []


def test_censored_old_events_do_not_starve_newer_signals_under_budget(store):
    settings = LearningSettings(
        horizons_seconds=[60], expansion_window_seconds=300, maximum_events_per_pass=1
    )
    engine = EventLearningEngine(store, settings)
    engine.record_event(event(event_id="old-missing"))
    engine.record_event(event(at=START + timedelta(minutes=1), event_id="new-complete"))
    persist_history(store, observations(start=START + timedelta(minutes=1), count=1))
    first = engine.evaluate(START + timedelta(minutes=2))
    assert first["censored_outcomes"] == 1
    assert first["events_deferred"] == 1
    second = engine.evaluate(START + timedelta(minutes=2, seconds=1))
    assert second["matured_outcomes"] == 1
    assert store.objects("research_outcome", START + timedelta(minutes=3))[0]["event_id"] == "new-complete"


def test_observed_expansion_without_any_alert_is_a_full_miss(store):
    settings = LearningSettings(horizons_seconds=[300], expansion_window_seconds=300)
    engine = EventLearningEngine(store, settings)
    persist_history(store, observations(baseline=True))
    result = engine.evaluate(START + timedelta(minutes=5))
    assert result["misses_recorded"] == 1
    assert result["expansions_recorded"] == 1
    miss = result["misses"][0]
    assert miss["kind"] == "FULL_MISS"
    assert miss["cause_classification"] == "UNKNOWN"
    assert miss["pre_move_rank"] is None
    assert miss["pre_move_features"] == {}
    assert miss["first_system_detection"] is None
    assert miss["coverage_scope"] == "MONITORED_ARCHIVED_ONLY"


def test_miss_records_actual_late_alert_and_negative_lead_without_retro_promotion(store):
    settings = LearningSettings(horizons_seconds=[300], expansion_window_seconds=300)
    engine = EventLearningEngine(store, settings)
    persist_history(store, observations(baseline=True))
    engine.record_event(event(at=START + timedelta(minutes=4), kind="LOADED"))
    result = engine.evaluate(START + timedelta(minutes=5))
    miss = result["misses"][0]
    assert miss["kind"] == "FULL_MISS"
    assert miss["lead_times_seconds"]["LOADED"] == -120
    assert miss["pre_move_event_ids"] == []
    assert len(store.objects("research_event", START + timedelta(minutes=5))) == 1


def test_warning_plus_timely_trigger_prevents_miss_but_still_tracks_expansion(store):
    settings = LearningSettings(horizons_seconds=[300], expansion_window_seconds=300)
    engine = EventLearningEngine(store, settings)
    persist_history(store, observations(baseline=True))
    engine.record_event(event(kind="LOADED", event_id="early"))
    engine.record_event(
        event(at=START + timedelta(minutes=2), kind="1M_BREAKOUT_CONFIRMED", event_id="trigger")
    )
    result = engine.evaluate(START + timedelta(minutes=5))
    assert result["misses_recorded"] == 0
    assert result["expansions_recorded"] == 1
    assert len(store.objects("expansion_event", START + timedelta(minutes=5))) == 1
    report = engine.daily_report(START + timedelta(minutes=5))
    assert report["expansions_today"] == 1
    assert report["mean_available_alert_lead_seconds"] is not None
    assert report["mean_lead_time_by_stage"]["LOADED"] == 120


def test_early_warning_without_trigger_is_a_trigger_miss(store):
    settings = LearningSettings(horizons_seconds=[300], expansion_window_seconds=300)
    engine = EventLearningEngine(store, settings)
    persist_history(store, observations(baseline=True))
    engine.record_event(event(kind="WATCH"))
    result = engine.evaluate(START + timedelta(minutes=5))
    assert result["misses"][0]["kind"] == "TRIGGER_MISS"
    assert result["misses"][0]["lead_times_seconds"]["WATCH"] == 120


def test_missing_pre_observations_prevent_invented_miss(store):
    settings = LearningSettings(horizons_seconds=[300], expansion_window_seconds=300)
    engine = EventLearningEngine(store, settings)
    history = observations(baseline=True)
    del history[2]
    persist_history(store, history)
    result = engine.evaluate(START + timedelta(minutes=5))
    assert result["misses_recorded"] == 0
    assert any("coverage incomplete" in issue for issue in result["issues"])


def test_expansion_episode_and_miss_dedupe_survive_restart(store):
    settings = LearningSettings(horizons_seconds=[300], expansion_window_seconds=300)
    engine = EventLearningEngine(store, settings)
    persist_history(store, observations(baseline=True))
    first = engine.evaluate(START + timedelta(minutes=5))
    assert first["misses_recorded"] == 1
    restored = IntelligenceStore(store.database_path)
    second = EventLearningEngine(restored, settings).evaluate(START + timedelta(minutes=6))
    assert second["misses_recorded"] == 0
    assert second["expansions_recorded"] == 0
    assert len(restored.objects("miss_event", START + timedelta(minutes=6))) == 1
    restored.close()


def test_false_positive_daily_report_uses_only_mature_known_outcomes(store):
    settings = LearningSettings(
        horizons_seconds=[300],
        expansion_window_seconds=300,
        false_positive_horizon_seconds=300,
    )
    engine = EventLearningEngine(store, settings)
    engine.record_event(event(kind="LOADED"))
    engine.record_event(event(at=START + timedelta(minutes=4), kind="LOADED", event_id="not-mature"))
    persist_history(store, observations(expansion=False, baseline=True))
    engine.evaluate(START + timedelta(minutes=5))
    report = engine.daily_report(START + timedelta(minutes=5))
    assert report["known_mature_false_positive_observations"] == 1
    assert report["false_positive_rate"] == 1
    assert report["event_counts"]["LOADED"] == 2
    assert report["regime_counts"]["RISK_ON"] == 2
    assert report["live_trading"] is False
    assert report["regime_conditioned_outcomes"]["RISK_ON"]["horizons"]["300"]["sample_size"] == 1


def test_fast_events_reuse_actual_timestamps_and_frozen_resistance(store):
    resistance = FastResistance(
        price=100,
        lower=99.9,
        upper=100.1,
        event_timestamp=START - timedelta(minutes=10),
        discovered_at=START - timedelta(minutes=5),
        atr=1,
        kind="SYNTHETIC_PRIOR_LEVEL",
        tests=3,
        reason="synthetic known resistance",
    )
    fast = FastEvent(
        event_id="fast",
        asset_id="test-asset",
        symbol="TESTUSDT",
        event_type="1M_BREAKOUT_CONFIRMED",
        previous_stage=FastStage.BREAKOUT_ATTEMPT,
        market_event_timestamp=START,
        data_received_timestamp=START + timedelta(milliseconds=50),
        evaluation_timestamp=START + timedelta(milliseconds=55),
        alert_created_timestamp=START + timedelta(milliseconds=56),
        detection_latency_ms=55,
        processing_latency_ms=5,
        alert_latency_ms=56,
        price=100.12,
        resistance=resistance,
        reason="synthetic completed close",
        regime="RISK_ON",
        metrics={"relative_volume": 2, "missing_order_flow": None},
        data_quality={"spot_candles": "AVAILABLE", "futures": "UNAVAILABLE"},
    )
    recorded = EventLearningEngine(store).record_fast_event(fast, {"cycle": "EARLY_BULL"})
    assert recorded.event_timestamp == fast.market_event_timestamp
    assert recorded.available_at == fast.alert_created_timestamp
    assert recorded.features["resistance"].available_at == resistance.discovered_at
    assert "missing_order_flow" not in recorded.features
    assert recorded.cycle == "EARLY_BULL"
    assert len(store.objects("research_event", START + timedelta(seconds=1))) == 1


def test_unaligned_intraminute_event_is_censored_without_fake_ticks():
    signal = event(at=START + timedelta(seconds=20))
    result = calculate_event_outcome(
        signal,
        observations(count=6),
        as_of=START + timedelta(minutes=6),
        horizon_seconds=300,
    )
    assert result.status == "CENSORED"
    assert result.mfe is None


def test_naive_timestamps_and_invalid_horizons_fail_closed():
    with pytest.raises(ValidationError, match="timezone"):
        event(at=datetime(2025, 1, 1))
    with pytest.raises(ValidationError, match="full-minute"):
        LearningSettings(horizons_seconds=[10])
    with pytest.raises(ValueError, match="full-minute"):
        calculate_event_outcome(
            event(), observations(), as_of=START + timedelta(minutes=5), horizon_seconds=10
        )
