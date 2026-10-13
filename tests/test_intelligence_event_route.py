"""The actual event API must expose misses from their persisted object kind."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from expansion_hunter.api import create_app
from expansion_hunter.config import Settings
from expansion_hunter.event_learning import MissRecord
from expansion_hunter.intelligence_store import IntelligenceStore

NOW = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    settings = Settings(db_path=str(tmp_path / "miss-api.sqlite3"))
    application = create_app(settings)
    application.state.store = IntelligenceStore(settings.db_path)
    try:
        yield application
    finally:
        application.state.store.close()


def record(app, identity, published):
    miss = MissRecord(miss_id=identity, asset_id="synthetic-test-only", detected_at=published,
        baseline_timestamp=NOW - timedelta(hours=1), baseline_price=100,
        expansion_start=NOW - timedelta(minutes=5), expansion_confirmed_at=NOW,
        expansion_start_definition="Synthetic route archive fixture", expansion_magnitude=0.15,
        kind="FULL_MISS", source="TEST_ONLY: archived synthetic scenario",
        reason="No earlier synthetic alert", missing_data=["sector"], pre_move_rank=40)
    app.state.store.append("miss_event", miss.miss_id, NOW, published, miss, miss.asset_id)
    return miss


async def fetch(app, when):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/api/intelligence/events", params={"before": when.isoformat()})


async def test_stored_miss_event_is_visible_through_real_api(app):
    miss = record(app, "known-miss", NOW)
    app.state.store.append("research_event", "unrelated-event", NOW, NOW, {"event_id": "unrelated-event"})
    response = await fetch(app, NOW)
    assert response.status_code == 200
    assert response.json()["misses"] == [miss.model_dump(mode="json")]
    assert response.json()["events"] == [{"event_id": "unrelated-event"}]


async def test_future_received_miss_cannot_appear_in_earlier_api_query(app):
    early = record(app, "known-miss", NOW)
    record(app, "late-miss", NOW + timedelta(seconds=30))
    earlier = await fetch(app, NOW + timedelta(seconds=29))
    assert earlier.json()["misses"] == [early.model_dump(mode="json")]
    later = await fetch(app, NOW + timedelta(seconds=30))
    assert {row["miss_id"] for row in later.json()["misses"]} == {"known-miss", "late-miss"}
