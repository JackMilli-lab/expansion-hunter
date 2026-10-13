"""Thin production reads retain immutable decisions, not costly OHLCV histories.

Fixtures are explicitly synthetic; no historical market claim is made here.
"""
from __future__ import annotations

import gzip
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.config import Settings
from expansion_hunter.discovery_models import (
    DiscoveryCycle,
    DiscoveryEvent,
    DiscoveryFeature,
    DiscoveryObservation,
    DiscoveryScore,
    DiscoveryStage,
    Freshness,
    RankHistory,
)
from expansion_hunter.discovery_store import DiscoveryStore
from expansion_hunter.live_runtime import LiveObservationWorker
from expansion_hunter.models import Asset, AssetSnapshot, Candle

T = datetime(2026, 10, 1, tzinfo=UTC)


def fixture_cycle(store, identity="synthetic-one", when=T):
    asset = Asset(asset_id="synthetic-asset", symbol="TEST", name="Test only", exchange_symbol="TESTUSDT")
    candle = Candle(open_time=when-timedelta(minutes=1), close_time=when,
                    available_at=when, open=100, high=101, low=99, close=100,
                    volume=20, source="test-only:synthetic")
    observation = DiscoveryObservation(
        asset=asset, as_of=when, eligible=True, stage=DiscoveryStage.WATCH,
        previous_stage=DiscoveryStage.RADAR, disposition="PARTIAL", freshness=Freshness.FRESH,
        features={"price": DiscoveryFeature(value=100, source_timestamp=when,
            retrieved_at=when, provider="test-only:synthetic", timeframe="1m", freshness=Freshness.FRESH)},
        scores={"stored_energy": DiscoveryScore(value=70, coverage=0.5)},
        rank=RankHistory(current_rank=1, previous_rank=5, rank_velocity=4),
        levels={"resistance": 101}, reasons=["synthetic improving rank"],
        snapshot=AssetSnapshot(asset=asset, as_of=when, candles={"1m": [candle]}),
    )
    return DiscoveryCycle(cycle_id=identity, started_at=when-timedelta(seconds=1),
        as_of=when, completed_at=when+timedelta(seconds=1), database_path=store.database_path,
        config_hash="synthetic", observations=[observation], counts={"assets_discovered": 1},
        events=[DiscoveryEvent(event_id="event:"+identity, cycle_id=identity,
            asset_id=asset.asset_id, timestamp=when, event_type=DiscoveryStage.WATCH,
            previous_stage=DiscoveryStage.RADAR, reason="synthetic promotion")],
        provider_issues=["explicit test-only missing derivatives"],
    )


def test_summary_keeps_all_decision_evidence_and_full_archive(tmp_path):
    store = DiscoveryStore(str(tmp_path / "summary.sqlite3"))
    cycle = fixture_cycle(store)
    store.save_cycle(cycle)
    summary = store.latest_summary()
    expected = cycle.model_copy(deep=True)
    expected.observations[0].snapshot = None
    assert summary == expected
    assert store.get_cycle(cycle.cycle_id) == cycle
    assert store.latest_cycle() == cycle
    thin = store._db.execute("SELECT payload FROM discovery_summaries").fetchone()[0]
    assert b'"snapshot"' not in gzip.decompress(thin)
    assert b'"candles"' not in gzip.decompress(thin)
    store.close()


def test_cached_read_cannot_mutate_next_read_or_archive(tmp_path, monkeypatch):
    store = DiscoveryStore(str(tmp_path / "summary.sqlite3"))
    cycle = fixture_cycle(store)
    store.save_cycle(cycle)
    summary = store.latest_summary()
    summary.observations[0].features["price"].value = 999
    summary.observations[0].reasons.append("untrusted consumer mutation")
    monkeypatch.setattr(store, "_read_summary", lambda *args: pytest.fail("cached cycle decoded twice"))
    assert store.latest_summary().observations[0].features["price"].value == 100
    assert store.latest_summary().observations[0].reasons == ["synthetic improving rank"]
    assert store.get_cycle(cycle.cycle_id) == cycle
    store.close()


def test_cache_detects_another_writer_and_never_reads_full_blob(tmp_path):
    path = str(tmp_path / "summary.sqlite3")
    reader = DiscoveryStore(path)
    writer = DiscoveryStore(path)
    writer.save_cycle(fixture_cycle(writer))
    queries = []
    reader._db.set_trace_callback(queries.append)
    assert reader.latest_summary().cycle_id == "synthetic-one"
    assert reader.latest_summary().cycle_id == "synthetic-one"
    writer.save_cycle(fixture_cycle(writer, "synthetic-two", T+timedelta(minutes=1)))
    assert reader.latest_summary().cycle_id == "synthetic-two"
    assert not any("SELECT payload FROM discovery_cycles" in sql for sql in queries)
    assert sum("SELECT payload FROM discovery_summaries" in sql for sql in queries) == 2
    reader.close()
    writer.close()


def test_old_archive_fallback_is_cached_and_does_not_rewrite_evidence(tmp_path, monkeypatch):
    store = DiscoveryStore(str(tmp_path / "summary.sqlite3"))
    cycle = fixture_cycle(store)
    store.save_cycle(cycle)
    store._db.execute("DELETE FROM discovery_summaries")
    store._db.commit()
    original = store._db.execute("SELECT payload FROM discovery_cycles").fetchone()[0]
    digest = hashlib.sha256(original).hexdigest()
    assert store.latest_summary().observations[0].snapshot is None
    monkeypatch.setattr(store, "_read_summary", lambda *args: pytest.fail("old archive decoded twice"))
    assert store.latest_summary().observations[0].rank.current_rank == 1
    assert store._db.execute("SELECT COUNT(*) FROM discovery_summaries").fetchone()[0] == 0
    assert hashlib.sha256(store._db.execute("SELECT payload FROM discovery_cycles").fetchone()[0]).hexdigest() == digest
    assert store.get_cycle(cycle.cycle_id) == cycle
    store.close()


def test_summary_transaction_rolls_back_with_failed_event(tmp_path):
    store = DiscoveryStore(str(tmp_path / "summary.sqlite3"))
    first = fixture_cycle(store)
    store.save_cycle(first)
    second = fixture_cycle(store, "synthetic-two", T+timedelta(minutes=1))
    second.events[0].event_id = first.events[0].event_id
    with pytest.raises(sqlite3.IntegrityError):
        store.save_cycle(second)
    assert store.get_cycle(second.cycle_id) is None
    assert store._db.execute("SELECT cycle_id FROM discovery_summaries").fetchall() == [(first.cycle_id,)]
    assert store.latest_summary().cycle_id == first.cycle_id
    store.close()


def test_empty_and_retry_are_safe(tmp_path):
    store = DiscoveryStore(str(tmp_path / "summary.sqlite3"))
    assert store.latest_summary() is None
    cycle = fixture_cycle(store)
    store.save_cycle(cycle)
    store.save_cycle(cycle)
    assert store._db.execute("SELECT COUNT(*) FROM discovery_summaries").fetchone()[0] == 1
    store.close()


def test_fast_membership_uses_summary_without_decoding_histories(tmp_path, monkeypatch):
    worker = LiveObservationWorker(Settings(db_path=str(tmp_path / "summary.sqlite3")))
    worker.store.save_cycle(fixture_cycle(worker.store))
    monkeypatch.setattr(worker.store, "latest_cycle", lambda: pytest.fail("fast membership read complete history"))
    assert list(worker.membership()) == ["TESTUSDT"]
    monkeypatch.setattr(worker.store, "latest_summary", lambda: pytest.fail("unchanged membership decoded twice"))
    assert worker.membership()["TESTUSDT"].asset_id == "synthetic-asset"
    worker.store.close()


def test_membership_projection_refreshes_on_publication_and_personal_changes(tmp_path):
    path = str(tmp_path / "summary.sqlite3")
    worker = LiveObservationWorker(Settings(db_path=path))
    writer = DiscoveryStore(path)
    writer.save_cycle(fixture_cycle(writer))
    first = worker.membership()
    first["TESTUSDT"].asset_id = "consumer-mutation"
    assert worker.membership()["TESTUSDT"].asset_id == "synthetic-asset"
    second = fixture_cycle(writer, "synthetic-two", T+timedelta(minutes=1))
    second.observations[0].stage = DiscoveryStage.DORMANT
    writer.save_cycle(second)
    assert worker.membership() == {}
    worker.store.watchlist_add("personal-test", "PERSONALUSDT", notes="synthetic personal fixture")
    assert list(worker.membership()) == ["PERSONALUSDT"]
    worker.store.watchlist_remove("personal-test")
    assert worker.membership() == {}
    worker.store.close()
    writer.close()
