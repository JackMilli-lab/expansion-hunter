"""Public market reads stay separate from private application access."""
from types import SimpleNamespace

import httpx
import pytest

from expansion_hunter.api import create_app
from expansion_hunter.config import Settings


@pytest.fixture
def market_app(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_API_TOKEN", "TEST_PRIVATE_ACCESS_CODE")
    monkeypatch.delenv("COCKPIT_PUBLIC_MARKET_DATA", raising=False)
    app = create_app(Settings(db_path=str(tmp_path / "market.sqlite3")))
    app.state.trade_alert_worker = SimpleNamespace(
        assets={"BTCUSDT": object()},
        snapshot=lambda: {"status": "RUNNING", "running": True,
            "assets": [{"symbol": "BTCUSDT", "price": 100., "notes": "PRIVATE_TEST_NOTE"}],
            "events": ["PRIVATE_TEST_EVENT"], "notifications": {"private": True}},
        _state=lambda symbol: SimpleNamespace(snapshot=lambda *args, **kwargs: {"price": 100.}),
        _history=lambda symbol, timeframe, now: [SimpleNamespace(
            model_dump=lambda **kwargs: {"timeframe": timeframe, "close": 100., "source": "TEST_FIXTURE"})],
    )
    return app


async def test_market_reads_are_protected_by_default(market_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=market_app), base_url="http://test") as client:
        for path in ("/api/market/status", "/api/market/assets/BTCUSDT"):
            assert (await client.get(path)).status_code == 401
        client.headers["Authorization"] = "Bearer TEST_PRIVATE_ACCESS_CODE"
        assert (await client.get("/api/market/status")).status_code == 200


async def test_public_status_does_not_expose_private_state(market_app, monkeypatch):
    monkeypatch.setenv("COCKPIT_PUBLIC_MARKET_DATA", "true")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=market_app), base_url="http://test") as client:
        response = await client.get("/api/market/status")
        assert response.status_code == 200
        payload = response.json()
        assert payload["public_market_data"] is True
        assert payload["assets"] == [{"symbol": "BTCUSDT", "price": 100.}]
        assert "events" not in payload and "notifications" not in payload
        for path in ("/api/watchlist", "/api/trade-alert/status", "/api/trade-alert/history"):
            assert (await client.get(path)).status_code == 401
        for path in ("/api/live/start", "/api/live/stop", "/api/trade-alert/telegram/test"):
            assert (await client.post(path)).status_code == 401
        assert (await client.put("/api/watchlist", json={})).status_code == 401


@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h", "4h", "1D"])
async def test_public_candles_reuse_selected_native_timeframe(market_app, monkeypatch, timeframe):
    monkeypatch.setenv("COCKPIT_PUBLIC_MARKET_DATA", "true")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=market_app), base_url="http://test") as client:
        response = await client.get("/api/market/assets/BTCUSDT", params={"timeframe": timeframe})
        assert response.status_code == 200
        payload = response.json()
        assert payload["timeframe"] == timeframe
        assert payload["candles"][0]["timeframe"] == timeframe
        assert payload["market"]["price"] == 100.
        assert payload["analysis"] is None and payload["analysis_status"] == "UNAVAILABLE"
        assert "pre_move" not in payload and "pre_ignition" not in payload


async def test_public_reads_do_not_expand_market_coverage(market_app, monkeypatch):
    monkeypatch.setenv("COCKPIT_PUBLIC_MARKET_DATA", "true")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=market_app), base_url="http://test") as client:
        assert (await client.get("/api/market/assets/UNKNOWNUSDT")).status_code == 404
        assert (await client.get("/api/market/assets/BTCUSDT?timeframe=2m")).status_code == 422
        assert (await client.post("/api/market/status")).status_code == 405
