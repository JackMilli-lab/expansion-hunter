"""Actual API controls create monitoring intent, never invented market data."""
import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from expansion_hunter.api import create_app
from expansion_hunter.config import Settings
from expansion_hunter.discovery_models import DiscoveryCycle, DiscoveryObservation
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.models import Asset, utcnow


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    settings = Settings(db_path=str(tmp_path / "test.sqlite3"))
    app = create_app(settings)
    app.state.settings = settings
    app.state.store = IntelligenceStore(settings.db_path)
    now = utcnow()
    app.state.store.save_cycle(DiscoveryCycle(cycle_id="test", started_at=now, as_of=now, completed_at=now,
        database_path=settings.db_path, config_hash="synthetic-test-only",
        observations=[DiscoveryObservation(asset=Asset(asset_id="hedera-hashgraph", symbol="HBAR", name="Hedera",
            exchange_symbol="HBARUSDT"), as_of=now, eligible=False)], counts={}))
    yield app
    app.state.store.close()


async def test_selected_coin_temporary_monitoring_does_not_add_a_personal_coin_or_score(app):
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        result = await client.post("/api/live/chart-interest/HBARUSDT")
        assert result.status_code == 200
        body = result.json()
        assert body["data_status"] == "PENDING_NATIVE_DATA"
        assert body["interest"]["score_bonus"] == 0
        assert body["interest"]["asset_id"] == "hedera-hashgraph"
        from datetime import datetime
        created = datetime.fromisoformat(body["interest"]["created_at"])
        expires = datetime.fromisoformat(body["interest"]["expires_at"])
        assert expires - created == timedelta(minutes=15)
        assert app.state.store.watchlist() == []
        assert app.state.store.get_state("live_asset", "HBARUSDT") is None
        assert len(app.state.store.states("chart_interest")) == 1


async def test_unknown_exchange_pair_is_not_fabricated(app):
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/live/chart-interest/FAKEUSDT")).status_code == 404
        assert app.state.store.states("chart_interest") == []


async def test_concurrent_chart_renewals_follow_commit_order_without_500_or_backward_state(app, monkeypatch):
    # Actual concurrent ASGI requests execute the synchronous API handler in
    # real worker threads. Delaying its first write reproduces the live race.
    latest = app.state.store.latest_summary()
    monkeypatch.setattr(app.state.store, "latest_summary", lambda: latest)
    initial = datetime(2026, 10, 6, 12, tzinfo=UTC)
    call_lock, counter = threading.Lock(), [0]

    def ordered_clock():
        with call_lock:
            counter[0] += 1
            return initial + timedelta(milliseconds=counter[0])

    monkeypatch.setattr("expansion_hunter.intelligence_api.utcnow", ordered_clock)
    original, writes = app.state.store.put_state, [0]

    def delayed_first_write(kind, object_id, updated_at, payload):
        with call_lock:
            writes[0] += 1
            first = writes[0] == 1
        if first:
            time.sleep(0.10)
        return original(kind, object_id, updated_at, payload)

    monkeypatch.setattr(app.state.store, "put_state", delayed_first_write)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(*(client.post("/api/live/chart-interest/HBARUSDT") for _ in range(12)))
    assert [response.status_code for response in responses] == [200] * 12
    dates = [datetime.fromisoformat(response.json()["interest"]["created_at"]) for response in responses]
    saved = app.state.store.states("chart_interest")
    assert len(saved) == 1
    assert datetime.fromisoformat(saved[0]["created_at"]) == max(dates)
    assert datetime.fromisoformat(saved[0]["expires_at"]) - max(dates) == timedelta(minutes=15)
    assert app.state.store.watchlist() == []


async def test_native_chart_pair_lookup_avoids_heavy_discovery_archive_read(app, monkeypatch):
    asset = Asset(asset_id="binance:spot:HBARUSDT", symbol="HBAR", name="HBAR", exchange_symbol="HBARUSDT")

    class NativeUniverse:
        def asset(self, symbol):
            return asset if symbol == "HBARUSDT" else None

    app.state.universe_worker = NativeUniverse()

    def forbidden_summary():
        raise AssertionError("Verified native pair must not read full discovery metadata")

    monkeypatch.setattr(app.state.store, "latest_summary", forbidden_summary)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/live/chart-interest/HBARUSDT")
    assert response.status_code == 200
    assert response.json()["interest"]["asset_id"] == asset.asset_id


async def test_unconfigured_wallet_provider_is_explicitly_unavailable(app):
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/api/whales?asset_id=hedera-hashgraph")).json()
        assert body["status"] == "UNAVAILABLE"
        assert body["transfers"] == [] and body["production_weight"] == 0
