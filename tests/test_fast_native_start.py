"""Actual ASGI startup and real fast membership; native transport fixtures only."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from expansion_hunter.api import create_app
from expansion_hunter.config import Settings
from expansion_hunter.discovery_models import DiscoveryCycle, DiscoveryObservation
from expansion_hunter.intelligence_api import stop_application_workers
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.live_runtime import LiveObservationWorker
from expansion_hunter.models import Asset
from expansion_hunter.providers.http import FetchedJSON
from expansion_hunter.universe_live import UniverseLiveWorker


class DiscoveryProcess:
    """Process boundary fixture: no CoinGecko, subprocess or market data."""

    def __init__(self):
        self.running = False

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False

    def status(self):
        return {"running": self.running, "status": "DISCOVERING" if self.running else "STOPPED"}


@pytest.fixture
async def native_app(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    now = datetime.now(UTC)
    monkeypatch.setattr("expansion_hunter.intelligence_api.utcnow", lambda: now)
    settings = Settings(db_path=str(tmp_path / "native-api.sqlite3"))
    app = create_app(settings)
    app.state.settings = settings
    app.state.store = IntelligenceStore(settings.db_path)
    app.state.discovery_service = DiscoveryProcess()
    app.state.live_requested = False

    class NativeHTTP:
        async def get_json(self, url, **kwargs):
            data = {"symbols": [{"symbol": "NEWUSDT", "baseAsset": "NEW", "quoteAsset": "USDT",
                                  "status": "TRADING", "isSpotTradingAllowed": True}]} if url.endswith(
                "exchangeInfo") else [{"symbol": "NEWUSDT", "lastPrice": "100", "openPrice": "99",
                                       "highPrice": "110", "lowPrice": "90", "volume": "100000",
                                       "quoteVolume": "10000000", "closeTime": int(now.timestamp() * 1000)}]
            return FetchedJSON(data=data, retrieved_at=now)

    universe = UniverseLiveWorker(settings, http=NativeHTTP(), clock=lambda: now)
    await universe.bootstrap()
    app.state.universe_worker = universe
    memberships = []

    async def await_native_feed(self):
        # Construction, SQLite restoration and actual membership are unchanged.
        # Only public transport/warmup are replaced, so no fake market event or
        # ready state is introduced by these startup regression tests.
        memberships.append(self.membership())
        try:
            await self.stop.wait()
        finally:
            self.store.close()

    monkeypatch.setattr(LiveObservationWorker, "run", await_native_feed)
    app.state.test_memberships = memberships
    app.state.test_now = now
    try:
        yield app
    finally:
        await stop_application_workers(app)
        app.state.store.close()


def promotion(app):
    now = app.state.test_now
    asset = app.state.universe_worker.asset("NEWUSDT")
    return {"asset": asset.model_dump(mode="json"), "asset_id": asset.asset_id,
            "symbol": asset.exchange_symbol, "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=180)).isoformat(),
            "source_timestamp": now.isoformat(), "source": "NATIVE_UNIVERSE_DISCOVERY",
            "native_instrument_verified": True, "bias": "LONG", "readiness": None}


def interest(app):
    row = promotion(app)
    return {"asset": row["asset"], "asset_id": row["asset_id"], "symbol": row["symbol"],
            "created_at": row["created_at"], "expires_at": row["expires_at"],
            "purpose": "TEMPORARY_CHART_OBSERVATION", "score_bonus": 0}


async def wait_for_worker(app, timeout=3):
    deadline = asyncio.get_running_loop().time() + timeout
    while not app.state.test_memberships and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert app.state.test_memberships
    assert isinstance(app.state.live_worker, LiveObservationWorker)


async def test_native_chart_interest_starts_real_fast_worker_without_cg_or_personal_list(native_app, monkeypatch):
    app = native_app

    def forbidden_cg_archive():
        raise AssertionError("Verified native chart/start must not require a CoinGecko cycle")

    monkeypatch.setattr(app.state.store, "latest_summary", forbidden_cg_archive)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
                                base_url="http://test") as client:
        selected = await client.post("/api/live/chart-interest/NEWUSDT")
        assert selected.status_code == 200
        assert selected.json()["interest"]["score_bonus"] == 0
        response = await client.post("/api/live/start")
        assert response.status_code == 200
        assert response.json()["status"] == "STARTING"
        await wait_for_worker(app)
    assert list(app.state.test_memberships[0]) == ["NEWUSDT"]
    assert app.state.live_worker._chart_interest_symbols == {"NEWUSDT"}
    assert app.state.store.watchlist() == []
    assert app.state.store.latest_cycle_id() is None
    assert app.state.store.get_state("live_asset", "NEWUSDT") is None


async def test_native_promotion_starts_real_fast_worker_without_cg_or_personal_list(native_app):
    app = native_app
    app.state.store.put_state("universe_promotion", "NEWUSDT", app.state.test_now, promotion(app))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
                                base_url="http://test") as client:
        response = await client.post("/api/live/start")
        assert response.status_code == 200 and response.json()["status"] == "STARTING"
        await wait_for_worker(app)
    assert list(app.state.test_memberships[0]) == ["NEWUSDT"]
    assert app.state.live_worker._native_universe_symbols == {"NEWUSDT"}
    assert app.state.store.watchlist() == []
    assert app.state.store.latest_cycle_id() is None
    assert app.state.store.get_state("live_asset", "NEWUSDT") is None


async def test_native_promotion_after_initial_start_is_seen_by_actual_supervisor(native_app):
    app = native_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
                                base_url="http://test") as client:
        assert (await client.post("/api/live/start")).json()["status"] == "DISCOVERING"
        app.state.store.put_state("universe_promotion", "NEWUSDT", app.state.test_now, promotion(app))
        await wait_for_worker(app)
        assert list(app.state.test_memberships[0]) == ["NEWUSDT"]
        assert app.state.store.latest_cycle_id() is None


@pytest.mark.parametrize("kind", ["universe_promotion", "chart_interest"])
@pytest.mark.parametrize("defect", ["expired", "future", "unknown", "wrong_identity", "naive", "too_long"])
async def test_expired_unknown_or_noncausal_native_intent_cannot_start_fast_worker(native_app, kind, defect):
    app = native_app
    row = promotion(app) if kind == "universe_promotion" else interest(app)
    now = app.state.test_now
    if defect == "expired":
        row["created_at"] = (now - timedelta(minutes=4)).isoformat()
        row["expires_at"] = (now - timedelta(seconds=1)).isoformat()
    elif defect == "future":
        row["created_at"] = (now + timedelta(minutes=1)).isoformat()
        row["expires_at"] = (now + timedelta(minutes=3)).isoformat()
    elif defect == "unknown":
        asset = Asset(asset_id="binance:spot:FAKEUSDT", symbol="FAKE", name="Fake",
                      exchange_symbol="FAKEUSDT")
        row.update(symbol=asset.exchange_symbol, asset_id=asset.asset_id, asset=asset.model_dump(mode="json"))
    elif defect == "wrong_identity":
        row["asset_id"] = "another-unverified-project"
    elif defect == "naive":
        row["created_at"] = now.replace(tzinfo=None).isoformat()
    elif defect == "too_long":
        row["expires_at"] = (now + timedelta(hours=1)).isoformat()
    app.state.store.put_state(kind, row["symbol"], now, row)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
                                base_url="http://test") as client:
        result = await client.post("/api/live/start")
    assert result.status_code == 200 and result.json()["status"] == "DISCOVERING"
    assert getattr(app.state, "live_task", None) is None
    assert app.state.test_memberships == []
    assert app.state.store.watchlist() == []


@pytest.mark.parametrize("changes", [{"native_instrument_verified": False}, {"source": "FAKE_SOURCE"},
                                     {"bias": "UNAVAILABLE"}, {"source_timestamp": "2000-01-01T00:00:00Z"}])
async def test_unverified_stale_or_undirected_promotion_does_not_start(native_app, changes):
    app = native_app
    row = {**promotion(app), **changes}
    app.state.store.put_state("universe_promotion", "NEWUSDT", app.state.test_now, row)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
                                base_url="http://test") as client:
        assert (await client.post("/api/live/start")).json()["status"] == "DISCOVERING"
    assert getattr(app.state, "live_task", None) is None


async def test_actual_legacy_chart_request_can_start_even_when_discovery_stage_is_dormant(native_app):
    app = native_app
    asset = Asset(asset_id="legacy-project", symbol="OLD", name="Old", exchange_symbol="OLDUSDT")
    now = app.state.test_now
    app.state.store.save_cycle(DiscoveryCycle(cycle_id="legacy", started_at=now, as_of=now, completed_at=now,
        database_path=app.state.settings.db_path, config_hash="test-only",
        observations=[DiscoveryObservation(asset=asset, as_of=now, eligible=False)], counts={}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
                                base_url="http://test") as client:
        assert (await client.post("/api/live/chart-interest/OLDUSDT")).status_code == 200
        assert (await client.post("/api/live/start")).json()["status"] == "STARTING"
        await wait_for_worker(app)
    assert list(app.state.test_memberships[0]) == ["OLDUSDT"]
    assert app.state.store.watchlist() == []
