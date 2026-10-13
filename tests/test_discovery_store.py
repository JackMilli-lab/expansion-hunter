"""Synthetic archive integrity checks; these are not historical market claims."""

from __future__ import annotations

import gzip
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.discovery_models import (
    DiscoveryCycle,
    DiscoveryEvent,
    DiscoveryFeature,
    DiscoveryObservation,
    DiscoveryStage,
    Freshness,
)
from expansion_hunter.discovery_store import DiscoveryStore
from expansion_hunter.models import Asset, AssetSnapshot
from expansion_hunter.storage import Store

T = datetime(2026, 9, 1, tzinfo=UTC)


def observation(identity="synthetic-alpha", when=T, disposition="COMPLETE"):
    return DiscoveryObservation(
        asset=Asset(asset_id=identity, symbol=identity.upper(), name="Synthetic fixture"),
        as_of=when, disposition=disposition, eligible=disposition != "EXCLUDED",
        stage=DiscoveryStage.RADAR, previous_stage=DiscoveryStage.DORMANT,
        freshness=Freshness.FRESH,
        reasons=["synthetic rank improvement"],
        features={"price": DiscoveryFeature(
            value=100, provider="test-only:synthetic", timeframe="15m",
            source_timestamp=when, retrieved_at=when, freshness=Freshness.FRESH,
        )},
    )


def cycle(store, identity="synthetic-cycle", when=T, rows=None, events=None):
    return DiscoveryCycle(
        cycle_id=identity, started_at=when - timedelta(seconds=10), as_of=when,
        completed_at=when + timedelta(seconds=5), database_path=store.database_path,
        config_hash="synthetic-config", config={"universe_size": 1000},
        observations=rows if rows is not None else [observation(when=when)],
        events=events or [], counts={"assets_discovered": len(rows or [1])},
    )


def event(cycle_id="synthetic-cycle", identity="synthetic-event", when=T):
    return DiscoveryEvent(
        event_id=identity, cycle_id=cycle_id, asset_id="synthetic-alpha", timestamp=when,
        event_type=DiscoveryStage.RADAR, previous_stage=DiscoveryStage.DORMANT,
        reason="synthetic promotion",
    )


def test_atomic_archive_contains_failed_excluded_and_source_lineage(tmp_path):
    store = DiscoveryStore(str(tmp_path / "operational.sqlite3"))
    rows = [observation("synthetic-" + state.lower(), disposition=state)
            for state in ("COMPLETE", "PARTIAL", "EXCLUDED", "FAILED")]
    payload = cycle(store, rows=rows)
    store.save_cycle(payload)
    read = store.get_cycle(payload.cycle_id)
    assert read == payload
    assert read.config["universe_size"] == 1000
    assert {item.disposition for item in read.observations} == {"COMPLETE", "PARTIAL", "EXCLUDED", "FAILED"}
    assert read.observations[0].features["price"].retrieved_at == T
    assert store._db.execute("SELECT COUNT(*) FROM discovery_observations").fetchone()[0] == 4
    assert store._db.execute("SELECT COUNT(*) FROM discovery_transitions").fetchone()[0] == 4
    assert store.database_path == str((tmp_path / "operational.sqlite3").resolve())
    store.close()


def test_cycle_retry_is_idempotent_but_changed_evidence_rejected(tmp_path):
    store = DiscoveryStore(str(tmp_path / "ops.sqlite3"))
    payload = cycle(store, events=[event()])
    store.save_cycle(payload)
    store.save_cycle(payload)
    assert len(store.events()) == 1
    changed = payload.model_copy(deep=True)
    changed.observations[0].features["price"].value = 101
    with pytest.raises(ValueError, match="immutable.*collision"):
        store.save_cycle(changed)
    assert store.latest_cycle() == payload
    store.close()


def test_event_collision_rolls_back_complete_new_cycle(tmp_path):
    store = DiscoveryStore(str(tmp_path / "ops.sqlite3"))
    first = cycle(store, events=[event()])
    store.save_cycle(first)
    next_time = T + timedelta(minutes=5)
    second = cycle(store, "next-cycle", next_time,
                   events=[event("next-cycle", when=next_time)])
    with pytest.raises(sqlite3.IntegrityError):
        store.save_cycle(second)
    assert store.get_cycle("next-cycle") is None
    assert len(store.asset_history("synthetic-alpha")) == 1
    assert len(store.events()) == 1
    store.close()


def test_history_uses_publication_time_not_backdated_market_time(tmp_path):
    store = DiscoveryStore(str(tmp_path / "ops.sqlite3"))
    old = cycle(store)
    store.save_cycle(old)
    assert store.history(T) == []
    assert store.asset_history("synthetic-alpha", before=T) == []
    assert store.history(T + timedelta(seconds=5)) == [old]
    newer = cycle(store, "second", T + timedelta(hours=1))
    store.save_cycle(newer)
    assert [row.cycle_id for row in store.history(T + timedelta(hours=2))] == [old.cycle_id, "second"]
    assert store.asset_history("synthetic-alpha", before=T + timedelta(minutes=30)) == old.observations
    assert store.history(T + timedelta(hours=2), limit=1) == [newer]
    assert store.get_cycle("absent") is None
    store.close()


def test_future_features_and_duplicate_assets_cannot_enter_archive(tmp_path):
    store = DiscoveryStore(str(tmp_path / "ops.sqlite3"))
    invalid = cycle(store)
    invalid.observations[0].features["price"].retrieved_at = T + timedelta(seconds=1)
    with pytest.raises(ValueError, match="retrieval"):
        store.save_cycle(invalid)
    with pytest.raises(ValueError, match="duplicate asset"):
        store.save_cycle(cycle(store, rows=[observation(), observation()]))
    assert store.latest_cycle() is None
    store.close()


def legacy_archive(path):
    old = Store(str(path))
    old.begin("legacy-one", T)
    old._db.execute(
        "INSERT INTO observations VALUES (?, ?, ?, ?, ?)",
        ("legacy-one", "synthetic-alpha", T.isoformat(), gzip.compress(b"synthetic immutable evidence"), "{}"),
    )
    old._db.execute("INSERT INTO global_observations VALUES (?, ?, ?)",
                    ("legacy-one", T.isoformat(), "{}"))
    old._db.commit()
    old.fail("legacy-one", "recorded synthetic outage")
    old.close()


def test_legacy_import_preserves_original_and_is_idempotent(tmp_path):
    source = tmp_path / "smoke.sqlite3"
    legacy_archive(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    store = DiscoveryStore(str(tmp_path / "cockpit.sqlite3"))
    counts = store.import_legacy(str(source))
    assert counts == {"runs": 1, "observations": 1, "global_observations": 1, "already_present": 0}
    assert store.run_status()[0]["error"] == "recorded synthetic outage"
    again = store.import_legacy(str(source))
    assert again == {"runs": 0, "observations": 0, "global_observations": 0, "already_present": 3}
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert source.exists()
    store.close()


def test_legacy_import_collision_rolls_back_other_rows(tmp_path):
    source = tmp_path / "smoke.sqlite3"
    legacy_archive(source)
    store = DiscoveryStore(str(tmp_path / "cockpit.sqlite3"))
    store.begin("legacy-one", T)
    store.fail("legacy-one", "conflicting evidence")
    with pytest.raises(ValueError, match="collision"):
        store.import_legacy(str(source))
    assert store._db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    assert store.run_status()[0]["error"] == "conflicting evidence"
    store.close()


def test_events_filter_asset_and_historical_publication(tmp_path):
    store = DiscoveryStore(str(tmp_path / "ops.sqlite3"))
    store.save_cycle(cycle(store, events=[event()]))
    assert store.events(before=T) == []
    assert store.events(asset_id="absent") == []
    assert store.events(asset_id="synthetic-alpha")[0].event_id == "synthetic-event"
    store.close()


def test_ranking_history_is_causal_bounded_and_thin_without_mutating_archive(tmp_path):
    store = DiscoveryStore(str(tmp_path / "ops.sqlite3"))
    for index in range(50):
        when = T + timedelta(hours=index)
        item = observation(when=when)
        item.snapshot = AssetSnapshot(asset=item.asset, as_of=when, candles={})
        store.save_cycle(cycle(store, f"synthetic-{index:03d}", when, rows=[item]))
    cutoff = T + timedelta(hours=48, seconds=5)
    thin = store.ranking_history(cutoff)
    assert len(thin) <= 8  # latest two plus six reporting windows, deduplicated
    assert all(row.completed_at <= cutoff for row in thin)
    assert all(row.observations[0].snapshot is None for row in thin)
    ids = {row.cycle_id for row in thin}
    assert "synthetic-048" in ids and "synthetic-047" in ids
    assert "synthetic-024" in ids  # nearest known 24-hour predecessor
    assert "synthetic-049" not in ids
    assert store.get_cycle("synthetic-048").observations[0].snapshot is not None
    assert store.ranking_history(T) == []
    store.close()
