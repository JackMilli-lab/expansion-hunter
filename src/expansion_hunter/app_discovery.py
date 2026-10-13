"""Supervise the public-data broad scanner outside the 1m hot path.

No personal watchlist is required. Unexpected exits remain visible and restart
with bounded backoff. The child performs market work; this manager only writes
configuration, waits for its process, and records operational lifecycle evidence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import sys
from datetime import timedelta
from pathlib import Path

from .config import Settings
from .discovery_config import DiscoverySettings
from .intelligence_store import IntelligenceStore
from .models import utcnow

_LOG = logging.getLogger(__name__)


class DiscoveryService:
    def __init__(
        self,
        settings: Settings,
        *,
        spawn=asyncio.create_subprocess_exec,
        restart_initial_seconds: float = 1,
        restart_max_seconds: float = 60,
        stable_reset_seconds: float = 30,
        shutdown_timeout_seconds: float = 10,
    ):
        for name, value in (
            ("restart_initial_seconds", restart_initial_seconds),
            ("restart_max_seconds", restart_max_seconds),
            ("shutdown_timeout_seconds", shutdown_timeout_seconds),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if restart_max_seconds < restart_initial_seconds:
            raise ValueError("restart_max_seconds must cover the initial delay")
        if not math.isfinite(stable_reset_seconds) or stable_reset_seconds < 0:
            raise ValueError("stable_reset_seconds must be finite and nonnegative")
        self.settings = settings
        self.spawn = spawn
        self.process = None
        self.started_at = None
        self.last_error = None
        self.last_exit = None
        self.restart_count = 0
        self.next_retry_at = None
        self.retry_delay_seconds = None
        self.persistence_error = None
        self.restart_initial_seconds = restart_initial_seconds
        self.restart_max_seconds = restart_max_seconds
        self.stable_reset_seconds = stable_reset_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self._log = None
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._desired_running = False
        self._manager_task = None
        self._process_started_monotonic = None
        self.database_path = str(Path(settings.db_path).resolve())
        self.work_dir = Path(self.database_path).parent / "app-runtime"

    async def start(self):
        async with self._lock:
            if self._desired_running and self._manager_task and not self._manager_task.done():
                return self.status()
            self._desired_running = True
            self._stop_event.clear()
            initial_error = None
            try:
                await self._spawn_once()
            except Exception as exc:
                initial_error = exc
                self.last_error = f"scanner start failed: {exc}"
                self._persist("START_FAILED")
            # Start supervision even if the first attempt failed. The API can
            # expose its initial 503 while recovery and Stop remain available.
            self._manager_task = asyncio.create_task(self._supervise(), name="broad-discovery-supervisor")
            self._persist()
            if initial_error is not None:
                raise initial_error
            return self.status()

    def _close_log(self):
        if self._log is not None:
            self._log.close()
            self._log = None

    async def _spawn_once(self):
        self._close_log()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        settings = self.settings.model_copy(update={"db_path": self.database_path})
        (self.work_dir / "settings.json").write_text(settings.model_dump_json(indent=2))
        config_path = Path("config/discovery.json")
        discovery = (
            DiscoverySettings.model_validate_json(config_path.read_text())
            if config_path.exists()
            else DiscoverySettings()
        )
        # Respect configured coverage, preserving all existing deep-scan budgets.
        discovery = discovery.model_copy(update={"universe_size": settings.top_n})
        discovery_path = self.work_dir / "discovery.json"
        discovery_path.write_text(discovery.model_dump_json(indent=2))
        log_path = self.work_dir / "discovery.log"
        # Rotate between attempts, not market evidence or an open child handle.
        if log_path.exists() and log_path.stat().st_size > 5_000_000:
            log_path.replace(self.work_dir / "discovery.previous.log")
        self._log = log_path.open("a")
        self.process = None
        try:
            self.process = await self.spawn(
                sys.executable, "-u", "-m", "expansion_hunter.cli",
                "--config", str(self.work_dir / "settings.json"), "discover-watch",
                "--discovery-config", str(discovery_path),
                "--output", str(self.work_dir / "latest-cycle.json"),
                stdout=self._log, stderr=asyncio.subprocess.STDOUT,
            )
        except BaseException as exc:
            # Cancellation during spawn must also release this attempt's handle.
            self._close_log()
            if isinstance(exc, Exception):
                self.last_error = f"scanner spawn failed: {exc}"
                self._persist("SPAWN_FAILED")
            raise
        self.started_at = utcnow().isoformat()
        self._process_started_monotonic = asyncio.get_running_loop().time()
        self.last_error = None
        self.next_retry_at = None
        self.retry_delay_seconds = None
        self._persist("STARTED")

    def status(self):
        process_running = bool(self.process and self.process.returncode is None)
        supervision_running = bool(
            self._desired_running and self._manager_task and not self._manager_task.done()
        )
        unexpected_exit = bool(
            self._desired_running and self.process and self.process.returncode is not None
        )
        state = (
            "DISCOVERING" if process_running
            else "FAILED" if self._desired_running and (self.last_error or unexpected_exit)
            else "STOPPED"
        )
        return {
            "status": state,
            # Running supervision does not claim a connected/fresh market feed.
            # It keeps the UI's Stop action available during backoff.
            "running": process_running or supervision_running,
            "process_running": process_running,
            "supervision_running": supervision_running,
            "pid": self.process.pid if self.process else None,
            "configured_assets": self.settings.top_n,
            "configured_universe_size": self.settings.top_n,
            "database_path": self.database_path,
            "started_at": self.started_at,
            "returncode": self.process.returncode if self.process else None,
            "error": self.last_error,
            "last_exit": self.last_exit,
            "restart_count": self.restart_count,
            "next_retry_at": self.next_retry_at,
            "retry_delay_seconds": self.retry_delay_seconds,
            "persistence_error": self.persistence_error,
            "log_path": str(self.work_dir / "discovery.log"),
            "personal_watchlist_required": False,
            "fast_path_isolation": "SEPARATE_PROCESS",
            "live_trading": False,
        }

    def _persist(self, event_kind=None):
        store = None
        try:
            store = IntelligenceStore(self.database_path)
            now = utcnow()
            snapshot = self.status()
            store.put_state("discovery_runtime", "app", now, snapshot)
            if event_kind:
                store.append(
                    "discovery_process_event", f"{now.isoformat()}:{event_kind}", now, now,
                    {"event": event_kind, "observed_at": now.isoformat(), **snapshot},
                )
            self.persistence_error = None
        except Exception as exc:
            # A temporarily locked/unavailable DB must not silently kill recovery.
            self.persistence_error = str(exc)
            _LOG.exception("Could not persist broad scanner lifecycle")
        finally:
            if store is not None:
                store.close()

    def _record_exit(self, returncode: int, *, expected: bool, forced: bool = False):
        self.last_exit = {
            "observed_at": utcnow().isoformat(),
            "pid": self.process.pid if self.process else None,
            "returncode": returncode,
            "expected": expected,
            "forced": forced,
        }
        if not expected:
            # Even exit 0 is unexpected for a continuously requested worker.
            self.last_error = f"scanner exited unexpectedly with returncode {returncode}"
        self._close_log()
        self._persist("STOPPED" if expected else "UNEXPECTED_EXIT")

    async def _wait_backoff(self, delay):
        self.retry_delay_seconds = delay
        self.next_retry_at = (utcnow() + timedelta(seconds=delay)).isoformat()
        self._persist()
        try:
            await asyncio.wait_for(self._stop_event.wait(), delay)
            return False
        except TimeoutError:
            return self._desired_running

    async def _supervise(self):
        delay = self.restart_initial_seconds
        while self._desired_running:
            process = self.process
            if process is not None:
                try:
                    returncode = await process.wait()
                except Exception as exc:
                    self.last_error = f"scanner wait failed: {exc}"
                    self._persist("WAIT_FAILED")
                    # Do not spawn a second child while its exit is uncertain.
                    if not await self._wait_backoff(delay):
                        return
                    delay = min(self.restart_max_seconds, delay * 2)
                    continue
                if not self._desired_running:
                    return
                if (
                    self._process_started_monotonic is not None
                    and asyncio.get_running_loop().time() - self._process_started_monotonic
                    >= self.stable_reset_seconds
                ):
                    delay = self.restart_initial_seconds
                self._record_exit(returncode, expected=False)
                self.process = None
            if not await self._wait_backoff(delay):
                return
            async with self._lock:
                if not self._desired_running:
                    return
                self.restart_count += 1
                try:
                    await self._spawn_once()
                except Exception as exc:
                    self.last_error = f"scanner restart failed: {exc}"
                    self._persist("RESTART_FAILED")
            delay = min(self.restart_max_seconds, delay * 2)

    async def stop(self):
        async with self._lock:
            self._desired_running = False
            self._stop_event.set()
            task = self._manager_task
            self._manager_task = None
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.next_retry_at = None
            self.retry_delay_seconds = None
            if self.process and self.process.returncode is None:
                forced = False
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.process.wait(), self.shutdown_timeout_seconds)
                except TimeoutError:
                    forced = True
                    try:
                        self.process.kill()
                    except ProcessLookupError:
                        pass
                    await self.process.wait()
                self._record_exit(self.process.returncode, expected=True, forced=forced)
            self._close_log()
            self._persist()
            return self.status()

    def latest_progress(self):
        path = self.work_dir / "latest-cycle.json"
        try:
            return json.loads(path.read_text()) if path.exists() else None
        except (OSError, json.JSONDecodeError):
            return None
