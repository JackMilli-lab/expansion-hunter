"""Synthetic point-in-time archive integrity and forward-write growth checks.

No old operational archive is rewritten, and these fixtures are not evidence of
market coverage or predictive performance.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.discovery import DiscoveryEngine
from expansion_hunter.discovery_config import DiscoverySettings
from expansion_hunter.discovery_models import (
    DiscoveryCycle,
    DiscoveryEvent,
    DiscoveryFeature,
    DiscoveryObservation,
    DiscoveryStage,
    Freshness,
)
from expansion_hunter.discovery_store import DiscoveryStore
from expansion_hunter.models import Asset, AssetSnapshot, Candle, Metric, Status

T = datetime(2026, 10, 1, tzinfo=UTC)


def synthetic_cycle(store, identity="synthetic-0", when=T, *, assets=1, bars=20):
    rows = []
    for index in range(assets):
        asset = Asset(asset_id=f"synthetic-{index}", symbol=f"TEST{index}", name="Synthetic only")
        candles = []
        for n in range(bars):
            close = T - timedelta(minutes=bars-n-1)
            price = 100 + index + n * 0.001
            candles.append(Candle(open_time=close-timedelta(minutes=1), close_time=close,
                available_at=close, open=price, high=price+1, low=price-1, close=price+.1,
                volume=50+n, source="synthetic:test-only"))
        snapshot = AssetSnapshot(asset=asset, as_of=T, candles={"1m": candles},
            derivatives={"open_interest": Metric(source="synthetic:unavailable", status=Status.UNAVAILABLE,
                                                 reason="test provider unavailable")})
        rows.append(DiscoveryObservation(asset=asset, as_of=when, snapshot=snapshot,
            eligible=True, disposition="PARTIAL", features={"price": DiscoveryFeature(
                value=100+index, source_timestamp=T, retrieved_at=T, freshness=Freshness.FRESH,
                provider="synthetic:test-only", timeframe="1m")}, reasons=["synthetic decision evidence"]))
    return DiscoveryCycle(cycle_id=identity, started_at=when-timedelta(seconds=1), as_of=when,
        completed_at=when+timedelta(seconds=1), database_path=store.database_path,
        config_hash="synthetic", observations=rows, counts={"assets_discovered": assets})


def legacy_write(store, cycle):
    """The original full-cycle/full-observation serialization, in a test DB."""
    with store._db:
        store._db.execute("INSERT INTO discovery_cycles VALUES(?,?,?,?,?,?,?,?)",
            (cycle.cycle_id, cycle.started_at.isoformat(), cycle.as_of.isoformat(),
             cycle.completed_at.isoformat(), cycle.status, cycle.config_hash,
             cycle.code_version, store._encode(cycle)))
        store._db.execute("INSERT INTO discovery_summaries VALUES(?,?)",
                          (cycle.cycle_id, store._encode_summary(cycle)))
        for row in cycle.observations:
            store._db.execute("INSERT INTO discovery_observations VALUES(?,?,?,?,?,?,?)",
                (cycle.cycle_id, row.asset.asset_id, row.as_of.isoformat(), row.stage,
                 row.disposition, row.rank.current_rank, store._encode(row)))
            store._db.execute("INSERT INTO discovery_monitoring VALUES(?,?,?,?,?)",
                (cycle.cycle_id, row.asset.asset_id, row.stage, None, int(row.deep_scanned)))


def test_unchanged_snapshots_are_shared_and_all_read_contracts_reconstruct(tmp_path):
    store = DiscoveryStore(str(tmp_path / "new.sqlite3"))
    first = synthetic_cycle(store)
    second = synthetic_cycle(store, "synthetic-1", T+timedelta(minutes=1))
    store.save_cycle(first)
    store.save_cycle(second)
    assert store._db.execute("SELECT COUNT(*) FROM discovery_snapshot_objects").fetchone()[0] == 1
    assert store._db.execute("SELECT COUNT(*) FROM discovery_snapshot_references").fetchone()[0] == 2
    assert store.get_cycle(first.cycle_id) == first
    assert store.latest_cycle() == second
    assert store.asset_history("synthetic-0") == first.observations + second.observations
    assert store.history(T+timedelta(minutes=2)) == [first, second]
    assert store.history(T) == []
    assert store.latest_summary().observations[0].snapshot is None
    assert all(c.observations[0].snapshot is None for c in store.ranking_history(T+timedelta(minutes=2)))
    raw = gzip.decompress(store._db.execute("SELECT payload FROM discovery_cycles LIMIT 1").fetchone()[0])
    assert b'"_archive_snapshot":' in raw and b'"candles":' not in raw
    store.close()


def test_changed_availability_creates_a_distinct_vintage_and_unavailable_stays_unknown(tmp_path):
    store = DiscoveryStore(str(tmp_path / "new.sqlite3"))
    first = synthetic_cycle(store)
    second = synthetic_cycle(store, "synthetic-1", T+timedelta(minutes=1))
    second.observations[0].snapshot.as_of = second.as_of
    second.observations[0].snapshot.candles["1m"][-1].available_at = T+timedelta(seconds=5)
    store.save_cycle(first)
    store.save_cycle(second)
    assert store._db.execute("SELECT COUNT(*) FROM discovery_snapshot_objects").fetchone()[0] == 2
    assert store.get_cycle(first.cycle_id) == first
    restored = store.latest_cycle()
    assert restored == second
    oi = restored.observations[0].snapshot.derivatives["open_interest"]
    assert oi.value is None and oi.status == Status.UNAVAILABLE
    assert store.asset_history("synthetic-0", before=T+timedelta(seconds=2)) == first.observations
    store.close()


def test_original_blobs_stay_unchanged_and_old_retry_remains_idempotent(tmp_path):
    store = DiscoveryStore(str(tmp_path / "mixed.sqlite3"))
    old = synthetic_cycle(store, "original-format")
    legacy_write(store, old)
    original = store._db.execute("SELECT payload FROM discovery_cycles WHERE cycle_id=?", (old.cycle_id,)).fetchone()[0]
    digest = hashlib.sha256(original).hexdigest()
    assert store.get_cycle(old.cycle_id) == old
    store.save_cycle(old)
    newer = synthetic_cycle(store, "normalized-format", T+timedelta(minutes=1))
    store.save_cycle(newer)
    assert store.latest_cycle() == newer
    assert store.history(T+timedelta(minutes=2)) == [old, newer]
    assert hashlib.sha256(store._db.execute("SELECT payload FROM discovery_cycles WHERE cycle_id=?", (old.cycle_id,)).fetchone()[0]).hexdigest() == digest
    assert store._db.execute("SELECT COUNT(*) FROM discovery_snapshot_references WHERE cycle_id=?", (old.cycle_id,)).fetchone()[0] == 0
    store.close()


def test_retry_collision_and_event_failure_leave_no_partial_objects_or_references(tmp_path):
    store = DiscoveryStore(str(tmp_path / "atomic.sqlite3"))
    first = synthetic_cycle(store)
    first.events = [DiscoveryEvent(event_id="same-event", cycle_id=first.cycle_id,
        asset_id="synthetic-0", timestamp=T, event_type=DiscoveryStage.RADAR,
        previous_stage=DiscoveryStage.DORMANT, reason="test-only event")]
    store.save_cycle(first)
    store.save_cycle(first)
    changed = first.model_copy(deep=True)
    changed.observations[0].features["price"].value = 999
    with pytest.raises(ValueError, match="immutable.*collision"):
        store.save_cycle(changed)
    second = synthetic_cycle(store, "synthetic-1", T+timedelta(minutes=1))
    second.observations[0].snapshot.as_of = second.as_of
    second.events = [first.events[0].model_copy(update={"cycle_id": second.cycle_id, "timestamp": second.as_of})]
    with pytest.raises(sqlite3.IntegrityError):
        store.save_cycle(second)
    assert store.get_cycle(second.cycle_id) is None
    assert store._db.execute("SELECT COUNT(*) FROM discovery_snapshot_objects").fetchone()[0] == 1
    assert store._db.execute("SELECT COUNT(*) FROM discovery_snapshot_references").fetchone()[0] == 1
    assert store.latest_cycle() == first
    store.close()


@pytest.mark.parametrize("failure", ["missing", "corrupt", "malformed-gzip"])
def test_missing_or_corrupt_snapshot_is_an_explicit_failure(tmp_path, failure):
    store = DiscoveryStore(str(tmp_path / "integrity.sqlite3"))
    cycle = synthetic_cycle(store)
    store.save_cycle(cycle)
    if failure == "missing":
        store._db.execute("PRAGMA foreign_keys=OFF")
        store._db.execute("DELETE FROM discovery_snapshot_objects")
    elif failure == "corrupt":
        store._db.execute("UPDATE discovery_snapshot_objects SET payload=?", (gzip.compress(b'{"corrupt":true}'),))
    else:
        store._db.execute("UPDATE discovery_snapshot_objects SET payload=?", (b"not valid gzip",))
    store._db.commit()
    expected = "missing" if failure == "missing" else "corrupt"
    with pytest.raises(ValueError, match=f"snapshot object {expected}"):
        store.get_cycle(cycle.cycle_id)
    with pytest.raises(ValueError, match=f"snapshot object {expected}"):
        store.save_cycle(cycle)
    # Decision-only reads remain possible and explicitly contain no snapshots.
    assert store.latest_summary().observations[0].snapshot is None
    store.close()


def test_referenced_objects_cannot_be_deleted_with_foreign_keys_enabled(tmp_path):
    store = DiscoveryStore(str(tmp_path / "integrity.sqlite3"))
    store.save_cycle(synthetic_cycle(store))
    with pytest.raises(sqlite3.IntegrityError):
        with store._db:
            store._db.execute("DELETE FROM discovery_snapshot_objects")
    assert store.latest_cycle().observations[0].snapshot is not None
    store.close()


def test_copy_on_write_aging_and_transition_do_not_copy_or_change_cached_history(tmp_path):
    store = DiscoveryStore(str(tmp_path / "copy.sqlite3"))
    original = synthetic_cycle(store).observations[0]
    carried = DiscoveryEngine._carry_observation(original)
    assert carried.snapshot is original.snapshot
    carried.features["price"].value = 999
    carried.reasons.append("new decision")
    assert original.features["price"].value == 100
    assert original.reasons == ["synthetic decision evidence"]
    engine = DiscoveryEngine.__new__(DiscoveryEngine)
    engine.discovery_settings = DiscoverySettings()
    result, _ = engine._transition(carried, original, "synthetic-cycle")
    assert result.snapshot is original.snapshot
    assert result.features["price"] is not carried.features["price"]
    assert original.snapshot.candles["1m"][-1].close == pytest.approx(100.119)
    store.close()


def test_repeated_500_asset_history_growth_is_measured_against_previous_storage(tmp_path):
    assets, cycles, bars = 500, 8, 240
    legacy = DiscoveryStore(str(tmp_path / "legacy.sqlite3"))
    normalized = DiscoveryStore(str(tmp_path / "normalized.sqlite3"))
    for index in range(cycles):
        when = T+timedelta(minutes=index)
        fixture = synthetic_cycle(normalized, f"synthetic-{index}", when, assets=assets, bars=bars)
        normalized.save_cycle(fixture)
        fixture.database_path = legacy.database_path
        legacy_write(legacy, fixture)
    assert normalized._db.execute("SELECT COUNT(*) FROM discovery_snapshot_objects").fetchone()[0] == assets
    assert normalized._db.execute("SELECT COUNT(*) FROM discovery_observations").fetchone()[0] == assets*cycles
    assert normalized.latest_cycle().observations[0].snapshot is not None
    sizes = {}
    for name, store in (("legacy", legacy), ("normalized", normalized)):
        store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        sizes[name] = store._db.execute("PRAGMA page_count").fetchone()[0] * store._db.execute("PRAGMA page_size").fetchone()[0]
    reduction = 1-sizes["normalized"]/sizes["legacy"]
    print(json.dumps({"scope": "SYNTHETIC_SQLITE_STORAGE_ONLY", "assets": assets, "cycles": cycles,
        "bars_per_asset": bars, "legacy_database_bytes": sizes["legacy"],
        "normalized_database_bytes": sizes["normalized"], "reduction_fraction": reduction}))
    assert reduction > .60
    legacy.close()
    normalized.close()
