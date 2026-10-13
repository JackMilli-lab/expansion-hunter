"""Two-sided broad discovery: causal hypotheses, never short-trading execution."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from benchmarks.phase1_benchmark import FixtureClock, SyntheticProviders
from expansion_hunter.config import Settings
from expansion_hunter.discovery import DiscoveryEngine
from expansion_hunter.discovery_config import DiscoverySettings
from expansion_hunter.discovery_features import deep_scores, light_features
from expansion_hunter.discovery_models import (
    DiscoveryCycle,
    DiscoveryFeature,
    DiscoveryObservation,
    DiscoveryScore,
    DiscoveryStage,
    Freshness,
)
from expansion_hunter.discovery_ranking import (
    DIRECTIONAL_FORMULA_VERSION,
    FEATURE_WEIGHTS,
    FORMULA_VERSION,
    dark_horses,
    rank_observations,
    ranking_formula_version,
)
from expansion_hunter.discovery_scheduler import transition
from expansion_hunter.discovery_store import DiscoveryStore
from expansion_hunter.models import Asset, AssetSnapshot, Candle

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)


def settings(**options):
    return DiscoverySettings(ranking_mode="BOTH", minimum_cross_section=3, minimum_history_samples=3,
                             **options)


def feature(value, at=NOW, *, direction=1, history=.5, name="ratio", critical=False):
    return DiscoveryFeature(value=value, provider="synthetic/spot", timeframe="15m", unit=name,
                            source_timestamp=at, retrieved_at=at, freshness=Freshness.FRESH,
                            direction=direction, historical_percentile=history, critical=critical)


def observed(asset_id, at=NOW, *, price_return=0, activity=1, low_distance=1, high_distance=1,
             rs=0, history=.5):
    values = {name: 1 for name in FEATURE_WEIGHTS}
    values.update(price_change=price_return, relative_strength_btc=rs, relative_strength_market=rs,
                  volume_acceleration=activity, relative_volume=activity,
                  distance_recent_high=high_distance, distance_recent_low=low_distance)
    inverse = {"distance_recent_high", "distance_recent_low", "volatility", "range_compression"}
    return DiscoveryObservation(
        asset=Asset(asset_id=asset_id, symbol=asset_id.upper(), name=asset_id), as_of=at,
        eligible=True, disposition="COMPLETE", freshness=Freshness.FRESH,
        features={name: feature(value, at, direction=-1 if name in inverse else 1, history=history,
                                name=name, critical=name == "price_change")
                  for name, value in values.items()},
    )


def frame(at=NOW):
    return [observed("unknown-short", at, price_return=-8, activity=5, low_distance=.1, high_distance=10, rs=-5),
            observed("unknown-long", at, price_return=5, activity=2, low_distance=10, high_distance=.1, rs=3),
            observed("neutral", at, price_return=0, activity=1, low_distance=5, high_distance=5, rs=0)]


def cycle(rows, options, history=(), *, code_version=None, config=None):
    ranked = rank_observations(rows, list(history), options)
    at = rows[0].as_of
    return DiscoveryCycle(cycle_id=at.isoformat(), started_at=at, as_of=at,
                          completed_at=at + timedelta(seconds=1), database_path="synthetic.sqlite3",
                          config_hash="synthetic", config=config or {"discovery": options.model_dump()},
                          code_version=code_version or ranking_formula_version(options), observations=ranked)


def by_id(rows):
    return {row.asset.asset_id: row for row in rows}


def component(score, name):
    return next(part for part in score.components if part.name == name)


def test_legacy_default_is_long_and_actual_operator_configuration_is_both():
    assert DiscoverySettings().ranking_mode == "LONG"
    assert ranking_formula_version(DiscoverySettings()) == FORMULA_VERSION
    config = Path(__file__).parents[1] / "config" / "discovery.json"
    operator = DiscoverySettings.model_validate_json(config.read_text())
    assert operator.ranking_mode == "BOTH"
    assert operator.universe_size == 500
    assert ranking_formula_version(operator) == DIRECTIONAL_FORMULA_VERSION
    with pytest.raises(ValueError):
        DiscoverySettings(ranking_mode="SHORT")


def test_unknown_bearish_asset_is_ranked_and_promoted_without_a_manual_watchlist():
    options = settings()
    ranked = rank_observations(frame(), [], options)
    short = ranked[0]
    assert short.asset.asset_id == "unknown-short"
    assert short.bias == "SHORT"
    assert short.rank.current_rank == 1
    assert short.ranking_mode == "BOTH"
    promoted, event = transition(short, None, options, "synthetic-cycle")
    assert promoted.stage.value in {"RADAR", "WATCH"}
    assert event is not None
    assert event.asset_id == "unknown-short"
    assert event.details["calibration"] == "UNCALIBRATED"


def test_each_side_is_explainable_and_selected_score_is_max_not_probability():
    rows = by_id(rank_observations(frame(), [], settings()))
    for row in rows.values():
        long = row.scores["light_discovery_long"]
        short = row.scores["light_discovery_short"]
        selected = row.scores["light_discovery"]
        assert selected.value == pytest.approx(max(long.lower_bound, short.lower_bound))
        for score in (long, short, selected):
            assert score.calibration == "UNCALIBRATED"
            assert score.formula_version == DIRECTIONAL_FORMULA_VERSION
            assert score.value == pytest.approx(sum(part.contribution or 0 for part in score.components))
            assert score.lower_bound <= score.upper_bound <= 100
        assert selected is not long and selected is not short
    assert rows["unknown-long"].bias == "LONG"


def test_short_reverses_only_signed_price_rs_components_without_mutating_native_values():
    rows = frame()
    rows[0].features["price_change"].historical_percentile = .1
    rows[0].features["relative_strength_btc"].historical_percentile = .2
    native = rows[0].features["price_change"].model_dump()
    ranked = by_id(rank_observations(rows, [], settings()))["unknown-short"]
    short = ranked.scores["light_discovery_short"]
    long = ranked.scores["light_discovery_long"]
    assert ranked.features["price_change"].value == native["value"] == -8
    assert ranked.features["price_change"].direction == native["direction"] == 1
    assert component(short, "price_change:historical").normalized == pytest.approx(.9)
    assert component(long, "price_change:historical").normalized == pytest.approx(.1)
    assert component(short, "relative_strength_btc:historical").normalized == pytest.approx(.8)
    assert component(short, "price_change:cross_sectional").normalized == 1
    for name in ("relative_volume", "volume_acceleration", "volatility", "range_compression", "liquidity_quality"):
        for channel in ("historical", "cross_sectional"):
            assert component(short, f"{name}:{channel}").normalized == component(long, f"{name}:{channel}").normalized


def test_short_uses_actual_distance_to_lows_not_inverse_distance_to_highs():
    short = by_id(rank_observations(frame(), [], settings()))["unknown-short"]
    score = short.scores["light_discovery_short"]
    low = component(score, "distance_recent_low:cross_sectional")
    assert low.normalized == 1
    assert low.inputs["direction"] == -1
    assert not any(part.name.startswith("distance_recent_high:") for part in score.components)
    assert component(short.scores["light_discovery_long"], "distance_recent_high:cross_sectional").normalized == 0


def test_unavailable_low_distance_remains_missing_without_a_fabricated_complement():
    rows = frame()
    rows[0].features.pop("distance_recent_low")
    score = by_id(rank_observations(rows, [], settings()))["unknown-short"].scores["light_discovery_short"]
    for channel in ("historical", "cross_sectional"):
        missing = component(score, f"distance_recent_low:{channel}")
        assert missing.normalized is None and missing.contribution is None
        assert missing.reason == "Evidence unavailable"
    assert score.coverage < 1


def test_native_completed_candles_provide_an_independent_causal_low_distance():
    bars = []
    for index in range(128):
        opened = NOW - timedelta(minutes=15 * (128 - index))
        bars.append(Candle(open_time=opened, close_time=opened + timedelta(minutes=15),
                           available_at=opened + timedelta(minutes=15), open=100, high=110,
                           low=90, close=100, volume=100, source="synthetic/spot"))
    snapshot = AssetSnapshot(asset=Asset(asset_id="unknown", symbol="UNKNOWN", name="Unknown"),
                             as_of=NOW, candles={"15m": bars})
    original = light_features(snapshot, settings())
    low = original["distance_recent_low"]
    high = original["distance_recent_high"]
    assert low.value == pytest.approx(100 * (100 / 90 - 1))
    assert high.value == pytest.approx(10)
    assert low.value != pytest.approx(-high.value)
    assert low.direction == -1 and low.source_timestamp == NOW
    assert low.historical_percentile == .5
    future = bars[-1].model_copy(update={"open_time": NOW, "close_time": NOW + timedelta(minutes=15),
                                         "available_at": NOW + timedelta(minutes=15), "low": 1, "high": 1000})
    contaminated = snapshot.model_copy(update={"candles": {"15m": bars + [future]}})
    assert light_features(contaminated, settings()) == original


def test_directionless_common_activity_does_not_get_an_invented_bullish_bias():
    rows = [observed(name, activity=activity) for name, activity in (("a", 3), ("b", 2), ("c", 1))]
    ranked = rank_observations(rows, [], settings())
    assert all(row.bias == "NEUTRAL" for row in ranked)
    empty = [row.model_copy(update={"features": {}}) for row in rows]
    assert all(row.bias == "UNAVAILABLE" for row in rank_observations(empty, [], settings()))


@pytest.mark.parametrize("quality", ["stale", "future", "excluded"])
def test_invalid_critical_native_data_never_gains_a_short_rank(quality):
    rows = frame()
    if quality == "stale":
        rows[0].features["price_change"].freshness = Freshness.STALE
    elif quality == "future":
        rows[0].features["price_change"].source_timestamp = NOW + timedelta(seconds=1)
        rows[0].features["price_change"].retrieved_at = NOW + timedelta(seconds=1)
    else:
        rows[0].eligible = False
        rows[0].disposition = "EXCLUDED"
    short = by_id(rank_observations(rows, [], settings()))["unknown-short"]
    assert short.rank.current_rank is None
    assert short.bias == "UNAVAILABLE"
    assert short.scores["light_discovery"].value is None


@pytest.mark.parametrize("change", ["legacy", "weights", "window", "history_window", "mode", "missing_mode"])
def test_changed_ranking_formula_or_baseline_cannot_manufacture_rank_migration(change):
    options = settings()
    prior = cycle(frame(NOW - timedelta(hours=1)), options)
    if change == "legacy":
        prior.code_version = FORMULA_VERSION
    elif change == "weights":
        prior.config["discovery"]["score_weights"] = {"light_discovery": {"price_change": 50}}
    elif change == "window":
        prior.config["discovery"]["light_timeframe"] = "5m"
    elif change == "history_window":
        prior.config["discovery"]["light_history_bars"] = 256
    elif change == "mode":
        prior.config["discovery"]["ranking_mode"] = "LONG"
    else:
        prior.config["discovery"].pop("ranking_mode")
    for row in rank_observations(frame(), [prior], options):
        assert row.rank.previous_rank is None
        assert row.rank.rank_velocity is None
        assert row.rank.rank_acceleration is None
        assert all(value is None for value in row.rank.ranks.values())
        assert "ranking formula/mode" in row.rank.reason


def test_new_two_sided_history_has_real_velocity_and_acceleration():
    options = settings()
    oldest_rows = frame(NOW - timedelta(hours=2))
    oldest_rows[0].features["relative_volume"].value = .1
    oldest_rows[0].features["volume_acceleration"].value = .1
    oldest_rows[0].features["distance_recent_low"].value = 8
    oldest = cycle(oldest_rows, options)
    prior_rows = frame(NOW - timedelta(hours=1))
    prior_rows[0].features["relative_volume"].value = 1
    prior_rows[0].features["volume_acceleration"].value = 1
    prior = cycle(prior_rows, options, [oldest])
    ranked = by_id(rank_observations(frame(), [oldest, prior], options))["unknown-short"]
    assert ranked.bias == "SHORT"
    assert ranked.rank.previous_rank is not None
    assert ranked.rank.rank_velocity is not None and ranked.rank.rank_velocity > 0
    assert ranked.rank.rank_acceleration is not None
    assert ranked.rank.comparable_cohort_size == 3
    assert ranked.rank.ranks["1h"] == by_id(prior.observations)["unknown-short"].rank.current_rank


def test_replay_suffix_or_late_publication_cannot_change_earlier_rank():
    options = settings()
    known = cycle(frame(NOW - timedelta(hours=1)), options)
    future = cycle(frame(NOW + timedelta(hours=1)), options)
    late = cycle(frame(NOW - timedelta(hours=2)), options)
    late.completed_at = NOW + timedelta(seconds=1)
    original = rank_observations(frame(), [known], options)
    contaminated = rank_observations(frame(), [future, late, known], options)
    assert [row.model_dump() for row in original] == [row.model_dump() for row in contaminated]


def test_short_dark_horse_historical_rs_confirmation_uses_negative_direction():
    options = settings(dark_horse_previous_rank=2, rank_velocity_percentile=.8)
    rows = rank_observations(frame(), [], options)
    short = by_id(rows)["unknown-short"]
    short.rank.previous_rank = 3
    short.rank.rank_velocity = 100
    short.rank.velocity_percentile = 1
    short.features["relative_strength_btc"].historical_percentile = .1
    short.features["relative_strength_market"].historical_percentile = .1
    short.features["relative_volume"].historical_percentile = .9
    assert dark_horses(rows, options) == ["unknown-short"]


def test_two_sided_observation_archive_preserves_raw_values_scores_and_bias(tmp_path):
    options = settings()
    record = cycle(frame(), options)
    store = DiscoveryStore(str(tmp_path / "directional.sqlite3"))
    try:
        record.database_path = store.database_path
        store.save_cycle(record)
        restored = store.get_cycle(record.cycle_id)
        assert restored.code_version == DIRECTIONAL_FORMULA_VERSION
        short = by_id(restored.observations)["unknown-short"]
        assert short.bias == "SHORT" and short.ranking_mode == "BOTH"
        assert short.features["price_change"].value == -8
        assert short.features["distance_recent_low"].value == .1
        assert json.loads(short.model_dump_json())["scores"]["light_discovery_short"]["calibration"] == "UNCALIBRATED"
    finally:
        store.close()


@pytest.mark.parametrize("bias", ["SHORT", "NEUTRAL", "UNAVAILABLE"])
def test_unimplemented_directional_deep_scores_are_unknown_not_legacy_long_scores(bias):
    bars = []
    for index in range(128):
        opened = NOW - timedelta(minutes=15 * (128 - index))
        price = 100 + index / 20
        bars.append(Candle(open_time=opened, close_time=opened + timedelta(minutes=15),
                           available_at=opened + timedelta(minutes=15), open=price, high=price + 1,
                           low=price - 1, close=price, volume=100, source="synthetic/spot"))
    snapshot = AssetSnapshot(asset=Asset(asset_id="unknown", symbol="UNKNOWN", name="Unknown"),
                             as_of=NOW, candles={"15m": bars})
    long_scores, long_levels = deep_scores(snapshot, settings())
    directional, levels = deep_scores(snapshot, settings(), bias=bias)
    for name in ("stored_energy", "absorption", "trigger_sensitivity", "expansion_readiness",
                 "relative_strength", "vwap_structure"):
        score = directional[name]
        assert score.value is None and score.coverage == 0
        assert score.lower_bound == 0 and score.upper_bound == 100
        assert score.formula_version == "phase1-v1-directional-unavailable"
        assert all(part.normalized is None and part.contribution is None for part in score.components)
        assert all(bias in part.reason and "unavailable" in part.reason for part in score.components)
    assert long_scores["stored_energy"].value is not None
    for name in ("volume_structure", "volume_awakening", "liquidity_quality"):
        assert directional[name] == long_scores[name]
    assert levels == long_levels
    with pytest.raises(ValueError, match="unsupported discovery score bias"):
        deep_scores(snapshot, settings(), bias="GUARANTEED_SHORT")


@pytest.mark.parametrize("price,expected", [(98, "WATCH"), (103, "INVALIDATED")])
def test_short_invalidation_uses_previous_resistance_not_a_valid_support_break(price, expected):
    options = settings()
    previous = observed("unknown-short", NOW - timedelta(minutes=1))
    previous.ranking_mode = "BOTH"
    previous.bias = "SHORT"
    previous.stage = DiscoveryStage.WATCH
    previous.levels = {"support": 99, "resistance": 102}
    current = observed("unknown-short")
    current.ranking_mode = "BOTH"
    current.bias = "SHORT"
    current.features["price"] = feature(price, critical=True)
    current.scores = {"light_discovery": DiscoveryScore(value=75, coverage=1)}
    result, event = transition(current, previous, options, "synthetic-cycle")
    assert result.stage == expected
    if event:
        assert event.details["bias"] == "SHORT"
        assert event.details["ranking_mode"] == "BOTH"
    if price < 99:
        assert not any("below previously known support" in reason for reason in result.reasons)
    else:
        assert "above previously known resistance" in event.reason


def test_changed_direction_cannot_reuse_previous_opposite_invalidation():
    options = settings()
    previous = observed("unknown")
    previous.ranking_mode = "BOTH"
    previous.bias = "LONG"
    previous.stage = DiscoveryStage.WATCH
    previous.levels = {"support": 99, "resistance": 102}
    current = previous.model_copy(deep=True, update={"bias": "SHORT"})
    current.features["price"] = feature(103, critical=True)
    current.scores = {"light_discovery": DiscoveryScore(value=75, coverage=1)}
    result, _ = transition(current, previous, options, "synthetic-cycle")
    assert result.stage == "WATCH"


def test_directional_unknown_deep_evidence_cannot_promote_loaded_or_ignition():
    options = settings()
    current = observed("unknown-short")
    current.ranking_mode = "BOTH"
    current.bias = "SHORT"
    current.features.update(price=feature(100, critical=True), rvol_1m=feature(5), atr_distance=feature(1))
    current.scores = {"light_discovery": DiscoveryScore(value=90, coverage=1),
                      "volume_awakening": DiscoveryScore(value=100, coverage=1)}
    for key in ("stored_energy", "absorption", "trigger_sensitivity", "expansion_readiness"):
        current.scores[key] = DiscoveryScore()
    result, event = transition(current, None, options, "synthetic-cycle")
    # Short activity may be flagged by the scanner, but unsupported deep energy
    # cannot become LOADED. Native fast support/close rules remain independent.
    assert result.stage == "WATCH"
    assert result.stage != "LOADED"
    assert event is not None


@pytest.mark.parametrize("change", ["mode", "weights", "none"])
def test_actual_engine_restart_refreshes_changed_directional_formula_or_weights(tmp_path, change):
    options = settings()
    record = cycle(frame(), options)
    for row in record.observations:
        row.last_light_at = NOW
    store = DiscoveryStore(str(tmp_path / "restart.sqlite3"))
    try:
        record.database_path = store.database_path
        store.save_cycle(record)
        restarted = options.model_copy(deep=True)
        if change == "mode":
            restarted.ranking_mode = "LONG"
        elif change == "weights":
            restarted.score_weights = {"light_discovery": {"price_change": 50}}
        engine = DiscoveryEngine(None, None, None, Settings(db_path=store.database_path), restarted,
                                 store, clock=lambda: NOW + timedelta(seconds=2))
        assert engine._last_broad == (NOW if change == "none" else None)
    finally:
        store.close()


async def test_actual_discovery_engine_archives_two_sided_formula_and_unknown_short(tmp_path):
    class TwoSidedProviders(SyntheticProviders):
        async def candles(self, asset, timeframe, limit, as_of):
            bars = await super().candles(asset, timeframe, limit, as_of)
            if asset.asset_id != "unknown-fixture-0000":
                return bars
            output = []
            for index, bar in enumerate(bars):
                price = 200 - index * .5
                output.append(bar.model_copy(update={"open": price + .1, "close": price,
                                                      "high": price + .2, "low": price - .2,
                                                      "volume": 1000 + index * 50,
                                                      "quote_volume": price * (1000 + index * 50),
                                                      "taker_buy_volume": (1000 + index * 50) * .4}))
            return output

    provider, clock = TwoSidedProviders(6), FixtureClock()
    options = settings(universe_size=6, deep_per_cycle=0, radar_threshold=0, watch_threshold=0)
    store = DiscoveryStore(str(tmp_path / "pipeline.sqlite3"))
    try:
        engine = DiscoveryEngine(provider, provider, provider,
                                 Settings(top_n=6, db_path=store.database_path), options, store, clock=clock)
        record = await engine.run_once()
        assert record.code_version == DIRECTIONAL_FORMULA_VERSION
        assert record.config["discovery"]["ranking_mode"] == "BOTH"
        short = by_id(record.observations)["unknown-fixture-0000"]
        assert short.bias == "SHORT"
        assert short.stage in {DiscoveryStage.RADAR, DiscoveryStage.WATCH}
        assert short.rank.current_rank is not None
        assert short.features["distance_recent_low"].value is not None
        assert provider.calls["liquidity"] == 0
        restored = by_id(store.get_cycle(record.cycle_id).observations)["unknown-fixture-0000"]
        assert restored.bias == "SHORT"
        assert restored.scores["light_discovery_short"].calibration == "UNCALIBRATED"
    finally:
        store.close()
