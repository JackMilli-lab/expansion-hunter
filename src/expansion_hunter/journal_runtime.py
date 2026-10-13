"""Separate outcome worker; paper research never delays candle confirmation."""

from __future__ import annotations

import asyncio

from .models import utcnow
from .paper_journal import PaperJournal


async def _complete_thread_call(function, *args):
    """Cancellation must finish SQLite access before lifespan closes its store."""
    pending = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        await pending
        raise


async def run_journal_worker(app, *, interval_seconds: float = 30):
    if interval_seconds <= 0:
        raise ValueError("journal interval must be positive")
    try:
        service = await _complete_thread_call(PaperJournal, app.state.store)
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                result = await _complete_thread_call(service.evaluate, utcnow())
                app.state.paper_journal_health = {"status": "AVAILABLE", "updated_at": utcnow().isoformat(),
                                                 "mode": "PAPER_ONLY", "results": result}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                app.state.paper_journal_health = {"status": "FAILED", "updated_at": utcnow().isoformat(),
                                                 "mode": "PAPER_ONLY", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        app.state.paper_journal_health = {"status": "STOPPED", "mode": "PAPER_ONLY"}
