import asyncio
import threading

import pytest

from expansion_hunter.journal_runtime import _complete_thread_call


async def test_cancellation_waits_for_owned_database_work_before_shutdown():
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()

    def owned_database_operation():
        entered.set()
        release.wait(2)
        completed.set()

    task = asyncio.create_task(_complete_thread_call(owned_database_operation))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0.01)
    try:
        assert not task.done(), "Cancelled SQLite thread outlived its owner and could race database close"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()


async def test_owned_thread_errors_are_not_silently_replaced():
    def fail():
        raise ValueError("database unavailable")

    with pytest.raises(ValueError, match="database unavailable"):
        await _complete_thread_call(fail)
