"""Independent observation-only research workers with owned SQLite connections.

No factor fitting, threshold updates or trading occurs. Calendar delivery is a
separate async task; it never joins candle ingestion or breakout evaluation.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import timedelta

from .event_learning import EventLearningEngine
from .intelligence import IntelligenceWorker, import_macro
from .intelligence_store import IntelligenceStore
from .journal_runtime import _complete_thread_call
from .models import aware, utcnow
from .providers.macro import BLSCalendarProvider


def _interval(value: float, name: str, *, allow_zero=False) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < 0 or (not allow_zero and value == 0):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return float(value)


class BackgroundIntelligence:
    """One serialized background owner; immutable source observations stay intact."""

    def __init__(self, database_path: str, *, clock=utcnow):
        self.store = IntelligenceStore(database_path)
        self.clock = clock
        self.worker = IntelligenceWorker(self.store, clock=clock)
        self.learning = EventLearningEngine(self.store)

    def close(self):
        self.store.close()

    def run_once(self) -> dict:
        started = time.perf_counter()
        boundary = aware(self.clock())
        issues = []
        try:
            cycle_id = self.store.latest_cycle_id()
            progress = self.store.get_state("intelligence_progress", "worker")
            if cycle_id is None:
                cycle = {"status": "UNAVAILABLE", "reason": "No published discovery cycle"}
            elif progress and progress.get("cycle_id") == cycle_id:
                cycle = {"status": "UNCHANGED", "cycle_id": cycle_id}
            else:
                cycle = self.worker.process_latest()
        except Exception as exc:
            cycle = {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}
            issues.append("cycle/analogue processing failed")
        # A failed cycle label must not prevent existing events from maturing.
        try:
            outcomes = self.learning.evaluate(boundary)
        except Exception as exc:
            outcomes = {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}
            issues.append("outcome/miss processing failed")
        completed = max(boundary, aware(self.clock()))
        report = {"status": "FAILED" if len(issues) == 2 else "PARTIAL" if issues else "AVAILABLE",
                  "mode": "OBSERVATION_RESEARCH", "updated_at": completed.isoformat(),
                  "decision_boundary": boundary.isoformat(), "cycle": cycle, "outcomes": outcomes,
                  "processing_latency_ms": (time.perf_counter() - started) * 1000,
                  "database_path": self.store.database_path, "issues": issues,
                  "research_weight_updates": False, "live_trading": False}
        self.store.put_state("intelligence_runtime", "worker", completed, report)
        return report


async def run_intelligence_worker(app, *, interval_seconds: float = 60, clock=utcnow):
    """Run immediately, then serially on cadence; drain owned work before close."""
    interval = _interval(interval_seconds, "intelligence interval")
    owned = {}

    def create():
        # Populate before returning, so cancellation during initialization can
        # still close the connection after the shielded call has completed.
        owned["service"] = BackgroundIntelligence(app.state.settings.db_path, clock=clock)
        return owned["service"]

    app.state.intelligence_health = {"status": "STARTING", "mode": "OBSERVATION_RESEARCH"}
    try:
        while True:
            try:
                service = owned.get("service") or await _complete_thread_call(create)
                app.state.intelligence_health = await _complete_thread_call(service.run_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                app.state.intelligence_health = {"status": "FAILED", "mode": "OBSERVATION_RESEARCH",
                    "updated_at": aware(clock()).isoformat(), "error": f"{type(exc).__name__}: {exc}",
                    "live_trading": False}
            await asyncio.sleep(interval)
    finally:
        if owned.get("service"):
            await _complete_thread_call(owned["service"].close)
        app.state.intelligence_health = {**getattr(app.state, "intelligence_health", {}), "status": "STOPPED"}


async def run_macro_calendar_worker(app, *, interval_seconds: float = 43200,
                                    startup_delay_seconds: float = 15,
                                    provider_factory=BLSCalendarProvider, clock=utcnow):
    """Authoritative schedules only; fetch only while observation is requested.

The source supplies no consensus or numeric releases. Those remain unavailable.
Providers receive their own actual retrieval clock and never backdate imports.
"""
    interval = _interval(interval_seconds, "macro interval")
    delay = _interval(startup_delay_seconds, "macro startup delay", allow_zero=True)
    owned = {}

    def create_store():
        owned["store"] = IntelligenceStore(app.state.settings.db_path)
        return owned["store"]

    app.state.macro_calendar_health = {"status": "WAITING_FOR_OBSERVATION", "mode": "CALENDAR_ONLY"}
    try:
        if delay:
            await asyncio.sleep(delay)
        while True:
            if not getattr(app.state, "live_requested", False):
                app.state.macro_calendar_health = {"status": "WAITING_FOR_OBSERVATION", "mode": "CALENDAR_ONLY"}
                # A disabled calendar must notice a later scanner start quickly,
                # while an enabled provider remains on the conservative cadence.
                await asyncio.sleep(min(interval, 15))
                continue
            store = None
            try:
                store = owned.get("store") or await _complete_thread_call(create_store)
                start = aware(clock())
                async with provider_factory() as provider:
                    report = await provider.calendar(start, start + timedelta(days=7))
                stored = await _complete_thread_call(import_macro, store, report)
                health = {"status": report.calendar_quality.value, "mode": "CALENDAR_ONLY",
                          "provider": report.provider, "updated_at": report.retrieved_at.isoformat(),
                          "calendar_quality": report.calendar_quality.value,
                          "consensus_quality": report.consensus_quality.value,
                          "release_quality": report.release_quality.value,
                          "stored": stored, "issues": report.issues, "live_trading": False}
                await _complete_thread_call(store.put_state, "macro_runtime", "worker", report.retrieved_at, health)
                app.state.macro_calendar_health = health
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                when = aware(clock())
                health = {"status": "FAILED", "mode": "CALENDAR_ONLY", "updated_at": when.isoformat(),
                          "calendar_quality": "UNAVAILABLE", "consensus_quality": "UNAVAILABLE",
                          "release_quality": "UNAVAILABLE", "error": f"{type(exc).__name__}: {exc}",
                          "live_trading": False}
                app.state.macro_calendar_health = health
                if store is not None:
                    try:
                        await _complete_thread_call(store.put_state, "macro_runtime", "worker", when, health)
                    except asyncio.CancelledError:
                        raise
                    except Exception as archive_error:
                        health["persistence_error"] = f"{type(archive_error).__name__}: {archive_error}"
            await asyncio.sleep(interval)
    finally:
        if owned.get("store"):
            await _complete_thread_call(owned["store"].close)
        app.state.macro_calendar_health = {**getattr(app.state, "macro_calendar_health", {}), "status": "STOPPED"}
