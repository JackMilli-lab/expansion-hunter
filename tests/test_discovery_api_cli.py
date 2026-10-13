"""Operator readback, configuration provenance and continuous recovery contracts."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from expansion_hunter import cli
from expansion_hunter.api import create_app
from expansion_hunter.config import Settings
from expansion_hunter.discovery_config import DiscoverySettings
from expansion_hunter.discovery_models import (
    DiscoveryCycle,
    DiscoveryEvent,
    DiscoveryFeature,
    DiscoveryObservation,
    DiscoveryStage,
    Freshness,
    RankHistory,
)
from expansion_hunter.discovery_store import DiscoveryStore
from expansion_hunter.models import Asset
from expansion_hunter.storage import Store


def archived_cycle(database_path):
    now = datetime(2026, 1, 5, 12, tzinfo=UTC)
    asset = Asset(asset_id="unknown-asset", symbol="XYZ", name="Unseen candidate")
    other = Asset(asset_id="other-asset", symbol="OTHER", name="Other candidate")
    observation = DiscoveryObservation(
        asset=asset, as_of=now, stage=DiscoveryStage.WATCH,
        previous_stage=DiscoveryStage.RADAR, eligible=True, disposition="COMPLETE",
        freshness=Freshness.FRESH, rank=RankHistory(current_rank=1, previous_rank=7),
        features={"price_change": DiscoveryFeature(
            value=0.01, provider="fixture", timeframe="15m", source_timestamp=now,
            retrieved_at=now, freshness=Freshness.FRESH, critical=True,
        )}, reasons=["Relative strength and compression aligned"],
    )
    stale = DiscoveryObservation(
        asset=other, as_of=now, freshness=Freshness.STALE,
        features={"price_change": DiscoveryFeature(
            value=0.01, provider="fixture", timeframe="15m", source_timestamp=now-timedelta(hours=1),
            retrieved_at=now, freshness=Freshness.STALE, critical=True,
        )}, missing=["fresh_price"], reasons=["Stale source cannot emit high-priority events"],
    )
    return DiscoveryCycle(
        cycle_id="fixture-cycle", started_at=now, as_of=now, completed_at=now+timedelta(seconds=2),
        database_path=str(Path(database_path).resolve()), config_hash="fixture-config",
        counts={"assets_discovered": 2, "assets_partial": 1}, observations=[observation, stale],
        events=[DiscoveryEvent(
            event_id="fixture-event", cycle_id="fixture-cycle", asset_id=asset.asset_id,
            timestamp=now, event_type=DiscoveryStage.WATCH, previous_stage=DiscoveryStage.RADAR,
            reason="Relative strength and compression aligned",
        )], lists={"DARK_HORSE": [asset.asset_id, other.asset_id]}, status="DEGRADED",
        provider_issues=["Derivatives unavailable; independent spot discovery continued"],
    )


@pytest.mark.asyncio
async def test_discovery_api_readback_quality_and_point_in_time_cutoff(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_API_TOKEN", raising=False)
    database_path = tmp_path / "operational.sqlite3"
    store = DiscoveryStore(str(database_path))
    cycle = archived_cycle(database_path)
    store.save_cycle(cycle)
    store.close()
    app = create_app(Settings(db_path=str(database_path)))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/discovery/latest")
            assert response.status_code == 200
            assert response.json()["cycle_id"] == cycle.cycle_id
            assert response.headers["X-Cockpit-Database"] == str(database_path.resolve())
            assert (await client.get(f"/api/discovery/cycles/{cycle.cycle_id}")).json() == response.json()
            assert (await client.get("/api/discovery/cycles/missing")).status_code == 404
            history = (await client.get("/api/discovery/assets/unknown-asset/history")).json()
            assert history["observations"][0]["rank"]["previous_rank"] == 7
            # Evidence is not readable at a time before the cycle was published.
            before_publication = cycle.as_of.isoformat()
            past = await client.get("/api/discovery/assets/unknown-asset/history", params={"before": before_publication})
            assert past.json()["observations"] == []
            events = (await client.get("/api/discovery/events", params={"asset_id": "unknown-asset"})).json()
            assert events["events"][0]["event_type"] == "WATCH"
            assert (await client.get("/api/discovery/events", params={"before": before_publication})).json()["events"] == []
            status = (await client.get("/api/discovery/status")).json()
            assert status["freshness_as_recorded"] == {"FRESH": 1, "STALE": 1}
            assert status["critical_data_unavailable_as_recorded"] == 1
            assert status["report_age_seconds"] > 0
            assert status["live_trading_enabled"] is False
            lists = (await client.get("/api/discovery/lists", params={"limit": 1})).json()
            assert lists["lists"]["DARK_HORSE"] == ["unknown-asset"]
            assert lists["calibration"] == "UNCALIBRATED"
            for endpoint in ["latest", "events", "status", "lists"]:
                assert (await client.post("/api/discovery/"+endpoint)).status_code == 405


@pytest.mark.asyncio
async def test_discovery_api_empty_auth_and_query_bounds(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_API_TOKEN", "fixture-secret")
    app = create_app(Settings(db_path=str(tmp_path / "empty.sqlite3")))
    headers = {"Authorization": "Bearer fixture-secret"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for endpoint in ["latest", "status", "events", "lists", "assets/XYZ/history"]:
                assert (await client.get("/api/discovery/"+endpoint)).status_code == 401
            assert (await client.get("/api/discovery/latest", headers=headers)).status_code == 404
            assert (await client.get("/api/discovery/lists", headers=headers)).status_code == 404
            assert (await client.get("/api/discovery/events", headers=headers)).json()["events"] == []
            status = (await client.get("/api/discovery/status", headers=headers)).json()
            assert status["status"] == "NO_DISCOVERY_CYCLE"
            assert (await client.get("/api/discovery/events", headers=headers, params={"limit": 1001})).status_code == 422
            assert (await client.get("/api/discovery/assets/XYZ/history", headers=headers, params={"limit": 0})).status_code == 422
            assert (await client.get("/api/discovery/events", headers=headers, params={"before": "2026-01-01T00:00:00"})).status_code == 422
            # Legacy body is kept intact even after additive discovery tables exist.
            assert (await client.get("/api/status", headers=headers)).json() == {"runs": []}


def test_new_cli_uses_operational_config_without_changing_legacy_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    configured = Settings(db_path="data/explicit-operational.sqlite3", binance_spot_url="https://data-api.binance.vision")
    (tmp_path / "config/settings.json").write_text(configured.model_dump_json())
    discover = cli.parser().parse_args(["discover"])
    assert cli.load_command_settings(discover).db_path == configured.db_path
    assert cli.load_command_settings(discover).binance_spot_url == "https://data-api.binance.vision"
    legacy = cli.parser().parse_args(["scan"])
    assert cli.load_command_settings(legacy).db_path == Settings().db_path
    override = tmp_path / "explicit.json"
    override.write_text(Settings(db_path="data/custom.sqlite3").model_dump_json())
    args = cli.parser().parse_args(["--config", str(override), "discover"])
    assert cli.load_command_settings(args).db_path == "data/custom.sqlite3"


@pytest.mark.asyncio
async def test_watch_recovers_with_visible_bounded_backoff_and_no_orders(tmp_path, monkeypatch, capsys):
    from expansion_hunter import runtime

    cycle = archived_cycle(tmp_path / "operational.sqlite3")
    attempts = []
    sleeps = []

    class FailingThenHealthyEngine:
        async def run_once(self):
            attempts.append(True)
            if len(attempts) <= 2:
                raise RuntimeError("temporary provider outage")
            return cycle

    @asynccontextmanager
    async def fixture_runtime(settings, discovery_settings):
        yield FailingThenHealthyEngine()

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(runtime, "discovery_runtime", fixture_runtime)
    monkeypatch.setattr(cli.asyncio, "sleep", fake_sleep)
    args = SimpleNamespace(command="discover-watch", poll_interval=2, output=None, cycles=1)
    await cli.discovery_live(args, Settings(db_path=cycle.database_path), DiscoverySettings())
    captured = capsys.readouterr()
    assert len(attempts) == 3
    assert sleeps == [4, 8]
    assert '"status": "FAILED"' in captured.out
    assert '"fixture-cycle"' in captured.out
    assert f"Operational database: {cycle.database_path}" in captured.err


@pytest.mark.asyncio
async def test_one_shot_discovery_failure_propagates_for_operator_exit_status(tmp_path, monkeypatch):
    from expansion_hunter import runtime

    class FailingEngine:
        async def run_once(self):
            raise RuntimeError("provider failed")

    @asynccontextmanager
    async def fixture_runtime(settings, discovery_settings):
        yield FailingEngine()

    monkeypatch.setattr(runtime, "discovery_runtime", fixture_runtime)
    args = SimpleNamespace(command="discover", poll_interval=None, output=None, cycles=None)
    with pytest.raises(RuntimeError, match="provider failed"):
        await cli.discovery_live(args, Settings(db_path=str(tmp_path / "db.sqlite3")), DiscoverySettings())


def test_cli_safe_import_and_status_expose_operational_database(tmp_path, monkeypatch, capsys):
    source_path = tmp_path / "smoke.sqlite3"
    source = Store(str(source_path))
    source.begin("old-failure", datetime(2026, 1, 1, tzinfo=UTC))
    source.fail("old-failure", "recorded old provider failure")
    source.close()
    source_bytes = source_path.read_bytes()
    destination = tmp_path / "cockpit.sqlite3"
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(Settings(db_path=str(destination)).model_dump_json())
    monkeypatch.setattr("sys.argv", ["expansion-hunter", "--config", str(settings_path), "import-smoke", "--source", str(source_path)])
    cli.main()
    first = capsys.readouterr()
    assert '"runs": 1' in first.out
    assert str(destination.resolve()) in first.out
    assert source_path.read_bytes() == source_bytes
    store = DiscoveryStore(str(destination))
    assert store.run_status()[0]["error"] == "recorded old provider failure"
    store.close()
    monkeypatch.setattr("sys.argv", ["expansion-hunter", "--config", str(settings_path), "discovery-status"])
    cli.main()
    assert '"live_trading_enabled": false' in capsys.readouterr().out
