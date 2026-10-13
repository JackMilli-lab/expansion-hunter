"""Exercise actual Trade Alert app routes/lifespan without external feeds."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from expansion_hunter.api import create_app
from expansion_hunter.config import Settings
from expansion_hunter.trade_alert_models import TradeAlertSettings


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_API_TOKEN", "EXPLICIT_TEST_ACCESS_TOKEN")
    monkeypatch.delenv("COCKPIT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("COCKPIT_TELEGRAM_CHAT_ID", raising=False)
    stopped = []

    async def scanner(app, config):
        app.state.trade_alert_worker = SimpleNamespace(snapshot=lambda: {
            "status": "RUNNING", "running": True, "assets": [], "events": [],
            "counts": {"selected": config.max_assets, "ready": 0}})
        try:
            await asyncio.Future()
        finally:
            stopped.append("scanner")

    async def notifications(app):
        try:
            await asyncio.Future()
        finally:
            stopped.append("notifications")

    async def research(app, config):
        app.state.trade_alert_research_health = {"status": "AVAILABLE", "actual_trade_pnl": None}
        try:
            await asyncio.Future()
        finally:
            stopped.append("research")

    async def deep(app, config):
        try:
            await asyncio.Future()
        finally:
            stopped.append("deep")

    monkeypatch.setattr("expansion_hunter.trade_alert_runtime.run_trade_alert_worker", scanner)
    monkeypatch.setattr("expansion_hunter.notification_runtime.run_notification_worker", notifications)
    monkeypatch.setattr("expansion_hunter.trade_alert_research.run_trade_alert_research_worker", research)
    monkeypatch.setattr("expansion_hunter.pre_move_runtime.run_pre_move_deep_worker", deep)
    application = create_app(Settings(db_path=str(tmp_path / "api.sqlite3")),
                             trade_alert_settings=TradeAlertSettings(max_assets=2))
    application.state.test_stopped = stopped
    return application


async def test_real_dashboard_auth_health_history_and_graceful_workers(app):
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for path in ("/", "/cockpit", "/trade-alert"):
                response = await client.get(path)
                assert response.status_code == 200
                assert '<h1>TRADE ALERT</h1>' in response.text
                assert "state.assets" in response.text
            assert (await client.get("/api/trade-alert/status")).status_code == 401
            client.headers["Authorization"] = "Bearer EXPLICIT_TEST_ACCESS_TOKEN"
            status = (await client.get("/api/trade-alert/status")).json()
            assert status["counts"]["selected"] == 2
            assert status["assets"] == []
            health = (await client.get("/health")).json()
            assert health["live_trading"] is False
            assert health["notifications"] == {"status": "STARTING"}
            assert (await client.get("/ready")).status_code == 503
            assert (await client.get("/api/trade-alert/history?limit=1001")).status_code == 422
            history = (await client.get("/api/trade-alert/history")).json()
            assert history["alerts"] == history["outcomes"] == history["misses"] == []
            telegram = (await client.post("/api/trade-alert/telegram/test")).json()
            assert telegram["status"] == "UNAVAILABLE"
    assert sorted(app.state.test_stopped) == ["deep", "notifications", "research", "scanner"]


async def test_ready_uses_fresh_measured_coverage_not_selected_count(app):
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                    headers={"Authorization": "Bearer EXPLICIT_TEST_ACCESS_TOKEN"}) as client:
            app.state.trade_alert_worker.snapshot = lambda: {
                "running": True, "status": "RUNNING", "counts": {"selected": 500, "ready": 1}}
            assert (await client.get("/ready")).json()["status"] == "READY"
            app.state.trade_alert_worker.snapshot = lambda: {
                "running": True, "status": "RUNNING", "counts": {"ready": 1, "persistence_failed": 1}}
            assert (await client.get("/ready")).status_code == 503
