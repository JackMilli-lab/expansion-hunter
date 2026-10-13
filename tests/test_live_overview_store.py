"""Bounded overview reads, using explicit synthetic SQLite evidence only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.api import create_app
from expansion_hunter.config import Settings
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.models import utcnow

T = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    archive = IntelligenceStore(str(tmp_path / "overview.sqlite3"))
    try:
        yield archive
    finally:
        archive.close()


def observed_cache(at=T):
    """Synthetic recorded levels, not inferred real exchange coverage."""
    return {
        "asset_id": "synthetic", "symbol": "TESTUSDT", "price": 100.125,
        "price_quality": "NATIVE", "price_source": "BINANCE_AGG_TRADE",
        "price_source_timestamp": (at - timedelta(seconds=8)).isoformat(),
        "price_received_timestamp": (at - timedelta(seconds=7)).isoformat(),
        "last_exchange_timestamp": at.isoformat(), "last_received_timestamp": at.isoformat(),
        "received_timestamp": at.isoformat(), "as_of": at.isoformat(), "quote_asset": "USDT",
        "data_quality": {"spot_candles": "AVAILABLE", "futures": "UNAVAILABLE"},
        "liquidity_map": [
            {"id": f"observed:{index}", "price": 101.125 + index * 0.25,
             "lower_price": 101.125 + index * 0.25, "upper_price": 101.125 + index * 0.25,
             "size": None if index % 2 else 12.5, "status": "RESTING",
             "category": "OBSERVED_ORDER_BOOK", "source": "SYNTHETIC_TEST_SNAPSHOT"}
            for index in range(20)
        ],
        "closed_candles": {"1m": [{"test_only": "synthetic history"}] * 400},
        "overlays": [{"type": "SYNTHETIC_TEST_LEVEL", "price": 105.125}],
    }


def test_legacy_overview_bounds_levels_without_changing_exact_evidence(store):
    evidence = observed_cache()
    store.put_state("live_asset", "TESTUSDT", T, evidence)
    original = store._db.execute(
        "SELECT payload FROM runtime_states WHERE kind='live_asset' AND object_id='TESTUSDT'"
    ).fetchone()[0]

    overview = store.live_overview_records()
    assert len(overview) == 1
    row = overview[0]
    assert row["liquidity_map"] == evidence["liquidity_map"][:8]
    assert [level["price"] for level in row["liquidity_map"]] == [
        101.125, 101.375, 101.625, 101.875, 102.125, 102.375, 102.625, 102.875,
    ]
    assert row["liquidity_map"][1]["size"] is None
    assert row["liquidity_map_scope"].startswith("FIRST_MAX8_LEGACY_OVERVIEW")
    assert "closed_candles" not in row and "overlays" not in row
    assert row["data_quality"]["futures"] == "UNAVAILABLE"
    assert store.get_state("live_asset", "TESTUSDT") == evidence
    assert store._db.execute(
        "SELECT payload FROM runtime_states WHERE kind='live_asset' AND object_id='TESTUSDT'"
    ).fetchone()[0] == original


def test_legacy_overview_keeps_asset_identity_and_missing_metrics(store):
    store.put_state("live_asset", "ZZZUSDT", T, {"asset_id": "last", "symbol": "ZZZUSDT"})
    store.put_state("live_asset", "AAAUSDT", T, observed_cache() | {"asset_id": "first", "symbol": "AAAUSDT"})
    rows = store.live_overview_records()
    assert [row["asset_id"] for row in rows] == ["first", "last"]
    assert rows[1]["price"] is None
    assert rows[1]["price_source_timestamp"] is None
    assert rows[1]["data_quality"] is None
    assert rows[1]["liquidity_map"] == []


def test_summary_avoids_reading_or_decoding_large_chart_cache(store):
    summary = {
        "asset_id": "synthetic", "symbol": "TESTUSDT", "price": 100.125,
        "price_source": "BINANCE_AGG_TRADE", "price_source_timestamp": T.isoformat(),
        "liquidity_map": [{"price": 101.125, "size": None}],
        "liquidity_map_scope": "NEAREST_OBSERVED_LEVELS", "price_quality": "NATIVE",
    }
    store.put_state("live_asset_summary", "TESTUSDT", T, summary)
    # Invalid full cache is a deliberate read-path sentinel: any JSON parse
    # of that row would fail. The dedicated summary must not touch it.
    store._db.execute(
        "INSERT INTO runtime_states VALUES(?,?,?,?)",
        ("live_asset", "TESTUSDT", T.isoformat(), "not-valid-chart-json"),
    )
    store._db.commit()
    queries = []
    store._db.set_trace_callback(queries.append)
    assert store.live_overview_records() == [summary]
    assert queries and all("live_asset_summary" in query for query in queries)
    assert not any("json_each" in query or "json_extract" in query for query in queries)


def test_overview_result_mutation_cannot_rewrite_stored_summary(store):
    summary = {"symbol": "TESTUSDT", "liquidity_map": [{"price": 101.125, "size": None}]}
    store.put_state("live_asset_summary", "TESTUSDT", T, summary)
    row = store.live_overview_records()[0]
    row["liquidity_map"][0]["price"] = 999
    assert store.live_overview_records() == [summary]


@pytest.mark.parametrize("summary", [False, True])
def test_invalid_json_is_visible_instead_of_returning_fake_empty_data(store, summary):
    kind = "live_asset_summary" if summary else "live_asset"
    store._db.execute(
        "INSERT INTO runtime_states VALUES(?,?,?,?)",
        (kind, "TESTUSDT", T.isoformat(), "{broken-record"),
    )
    store._db.commit()
    expected_error = json.JSONDecodeError if summary else sqlite3.OperationalError
    with pytest.raises(expected_error):
        store.live_overview_records()


def test_overview_api_preserves_price_trade_time_instead_of_latest_book_time(store, monkeypatch):
    """Call the actual sync route; no ASGI threads or network are required."""
    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    now = utcnow()
    evidence = observed_cache(now)
    store.put_state("live_asset", "TESTUSDT", now, evidence)
    store.put_state("fast_state", "synthetic", now, {
        "asset": {"asset_id": "synthetic", "symbol": "TEST"},
        "stage": "BREAKOUT_PRESSURE", "stale": False,
        "last_evaluation_at": now.isoformat(), "data_quality": {"spot_candles": "AVAILABLE"},
        "resistance": {"price": 101.125}, "candles": {"1m": ["synthetic"] * 400},
    })
    app = create_app(Settings(db_path=store.database_path))
    app.state.store = store
    endpoint = next(route.endpoint for route in app.router.routes if getattr(route, "name", None) == "overview")
    response = endpoint()
    row = next(asset for asset in response["assets"] if asset["asset_id"] == "synthetic")
    assert row["price"] == 100.125
    assert row["price_source"] == "BINANCE_AGG_TRADE"
    assert row["price_as_of"] == evidence["price_source_timestamp"]
    assert row["price_as_of"] != evidence["last_exchange_timestamp"]
    assert row["levels"]["resistance"] == 101.125
    assert len(row["liquidity_map"]) == 8
    assert row["data_quality"]["futures"] == "UNAVAILABLE"
    assert "closed_candles" not in row and "candles" not in row
    assert response["live_trading"] is False
