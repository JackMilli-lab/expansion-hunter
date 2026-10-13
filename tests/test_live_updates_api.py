"""Synthetic native protocol fixtures exercise the real incremental API."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from expansion_hunter.api import create_app
from expansion_hunter.config import Settings
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.live_runtime import LiveObservationWorker
from expansion_hunter.models import Asset, Candle
from expansion_hunter.providers.binance_ws import decode_message

NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    monkeypatch.setattr("expansion_hunter.intelligence_api.utcnow", lambda: NOW)
    settings = Settings(db_path=str(tmp_path / "updates.sqlite3"))
    app = create_app(settings)
    app.state.store = IntelligenceStore(settings.db_path)
    worker = LiveObservationWorker(settings, clock=lambda: NOW)
    asset = Asset(asset_id="native-test-only", symbol="BTC", name="Bitcoin", exchange_symbol="BTCUSDT")
    worker.assets = {"BTCUSDT": asset}
    stamp = int(NOW.timestamp() * 1000) - 100
    event = decode_message({"stream": "btcusdt@aggTrade", "data": {
        "e": "aggTrade", "E": stamp, "T": stamp, "s": "BTCUSDT", "a": 1,
        "p": "100", "q": "1", "m": False}}, NOW - timedelta(milliseconds=50), NOW)
    worker.state.apply(event)
    bars = [Candle(open_time=NOW - timedelta(minutes=6 - i), close_time=NOW - timedelta(minutes=5 - i),
                   open=99, high=101, low=98, close=100, volume=10, source="test/native",
                   available_at=NOW - timedelta(minutes=5 - i)) for i in range(5)]
    worker.state.seed_candles("BTCUSDT", "1m", bars, NOW)
    worker.state.seed_candles("BTCUSDT", "5m", [Candle(open_time=NOW - timedelta(minutes=10),
        close_time=NOW - timedelta(minutes=5), open=99, high=101, low=98, close=100,
        volume=10, source="test/native", available_at=NOW - timedelta(minutes=5))], NOW)
    worker._analysis["BTCUSDT"] = {"as_of": (NOW - timedelta(seconds=1)).isoformat(),
        "data_quality": {"1m": "DERIVED"}, "active_fibonacci": {"1m": {"status": "INSUFFICIENT_DATA"}},
        "scores": {"stored_energy": {"value": None, "status": "UNKNOWN"}}, "liquidity_map": []}
    app.state.live_worker = worker
    app.state.live_task = SimpleNamespace(done=lambda: False)
    yield app
    worker.store.close()
    app.state.store.close()


async def get(app, suffix=""):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        return await client.get("/api/live/updates/BTCUSDT" + suffix)


async def test_incremental_updates_have_native_price_and_two_selected_closed_bars_without_archive(app, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Incremental current data must not read SQLite history")
    monkeypatch.setattr(app.state.store, "get_state", forbidden)
    value = (await get(app)).json()
    assert value["incremental"] is True and value["monitoring"] == "ACTIVE"
    assert value["status"] == "AVAILABLE"
    assert value["live"]["price"] == 100
    assert set(value["live"]["closed_candles"]) == {"1m"}
    assert len(value["live"]["closed_candles"]["1m"]) == 2
    assert len(app.state.live_worker.state.candles("BTCUSDT", "1m")) == 5
    assert value["live"]["received_timestamp"] != NOW.isoformat()
    assert value["live"]["data_quality"]["futures"] == "UNAVAILABLE"
    assert value["live"]["order_book"]["quality"] == "UNAVAILABLE"


async def test_unchanged_analysis_not_redownloaded(app):
    initial = (await get(app)).json()
    assert "scores" in initial["live"] and "active_fibonacci" in initial["live"]
    after = initial["live"]["analysis_at"]
    value = (await get(app, "?analysis_after=" + after.replace("+", "%2B"))).json()
    assert value["live"]["analysis_at"] == after
    assert "scores" not in value["live"] and "active_fibonacci" not in value["live"]


async def test_timeframe_only_returns_that_frames_native_bars(app):
    value = (await get(app, "?timeframe=5m")).json()
    assert set(value["live"]["closed_candles"]) == {"5m"}
    assert len(value["live"]["closed_candles"]["5m"]) == 1


async def test_subscribed_pair_before_first_native_message_is_unavailable_not_server_error(app, monkeypatch):
    monkeypatch.setattr(app.state.live_worker.state, "snapshot", lambda *args, **kwargs: {
        "symbol": "BTCUSDT", "price": None, "price_quality": "UNAVAILABLE",
        "data_quality": {}, "as_of": NOW.isoformat(),
    })
    response = await get(app)
    assert response.status_code == 200
    value = response.json()
    assert value["live"]["price"] is None
    assert value["live"]["open_candles"] == {}


async def test_stopped_worker_never_relabels_archived_or_synthetic_data_live(app):
    app.state.live_worker.stop.set()
    value = (await get(app)).json()
    assert value["status"] == "UNAVAILABLE" and value["monitoring"] == "UNAVAILABLE"
    assert value["live"] is None and value["fast"] == []


async def test_stale_native_price_does_not_become_fresh_at_response_time(app, monkeypatch):
    monkeypatch.setattr("expansion_hunter.intelligence_api.utcnow", lambda: NOW + timedelta(minutes=2))
    value = (await get(app)).json()
    assert value["status"] == "STALE"
    assert value["age_seconds"] > 120
    assert value["live"]["price"] == 100


async def test_future_analysis_and_invalid_cursors_are_not_features(app):
    app.state.live_worker._analysis["BTCUSDT"]["as_of"] = (NOW + timedelta(seconds=1)).isoformat()
    value = (await get(app)).json()
    assert "scores" not in value["live"] and value["live"]["analysis_at"] is None
    assert (await get(app, "?analysis_after=2026-10-07T12:00:00Z")).status_code == 422
    assert (await get(app, "?analysis_after=2026-10-06T12:00:00")).status_code == 422
    assert (await get(app, "?timeframe=weekly")).status_code == 422
