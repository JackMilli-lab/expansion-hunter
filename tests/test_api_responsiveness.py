from __future__ import annotations

import asyncio
import threading

import pytest
from test_app import app_client, make_app

from expansion_hunter.config import Settings


async def test_blocked_database_read_does_not_block_market_event_loop(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    app = make_app(Settings(db_path=str(tmp_path / "app.sqlite3")))
    entered, release = threading.Event(), threading.Event()
    async with app_client(app) as client:
        original = app.state.store.watchlist

        def blocked_read():
            entered.set()
            release.wait(1)
            return original()

        monkeypatch.setattr(app.state.store, "watchlist", blocked_read)
        pending = asyncio.create_task(client.get("/api/watchlist"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(0.01)
            assert not pending.done(), "Database wait blocked the whole asynchronous event loop"
        finally:
            release.set()
        assert (await pending).status_code == 200


async def test_process_health_does_not_claim_fresh_market_data(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    async with app_client(make_app(Settings(db_path=str(tmp_path / "app.sqlite3")))) as client:
        report = (await client.get("/health")).json()
        assert report["database"] == "AVAILABLE"
        assert report["feed_fresh"] is False and report["ready"] is False
        assert report["live_trading"] is False and report["mode"] == "OBSERVATION"
        assert (await client.get("/ready")).status_code == 503


@pytest.mark.parametrize("status", ["FAILED", "DEGRADED"])
async def test_unhealthy_persistence_cannot_report_ready(tmp_path, monkeypatch, status):
    from test_app import write_health

    from expansion_hunter.fast_models import FastStage, FastState
    from expansion_hunter.models import Asset, utcnow

    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    app = make_app(Settings(db_path=str(tmp_path / "app.sqlite3")))
    async with app_client(app) as client:
        now = utcnow()
        write_health(app.state.store, "native", at=now)
        asset = Asset(asset_id="x", symbol="X", name="X", exchange_symbol="XUSDT")
        state = FastState(asset=asset, stage=FastStage.WATCH, stale=False,
                          last_evaluation_at=now, data_quality={"spot_candles": "AVAILABLE"})
        app.state.store.put_state("fast_state", "x", now, state)
        app.state.store.put_state("live_runtime", "worker", now, {"running": True, "updated_at": now.isoformat()})
        app.state.store.put_state("live_metrics", "worker", now, {"persistence": {"status": status}})
        report = (await client.get("/api/live/health")).json()
        assert report["feed_fresh"] and report["fresh_fast_assets"] == 1
        assert report["ready"] is False
