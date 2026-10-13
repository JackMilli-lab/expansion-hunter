"""Actual ASGI application tests; no external price feed or invented live status."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from expansion_hunter.api import create_app as actual_create_app
from expansion_hunter.config import Settings
from expansion_hunter.discovery_models import (
    DiscoveryCycle,
    DiscoveryObservation,
    DiscoveryScore,
    DiscoveryStage,
    RankHistory,
)
from expansion_hunter.fast_models import FastStage, FastState
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.live_runtime import LiveObservationWorker
from expansion_hunter.models import Asset

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


class FakeDiscoveryService:
    """Boundary-only process fixture; no network or fake market observations."""

    def __init__(self, settings):
        self.settings = settings
        self.running = False
        self.start_calls = self.stop_calls = 0

    async def start(self):
        self.start_calls += 1
        self.running = True

    async def stop(self):
        self.stop_calls += 1
        self.running = False

    def status(self):
        return {
            "running": self.running,
            "status": "DISCOVERING" if self.running else "STOPPED",
            "pid": 12345 if self.running else None,
            "database_path": self.settings.db_path,
            "configured_assets": 500,
            "configured_universe_size": 500,
        }


def make_app(settings, *, discovery_service_factory=FakeDiscoveryService):
    return actual_create_app(settings, discovery_service_factory=discovery_service_factory)


@asynccontextmanager
async def app_client(app):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.fixture
def app_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    return Settings(db_path=str(tmp_path / "app.sqlite3"))


def body(symbol="XRPUSDT", asset_id="ripple", **extra):
    return {"symbol": symbol, "asset_id": asset_id, **extra}


async def test_personal_watchlist_add_pin_notes_restart_and_remove(app_settings):
    async with app_client(make_app(app_settings)) as client:
        response = await client.put(
            "/api/watchlist", json=body(priority=90, pinned=True, notes="observe demand")
        )
        assert response.status_code == 200
        assert response.json()["exchange_access"] == "PUBLIC_READ_ONLY"
        xrp = response.json()["watchlist"][0]
        assert xrp["symbol"] == "XRPUSDT"
        assert xrp["pinned"] == 1 and xrp["priority"] == 90
        assert xrp["notes"] == "observe demand"
        added_at = xrp["added_at"]
    async with app_client(make_app(app_settings)) as client:
        response = (await client.get("/api/watchlist")).json()
        assert response["monitoring_priority_is_score_boost"] is False
        assert response["watchlist"][0]["symbol"] == "XRPUSDT"
        update = (await client.put("/api/watchlist", json=body(pinned=False, priority=5))).json()[
            "watchlist"
        ][0]
        assert update["added_at"] == added_at
        assert update["pinned"] == 0
        assert (await client.delete("/api/watchlist/ripple")).json()["watchlist"] == []
    async with app_client(make_app(app_settings)) as client:
        assert (await client.get("/api/watchlist")).json()["watchlist"] == []


async def test_empty_watchlist_automatic_scanner_request_survives_restart_until_explicit_stop(app_settings):
    first = make_app(app_settings)
    async with app_client(first) as client:
        assert (await client.get("/api/watchlist")).json()["watchlist"] == []
        assert (await client.post("/api/live/start")).json()["status"] == "DISCOVERING"
        assert first.state.store.states("app_control")[0]["automatic_observation_requested"] is True
    restarted = make_app(app_settings)
    async with app_client(restarted) as client:
        health = (await client.get("/api/live/health")).json()
        assert health["broad_running"] is True
        assert health["feed_fresh"] is False and health["ready"] is False
        assert restarted.state.discovery_service.start_calls == 1
        assert restarted.state.live_requested is True
        assert (await client.post("/api/live/stop")).status_code == 200
        assert restarted.state.store.states("app_control")[0]["automatic_observation_requested"] is False
    stopped = make_app(app_settings)
    async with app_client(stopped) as client:
        assert (await client.get("/api/live/health")).json()["broad_running"] is False
        assert stopped.state.discovery_service.start_calls == 0


@pytest.mark.parametrize(
    "invalid",
    [
        body(symbol="BTC/USDT"),
        body(symbol="https://evil.example"),
        body(asset_id=""),
        body(priority=101),
        body(priority=-1),
        body(notes="a" * 2001),
    ],
)
async def test_invalid_watchlist_payload_never_persists(app_settings, invalid):
    async with app_client(make_app(app_settings)) as client:
        assert (await client.put("/api/watchlist", json=invalid)).status_code == 422
        assert (await client.get("/api/watchlist")).json()["watchlist"] == []


async def test_cross_origin_app_changes_blocked_even_with_valid_bearer(app_settings, monkeypatch):
    monkeypatch.setenv("COCKPIT_API_TOKEN", "test-secret-not-for-logs")
    async with app_client(make_app(app_settings)) as client:
        headers = {"Authorization": "Bearer test-secret-not-for-logs", "Origin": "https://malicious.example"}
        assert (await client.put("/api/watchlist", json=body(), headers=headers)).status_code == 403
        assert (await client.post("/api/live/start", headers=headers)).status_code == 403
        assert (await client.post("/api/live/stop", headers=headers)).status_code == 403


async def test_same_origin_local_watchlist_change_and_missing_token_protection(app_settings, monkeypatch):
    async with app_client(make_app(app_settings)) as client:
        assert (
            await client.put("/api/watchlist", json=body(), headers={"Origin": "http://testserver"})
        ).status_code == 200
    monkeypatch.setenv("COCKPIT_API_TOKEN", "fixture-token")
    async with app_client(make_app(app_settings)) as client:
        assert (await client.get("/api/watchlist")).status_code == 401
        assert (await client.put("/api/watchlist", json=body())).status_code == 401
        assert (await client.post("/api/live/start")).status_code == 401
        assert (await client.get("/api/cockpit/overview")).status_code == 401
        authorized = await client.get("/api/watchlist", headers={"Authorization": "Bearer fixture-token"})
        assert authorized.status_code == 200
        assert "fixture-token" not in authorized.text


async def test_public_html_contains_no_private_data_or_bearer_secret(app_settings, monkeypatch):
    monkeypatch.setenv("COCKPIT_API_TOKEN", "secret-token-must-not-appear")
    async with app_client(make_app(app_settings)) as client:
        page = await client.get("/cockpit")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert "secret-token-must-not-appear" not in page.text
        assert (await client.get("/")).status_code == 200


async def test_empty_operational_app_and_readiness_do_not_pretend_feed_is_live(app_settings):
    async with app_client(make_app(app_settings)) as client:
        overview = (await client.get("/api/cockpit/overview")).json()
        assert overview["assets"] == [] and overview["watchlist"] == []
        assert overview["discovery_status"] == {
            "status": "NO_DISCOVERY_CYCLE",
            "counts": {},
            "completed_at": None,
            "provider_issues": [],
        }
        assert overview["live_trading"] is False
        assert overview["mode"] == "OBSERVATION"
        health = (await client.get("/api/live/health")).json()
        assert not health["ready"] and (not health["feed_fresh"])
        assert health["feed_age_seconds"] is None
        assert health["database"] == "AVAILABLE"
        assert (await client.get("/ready")).status_code == 503
        assert (await client.get("/api/live/assets/XRPUSDT")).status_code == 404
        assert (await client.post("/api/live/start")).json()["status"] == "DISCOVERING"
        assert (await client.get("/api/status")).json() == {"runs": []}


def write_health(store, identifier, *, at, connection="CONNECTED", stale=False):
    store.put_state(
        "feed_health",
        identifier,
        at,
        {
            "provider": identifier,
            "connection": connection,
            "stale": stale,
            "last_message_timestamp": at.isoformat(),
        },
    )


async def test_ready_requires_fresh_timestamp_from_same_connected_feed(app_settings):
    app = make_app(app_settings)
    async with app_client(app) as client:
        now = datetime.now(UTC)
        write_health(app.state.store, "old-connected", at=now - timedelta(minutes=5))
        write_health(app.state.store, "fresh-stopped", at=now, connection="STOPPED")
        state = FastState(asset=Asset(asset_id="ripple", symbol="XRP", name="Ripple"))
        app.state.store.put_state("fast_state", "ripple", now, state)
        health = (await client.get("/api/live/health")).json()
        assert health["feed_fresh"] is False
        assert (await client.get("/ready")).status_code == 503


async def test_ready_connected_fresh_feed_plus_fast_state(app_settings):
    app = make_app(app_settings)
    async with app_client(app) as client:
        now = datetime.now(UTC)
        write_health(app.state.store, "native", at=now)
        state = FastState(
            asset=Asset(asset_id="ripple", symbol="XRP", name="Ripple"),
            stale=False,
            last_evaluation_at=now,
            data_quality={"spot_candles": "AVAILABLE"},
        )
        app.state.store.put_state("fast_state", "ripple", now, state)
        app.state.store.put_state(
            "live_runtime", "worker", now, {"running": True, "updated_at": now.isoformat()}
        )
        assert (await client.get("/api/live/health")).json()["feed_fresh"] is True
        assert (await client.get("/ready")).status_code == 200


@pytest.mark.parametrize("seconds,expected", [(-120, "STALE"), (120, "UNAVAILABLE")])
async def test_live_asset_old_or_future_receipt_never_looks_available(app_settings, seconds, expected):
    app = make_app(app_settings)
    async with app_client(app) as client:
        now = datetime.now(UTC)
        record_at = now + timedelta(seconds=seconds)
        app.state.store.put_state(
            "live_asset",
            "XRPUSDT",
            record_at,
            {
                "symbol": "XRPUSDT",
                "asset_id": "ripple",
                "price": 0.5,
                "received_timestamp": record_at.isoformat(),
                "data_quality": {"spot_trades": "AVAILABLE"},
            },
        )
        response = await client.get("/api/live/assets/xrpusdt")
        assert response.status_code == 200
        assert response.json()["status"] == expected


async def test_live_asset_serialization_keeps_native_exact_prices_and_absent_values(app_settings):
    app = make_app(app_settings)
    async with app_client(app) as client:
        now = datetime.now(UTC)
        levels = [
            {"price": 101.0, "type": "OBSERVED_ORDER_BOOK", "side": "BUY_SIDE"},
            {"price": 102.5, "type": "OBSERVED_ORDER_BOOK", "side": "BUY_SIDE"},
            {"price": 98.5, "type": "OBSERVED_ORDER_BOOK", "side": "SELL_SIDE"},
        ]
        app.state.store.put_state(
            "live_asset",
            "BTCUSDT",
            now,
            {
                "symbol": "BTCUSDT",
                "asset_id": "bitcoin",
                "price": 100,
                "received_timestamp": now.isoformat(),
                "liquidity_map": levels,
                "data_quality": {"futures": "UNAVAILABLE", "spot_cvd": "UNAVAILABLE"},
                "order_flow": {"cvd": None},
            },
        )
        app.state.store.put_state(
            "fast_state",
            "bitcoin",
            now,
            FastState(asset=Asset(asset_id="bitcoin", symbol="BTC", name="Bitcoin"), stage=FastStage.WATCH),
        )
        response = (await client.get("/api/live/assets/BTCUSDT")).json()
        assert response["status"] == "AVAILABLE"
        assert [level["price"] for level in response["live"]["liquidity_map"]] == [101, 102.5, 98.5]
        assert response["live"]["order_flow"]["cvd"] is None
        assert response["fast"][0]["stage"] == "WATCH"


async def test_live_start_stop_is_idempotent_and_never_requires_private_keys(app_settings, monkeypatch):
    started = asyncio.Event()

    async def wait_until_stopped(self):
        started.set()
        try:
            await self.stop.wait()
        finally:
            self.store.close()

    monkeypatch.setattr(LiveObservationWorker, "run", wait_until_stopped)
    app = make_app(app_settings)
    async with app_client(app) as client:
        assert (await client.put("/api/watchlist", json=body())).status_code == 200
        response = (await client.post("/api/live/start")).json()
        assert response["status"] == "STARTING"
        assert response["private_exchange_keys_required"] is False
        assert (await client.post("/api/live/start")).json()["status"] == "RUNNING"
        assert app.state.live_worker.membership()["XRPUSDT"].asset_id == "ripple"
        assert (await client.post("/api/live/stop")).json()["status"] == "STOPPED"
        assert app.state.live_task.done()
        assert (await client.post("/api/live/stop")).json()["status"] == "STOPPED"
    assert started.is_set()


async def test_api_worker_error_is_visible_and_does_not_crash_app(app_settings, monkeypatch):

    async def fail_run(self):
        self.store.close()
        raise ConnectionError("synthetic WebSocket unavailable")

    monkeypatch.setattr(LiveObservationWorker, "run", fail_run)
    app = make_app(app_settings)
    async with app_client(app) as client:
        await client.put("/api/watchlist", json=body())
        assert (await client.post("/api/live/start")).status_code == 200
        response = (await client.get("/api/live/health")).json()
        assert not response["ready"]
        assert response["provider_errors"][0]["status"] == "FAILED"
        assert "synthetic WebSocket unavailable" in response["provider_errors"][0]["error"]
        assert (await client.get("/api/watchlist")).status_code == 200


async def test_personal_priority_monitoring_survives_app_shutdown(app_settings):
    async with app_client(make_app(app_settings)) as client:
        await client.put("/api/watchlist", json=body())
    worker = LiveObservationWorker(app_settings)
    try:
        assert worker.membership()["XRPUSDT"].asset_id == "ripple"
        assert worker.monitor.states == {}
    finally:
        worker.store.close()
    archive = IntelligenceStore(app_settings.db_path)
    try:
        assert archive.watchlist()[0]["symbol"] == "XRPUSDT"
        assert archive._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        archive.close()


def discovery_fixture(store):
    rows = []
    for identifier, symbol, stage, rank in [
        ("unknown-next-mover", "NEXTUSDT", DiscoveryStage.RADAR, 2),
        ("not-promoted", "OTHERUSDT", DiscoveryStage.DORMANT, 1),
    ]:
        rows.append(
            DiscoveryObservation(
                asset=Asset(
                    asset_id=identifier,
                    symbol=symbol.removesuffix("USDT"),
                    name=identifier,
                    exchange_symbol=symbol,
                ),
                as_of=NOW,
                eligible=True,
                stage=stage,
                rank=RankHistory(current_rank=rank),
                scores={"expansion_readiness": DiscoveryScore(value=73, coverage=0.8)},
            )
        )
    return DiscoveryCycle(
        cycle_id="synthetic-discovery-membership",
        started_at=NOW,
        as_of=NOW,
        completed_at=NOW,
        database_path=store.database_path,
        config_hash="synthetic-test-only",
        observations=rows,
    )


async def test_unknown_discovery_candidate_enters_fast_watch_automatically_and_personal_is_priority(
    app_settings,
):
    archive = IntelligenceStore(app_settings.db_path)
    archive.save_cycle(discovery_fixture(archive))
    archive.watchlist_add("ripple", "XRPUSDT", priority=100, pinned=True, at=NOW)
    before = archive.latest_cycle().model_dump()
    archive.close()
    worker = LiveObservationWorker(app_settings)
    try:
        assert list(worker.membership()) == ["XRPUSDT", "NEXTUSDT"]
        assert "OTHERUSDT" not in worker.membership()
        assert worker.store.latest_cycle().model_dump() == before
    finally:
        worker.store.close()


async def test_verified_discovery_identity_used_for_watchlist_pair(app_settings):
    archive = IntelligenceStore(app_settings.db_path)
    archive.save_cycle(discovery_fixture(archive))
    archive.close()
    async with app_client(make_app(app_settings)) as client:
        added = await client.put("/api/watchlist", json=body("NEXTUSDT", "incorrect-caller-alias"))
        assert added.status_code == 200
        assert added.json()["watchlist"][0]["asset_id"] == "unknown-next-mover"


async def test_remote_controls_require_token_even_without_origin(app_settings):
    app = make_app(app_settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("203.0.113.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            assert (await client.put("/api/watchlist", json=body())).status_code == 403
            assert (await client.post("/api/live/start")).status_code == 403


async def test_future_connected_feed_does_not_satisfy_readiness(app_settings):
    app = make_app(app_settings)
    async with app_client(app) as client:
        future = datetime.now(UTC) + timedelta(minutes=1)
        write_health(app.state.store, "native", at=future)
        assert (await client.get("/api/live/health")).json()["feed_fresh"] is False
        assert (await client.get("/ready")).status_code == 503


def test_app_has_no_exchange_order_or_withdrawal_endpoints(app_settings):
    paths = {route.path.lower() for route in make_app(app_settings).routes}
    assert not any("order" in path or "withdraw" in path or "execute" in path for path in paths)


async def test_overview_personal_dormant_remains_visible_without_broad_or_market_data(app_settings):
    async with app_client(make_app(app_settings)) as client:
        await client.put("/api/watchlist", json=body(priority=100, pinned=True))
        overview = (await client.get("/api/cockpit/overview")).json()
        row = overview["assets"][0]
        assert row["asset_id"] == "ripple" and row["exchange_symbol"] == "XRPUSDT"
        assert row["state"] == "DORMANT" and row["freshness"] == "MISSING"
        assert row["price"] is None and row["rank"] is None and row["scores"] == {}
        assert row["personal_watchlist"] is True and row["monitoring_priority"] == 100
        assert overview["discovery_status"]["completed_at"] is None


async def test_overview_merges_native_live_price_fast_state_and_preserves_discovery_scores(app_settings):
    app = make_app(app_settings)
    async with app_client(app) as client:
        app.state.store.save_cycle(discovery_fixture(app.state.store))
        await client.put("/api/watchlist", json=body("NEXTUSDT", "unknown-next-mover", priority=100))
        now = datetime.now(UTC)
        app.state.store.put_state(
            "live_asset",
            "NEXTUSDT",
            now,
            {
                "symbol": "NEXTUSDT",
                "asset_id": "unknown-next-mover",
                "price": 101.25,
                "received_timestamp": now.isoformat(),
                "as_of": now.isoformat(),
                "price_quality": "AVAILABLE",
                "data_quality": {"spot_trades": "AVAILABLE", "futures": "UNAVAILABLE"},
                "liquidity_map": [{"price": 102.5, "type": "OBSERVED_ORDER_BOOK"}],
            },
        )
        app.state.store.put_state(
            "fast_state",
            "unknown-next-mover",
            now,
            {
                "asset": {"asset_id": "unknown-next-mover", "symbol": "NEXT"},
                "stage": "BREAKOUT_PRESSURE",
                "stale": False,
                "last_evaluation_at": now.isoformat(),
                "data_quality": {"spot_candles": "AVAILABLE"},
                "pressure_metrics": {"pressure_score": 81},
                "resistance": {"price": 101.3, "discovered_at": now.isoformat()},
            },
        )
        overview = (await client.get("/api/cockpit/overview")).json()
        row = next(item for item in overview["assets"] if item["asset_id"] == "unknown-next-mover")
        assert row["price"] == 101.25 and row["price_source"] == "NATIVE_LIVE_CACHE"
        assert row["state"] == "BREAKOUT_PRESSURE" and row["fast_state_stale"] is False
        assert row["freshness"] == "FRESH" and row["data_quality"]["futures"] == "UNAVAILABLE"
        assert row["levels"]["resistance"] == 101.3 and row["liquidity_map"][0]["price"] == 102.5
        assert row["pressure"]["pressure_score"] == 81
        assert row["scores"]["expansion_readiness"] == {"value": 73, "coverage": 0.8}
        assert row["rank"]["current_rank"] == 2
        assert overview["discovery_status"]["status"] == "COMPLETE"
        assert overview["discovery_status"]["completed_at"] == NOW.isoformat()
        assert app.state.store.latest_cycle().observations[0].scores["expansion_readiness"].value == 73


@pytest.mark.parametrize("stale,seconds", [(True, 0), (False, -180), (False, 180)])
async def test_ready_rejects_restored_old_or_future_fast_states(app_settings, stale, seconds):
    app = make_app(app_settings)
    async with app_client(app) as client:
        now = datetime.now(UTC)
        write_health(app.state.store, "native", at=now)
        app.state.store.put_state(
            "live_runtime", "worker", now, {"running": True, "updated_at": now.isoformat()}
        )
        app.state.store.put_state(
            "fast_state",
            "ripple",
            now,
            FastState(
                asset=Asset(asset_id="ripple", symbol="XRP", name="Ripple"),
                stale=stale,
                last_evaluation_at=now + timedelta(seconds=seconds),
                data_quality={"spot_candles": "AVAILABLE"},
            ),
        )
        health = (await client.get("/api/live/health")).json()
        assert health["feed_fresh"] and health["runtime"]["running"]
        assert health["fresh_fast_assets"] == 0
        assert (await client.get("/ready")).status_code == 503


async def test_runtime_reports_in_process_worker_separately_from_feed_and_readiness(
    app_settings, monkeypatch
):
    async def awaiting_public_data(self):
        try:
            await self.stop.wait()
        finally:
            self.store.close()

    monkeypatch.setattr(LiveObservationWorker, "run", awaiting_public_data)
    async with app_client(make_app(app_settings)) as client:
        await client.put("/api/watchlist", json=body())
        await client.post("/api/live/start")
        health = (await client.get("/api/live/health")).json()
        assert health["runtime"]["running"] and health["runtime"]["in_process"]
        assert health["runtime"]["evidence"] == "IN_PROCESS_TASK"
        assert health["feed_fresh"] is False and health["ready"] is False
        await client.post("/api/live/stop")
        assert (await client.get("/api/live/health")).json()["runtime"]["running"] is False


async def test_expired_standalone_worker_heartbeat_does_not_claim_running(app_settings):
    app = make_app(app_settings)
    async with app_client(app) as client:
        now = datetime.now(UTC)
        app.state.store.put_state(
            "live_runtime",
            "worker",
            now,
            {"running": True, "updated_at": (now - timedelta(seconds=20)).isoformat()},
        )
        runtime = (await client.get("/api/live/health")).json()["runtime"]
        assert runtime["running"] is False and runtime["evidence"] == "UNAVAILABLE"


async def test_no_watchlist_start_runs_automatic_500_discovery_without_fabricated_fast_symbols(app_settings):
    app = make_app(app_settings)
    async with app_client(app) as client:
        response = await client.post("/api/live/start")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "DISCOVERING" and payload["manual_watchlist_required"] is False
        assert payload["private_exchange_keys_required"] is False
        assert app.state.discovery_service.start_calls == 1
        assert payload["discovery_runtime"]["configured_universe_size"] == 500
        assert payload["discovery_runtime"]["running"]
        assert getattr(app.state, "live_task", None) is None
        assert app.state.store.watchlist() == []
        assert app.state.store.latest_cycle() is None  # Starting is not evidence of a completed scan.
        health = (await client.get("/api/live/health")).json()
        assert health["broad_running"] and health["runtime"]["running"]
        assert not health["runtime"]["fast_running"]
        assert health["broad_counts"] == {} and health["broad_freshness_counts"] == {}
        assert health["fast_assets"] == 0 and not health["feed_fresh"] and not health["ready"]
        assert (await client.get("/api/cockpit/overview")).json()["discovery_runtime"]["running"]
        assert (await client.post("/api/live/start")).json()["status"] == "DISCOVERING"
        supervisor = app.state.live_supervisor_task
        assert (await client.post("/api/live/stop")).json()["status"] == "STOPPED"
        assert supervisor.done()
        assert not app.state.discovery_service.running


async def test_automatic_promotion_starts_fast_worker_without_personal_entries(app_settings, monkeypatch):
    monitored = []

    async def observation_worker(self):
        monitored.extend(self.membership())
        try:
            await self.stop.wait()
        finally:
            self.store.close()

    monkeypatch.setattr(LiveObservationWorker, "run", observation_worker)
    app = make_app(app_settings)
    async with app_client(app) as client:
        assert (await client.post("/api/live/start")).json()["status"] == "DISCOVERING"
        cycle = discovery_fixture(app.state.store)
        cycle.counts = {"assets_discovered": 2, "assets_light_scanned": 2}
        app.state.store.save_cycle(cycle)
        deadline = asyncio.get_running_loop().time() + 3
        while not monitored and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert monitored == ["NEXTUSDT"]
        assert app.state.live_worker.symbols == []
        assert app.state.store.watchlist() == []
        health = (await client.get("/api/live/health")).json()
        assert health["broad_counts"] == cycle.counts
        assert health["broad_freshness_counts"] == {"MISSING": 2}
        assert health["runtime"]["fast_running"] and health["discovery_runtime"]["running"]
        assert not health["feed_fresh"]  # A running worker never fabricates provider connectivity.
        task = app.state.live_task
    assert task.done()
    assert not app.state.discovery_service.running and app.state.discovery_service.stop_calls == 1


async def test_discovery_service_endpoints_share_same_lifecycle_and_read_only_archives(app_settings):
    app = make_app(app_settings)
    async with app_client(app) as client:
        initial = (await client.get("/api/discovery/health")).json()
        assert initial["discovery_runtime"]["status"] == "STOPPED" and initial["counts"] == {}
        started = (await client.post("/api/discovery/start")).json()
        assert started["discovery_runtime"]["configured_assets"] == 500
        assert started["discovery_runtime"]["running"]
        assert app.state.discovery_service.start_calls == 1
        assert app.state.store.latest_cycle() is None
        stopped = (await client.post("/api/discovery/stop")).json()
        assert not stopped["discovery_runtime"]["running"]
        assert app.state.discovery_service.stop_calls == 1
        assert (await client.get("/api/status")).json() == {"runs": []}


@pytest.mark.parametrize("path", ["/api/live/start", "/api/discovery/start"])
async def test_discovery_subprocess_start_failure_visible_without_manual_fallback(app_settings, path):
    class FailingDiscovery(FakeDiscoveryService):
        async def start(self):
            raise RuntimeError("synthetic subprocess could not start")

    app = make_app(app_settings, discovery_service_factory=FailingDiscovery)
    async with app_client(app) as client:
        response = await client.post(path)
        assert response.status_code == 503
        health = (await client.get("/api/live/health")).json()
        assert health["provider_errors"][0]["status"] == "FAILED"
        assert "subprocess could not start" in health["provider_errors"][0]["error"]
        assert not health["discovery_runtime"]["running"] and not health["feed_fresh"]
        assert app.state.store.watchlist() == []


async def test_new_discovery_controls_require_token_and_same_origin(app_settings, monkeypatch):
    monkeypatch.setenv("COCKPIT_API_TOKEN", "synthetic-discovery-token")
    async with app_client(make_app(app_settings)) as client:
        for path in ("/api/discovery/start", "/api/discovery/stop"):
            assert (await client.post(path)).status_code == 401
            assert (
                await client.post(
                    path,
                    headers={
                        "Authorization": "Bearer synthetic-discovery-token",
                        "Origin": "https://evil.example",
                    },
                )
            ).status_code == 403
        assert (await client.get("/api/discovery/health")).status_code == 401
