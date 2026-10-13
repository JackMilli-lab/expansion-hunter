"""Synthetic discovery ranking invariants; these tests assert no trading edge."""

from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.discovery_config import DiscoverySettings
from expansion_hunter.discovery_models import (
    DiscoveryCycle,
    DiscoveryFeature,
    DiscoveryObservation,
    Freshness,
)
from expansion_hunter.discovery_ranking import dark_horses, rank_observations
from expansion_hunter.models import Asset

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)


@pytest.fixture
def settings():
    return DiscoverySettings(minimum_cross_section=3, minimum_history_samples=3)


def observed(asset_id, value, at=NOW, *, historical=0.5, name="relative_volume", direction=1):
    return DiscoveryObservation(
        asset=Asset(asset_id=asset_id, symbol=asset_id.upper(), name=asset_id), as_of=at,
        eligible=True, disposition="COMPLETE", freshness=Freshness.FRESH,
        features={name: DiscoveryFeature(
            value=value, provider="synthetic_test_only", timeframe="15m", unit="ratio",
            source_timestamp=at, retrieved_at=at, freshness=Freshness.FRESH,
            historical_percentile=historical, direction=direction,
        )},
    )


def frame(values, at=NOW, **kwargs):
    return [observed(asset_id, value, at, **kwargs) for asset_id, value in values.items()]


def cycle(items, settings, history=()):
    ranked = rank_observations(items, list(history), settings)
    at = ranked[0].as_of
    return DiscoveryCycle(
        cycle_id=at.isoformat(), started_at=at, as_of=at, completed_at=at + timedelta(seconds=1),
        database_path="synthetic_test_only.sqlite3", config_hash="synthetic", observations=ranked,
    )


def by_id(items):
    return {item.asset.asset_id: item for item in items}


def test_real_cross_section_and_independent_historical_contribution(settings):
    items = frame({"a": 1, "b": 2, "c": 3}, historical=0.9)
    result = by_id(rank_observations(items, [], settings))
    top = result["c"]
    assert top.rank.current_rank == 1
    assert top.features["relative_volume"].cross_sectional_percentile == 1
    assert top.features["relative_volume"].historical_percentile == 0.9
    score = top.scores["light_discovery"]
    assert score.value == pytest.approx(9.5)
    assert score.coverage == pytest.approx(0.1)
    assert score.upper_bound == pytest.approx(99.5)
    assert score.calibration == "UNCALIBRATED"
    assert {component.name for component in score.components} >= {
        "relative_volume:historical", "relative_volume:cross_sectional",
    }


def test_missing_self_history_is_unknown_not_neutral(settings):
    result = rank_observations(frame({"a": 1, "b": 2, "c": 3}, historical=None), [], settings)
    assert result[0].scores["light_discovery"].coverage == pytest.approx(0.05)
    component = next(component for component in result[0].scores["light_discovery"].components
                     if component.name == "relative_volume:historical")
    assert component.normalized is None
    assert component.contribution is None


def test_native_symbol_provenance_is_not_a_single_asset_cohort(settings):
    items = frame({"a": 1, "b": 2, "c": 3}, historical=0.5)
    for item in items:
        item.features["relative_volume"].provider = f"binance/spot/{item.asset.symbol}USDT/15m"
    result = by_id(rank_observations(items, [], settings))
    assert result["a"].features["relative_volume"].cross_sectional_percentile == 0
    assert result["b"].features["relative_volume"].cross_sectional_percentile == .5
    assert result["c"].features["relative_volume"].cross_sectional_percentile == 1
    assert result["c"].features["relative_volume"].provider == "binance/spot/CUSDT/15m"


def test_native_cohort_normalization_keeps_different_venues_separate(settings):
    items = frame({"a": 1, "b": 2, "c": 3}, historical=0.5)
    items[0].features["relative_volume"].provider = "binance/spot/AUSDT/15m"
    items[1].features["relative_volume"].provider = "binance/spot/BUSDT/15m"
    items[2].features["relative_volume"].provider = "other-exchange/spot/CUSDT/15m"
    result = rank_observations(items, [], settings)
    assert all(item.features["relative_volume"].cross_sectional_percentile is None for item in result)


def test_own_history_uses_prior_observations_and_excludes_current(settings):
    previous = []
    for hours, value in ((3, 1), (2, 2), (1, 3)):
        previous.append(cycle(frame({"a": value, "b": value + 1, "c": value + 2},
                                    NOW - timedelta(hours=hours), historical=None), settings))
    result = by_id(rank_observations(frame({"a": 4, "b": 5, "c": 6}, historical=None), previous, settings))
    assert result["a"].features["relative_volume"].historical_percentile == 1
    assert result["a"].features["relative_volume"].sample_count == 3


def test_repeated_polling_same_source_does_not_create_history(settings):
    previous = []
    source = NOW - timedelta(hours=3)
    for hours in (3, 2, 1):
        items = frame({"a": 1, "b": 2, "c": 3}, NOW - timedelta(hours=hours), historical=None)
        for item in items:
            item.features["relative_volume"].source_timestamp = source
        previous.append(cycle(items, settings))
    current = rank_observations(frame({"a": 4, "b": 5, "c": 6}, historical=None), previous, settings)
    assert current[0].features["relative_volume"].historical_percentile is None


def test_historical_baseline_ignores_future_and_source_changes(settings):
    previous = []
    for hours in (3, 2, 1):
        item = cycle(frame({"a": 1, "b": 2, "c": 3}, NOW - timedelta(hours=hours), historical=None), settings)
        if hours == 1:
            item.completed_at = NOW + timedelta(seconds=1)
        elif hours == 2:
            for observation in item.observations:
                observation.features["relative_volume"].provider = "different-provider"
        previous.append(item)
    result = rank_observations(frame({"a": 2, "b": 3, "c": 4}, historical=None), previous, settings)
    assert result[0].features["relative_volume"].historical_percentile is None


def test_compression_direction_is_reversed(settings):
    ranked = rank_observations(frame({"a": 0.1, "b": 0.2, "c": 0.3},
                                    name="range_compression", direction=-1), [], settings)
    assert ranked[0].asset.asset_id == "a"
    assert ranked[0].features["range_compression"].cross_sectional_percentile == 0


def test_ties_are_midrank_with_stable_display_order(settings):
    ranked = rank_observations(frame({"b": 1, "a": 1, "c": 2}), [], settings)
    assert [(item.asset.asset_id, item.rank.current_rank) for item in ranked] == [
        ("c", 1), ("a", 2.5), ("b", 2.5),
    ]


def test_all_ties_are_neutral(settings):
    ranked = rank_observations(frame({"a": 1, "b": 1, "c": 1}), [], settings)
    assert all(item.rank.current_rank == 2 for item in ranked)
    assert all(item.features["relative_volume"].cross_sectional_percentile == 0.5 for item in ranked)


def test_insufficient_cross_section_withholds_rank(settings):
    ranked = rank_observations(frame({"a": 1, "b": 2}), [], settings)
    assert all(item.rank.current_rank is None for item in ranked)
    assert "Insufficient" in ranked[0].rank.reason


@pytest.mark.parametrize("freshness", [Freshness.STALE, Freshness.MISSING])
def test_stale_or_missing_assets_cannot_rank(settings, freshness):
    items = frame({"a": 1, "b": 2, "c": 3, "d": 4})
    items[-1].freshness = freshness
    ranked = by_id(rank_observations(items, [], settings))
    assert ranked["d"].rank.current_rank is None
    assert ranked["d"].features["relative_volume"].cross_sectional_percentile is None


def test_stale_critical_feature_excludes_whole_asset(settings):
    items = frame({"a": 1, "b": 2, "c": 3, "d": 4})
    items[-1].features["price_change"] = DiscoveryFeature(
        provider="synthetic_test_only", timeframe="15m", critical=True,
    )
    assert by_id(rank_observations(items, [], settings))["d"].rank.current_rank is None


def test_ineligible_asset_is_unranked(settings):
    items = frame({"a": 1, "b": 2, "c": 3, "d": 4})
    items[-1].eligible = False
    excluded = by_id(rank_observations(items, [], settings))["d"]
    assert excluded.rank.current_rank is None
    assert excluded.scores["light_discovery"].value is None
    assert excluded.scores["light_discovery"].coverage == 0


def test_return_windows_are_aligned_and_market_excludes_own_return(settings):
    items = frame({"a": 100, "b": 1, "c": 3}, name="price_change")
    ranked = by_id(rank_observations(items, [], settings))
    assert ranked["a"].features["relative_strength_market"].value == 98
    assert ranked["a"].features["relative_strength_market"].sample_count == 2
    assert ranked["b"].features["relative_strength_market"].value == pytest.approx(-50.5)


def test_misaligned_return_endpoints_do_not_mix(settings):
    items = frame({"a": 1, "b": 2, "c": 100}, name="price_change")
    items[-1].features["price_change"].source_timestamp -= timedelta(minutes=15)
    ranked = by_id(rank_observations(items, [], settings))
    assert "relative_strength_market" not in ranked["c"].features
    assert ranked["c"].features["price_change"].cross_sectional_percentile is None


def test_rank_velocity_uses_elapsed_hours_and_frozen_comparable_cohort(settings):
    prior = cycle(frame({"a": 1, "b": 2, "c": 3}, NOW - timedelta(minutes=30)), settings)
    result = by_id(rank_observations(frame({"a": 3, "b": 2, "c": 1}), [prior], settings))
    assert result["a"].rank.previous_rank == 3
    assert result["a"].rank.current_rank == 1
    assert result["a"].rank.rank_velocity == pytest.approx(200)
    assert result["a"].rank.velocity_percentile == 1


def test_rank_acceleration_respects_irregular_interval_midpoints(settings):
    older = cycle(frame({"a": 1, "b": 2, "c": 3}, NOW - timedelta(hours=1.5)), settings)
    prior = cycle(frame({"a": 2, "b": 1, "c": 3}, NOW - timedelta(minutes=30)), settings, [older])
    result = by_id(rank_observations(frame({"a": 3, "b": 1, "c": 2}), [older, prior], settings))
    # a q:0 -> .5 over 1h -> 1 over .5h. Midpoint distance .75h.
    assert result["a"].rank.rank_velocity == pytest.approx(100)
    assert result["a"].rank.rank_acceleration == pytest.approx(200 / 3)


def test_universe_composition_cannot_manufacture_rank_velocity(settings):
    settings.minimum_rank_overlap = 0.75
    prior = cycle(frame({"a": 1, "b": 2, "c": 3, "old": 100}, NOW - timedelta(hours=1)), settings)
    result = by_id(rank_observations(frame({"a": 1, "b": 2, "c": 3, "new": -100}), [prior], settings))
    assert result["c"].rank.previous_rank == 2
    assert result["c"].rank.current_rank == 1
    assert result["c"].rank.rank_velocity == 0
    assert result["c"].rank.comparable_cohort_size == 3


def test_low_universe_overlap_withholds_migration(settings):
    prior = cycle(frame({"a": 1, "b": 2, "c": 3, "old": 4}, NOW - timedelta(hours=1)), settings)
    result = rank_observations(frame({"a": 2, "b": 3, "c": 4, "new": 5}), [prior], settings)
    assert all(item.rank.rank_velocity is None for item in result)
    assert "overlap" in result[0].rank.reason


def test_provider_changes_withhold_migration(settings):
    prior = cycle(frame({"a": 1, "b": 2, "c": 3}, NOW - timedelta(hours=1)), settings)
    items = frame({"a": 3, "b": 2, "c": 1})
    for item in items:
        item.features["relative_volume"].provider = "new-provider"
    result = rank_observations(items, [prior], settings)
    assert all(item.rank.rank_velocity is None for item in result)


def test_changed_feature_mask_recomputed_without_fake_velocity(settings):
    old = frame({"a": 1, "b": 2, "c": 3}, NOW - timedelta(hours=1))
    for item in old:
        item.features["volatility"] = observed(
            item.asset.asset_id, 10 if item.asset.asset_id == "a" else 1,
            item.as_of, name="volatility",
        ).features["volatility"]
    prior = cycle(old, settings)
    result = rank_observations(frame({"a": 1, "b": 2, "c": 3}), [prior], settings)
    assert all(item.rank.rank_velocity == 0 for item in result)


def test_historical_window_uses_at_or_before_never_after_target(settings):
    older = cycle(frame({"a": 1, "b": 2, "c": 3}, NOW - timedelta(minutes=31)), settings)
    later = cycle(frame({"a": 3, "b": 2, "c": 1}, NOW - timedelta(minutes=29)), settings)
    result = by_id(rank_observations(frame({"a": 2, "b": 1, "c": 3}), [older, later], settings))
    assert result["a"].rank.ranks["30m"] == 3
    assert result["a"].rank.previous_rank == 1
    assert result["a"].rank.ranks["24h"] is None


def test_historical_window_does_not_use_unbounded_old_rank(settings):
    prior = cycle(frame({"a": 1, "b": 2, "c": 3}, NOW - timedelta(hours=10)), settings)
    result = rank_observations(frame({"a": 3, "b": 2, "c": 1}), [prior], settings)
    assert all(item.rank.ranks["1h"] is None for item in result)


def test_future_cycles_do_not_supply_previous_ranks_or_migration(settings):
    future = cycle(frame({"a": 1, "b": 2, "c": 3}, NOW + timedelta(hours=1)), settings)
    result = rank_observations(frame({"a": 3, "b": 2, "c": 1}), [future], settings)
    assert all(item.rank.previous_rank is None and item.rank.rank_velocity is None for item in result)


def test_future_feature_availability_cannot_enter_percentiles(settings):
    items = frame({"a": 1, "b": 2, "c": 3, "d": 4})
    items[-1].features["relative_volume"].retrieved_at += timedelta(seconds=1)
    result = by_id(rank_observations(items, [], settings))
    assert result["d"].rank.current_rank is None
    assert result["d"].features["relative_volume"].cross_sectional_percentile is None


def test_migration_never_feeds_back_into_current_discovery_rank(settings):
    prior = cycle(frame({"a": 1, "b": 2, "c": 3}, NOW - timedelta(hours=1)), settings)
    with_history = rank_observations(frame({"a": 3, "b": 2, "c": 1}), [prior], settings)
    without = rank_observations(frame({"a": 3, "b": 2, "c": 1}), [], settings)
    assert [(item.asset.asset_id, item.rank.current_rank) for item in with_history] == [
        (item.asset.asset_id, item.rank.current_rank) for item in without
    ]


def test_dark_horse_requires_real_velocity_and_two_history_channels(settings):
    settings.dark_horse_previous_rank = 3
    prior = cycle(frame({"a": 1, "b": 2, "c": 3}, NOW - timedelta(hours=1)), settings)
    items = frame({"a": 3, "b": 2, "c": 1}, historical=0.8)
    for item in items:
        item.features["relative_strength_btc"] = observed(
            item.asset.asset_id, 1, historical=0.9, name="relative_strength_btc",
        ).features["relative_strength_btc"]
    ranked = rank_observations(items, [prior], settings)
    assert dark_horses(ranked, settings) == ["a"]
    ranked[0].features["relative_strength_btc"].historical_percentile = None
    assert dark_horses(ranked, settings) == []


def test_empty_input_is_safe(settings):
    assert rank_observations([], [], settings) == []
    assert dark_horses([], settings) == []


def test_configurable_feature_weights_keep_contributions_normalized(settings):
    settings.score_weights = {"light_discovery": {"relative_volume": 100}}
    ranked = rank_observations(frame({"a": 1, "b": 2, "c": 3}, historical=1), [], settings)
    score = ranked[0].scores["light_discovery"]
    assert score.value == pytest.approx(100 * 100 / 190)
    assert score.coverage == pytest.approx(100 / 190)
    assert sum(component.contribution or 0 for component in score.components) == pytest.approx(score.value)


def test_unknown_weight_name_fails_visibly(settings):
    settings.score_weights = {"light_discovery": {"invented_predictor": 1}}
    with pytest.raises(ValueError, match="unknown light_discovery"):
        rank_observations(frame({"a": 1, "b": 2, "c": 3}), [], settings)
