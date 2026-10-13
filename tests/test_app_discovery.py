"""Synthetic subprocess-boundary tests, without invented live-market evidence."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from expansion_hunter.app_discovery import DiscoveryService
from expansion_hunter.config import Settings
from expansion_hunter.discovery_config import DiscoverySettings
from expansion_hunter.intelligence_store import IntelligenceStore


class FakeProcess:
    def __init__(self, pid: int, *, ignore_terminate=False):
        self.pid = pid
        self.returncode = None
        self.ignore_terminate = ignore_terminate
        self.terminate_calls = 0
        self.kill_calls = 0
        self._exit = asyncio.Event()

    async def wait(self):
        await self._exit.wait()
        return self.returncode

    def exit(self, code):
        self.returncode = code
        self._exit.set()

    def terminate(self):
        self.terminate_calls += 1
        if not self.ignore_terminate:
            self.exit(-15)

    def kill(self):
        self.kill_calls += 1
        self.exit(-9)


class FakeSpawn:
    def __init__(self, *, failures=0, ignore_terminate=False):
        self.failures = failures
        self.ignore_terminate = ignore_terminate
        self.calls = []
        self.processes = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if len(self.calls) <= self.failures:
            raise OSError("synthetic spawn unavailable")
        process = FakeProcess(12000 + len(self.calls), ignore_terminate=self.ignore_terminate)
        self.processes.append(process)
        return process


async def until(predicate, *, timeout=2):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("supervisor condition did not become true")
        await asyncio.sleep(0.002)


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=str(tmp_path / "operational.sqlite3"))


def service(settings, spawn, **kwargs):
    return DiscoveryService(
        settings,
        spawn=spawn,
        restart_initial_seconds=kwargs.pop("restart_initial_seconds", 0.02),
        restart_max_seconds=kwargs.pop("restart_max_seconds", 0.08),
        stable_reset_seconds=kwargs.pop("stable_reset_seconds", 100),
        shutdown_timeout_seconds=kwargs.pop("shutdown_timeout_seconds", 0.02),
        **kwargs,
    )


async def test_empty_watchlist_starts_configured_500_separate_process(settings):
    spawn = FakeSpawn()
    worker = service(settings, spawn)
    try:
        status = await worker.start()
        assert status["configured_assets"] == status["configured_universe_size"] == 500
        assert status["fast_path_isolation"] == "SEPARATE_PROCESS"
        assert status["personal_watchlist_required"] is False
        assert status["live_trading"] is False
        assert status["status"] == "DISCOVERING"
        assert status["process_running"] and status["supervision_running"]
        args, options = spawn.calls[0]
        assert args[:4] == (sys.executable, "-u", "-m", "expansion_hunter.cli")
        assert args[6] == "discover-watch"
        assert not any(argument.startswith(("--watchlist", "--symbol")) for argument in args)
        assert options["stderr"] == asyncio.subprocess.STDOUT
        assert options["stdout"] is worker._log
        store = IntelligenceStore(settings.db_path)
        try:
            assert store.watchlist() == []
            assert store.latest_cycle() is None
            assert store.states("discovery_runtime")[0]["running"] is True
            assert store.objects("feed_health") == []  # Process start is not feed connectivity.
        finally:
            store.close()
    finally:
        await worker.stop()


async def test_start_idempotent_and_concurrent(settings):
    spawn = FakeSpawn()
    worker = service(settings, spawn)
    try:
        first, second = await asyncio.gather(worker.start(), worker.start())
        third = await worker.start()
        assert first["pid"] == second["pid"] == third["pid"]
        assert len(spawn.calls) == 1
        assert worker.restart_count == 0
    finally:
        await worker.stop()


async def test_absolute_operational_database_public_provider_config_and_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COINGECKO_API_KEY", "synthetic-secret-not-for-config")
    monkeypatch.setenv("PYTHONPATH", "/synthetic/inherited/pythonpath")
    settings = Settings(
        db_path="archive/cockpit.sqlite3",
        binance_spot_url="https://data-api.binance.vision",
        binance_spot_fallback_urls=["https://api1.binance.com"],
    )
    spawn = FakeSpawn()
    worker = service(settings, spawn)
    try:
        status = await worker.start()
        native = json.loads((worker.work_dir / "settings.json").read_text())
        assert native["db_path"] == str((tmp_path / "archive/cockpit.sqlite3").resolve())
        assert status["database_path"] == native["db_path"]
        assert native["binance_spot_url"] == settings.binance_spot_url
        assert native["binance_spot_fallback_urls"] == settings.binance_spot_fallback_urls
        assert "synthetic-secret-not-for-config" not in json.dumps(native)
        assert "PYTHONPATH" not in native
        # No env replacement: create_subprocess_exec inherits configured public
        # provider variables without copying or printing their values.
        assert "env" not in spawn.calls[0][1]
        args = spawn.calls[0][0]
        assert Path(args[args.index("--config") + 1]).is_absolute()
        assert Path(args[args.index("--output") + 1]).is_absolute()
    finally:
        await worker.stop()


async def test_universe_override_preserves_existing_discovery_budgets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    configured = DiscoverySettings(universe_size=77, deep_per_cycle=2)
    (tmp_path / "config/discovery.json").write_text(configured.model_dump_json())
    settings = Settings(db_path=str(tmp_path / "archive.sqlite3"), top_n=1000)
    worker = service(settings, FakeSpawn())
    try:
        await worker.start()
        saved = DiscoverySettings.model_validate_json((worker.work_dir / "discovery.json").read_text())
        assert saved.universe_size == 1000
        assert saved.deep_per_cycle == 2
        before = configured.model_dump(exclude={"universe_size"})
        after = saved.model_dump(exclude={"universe_size"})
        assert before == after
    finally:
        await worker.stop()


async def test_graceful_stop_closes_log_keeps_archive_and_records_expected_exit(settings):
    store = IntelligenceStore(settings.db_path)
    when = datetime(2026, 10, 1, tzinfo=UTC)
    store.append("smoke_evidence", "existing", when, when, {"preserve": True})
    store.close()
    spawn = FakeSpawn()
    worker = service(settings, spawn)
    await worker.start()
    log = spawn.calls[0][1]["stdout"]
    stopped = await worker.stop()
    assert stopped["status"] == "STOPPED"
    assert not stopped["running"] and not stopped["supervision_running"]
    assert spawn.processes[0].terminate_calls == 1
    assert spawn.processes[0].kill_calls == 0
    assert log.closed
    assert stopped["last_exit"]["expected"] is True
    assert stopped["last_exit"]["forced"] is False
    store = IntelligenceStore(settings.db_path)
    try:
        assert store.objects("smoke_evidence") == [{"preserve": True}]
        assert store.states("discovery_runtime")[0]["status"] == "STOPPED"
        events = store.objects("discovery_process_event")
        assert [event["event"] for event in events] == ["STARTED", "STOPPED"]
        assert store._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        store.close()
    assert (await worker.stop())["status"] == "STOPPED"  # Idempotent shutdown.


async def test_graceful_timeout_forces_kill_but_is_expected_shutdown(settings):
    spawn = FakeSpawn(ignore_terminate=True)
    worker = service(settings, spawn)
    await worker.start()
    status = await worker.stop()
    process = spawn.processes[0]
    assert process.terminate_calls == process.kill_calls == 1
    assert status["returncode"] == -9
    assert status["status"] == "STOPPED"
    assert status["last_exit"]["expected"] is True
    assert status["last_exit"]["forced"] is True
    assert spawn.calls[0][1]["stdout"].closed


@pytest.mark.parametrize("code", [0, 7, -9])
async def test_unexpected_exit_visible_archived_and_restarted(settings, code):
    spawn = FakeSpawn()
    worker = service(settings, spawn, restart_initial_seconds=0.05)
    try:
        await worker.start()
        old_pid = worker.process.pid
        log = spawn.calls[0][1]["stdout"]
        worker.process.exit(code)
        await until(lambda: worker.last_exit is not None)
        failed = worker.status()
        assert failed["status"] == "FAILED"
        assert not failed["process_running"]
        assert failed["supervision_running"] and failed["running"]
        assert failed["next_retry_at"] is not None
        assert failed["last_exit"] == {
            "observed_at": failed["last_exit"]["observed_at"],
            "pid": old_pid,
            "returncode": code,
            "expected": False,
            "forced": False,
        }
        assert log.closed
        store = IntelligenceStore(settings.db_path)
        try:
            exits = store.objects("discovery_process_event")
            failure = next(event for event in exits if event["event"] == "UNEXPECTED_EXIT")
            assert failure["last_exit"]["returncode"] == code
            assert failure["error"] == f"scanner exited unexpectedly with returncode {code}"
        finally:
            store.close()
        # A repeated Start during retry must not bypass backoff or duplicate workers.
        assert (await worker.start())["status"] == "FAILED"
        assert len(spawn.calls) == 1
        await until(lambda: len(spawn.calls) == 2)
        resumed = worker.status()
        assert resumed["status"] == "DISCOVERING"
        assert resumed["pid"] != old_pid
        assert resumed["restart_count"] == 1
        assert resumed["error"] is None
        assert resumed["last_exit"]["expected"] is False
        assert resumed["next_retry_at"] is None
    finally:
        await worker.stop()


async def test_restart_backoff_is_exponential_and_bounded(settings):
    spawn = FakeSpawn()
    worker = service(settings, spawn, restart_initial_seconds=0.01, restart_max_seconds=0.025)
    observed_delays = []
    original_wait = worker._wait_backoff

    async def capture(delay):
        observed_delays.append(delay)
        return await original_wait(delay)

    worker._wait_backoff = capture
    try:
        await worker.start()
        for expected_count in range(2, 6):
            worker.process.exit(9)
            await until(lambda expected=expected_count: len(spawn.calls) == expected)
        assert observed_delays == [0.01, 0.02, 0.025, 0.025]
        assert worker.restart_count == 4
        assert all(call[1]["stdout"].closed for call in spawn.calls[:-1])
    finally:
        await worker.stop()


async def test_stable_worker_resets_restart_backoff(settings):
    spawn = FakeSpawn()
    worker = service(settings, spawn, stable_reset_seconds=0)
    delays = []
    original_wait = worker._wait_backoff

    async def capture(delay):
        delays.append(delay)
        return await original_wait(delay)

    worker._wait_backoff = capture
    try:
        await worker.start()
        for expected_count in (2, 3, 4):
            worker.process.exit(9)
            await until(lambda expected=expected_count: len(spawn.calls) == expected)
        assert delays == [0.02, 0.02, 0.02]
    finally:
        await worker.stop()


async def test_stop_cancels_long_backoff_and_does_not_respawn(settings):
    spawn = FakeSpawn()
    worker = service(settings, spawn, restart_initial_seconds=60, restart_max_seconds=60)
    await worker.start()
    worker.process.exit(1)
    await until(lambda: worker.next_retry_at is not None)
    stopped = await asyncio.wait_for(worker.stop(), 1)
    assert not stopped["running"]
    assert stopped["status"] == "STOPPED"
    assert stopped["next_retry_at"] is None
    await asyncio.sleep(0.02)
    assert len(spawn.calls) == 1
    assert spawn.calls[0][1]["stdout"].closed


async def test_spawn_failure_stays_visible_but_supervisor_recovers(settings):
    spawn = FakeSpawn(failures=1)
    worker = service(settings, spawn)
    try:
        with pytest.raises(OSError, match="synthetic spawn unavailable"):
            await worker.start()
        failed = worker.status()
        assert failed["status"] == "FAILED"
        assert failed["running"] and not failed["process_running"]
        assert spawn.calls[0][1]["stdout"].closed
        store = IntelligenceStore(settings.db_path)
        try:
            failure = next(event for event in store.objects("discovery_process_event")
                           if event["event"] == "SPAWN_FAILED")
            assert "spawn unavailable" in failure["error"]
        finally:
            store.close()
        await until(lambda: len(spawn.processes) == 1)
        assert worker.status()["process_running"] is True
        assert worker.restart_count == 1
    finally:
        await worker.stop()


async def test_repeated_spawn_failures_do_not_leak_handles_or_stop_supervision(settings):
    spawn = FakeSpawn(failures=100)
    worker = service(settings, spawn, restart_initial_seconds=0.01, restart_max_seconds=0.02)
    try:
        with pytest.raises(OSError):
            await worker.start()
        await until(lambda: len(spawn.calls) >= 3)
        assert worker.status()["status"] == "FAILED"
        assert worker.status()["supervision_running"] is True
        assert worker.retry_delay_seconds <= 0.02
        assert all(options["stdout"].closed for _, options in spawn.calls)
        assert worker.restart_count >= 2
    finally:
        await worker.stop()


async def test_start_stop_start_uses_new_child_and_preserves_failure_history(settings):
    spawn = FakeSpawn()
    worker = service(settings, spawn)
    await worker.start()
    first = worker.process.pid
    await worker.stop()
    try:
        second = await worker.start()
        assert second["pid"] != first
        assert second["process_running"]
        assert second["last_exit"]["pid"] == first
        assert len(spawn.calls) == 2
    finally:
        await worker.stop()


def test_progress_missing_partial_and_valid_fixture_are_distinct(settings):
    worker = service(settings, FakeSpawn())
    assert worker.latest_progress() is None
    worker.work_dir.mkdir(parents=True)
    path = worker.work_dir / "latest-cycle.json"
    path.write_text('{"fixture":')
    assert worker.latest_progress() is None
    fixture = {"fixture": "synthetic report", "counts": {"requested": 500, "scanned": 0}}
    path.write_text(json.dumps(fixture))
    assert worker.latest_progress() == fixture


@pytest.mark.parametrize("kwargs", [
    {"restart_initial_seconds": 0},
    {"restart_max_seconds": float("inf")},
    {"restart_initial_seconds": 2, "restart_max_seconds": 1},
    {"stable_reset_seconds": -1},
    {"shutdown_timeout_seconds": 0},
])
def test_invalid_supervision_configuration_is_rejected(settings, kwargs):
    with pytest.raises(ValueError):
        DiscoveryService(settings, **kwargs)
