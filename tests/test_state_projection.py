"""Actual state/query paths; synthetic fixtures are not public-market evidence."""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from expansion_hunter.api import create_app
from expansion_hunter.config import Settings
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.models import utcnow

T = datetime(2026, 10, 1, tzinfo=UTC)
FIELDS = ("asset_id", "symbol", "price", "price_quality", "data_quality", "liquidity_map",
          "as_of", "received_timestamp", "last_exchange_timestamp", "last_received_timestamp")


def live_fixture(symbol="TESTUSDT", asset_id="synthetic", at=T):
    return {"asset_id": asset_id, "symbol": symbol, "price": 100.25,
            "price_quality": "NATIVE", "data_quality": {"futures": "UNAVAILABLE", "spot": "NATIVE"},
            "liquidity_map": [{"price": 101, "size": None, "status": "RESTING", "inferred": True}],
            "as_of": at.isoformat(), "received_timestamp": at.isoformat(),
            "last_exchange_timestamp": at.isoformat(), "last_received_timestamp": at.isoformat(),
            "closed_candles": {"1m": [{"test_only": "synthetic candle"}] * 100},
            "overlays": [{"type": "SYNTHETIC_TEST_LEVEL", "price": 101}],
            "scores": {"stored_energy": {"value": 77, "calibration": "UNCALIBRATED"}}}


def test_get_state_uses_kind_and_object_primary_key_and_preserves_missing_values(tmp_path):
    store = IntelligenceStore(str(tmp_path / "states.sqlite3"))
    expected = live_fixture()
    store.put_state("live_asset", "TESTUSDT", T, expected)
    store.put_state("live_asset", "OTHERUSDT", T, live_fixture("OTHERUSDT", "other"))
    store.put_state("unrelated", "TESTUSDT", T, {"price": 0})
    queries = []
    store._db.set_trace_callback(queries.append)
    assert store.get_state("live_asset", "TESTUSDT") == expected
    assert store.get_state("live_asset", "ABSENT") is None
    assert store.get_state("absent-kind", "TESTUSDT") is None
    assert all("AND object_id=" in sql for sql in queries)
    assert store.get_state("live_asset", "TESTUSDT")["liquidity_map"][0]["size"] is None
    store.close()


def test_projection_keeps_native_exact_fields_without_decoding_candles(tmp_path, monkeypatch):
    store = IntelligenceStore(str(tmp_path / "states.sqlite3"))
    expected = live_fixture()
    store.put_state("live_asset", "TESTUSDT", T, expected)
    store.put_state("live_asset", "OTHERUSDT", T, live_fixture("OTHERUSDT", "other"))
    monkeypatch.setattr(store, "states", lambda *_: pytest.fail("full JSON state lookup"))
    projected = store.states_projected("live_asset", FIELDS)
    assert [row["symbol"] for row in projected] == ["OTHERUSDT", "TESTUSDT"]
    assert projected[1] == {field: expected[field] for field in FIELDS}
    assert projected[1]["liquidity_map"][0]["inferred"] is True
    assert "closed_candles" not in projected[1] and "overlays" not in projected[1]
    assert store.get_state("live_asset", "TESTUSDT") == expected
    store.close()


@pytest.mark.parametrize("fields", [(), ("bad.path",), ("price", "price"), ("x');DROP TABLE runtime_states;--",)])
def test_projection_rejects_ambiguous_or_unsafe_identifiers(tmp_path, fields):
    store = IntelligenceStore(str(tmp_path / "states.sqlite3"))
    with pytest.raises(ValueError, match="top-level"):
        store.states_projected("live_asset", fields)
    store.close()


def test_projection_missing_field_remains_null_not_zero(tmp_path):
    store = IntelligenceStore(str(tmp_path / "states.sqlite3"))
    store.put_state("live_asset", "TESTUSDT", T, {"symbol": "TESTUSDT", "price": None})
    assert store.states_projected("live_asset", ("symbol", "price", "futures")) == [
        {"symbol": "TESTUSDT", "price": None, "futures": None}]
    store.close()


async def test_live_asset_route_reads_only_requested_symbol_and_matching_fast_state(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    app = create_app(Settings(db_path=str(tmp_path / "app.sqlite3")))
    async with app.router.lifespan_context(app):
        store = app.state.store
        now = utcnow()
        expected = live_fixture(at=now)
        store.put_state("live_asset", "TESTUSDT", now, expected)
        store.put_state("live_asset", "OTHERUSDT", now, live_fixture("OTHERUSDT", "other", now))
        store.put_state("fast_state", "synthetic", now, {"asset": {"asset_id": "synthetic"}, "stage": "BREAKOUT_PRESSURE"})
        store.put_state("fast_state", "other", now, {"asset": {"asset_id": "other"}, "stage": "LOADED"})
        monkeypatch.setattr(store, "states", lambda *_: pytest.fail("live chart decoded other assets"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.get("/api/live/assets/testusdt")
            assert response.status_code == 200
            assert response.json()["live"] == expected
            assert response.json()["fast"] == [{"asset": {"asset_id": "synthetic"}, "stage": "BREAKOUT_PRESSURE"}]
            assert response.json()["status"] == "AVAILABLE"
            assert (await client.get("/api/live/assets/ABSENT")).status_code == 404


async def test_overview_projects_live_state_and_preserves_price_liquidity_and_fast_state(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    app = create_app(Settings(db_path=str(tmp_path / "app.sqlite3")))
    async with app.router.lifespan_context(app):
        store = app.state.store
        now = utcnow()
        expected = live_fixture(at=now)
        store.put_state("live_asset", "TESTUSDT", now, expected)
        store.put_state("fast_state", "synthetic", now, {"asset": {"asset_id": "synthetic", "symbol": "TEST"},
            "stage": "BREAKOUT_PRESSURE", "last_evaluation_at": now.isoformat(), "stale": False,
            "data_quality": {"spot_candles": "AVAILABLE"}, "resistance": {"price": 101}})
        original = store.states
        def no_full_live(kind):
            if kind == "live_asset":
                pytest.fail("overview decoded full live histories")
            return original(kind)
        monkeypatch.setattr(store, "states", no_full_live)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.get("/api/cockpit/overview")
            assert response.status_code == 200
            row = next(r for r in response.json()["assets"] if r["asset_id"] == "synthetic")
            assert row["price"] == 100.25
            assert row["state"] == "BREAKOUT_PRESSURE"
            assert row["levels"]["resistance"] == 101
            assert row["freshness"] == "FRESH"
            assert row["liquidity_map"] == expected["liquidity_map"]
            assert row["data_quality"]["futures"] == "UNAVAILABLE"
            assert "closed_candles" not in row
