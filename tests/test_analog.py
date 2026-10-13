"""Synthetic analog protocol tests are infrastructure evidence, not an edge."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from expansion_hunter.analog import (
    AnalogSettings,
    AnalogVector,
    FactorQuality,
    FactorValue,
    attach_outcomes,
    benchmark_metrics,
    match_analogs,
)
from expansion_hunter.models import Candle

NOW = datetime(2025, 1, 20, tzinfo=UTC)
SOURCE = "synthetic_test_only: analog protocol"


def factor(value, timestamp, *, unit="fraction", quality=FactorQuality.DERIVED, available_at=None):
    return FactorValue(
        value=value,
        event_timestamp=timestamp,
        available_at=available_at or timestamp,
        source=SOURCE,
        unit=unit,
        quality=quality,
    )


def vector(index, *, decision_time=None, values=None, asset_id=None):
    decision_time = decision_time or NOW - timedelta(days=10 - index)
    values = values or [index / 10, index / 10 + 0.1, index / 10 + 0.2]
    return AnalogVector(
        event_id=f"event-{index}",
        asset_id=asset_id or f"asset-{index}",
        decision_time=decision_time,
        available_at=decision_time,
        factors={
            name: factor(value, decision_time) for name, value in zip(("a", "b", "c"), values, strict=True)
        },
        reference_price=factor(100, decision_time, unit="USDT", quality=FactorQuality.NATIVE),
        regime="RISK_ON",
        cycle="EARLY_BULL",
    )


def setup():
    query = vector(99, decision_time=NOW, values=[0.25, 0.35, 0.45])
    references = [vector(index) for index in range(4)]
    settings = AnalogSettings(feature_names=["a", "b", "c"])
    return query, references, settings


def bars(start, *, count=5, winner=True):
    output = []
    previous = 100
    for index in range(count):
        close = 100 + index + 1 if winner else 100 - index - 1
        opened = start + timedelta(minutes=index)
        output.append(
            Candle(
                open_time=opened,
                close_time=opened + timedelta(minutes=1),
                available_at=opened + timedelta(minutes=1),
                open=previous,
                high=max(previous, close) + 0.5,
                low=min(previous, close) - 0.5,
                close=close,
                volume=100,
                source=SOURCE,
            )
        )
        previous = close
    return output


def test_matching_is_deterministic_under_reference_reordering():
    query, references, settings = setup()
    first = match_analogs(query, references, settings)
    second = match_analogs(query, list(reversed(references)), settings)
    assert first.model_dump() == second.model_dump()
    assert first.status == "MATCHED"
    assert first.matches[0].event_id in ("event-2", "event-3")
    assert first.similarity_is_probability is False
    assert first.label == "HISTORICAL RESEARCH — NOT FORECAST"


@pytest.mark.parametrize("metric", ["euclidean", "cosine"])
def test_both_predeclared_metrics_are_executable(metric):
    query, references, settings = setup()
    report = match_analogs(query, references, settings.model_copy(update={"metric": metric}))
    assert report.status == "MATCHED"
    assert all(0 <= match.similarity <= 1 for match in report.matches)
    assert all(match.distance >= 0 for match in report.matches)


def test_future_reference_cannot_change_normalization_or_matches():
    query, references, settings = setup()
    before = match_analogs(query, references, settings)
    future = vector(5, decision_time=NOW + timedelta(hours=1), values=[1e9, 1e9, 1e9])
    after = match_analogs(query, [*references, future], settings)
    assert before.normalizer == after.normalizer
    assert before.matches == after.matches
    assert "reference decision is not before query" in after.excluded[future.event_id]


def test_reference_recorded_today_is_not_old_point_in_time_evidence():
    query, references, settings = setup()
    late = references[0].model_copy(update={"available_at": NOW + timedelta(seconds=1)})
    report = match_analogs(query, [late, *references[1:]], settings)
    assert late.event_id in report.excluded
    assert all(late.event_id not in item.reference_event_ids for item in report.normalizer.values())


def test_reference_features_must_exist_at_own_decision_not_just_query():
    query, references, settings = setup()
    references[0].factors["a"].available_at += timedelta(minutes=1)
    report = match_analogs(query, references, settings)
    assert "event-0" in report.excluded
    assert "event-0" not in report.normalizer["a"].reference_event_ids


def test_query_future_factor_never_enters_matching():
    query, references, settings = setup()
    query.factors["a"].available_at = NOW + timedelta(seconds=1)
    report = match_analogs(query, references, settings)
    assert report.status == "INSUFFICIENT_DATA"
    assert "a" not in report.normalizer
    assert not report.matches


def test_normalizer_does_not_fit_the_query_values():
    query, references, settings = setup()
    before = match_analogs(query, references, settings)
    query.factors["a"].value = 1e10
    after = match_analogs(query, references, settings)
    assert before.normalizer == after.normalizer
    assert before.normalized_query != after.normalized_query


@pytest.mark.parametrize("normalization", ["zscore", "robust_zscore"])
def test_normalization_exposes_fit_provenance(normalization):
    query, references, settings = setup()
    report = match_analogs(query, references, settings.model_copy(update={"normalization": normalization}))
    item = report.normalizer["a"]
    assert item.center == pytest.approx(0.15)
    assert item.sample_count == 4
    assert item.latest_available_at < query.decision_time
    assert item.reference_event_ids == ["event-0", "event-1", "event-2", "event-3"]


def test_constant_features_are_excluded_not_given_fake_unit_scale():
    query, references, settings = setup()
    for reference in references:
        reference.factors["a"].value = 0.5
    report = match_analogs(query, references, settings)
    assert "a" not in report.normalizer
    assert report.status == "INSUFFICIENT_DATA"
    assert any("zero-scale" in warning for warning in report.warnings)


def test_stale_missing_and_estimated_features_are_not_zero_filled():
    query, references, settings = setup()
    query.factors["a"].quality = FactorQuality.STALE
    query.factors["b"].quality = FactorQuality.ESTIMATED
    del query.factors["c"]
    report = match_analogs(query, references, settings)
    assert report.normalized_query == {}
    assert report.status == "INSUFFICIENT_DATA"


def test_estimates_require_explicit_research_opt_in():
    query, references, settings = setup()
    query.factors["a"].quality = FactorQuality.ESTIMATED
    report = match_analogs(query, references, settings.model_copy(update={"allow_estimates": True}))
    assert report.status == "MATCHED"


def test_unit_incompatibility_cannot_be_normalized_away():
    query, references, settings = setup()
    for reference in references:
        reference.factors["a"].unit = "USD"
    report = match_analogs(query, references, settings)
    assert "a" not in report.normalizer
    assert report.status == "INSUFFICIENT_DATA"


def test_regime_and_cycle_filters_apply_before_fit():
    query, references, settings = setup()
    references[0].regime = "RISK_OFF"
    references[1].cycle = "EARLY_BEAR"
    report = match_analogs(
        query, references, settings.model_copy(update={"same_regime": True, "same_cycle": True})
    )
    assert "event-0" in report.excluded
    assert "event-1" in report.excluded
    assert report.status == "INSUFFICIENT_DATA"


def test_same_asset_repeated_events_do_not_become_independent_matches():
    query, references, settings = setup()
    for reference in references:
        reference.asset_id = "same-asset"
    report = match_analogs(query, references, settings)
    assert len(report.matches) == 1
    assert len(report.excluded) == 3


def test_duplicate_event_ids_and_future_label_feature_names_fail_closed():
    query, references, settings = setup()
    with pytest.raises(ValueError, match="duplicate reference"):
        match_analogs(query, [*references, references[0]], settings)
    with pytest.raises(ValidationError, match="future outcome labels"):
        AnalogSettings(feature_names=["future_return_24h"])
    with pytest.raises(ValidationError, match="Extra inputs"):
        AnalogVector(**query.model_dump(), future_return=0.2)


def test_matching_cannot_consume_future_outcomes_and_outcomes_cannot_change_matching():
    query, references, settings = setup()
    report = match_analogs(query, references, settings)
    original = report.model_dump()
    histories = {match.event_id: bars(match.decision_time) for match in report.matches}
    good = attach_outcomes(report, histories, as_of=NOW, horizons_seconds=(300,))
    bad = attach_outcomes(
        report,
        {match.event_id: bars(match.decision_time, winner=False) for match in report.matches},
        as_of=NOW,
        horizons_seconds=(300,),
    )
    assert report.model_dump() == original
    assert all(outcome.status == "MATURED" for outcome in good)
    assert all(outcome.close_return > 0 for outcome in good)
    assert all(outcome.close_return < 0 for outcome in bad)


def test_outcomes_have_correct_mfe_mae_and_mature_timestamp():
    query, references, settings = setup()
    report = match_analogs(query, references, settings)
    histories = {match.event_id: bars(match.decision_time) for match in report.matches}
    outcomes = attach_outcomes(report, histories, as_of=NOW, horizons_seconds=(300,))
    first = outcomes[0]
    assert first.close_return == pytest.approx(0.05)
    assert first.mfe == pytest.approx(0.055)
    assert first.mae == pytest.approx(-0.005)
    assert first.available_at == report.matches[0].decision_time + timedelta(minutes=5)
    assert first.expansion_threshold_hits["0.1"] is False


def test_future_horizon_is_pending_not_failed():
    query, references, settings = setup()
    report = match_analogs(query, references, settings)
    outcomes = attach_outcomes(report, {}, as_of=NOW, horizons_seconds=(30 * 86400,))
    assert all(outcome.status == "PENDING" for outcome in outcomes)
    assert all(outcome.mfe is None for outcome in outcomes)


@pytest.mark.parametrize("failure", ["gap", "duplicate", "late", "mixed_source", "empty"])
def test_invalid_outcome_data_is_censored_not_counted_as_failure(failure):
    query, references, settings = setup()
    report = match_analogs(query, references, settings)
    histories = {match.event_id: bars(match.decision_time) for match in report.matches}
    for history in histories.values():
        if failure == "gap":
            del history[2]
        elif failure == "duplicate":
            history.append(history[0])
        elif failure == "late":
            history[2].available_at = NOW + timedelta(seconds=1)
        elif failure == "mixed_source":
            history[2].source = "synthetic_other"
        else:
            history.clear()
    outcomes = attach_outcomes(report, histories, as_of=NOW, horizons_seconds=(300,))
    assert all(outcome.status == "CENSORED" for outcome in outcomes)
    assert all(outcome.close_return is None for outcome in outcomes)


def test_outcome_rows_are_separate_and_reject_pre_match_evaluation():
    query, references, settings = setup()
    report = match_analogs(query, references, settings)
    with pytest.raises(ValueError, match="precedes matching"):
        attach_outcomes(report, {}, as_of=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="positive multiples"):
        attach_outcomes(report, {}, as_of=NOW, horizons_seconds=(10,))


def test_benchmark_measures_matching_not_predictive_performance():
    query, references, settings = setup()
    result = benchmark_metrics(query, references, settings)
    assert set(result) == {"euclidean", "cosine"}
    assert all(item["duration_seconds"] >= 0 for item in result.values())
    assert all(item["reference_count"] == 4 for item in result.values())
    assert all("precision" not in item and "probability" not in item for item in result.values())


def test_naive_timestamps_and_nonfinite_values_are_rejected():
    with pytest.raises(ValidationError, match="timezone"):
        factor(0.5, datetime(2025, 1, 1))
    with pytest.raises(ValidationError):
        factor(float("nan"), NOW)
