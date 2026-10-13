"""End-to-end discovery contracts using explicit synthetic data, never winners.

Zero RADAR/WATCH thresholds below exercise infrastructure independently of the
uncalibrated production rules. They are not candidate-quality recommendations.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from benchmarks.phase1_benchmark import FixtureClock, SyntheticProviders
from expansion_hunter.config import Settings
from expansion_hunter.discovery import DiscoveryEngine
from expansion_hunter.discovery_config import DiscoverySettings
from expansion_hunter.discovery_models import DiscoveryStage, Freshness
from expansion_hunter.discovery_store import DiscoveryStore
from expansion_hunter.models import TIMEFRAMES


@pytest.fixture
def build_engine(tmp_path):
    stores = []

    def build(size=20, *, deep=3, provider=None, **discovery_options):
        provider = provider or SyntheticProviders(size)
        clock = FixtureClock()
        path = tmp_path / f"operational-discovery-{len(stores)}.sqlite3"
        store = DiscoveryStore(str(path))
        stores.append(store)
        settings = Settings(top_n=size, concurrency=6, db_path=str(path))
        options = {
            "universe_size": size,
            "deep_per_cycle": deep,
            "deep_watchlist_size": min(size, 50),
            "radar_threshold": 0,
            "watch_threshold": 0,
        }
        options.update(discovery_options)
        discovery = DiscoverySettings(**options)
        engine = DiscoveryEngine(provider, provider, provider, settings, discovery, store, clock=clock)
        return engine, provider, clock, store, path

    yield build
    for store in stores:
        store.close()


async def test_broad_1000_assets_only_promotes_budgeted_deep_subset(build_engine):
    engine, provider, _, store, path = build_engine(1000, deep=2)
    cycle = await engine.run_once()
    assert cycle.counts["assets_discovered"] == 1000
    assert len(cycle.observations) == 1000
    assert cycle.counts["assets_light_scanned"] == 1000
    assert cycle.counts["assets_deep_scanned"] <= 2
    assert provider.calls["liquidity"] <= 2
    assert provider.calls["derivatives"] <= 2
    assert provider.calls["light_market"] == 1
    # One inexpensive history per asset, then bounded deep frames. This guards
    # against accidentally running the old seven-frame/book scanner on 1000.
    assert provider.calls["candles"] <= 1000 + 2 * 6 + 2
    assert cycle.database_path == str(path)
    assert store.latest_cycle().cycle_id == cycle.cycle_id
    assert cycle.config_hash


async def test_unknown_asset_is_promoted_without_manual_symbol_list(build_engine):
    engine, provider, _, store, _ = build_engine(20, deep=3)
    cycle = await engine.run_once()
    unknown = [o for o in cycle.observations if o.asset.asset_id.startswith("unknown-fixture-")]
    promoted = [o for o in unknown if o.stage in {
        DiscoveryStage.RADAR, DiscoveryStage.WATCH, DiscoveryStage.PRE_IGNITION,
        DiscoveryStage.LOADED, DiscoveryStage.IGNITION,
    }]
    assert promoted
    assert all(o.asset.exchange_symbol for o in promoted)
    assert provider.calls["resolve_assets"] >= 1
    assert any(event.asset_id == promoted[0].asset.asset_id for event in cycle.events)
    events = store.events(asset_id=promoted[0].asset.asset_id)
    assert any(event.cycle_id == cycle.cycle_id for event in events)
    archived = store.asset_history(promoted[0].asset.asset_id)
    assert archived[-1].stage == promoted[0].stage
    assert archived[-1].features


async def test_missing_derivatives_does_not_block_spot_discovery(build_engine):
    engine, _, _, _, _ = build_engine(20, deep=3)
    cycle = await engine.run_once()
    deep = [o for o in cycle.observations if o.deep_scanned]
    assert deep
    assert cycle.counts["assets_light_scanned"] == 20
    for observation in deep:
        assert observation.snapshot
        assert observation.snapshot.candles["4h"]
        assert observation.snapshot.derivatives["open_interest"].value is None
        assert observation.snapshot.derivatives["funding_rate"].value is None
    assert cycle.status != "FAILED"


async def test_every_failed_or_partial_asset_has_persisted_reason(build_engine):
    provider = SyntheticProviders(20, failures={"unknown-fixture-0000"}, missing={"unknown-fixture-0001"})
    engine, _, _, store, path = build_engine(20, provider=provider)
    cycle = await engine.run_once()
    assert len(cycle.observations) == 20
    by_id = {o.asset.asset_id: o for o in cycle.observations}
    failed = by_id["unknown-fixture-0000"]
    partial = by_id["unknown-fixture-0001"]
    assert failed.disposition in {"FAILED", "PARTIAL", "EXCLUDED"}
    assert failed.reasons or failed.missing
    assert partial.disposition in {"PARTIAL", "EXCLUDED", "FAILED"}
    assert partial.reasons or partial.missing
    assert cycle.counts["assets_partial"] + cycle.counts["assets_failed"] + cycle.counts["assets_excluded"] >= 2
    assert store.asset_history(failed.asset.asset_id)[-1].reasons == failed.reasons
    # Every discovery attempt has a row, regardless of tradability.
    with sqlite3.connect(path) as database:
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "discovery_observations" in tables
    assert len(store.latest_cycle().observations) == 20


async def test_stale_critical_data_cannot_emit_loaded_or_ignition(build_engine):
    identity = "unknown-fixture-0000"
    provider = SyntheticProviders(20, stale={identity})
    engine, _, _, store, _ = build_engine(20, provider=provider)
    cycle = await engine.run_once()
    observation = next(o for o in cycle.observations if o.asset.asset_id == identity)
    assert observation.freshness in {Freshness.STALE, Freshness.MISSING, Freshness.DEGRADED}
    assert observation.stage not in {DiscoveryStage.LOADED, DiscoveryStage.IGNITION}
    assert not any(event.asset_id == identity and event.high_priority for event in cycle.events)
    assert observation.reasons or observation.missing
    assert store.asset_history(identity)[-1].freshness == observation.freshness


async def test_warm_cycle_skips_unchanged_light_histories(build_engine):
    engine, provider, clock, _, _ = build_engine(20, deep=0)
    await engine.run_once()
    first = provider.calls["candles"]
    clock.advance(300)
    cycle = await engine.run_once()
    assert provider.calls["candles"] == first
    assert len(cycle.observations) == 20
    assert cycle.counts["assets_light_scanned"] == 20


async def test_incremental_light_histories_request_only_new_bar_prefix(build_engine):
    engine, provider, clock, _, _ = build_engine(20, deep=0)
    await engine.run_once()
    count = len(provider.candle_limits)
    clock.advance(900)
    await engine.run_once()
    updates = provider.candle_limits[count:]
    assert updates
    assert all(timeframe == "15m" and limit <= 3 for _, timeframe, limit in updates)


async def test_archive_keeps_cycles_and_previous_rank_instead_of_overwriting(build_engine):
    engine, _, clock, store, _ = build_engine(20, deep=0)
    first = await engine.run_once()
    clock.advance(900)
    second = await engine.run_once()
    assert first.cycle_id != second.cycle_id
    assert [c.cycle_id for c in store.history(second.as_of)] == [first.cycle_id, second.cycle_id]
    earlier = store.history(first.as_of)
    assert [c.cycle_id for c in earlier] == [first.cycle_id]
    first_ranks = {o.asset.asset_id: o.rank.current_rank for o in first.observations}
    for observation in second.observations:
        if observation.rank.current_rank is not None:
            assert observation.rank.previous_rank == first_ranks[observation.asset.asset_id]


async def test_source_retrieval_and_feature_times_never_exceed_knowledge_boundary(build_engine):
    engine, _, _, store, _ = build_engine(20, deep=2)
    cycle = await engine.run_once()
    for observation in cycle.observations:
        assert observation.as_of <= cycle.as_of
        for feature in observation.features.values():
            if feature.value is not None:
                assert feature.source_timestamp <= feature.retrieved_at <= observation.as_of
                assert feature.age_seconds is not None
                assert feature.provider and feature.timeframe
        if observation.snapshot:
            for bars in observation.snapshot.candles.values():
                assert all(b.available_at <= cycle.as_of and b.close_time <= cycle.as_of for b in bars)
    assert store.history(cycle.as_of - timedelta(microseconds=1)) == []


async def test_future_candle_version_is_excluded_not_used_as_ignition(build_engine):
    class FutureVersionProvider(SyntheticProviders):
        async def candles(self, asset, timeframe, limit, as_of):
            bars = await super().candles(asset, timeframe, limit, as_of)
            future = bars[-1].model_copy(update={
                "open_time": as_of + timedelta(hours=1),
                "close_time": as_of + timedelta(hours=1, seconds=TIMEFRAMES[timeframe]),
                "available_at": as_of + timedelta(hours=1, seconds=TIMEFRAMES[timeframe]),
                "open": 1000,
                "high": 1100,
                "low": 900,
                "close": 1050,
                "volume": 1_000_000,
                "quote_volume": 1_000_000_000,
                "taker_buy_volume": 900_000,
            })
            return bars + [future]

    engine, _, _, _, _ = build_engine(20, deep=2, provider=FutureVersionProvider(20))
    cycle = await engine.run_once()
    for observation in cycle.observations:
        if observation.snapshot:
            assert all(
                bar.close_time <= cycle.as_of and bar.available_at <= cycle.as_of
                for bars in observation.snapshot.candles.values() for bar in bars
            )
        assert observation.stage != DiscoveryStage.IGNITION


async def test_universe_outage_is_archived_as_failed_cycle(build_engine):
    provider = SyntheticProviders(20, fail_universe=True)
    engine, _, _, store, _ = build_engine(20, provider=provider)
    cycle = await engine.run_once()
    assert cycle.status == "FAILED"
    assert any("outage" in issue for issue in cycle.provider_issues)
    assert store.latest_cycle().cycle_id == cycle.cycle_id
    assert provider.calls["liquidity"] == 0


async def test_configurable_zero_deep_budget_preserves_light_discovery(build_engine):
    engine, provider, _, _, _ = build_engine(20, deep=10, deep_weight_budget=0)
    cycle = await engine.run_once()
    assert len(cycle.observations) == 20
    assert cycle.counts["assets_deep_scanned"] == 0
    assert provider.calls["liquidity"] == 0
    assert provider.calls["derivatives"] == 0
    assert any(o.monitoring_due_at is not None for o in cycle.observations)
