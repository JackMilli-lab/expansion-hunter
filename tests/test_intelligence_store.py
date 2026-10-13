"""Synthetic intelligence archive checks; no market-prediction claims."""

from __future__ import annotations

import gzip
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.models import Metric, Status
from expansion_hunter.storage import Store

T = datetime(2026, 9, 1, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    archive = IntelligenceStore(str(tmp_path / "operational.sqlite3"))
    try:
        yield archive
    finally:
        archive.close()


def test_migrations_are_idempotent_and_preserve_legacy_evidence(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    original = Store(str(path))
    original.begin("legacy-failure", T)
    original._db.execute(
        "INSERT INTO observations VALUES(?,?,?,?,?)",
        ("legacy-failure", "fixture-asset", T.isoformat(), gzip.compress(b"original evidence"), "{}"),
    )
    original._db.execute(
        "INSERT INTO global_observations VALUES(?,?,?)", ("legacy-failure", T.isoformat(), "{}")
    )
    original._db.commit()
    original.fail("legacy-failure", "synthetic recorded outage")
    expected = {
        name: original._db.execute(f"SELECT * FROM {name}").fetchall()
        for name in ("runs", "observations", "global_observations")
    }
    original.close()
    for _ in range(2):
        migrated = IntelligenceStore(str(path))
        assert migrated._db.execute("SELECT version FROM intelligence_migrations ORDER BY version").fetchall() == [(1,), (2,)]
        assert migrated._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert migrated._db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        for name, rows in expected.items():
            assert migrated._db.execute(f"SELECT * FROM {name}").fetchall() == rows
        indexes = {
            row[0] for row in migrated._db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {"intelligence_asof", "intelligence_asset", "discovery_asset_time"} <= indexes
        migrated.close()


def test_publication_boundary_excludes_delayed_and_future_intelligence(store):
    assert store.append("macro_release", "initial", T, T + timedelta(minutes=2), {"actual": 2.5})
    assert store.append("macro_release", "revision", T, T + timedelta(days=30), {"actual": 2.6})
    assert store.objects("macro_release", T) == []
    assert store.objects("macro_release", T + timedelta(minutes=2)) == [{"actual": 2.5}]
    assert store.objects("macro_release", T + timedelta(days=30)) == [{"actual": 2.5}, {"actual": 2.6}]
    assert store.objects("unavailable-kind", T) == []


def test_same_id_retry_is_idempotent_and_conflicting_payload_cannot_replace_initial(store):
    payload = {"actual_initial": 2.5, "consensus": None}
    assert store.append("macro", "release-1", T, T, payload, "bitcoin")
    assert not store.append("macro", "release-1", T, T, {"consensus": None, "actual_initial": 2.5}, "bitcoin")
    with pytest.raises(ValueError, match="immutable.*collision"):
        store.append("macro", "release-1", T, T, {"actual_initial": 2.6}, "bitcoin")
    with pytest.raises(ValueError, match="immutable.*collision"):
        store.append("macro", "release-1", T, T + timedelta(seconds=1), payload, "bitcoin")
    assert store.objects("macro", T) == [payload]


def test_intelligence_query_filters_assets_and_bounds_latest_results(store):
    for index in range(5):
        store.append("research", str(index), T, T + timedelta(seconds=index), {"index": index},
                     "bitcoin" if index % 2 == 0 else "ethereum")
    cutoff = T + timedelta(minutes=1)
    assert store.objects("research", cutoff, asset_id="bitcoin") == [{"index": 0}, {"index": 2}, {"index": 4}]
    assert store.objects("research", cutoff, limit=2) == [{"index": 3}, {"index": 4}]
    assert store.objects("research", cutoff, limit=0) == []
    assert store.objects("research", cutoff, limit=-1) == []


def test_timezone_and_dst_are_normalized_to_same_utc_publication(store):
    before_fallback = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=0)
    after_fallback = before_fallback.replace(fold=1)
    store.append("calendar", "first", before_fallback, before_fallback, {"event": "first"})
    store.append("calendar", "second", after_fallback, after_fallback, {"event": "second"})
    assert store.objects("calendar", before_fallback) == [{"event": "first"}]
    assert store.objects("calendar", after_fallback) == [{"event": "first"}, {"event": "second"}]
    timestamps = store._db.execute("SELECT available_at FROM intelligence_objects ORDER BY available_at").fetchall()
    assert timestamps == [("2026-11-01T05:30:00+00:00",), ("2026-11-01T06:30:00+00:00",)]


@pytest.mark.parametrize("event,availability", [(T, T - timedelta(seconds=1)), (T.replace(tzinfo=None), T)])
def test_invalid_availability_is_rejected_before_archive_mutation(store, event, availability):
    with pytest.raises(ValueError):
        store.append("macro", "invalid", event, availability, {"actual": 1})
    assert store.objects("macro", T) == []


def test_json_rejects_nonfinite_values_instead_of_inventing_numbers(store):
    with pytest.raises(ValueError):
        store.append("factor", "invalid", T, T, {"value": float("nan")})
    assert store.objects("factor", T) == []


def test_model_serialization_and_domain_content_id_are_reproducible(store):
    model = Metric(value=100, observed_at=T, available_at=T, source="synthetic:public", status=Status.AVAILABLE)
    assert store.save_domain("native_metric", model, event_at=T, available_at=T, asset_id="bitcoin")
    assert not store.save_domain("native_metric", model, event_at=T, available_at=T, asset_id="bitcoin")
    assert store.objects("native_metric", T) == [model.model_dump(mode="json")]


def test_signal_context_is_immutable_and_hidden_until_available(store):
    known = T + timedelta(seconds=2)
    context = {"cycle": "UNCERTAIN", "macro": {"mode": "EVENT_RISK"}, "oi": None}
    store.record_context("signal-1", known, context)
    store.record_context("signal-1", known, context)
    assert store.signal_context("signal-1", T) is None
    assert store.signal_context("signal-1", known) == context
    assert store.signal_context("missing", known) is None
    with pytest.raises(ValueError, match="context.*collision"):
        store.record_context("signal-1", known, {"cycle": "BULL"})
    assert store.signal_context("signal-1", known) == context


def test_runtime_cache_advances_without_rewriting_archival_features(store):
    store.append("cycle", "first", T, T, {"cycle": "UNCERTAIN"})
    store.put_state("cycle", "active", T, {"cycle": "UNCERTAIN"})
    store.put_state("cycle", "active", T + timedelta(minutes=1), {"cycle": "EARLY_RECOVERY"})
    with pytest.raises(ValueError, match="backward"):
        store.put_state("cycle", "active", T, {"cycle": "obsolete"})
    assert store.states("cycle") == [{"cycle": "EARLY_RECOVERY"}]
    assert store.objects("cycle", T + timedelta(hours=1)) == [{"cycle": "UNCERTAIN"}]
    assert store.states("unknown-kind") == []


def test_watchlist_priority_notes_and_removed_membership_survive_restart(tmp_path):
    path = str(tmp_path / "ops.sqlite3")
    first = IntelligenceStore(path)
    first.watchlist_add("ripple", "XRPUSDT", priority=10, notes="priority monitoring only", at=T)
    first.watchlist_add("bitcoin", "BTCUSDT", priority=99, at=T)
    first.watchlist_add("ethereum", "ETHUSDT", priority=1, pinned=True, at=T)
    first.close()
    restored = IntelligenceStore(path)
    assert [row["asset_id"] for row in restored.watchlist()] == ["ethereum", "bitcoin", "ripple"]
    xrp = next(row for row in restored.watchlist() if row["asset_id"] == "ripple")
    assert xrp["notes"] == "priority monitoring only"
    assert xrp["added_at"] == T.isoformat()
    restored.watchlist_add("ripple", "XRPUSDT", priority=20, pinned=True, notes="updated", at=T + timedelta(minutes=1))
    updated = next(row for row in restored.watchlist() if row["asset_id"] == "ripple")
    assert updated["added_at"] == T.isoformat()
    assert updated["updated_at"] == (T + timedelta(minutes=1)).isoformat()
    assert updated["priority"] == 20
    restored.watchlist_remove("ripple", at=T + timedelta(minutes=2))
    restored.close()
    restarted = IntelligenceStore(path)
    assert "ripple" not in {row["asset_id"] for row in restarted.watchlist()}
    # Removal is a tombstone; research can still inspect its historical membership.
    assert restarted._db.execute("SELECT removed_at FROM personal_watchlist WHERE asset_id='ripple'").fetchone()[0]
    restarted.watchlist_add("ripple", "XRPUSDT", at=T + timedelta(minutes=3))
    assert "ripple" in {row["asset_id"] for row in restarted.watchlist()}
    restarted.close()


@pytest.mark.parametrize("fields", [
    {"asset_id": "", "symbol": "XRPUSDT"},
    {"asset_id": "ripple", "symbol": ""},
    {"asset_id": "ripple", "symbol": "XRPUSDT", "priority": -1},
    {"asset_id": "ripple", "symbol": "XRPUSDT", "priority": 101},
    {"asset_id": "ripple", "symbol": "XRPUSDT", "notes": "x" * 2001},
])
def test_watchlist_invalid_input_is_not_persisted(store, fields):
    with pytest.raises(ValueError, match="watchlist"):
        store.watchlist_add(**fields, at=T)
    assert store.watchlist() == []


def test_notification_outbox_deduplicates_attempts_and_records_delivery(store):
    store.enqueue_notification("signal-1", "telegram", T)
    store.enqueue_notification("signal-1", "telegram", T + timedelta(seconds=1))
    store.enqueue_notification("signal-1", "browser", T)
    assert store._db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 2
    store.notification_result("signal-1", "telegram", T + timedelta(seconds=2), error="synthetic unavailable")
    failed = store._db.execute(
        "SELECT attempts,delivered_at,last_error,created_at FROM notification_outbox WHERE channel='telegram'"
    ).fetchone()
    assert failed == (1, None, "synthetic unavailable", T.isoformat())
    delivered = T + timedelta(seconds=5)
    store.notification_result("signal-1", "telegram", delivered)
    assert store._db.execute(
        "SELECT attempts,delivered_at,last_error FROM notification_outbox WHERE channel='telegram'"
    ).fetchone() == (2, delivered.isoformat(), None)


def test_shared_connection_serializes_parallel_immutable_writes(store):
    def append(index):
        store.append("feed_health", f"health-{index}", T, T, {"index": index})
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(append, range(24)))
    assert {row["index"] for row in store.objects("feed_health", T)} == set(range(24))
    assert store._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_database_handles_remain_available_to_independent_reader(tmp_path):
    path = str(tmp_path / "ops.sqlite3")
    store = IntelligenceStore(path)
    store.append("macro", "release", T, T, {"actual": None})
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    assert reader.execute("SELECT COUNT(*) FROM intelligence_objects").fetchone()[0] == 1
    reader.close()
    store.close()
