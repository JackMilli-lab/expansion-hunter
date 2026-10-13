"""Actual SQLite research passes and isolated worker/provider lifecycle tests."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from expansion_hunter.config import Settings
from expansion_hunter.intelligence_runtime import (
    BackgroundIntelligence,
    run_intelligence_worker,
    run_macro_calendar_worker,
)
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.macro_models import MacroEvent, MacroProviderReport, MacroQuality

NOW = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def service(tmp_path):
    result = BackgroundIntelligence(str(tmp_path / "research.sqlite3"), clock=lambda: NOW)
    try:
        yield result
    finally:
        result.close()


def test_empty_real_archive_reports_missing_coverage_without_synthetic_outcomes(service):
    report = service.run_once()
    assert report["status"] == "AVAILABLE"  # Worker health, not a market prediction.
    assert report["cycle"]["status"] == "UNAVAILABLE"
    assert report["outcomes"]["matured_outcomes"] == 0
    assert report["outcomes"]["misses_recorded"] == 0
    assert report["live_trading"] is False and report["research_weight_updates"] is False
    assert report["processing_latency_ms"] >= 0
    assert service.store.get_state("intelligence_runtime", "worker") == report


def test_current_cycle_progress_skips_full_history_loading_but_keeps_outcomes(service, monkeypatch):
    service.store.put_state("intelligence_progress", "worker", NOW, {"cycle_id": "synthetic-cycle"})
    monkeypatch.setattr(service.store, "latest_cycle_id", lambda: "synthetic-cycle")

    def forbidden():
        pytest.fail("unchanged cycle loaded the expensive complete archive")

    monkeypatch.setattr(service.worker, "process_latest", forbidden)
    report = service.run_once()
    assert report["cycle"] == {"status": "UNCHANGED", "cycle_id": "synthetic-cycle"}
    assert report["outcomes"]["matured_outcomes"] == 0


def test_new_cycle_is_processed_once_and_next_pass_checks_cheap_progress(service, monkeypatch):
    monkeypatch.setattr(service.store, "latest_cycle_id", lambda: "synthetic-new-cycle")
    calls = []

    def publish():
        calls.append(True)
        service.store.put_state("intelligence_progress", "worker", NOW, {"cycle_id": "synthetic-new-cycle"})
        return {"status": "COMPLETE", "cycle_id": "synthetic-new-cycle"}

    monkeypatch.setattr(service.worker, "process_latest", publish)
    assert service.run_once()["cycle"]["status"] == "COMPLETE"
    assert service.run_once()["cycle"]["status"] == "UNCHANGED"
    assert calls == [True]


@pytest.mark.parametrize("failed", ["cycle", "outcomes", "both"])
def test_failed_component_is_visible_and_does_not_block_other_work(service, monkeypatch, failed):
    monkeypatch.setattr(service.store, "latest_cycle_id", lambda: "synthetic-unprocessed")

    def failure(*args):
        raise RuntimeError("synthetic bounded component failure")

    if failed in {"cycle", "both"}:
        monkeypatch.setattr(service.worker, "process_latest", failure)
    else:
        monkeypatch.setattr(service.worker, "process_latest", lambda: {"status": "COMPLETE"})
    if failed in {"outcomes", "both"}:
        monkeypatch.setattr(service.learning, "evaluate", failure)
    report = service.run_once()
    assert report["status"] == ("FAILED" if failed == "both" else "PARTIAL")
    assert len(report["issues"]) == (2 if failed == "both" else 1)
    if failed == "cycle":
        assert report["outcomes"]["matured_outcomes"] == 0
    elif failed == "outcomes":
        assert report["cycle"]["status"] == "COMPLETE"


def app(tmp_path, live=False):
    return SimpleNamespace(state=SimpleNamespace(
        settings=Settings(db_path=str(tmp_path / "worker-owned.sqlite3")), live_requested=live))


async def until(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    pytest.fail("deterministic worker did not reach expected state")


async def test_real_automatic_worker_owns_connection_and_restores_persistent_health(tmp_path):
    application = app(tmp_path)
    main_store = IntelligenceStore(application.state.settings.db_path)
    task = asyncio.create_task(run_intelligence_worker(application, interval_seconds=0.01, clock=lambda: NOW))
    try:
        await until(lambda: getattr(application.state, "intelligence_health", {}).get("status") == "AVAILABLE")
        assert main_store.get_state("intelligence_runtime", "worker")["updated_at"] == NOW.isoformat()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert application.state.intelligence_health["status"] == "STOPPED"
        assert main_store._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        main_store.close()


async def test_cancellation_drains_running_research_before_own_database_closes(tmp_path, monkeypatch):
    entered, release, finished, closed = (threading.Event() for _ in range(4))
    main_thread = threading.get_ident()

    class Controlled:
        def __init__(self, *args, **kwargs):
            assert threading.get_ident() != main_thread

        def run_once(self):
            entered.set()
            release.wait(2)
            finished.set()
            return {"status": "AVAILABLE"}

        def close(self):
            assert finished.is_set(), "connection closed while research still owned it"
            closed.set()

    monkeypatch.setattr("expansion_hunter.intelligence_runtime.BackgroundIntelligence", Controlled)
    application = app(tmp_path)
    task = asyncio.create_task(run_intelligence_worker(application, interval_seconds=60))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0.01)
    try:
        assert not task.done() and not closed.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() and closed.is_set()


async def test_macro_calendar_never_requests_provider_while_observation_is_disabled(tmp_path):
    def forbidden():
        pytest.fail("disabled app made a calendar request")

    application = app(tmp_path)
    task = asyncio.create_task(run_macro_calendar_worker(application, interval_seconds=0.01,
        startup_delay_seconds=0, provider_factory=forbidden))
    await asyncio.sleep(0.025)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert application.state.macro_calendar_health["status"] == "STOPPED"
    assert not Path(application.state.settings.db_path).exists()


class CalendarFixture:
    failure = False
    closed = False
    calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        type(self).closed = True

    async def calendar(self, start, end):
        type(self).calls.append((start, end))
        if type(self).failure:
            raise ConnectionError("synthetic authoritative-calendar failure")
        event = MacroEvent(event_id="synthetic-appointment", event_name="Synthetic schedule only",
            country="US", category="US_CPI", importance=3, scheduled_timestamp=NOW + timedelta(hours=1),
            available_at=NOW, source="TEST_ONLY: authoritative-calendar fixture", source_quality="AUTHORITATIVE")
        return MacroProviderReport(provider="TEST_ONLY_CALENDAR", retrieved_at=NOW,
            calendar_quality=MacroQuality.AVAILABLE, events=[event], issues=["Numeric actual and consensus unavailable"])


@pytest.mark.parametrize("failed", [False, True])
async def test_macro_worker_imports_original_vintage_or_records_provider_failure(tmp_path, failed):
    CalendarFixture.failure, CalendarFixture.closed, CalendarFixture.calls = failed, False, []
    application = app(tmp_path, live=True)
    task = asyncio.create_task(run_macro_calendar_worker(application, interval_seconds=60,
        startup_delay_seconds=0, provider_factory=CalendarFixture, clock=lambda: NOW))
    try:
        await until(lambda: getattr(application.state, "macro_calendar_health", {}).get("status") == ("FAILED" if failed else "AVAILABLE"))
        store = IntelligenceStore(application.state.settings.db_path)
        try:
            health = store.get_state("macro_runtime", "worker")
            assert health["consensus_quality"] == health["release_quality"] == "UNAVAILABLE"
            assert CalendarFixture.calls == [(NOW, NOW + timedelta(days=7))]
            assert CalendarFixture.closed
            events = store.objects("macro_event", NOW)
            assert len(events) == (0 if failed else 1)
            assert not store.objects("macro_release", NOW) and not store.objects("macro_consensus", NOW)
            if not failed:
                assert datetime.fromisoformat(events[0]["available_at"]) == NOW
                assert not store.objects("macro_event", NOW - timedelta(seconds=1))
            else:
                assert "synthetic authoritative-calendar failure" in health["error"]
        finally:
            store.close()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True])
async def test_invalid_worker_cadence_fails_before_start(tmp_path, bad):
    with pytest.raises(ValueError):
        await run_intelligence_worker(app(tmp_path), interval_seconds=bad)
