"""Synthetic research protocol tests; fixtures are never performance evidence.

The fixtures include every contemporaneously eligible asset, including controls
that subsequently win. Matching is tested independently of future labels.
"""

from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.models import (
    Asset,
    AssetSnapshot,
    Candle,
    Catalyst,
    Metric,
    Regime,
    RegimeReport,
    Status,
)
from expansion_hunter.research import (
    ResearchSettings,
    StudyCase,
    StudyCohort,
    StudyDataset,
    StudyObservation,
    run_study,
)

BOUNDARY = datetime(2025, 1, 1, tzinfo=UTC)
SOURCE = "synthetic_test_only:spot: research protocol fixture"


def known_metric(value, as_of):
    return Metric(value=value, observed_at=as_of, available_at=as_of, source=SOURCE, status=Status.AVAILABLE)


def history(as_of, duration):
    return [
        Candle(
            open_time=as_of - (240 - i) * duration,
            close_time=as_of - (239 - i) * duration,
            available_at=as_of - (239 - i) * duration,
            open=100,
            high=102,
            low=98,
            close=100,
            volume=1_000,
            quote_volume=100_000,
            source=SOURCE,
        )
        for i in range(240)
    ]


def future(as_of, winner=False):
    output = []
    previous = 100.0
    for i in range(30):
        close = 100 + 2 * (i + 1) if winner else 100
        opened = as_of + timedelta(days=i)
        output.append(
            Candle(
                open_time=opened,
                close_time=opened + timedelta(days=1),
                available_at=opened + timedelta(days=1),
                open=previous,
                high=max(previous, close) + 1,
                low=min(previous, close) - 1,
                close=close,
                volume=1_000,
                source=SOURCE,
            )
        )
        previous = close
    return output


def cohort(as_of=BOUNDARY, controls=55):
    daily, intraday = history(as_of, timedelta(days=1)), history(as_of, timedelta(hours=4))
    observations = []
    for i in range(controls + 1):
        asset_id = "case" if i == 0 else f"control_{i:03d}"
        snapshot = AssetSnapshot(
            asset=Asset(asset_id=asset_id, symbol=asset_id.upper(), name="Synthetic test asset"),
            as_of=as_of,
            candles={"1D": daily, "4h": intraday},
            market={
                "market_cap": known_metric(1e9 * (1 + i / 100), as_of),
                "turnover_24h": known_metric(0.01, as_of),
                "depth_usd": known_metric(100_000, as_of),
                "quote_volume_24h": known_metric(10e6, as_of),
            },
        )
        observations.append(
            StudyObservation(
                snapshot=snapshot,
                future_bars=future(as_of, winner=i in (0, 1)),
                future_timeframe="1D",
                future_source=SOURCE,
                pre_event_source=SOURCE,
                point_in_time_metadata=True,
            )
        )
    return StudyCohort(
        as_of=as_of,
        universe_asset_ids=[item.snapshot.asset.asset_id for item in observations],
        universe_available_at=as_of,
        universe_source=SOURCE,
        point_in_time_universe=True,
        includes_delisted=True,
        regime=RegimeReport(
            as_of=as_of, available_at=as_of, regime=Regime.NEUTRAL, confidence=0.8, risk_multiplier=0.5
        ),
        observations=observations,
    )


@pytest.fixture(scope="module")
def dataset():
    return StudyDataset(cohorts=[cohort()], cases=[StudyCase(asset_id="case", as_of=BOUNDARY)])


@pytest.fixture(scope="module")
def reference(dataset):
    return run_study(dataset, ResearchSettings(discover_cases=False))


@pytest.mark.parametrize("field", ["point_in_time_universe", "includes_delisted"])
def test_retrospective_or_survivor_only_universe_fails_closed(dataset, field):
    changed = dataset.model_copy(deep=True)
    setattr(changed.cohorts[0], field, False)
    with pytest.raises(ValueError, match="archived point-in-time universe"):
        StudyDataset.model_validate(changed.model_dump())


def test_universe_unknown_at_decision_boundary_fails_closed(dataset):
    changed = dataset.model_copy(deep=True)
    changed.cohorts[0].universe_available_at = BOUNDARY + timedelta(seconds=1)
    with pytest.raises(ValueError, match="universe was not available"):
        StudyDataset.model_validate(changed.model_dump())


def test_future_case_metadata_cannot_enter_matching(dataset):
    changed = dataset.model_copy(deep=True)
    changed.cohorts[0].observations[0].snapshot.market["market_cap"].available_at = BOUNDARY + timedelta(
        seconds=1
    )
    report = run_study(changed, ResearchSettings(discover_cases=False))
    assert not report.cases
    assert report.warnings
    assert "case" in report.cohort_statistics[0].excluded


def test_unattested_point_in_time_metadata_cannot_enter_matching(dataset):
    changed = dataset.model_copy(deep=True)
    changed.cohorts[0].observations[0].point_in_time_metadata = False
    with pytest.raises(ValueError, match="current metadata"):
        StudyDataset.model_validate(changed.model_dump())


def test_empty_research_is_explicitly_uninformative():
    with pytest.raises(ValueError, match="at least 1 item"):
        StudyDataset(cohorts=[])


def selection(case):
    return [(item.asset_id, item.distance) for item in case.matches]


def test_controls_selected_on_similarity_without_filtering_winners(reference):
    case = reference.cases[0]
    assert case.outcome.label == "winner"
    assert len(case.matches) == 50
    assert [item.asset_id for item in case.matches] == [f"control_{i:03d}" for i in range(1, 51)]
    assert case.matches[0].outcome.label == "winner"
    assert case.matched_failures == 49
    assert case.non_breakout_shortfall == 1
    assert case.control_shortfall == 0
    assert reference.cohort_statistics[0].base_rate == pytest.approx(2 / 56)
    assert reference.automatic_orders_enabled is False


def test_changing_control_outcomes_cannot_change_matching(dataset, reference):
    changed = dataset.model_copy(deep=True)
    for index, item in enumerate(changed.cohorts[0].observations[1:], start=1):
        item.future_bars = future(BOUNDARY, winner=index != 1)
    report = run_study(changed, ResearchSettings(discover_cases=False))
    assert selection(report.cases[0]) == selection(reference.cases[0])
    assert report.cases[0].candidate_distances == reference.cases[0].candidate_distances
    assert report.cases[0].matched_failures == 1
    assert report.cohort_statistics[0].base_rate == pytest.approx(55 / 56)


def test_appending_future_candles_cannot_change_features_or_ranking(dataset, reference):
    changed = dataset.model_copy(deep=True)
    for item in changed.cohorts[0].observations:
        item.snapshot.candles["1D"].extend(item.future_bars)
    report = run_study(changed, ResearchSettings(discover_cases=False))
    assert selection(report.cases[0]) == selection(reference.cases[0])
    assert report.cases[0].match_features == reference.cases[0].match_features
    assert report.cases[0].distinguishing_features == reference.cases[0].distinguishing_features
    assert report.cases[0].candidate_distances == reference.cases[0].candidate_distances


@pytest.mark.parametrize("failure", ["missing", "gap", "duplicate"])
def test_missing_and_gapped_outcomes_are_censored_not_negative(dataset, failure):
    changed = dataset.model_copy(deep=True)
    target = changed.cohorts[0].observations[2]
    if failure == "missing":
        target.future_bars = []
    elif failure == "gap":
        del target.future_bars[10]
    else:
        target.future_bars[10] = target.future_bars[9]
    report = run_study(changed, ResearchSettings(discover_cases=False))
    case = report.cases[0]
    matched = next(item for item in case.matches if item.asset_id == "control_002")
    assert matched.outcome.label == "censored"
    assert matched.outcome.accepted_breakout is None
    assert matched.outcome.maximum_favorable_return is None
    assert case.matched_failures == 48
    assert report.cohort_statistics[0].label_counts["censored"] == 1
    assert report.cohort_statistics[0].base_rate == pytest.approx(2 / 55)


def test_control_shortage_is_reported_without_fabricated_observations():
    data = StudyDataset(cohorts=[cohort(controls=8)], cases=[StudyCase(asset_id="case", as_of=BOUNDARY)])
    report = run_study(data, ResearchSettings(discover_cases=False))
    case = report.cases[0]
    assert len(case.matches) == 8
    assert case.eligible_controls == 8
    assert case.control_shortfall == 42
    assert case.non_breakout_shortfall == 43
    assert case.warnings
    assert report.status == "insufficient_data"


def split_dataset():
    boundaries = [BOUNDARY - timedelta(days=61), BOUNDARY - timedelta(days=31), BOUNDARY]
    data = StudyDataset(
        cohorts=[cohort(when, controls=3) for when in boundaries],
        cases=[StudyCase(asset_id="case", as_of=when) for when in boundaries],
    )
    return data, ResearchSettings(
        discover_cases=False,
        controls_per_case=3,
        train_end=BOUNDARY - timedelta(days=17),
        minimum_cases_per_split=1,
    )


def test_training_horizon_overlaps_are_purged_from_feature_comparison():
    data, settings = split_dataset()
    report = run_study(data, settings)
    assert [item.split for item in report.cases] == ["train", "purged", "holdout"]
    assert report.cases[0].outcome.horizon_end <= settings.train_end
    assert report.cases[1].as_of <= settings.train_end < report.cases[1].outcome.horizon_end
    assert report.cases[2].as_of > settings.train_end
    effect = next(item for item in report.feature_effects if item.feature == "spot_relative_volume")
    assert effect.train_case_count == effect.holdout_case_count == 1
    assert any("purged" in warning.lower() for warning in report.warnings)


def test_holdout_features_cannot_retrain_matching_scales_or_training_effects():
    data, settings = split_dataset()
    expected = run_study(data, settings)
    changed = data.model_copy(deep=True)
    bars = changed.cohorts[-1].observations[0].snapshot.candles["4h"]
    bars[-1] = bars[-1].model_copy(update={"volume": 4_000, "quote_volume": 400_000})
    actual = run_study(changed, settings)
    assert actual.matching_scales == expected.matching_scales
    assert actual.cases[0] == expected.cases[0]
    expected_effect = next(
        item for item in expected.feature_effects if item.feature == "spot_relative_volume"
    )
    actual_effect = next(item for item in actual.feature_effects if item.feature == "spot_relative_volume")
    assert actual_effect.train_mean_paired_difference == expected_effect.train_mean_paired_difference
    assert actual_effect.holdout_mean_paired_difference != expected_effect.holdout_mean_paired_difference


@pytest.mark.parametrize("late_asset", ["case", "control_002"])
def test_training_purges_labels_unavailable_at_the_train_cutoff(late_asset):
    data, settings = split_dataset()
    observation = next(
        item for item in data.cohorts[0].observations if item.snapshot.asset.asset_id == late_asset
    )
    observation.future_bars = [
        bar.model_copy(update={"available_at": settings.train_end + timedelta(days=1)})
        for bar in observation.future_bars
    ]
    report = run_study(data, settings)
    assert report.cases[0].outcome.horizon_end < settings.train_end
    assert report.cases[0].split == "purged"
    effect = next(item for item in report.feature_effects if item.feature == "spot_relative_volume")
    assert effect.train_case_count == 0


def small_dataset():
    return StudyDataset(cohorts=[cohort(controls=2)], cases=[StudyCase(asset_id="case", as_of=BOUNDARY)])


def test_provider_oi_key_and_funding_cadence_preserve_economic_meaning():
    data = small_dataset()
    snapshot = data.cohorts[0].observations[0].snapshot
    snapshot.derivatives = {
        "open_interest_change_pct": known_metric(0.15, BOUNDARY),
        "funding_rate": known_metric(0.0001, BOUNDARY),
        "funding_interval_hours": known_metric(1, BOUNDARY),
    }
    report = run_study(data, ResearchSettings(discover_cases=False, controls_per_case=2))
    evidence = report.cases[0].distinguishing_features
    assert evidence["oi_change_24h"] == pytest.approx(0.15)
    assert evidence["funding_8h_equivalent"] == pytest.approx(0.0008)


@pytest.mark.parametrize("cadence", ["missing", "unavailable_at_boundary"])
def test_ambiguous_funding_cadence_remains_unknown(cadence):
    data = small_dataset()
    snapshot = data.cohorts[0].observations[0].snapshot
    snapshot.derivatives["funding_rate"] = known_metric(0.001, BOUNDARY)
    if cadence != "missing":
        snapshot.derivatives["funding_interval_hours"] = known_metric(1, BOUNDARY)
        snapshot.derivatives["funding_interval_hours"].available_at = BOUNDARY + timedelta(seconds=1)
    report = run_study(data, ResearchSettings(discover_cases=False, controls_per_case=2))
    assert report.cases[0].distinguishing_features["funding_8h_equivalent"] is None


@pytest.mark.parametrize("include_adoption", [True, False])
def test_marketing_and_partnership_announcements_are_not_adoption_evidence(include_adoption):
    data = small_dataset()
    snapshot = data.cohorts[0].observations[0].snapshot
    categories = [("marketing", 1), ("social", 0.95), ("partnership", 0.9)]
    if include_adoption:
        categories.append(("network_usage", 0.6))
    snapshot.catalysts = [
        Catalyst(
            category=category,
            strength=strength,
            description="Synthetic test evidence",
            source_url="https://fixture.invalid/",
            observed_at=BOUNDARY,
            available_at=BOUNDARY,
            verified=True,
            quantitative=True,
        )
        for category, strength in categories
    ]
    report = run_study(data, ResearchSettings(discover_cases=False, controls_per_case=2))
    value = report.cases[0].distinguishing_features["verified_quantitative_catalyst_strength"]
    assert value == (0.6 if include_adoption else None)


def test_wicks_and_nonconsecutive_closes_do_not_establish_accepted_expansion():
    data = small_dataset()
    observation = data.cohorts[0].observations[0]
    bars = future(BOUNDARY)
    for i, bar in enumerate(bars):
        close = 105 if i % 2 == 0 else 100
        bars[i] = bar.model_copy(update={"close": close, "high": 170})
    observation.future_bars = bars
    report = run_study(data, ResearchSettings(discover_cases=False, controls_per_case=2))
    assert report.cases[0].outcome.label == "failure"
    assert report.cases[0].outcome.accepted_breakout is False


def test_false_positive_rates_use_the_full_visible_scored_cohort():
    data = small_dataset()
    for observation, score in zip(data.cohorts[0].observations, [80, 20, 80], strict=True):
        observation.score = known_metric(score, BOUNDARY)
    report = run_study(data, ResearchSettings(discover_cases=False, controls_per_case=2))
    stats = report.cohort_statistics[0]
    assert stats.score_coverage == 3
    assert stats.score_flagged == 2
    assert stats.false_positive_rate == 1
    assert stats.false_discovery_rate == 0.5
    assert stats.base_rate == pytest.approx(2 / 3)


def test_reconstructed_future_scores_cannot_claim_historical_false_positive_rates():
    data = small_dataset()
    for observation in data.cohorts[0].observations:
        observation.score = known_metric(80, BOUNDARY)
        observation.score.available_at = BOUNDARY + timedelta(seconds=1)
    report = run_study(data, ResearchSettings(discover_cases=False, controls_per_case=2))
    stats = report.cohort_statistics[0]
    assert stats.score_coverage == 0
    assert stats.false_positive_rate is None
    assert stats.false_discovery_rate is None
