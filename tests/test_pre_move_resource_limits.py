"""Deep-watch retention follows the active candidate bound without spawning processes."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from expansion_hunter import pre_move_runtime


async def test_rotating_candidates_evict_cached_analysis_and_recalculate_on_return(monkeypatch):
    now = datetime(2026, 10, 13, tzinfo=UTC)
    selections = iter(("A", "B", "A", None))
    calculated = []
    retained = []
    persisted = []

    def snapshot():
        symbol = next(selections)
        rows = [] if symbol is None else [{"symbol": symbol, "asset_id": symbol, "bias": "LONG",
            "state": "WATCH", "status": "AVAILABLE", "score": 70, "as_of": now.isoformat()}]
        return {"pre_move": rows}

    def calculate(symbol, *args):
        calculated.append(symbol)
        return {"symbol": symbol, "war_room": {}}

    def persist(*args):
        persisted.append(args)

    app = SimpleNamespace(state=SimpleNamespace(
        trade_alert_worker=SimpleNamespace(snapshot=snapshot, assets={"A": SimpleNamespace(asset_id="A"),
            "B": SimpleNamespace(asset_id="B")}, _state=lambda symbol: None, _episode_state={}),
        store=SimpleNamespace(put_state=persist, append=persist)))

    async def refresh_pause(seconds):
        retained.append(set(app.state.pre_move_analysis))
        if len(retained) == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(pre_move_runtime, "ProcessPoolExecutor",
        lambda **kwargs: ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(pre_move_runtime, "chart_snapshot",
        lambda *args: SimpleNamespace(candles={"1m": [SimpleNamespace(close_time=now)]}))
    monkeypatch.setattr(pre_move_runtime, "_calculate_analysis", calculate)
    monkeypatch.setattr(pre_move_runtime.asyncio, "sleep", refresh_pause)

    with pytest.raises(asyncio.CancelledError):
        await pre_move_runtime.run_pre_move_deep_worker(app,
            SimpleNamespace(deep_watch_limit=1, deep_refresh_seconds=60))

    assert calculated == ["A", "B", "A"]
    assert retained == [{"A"}, {"B"}, {"A"}, set()]
    assert len(persisted) == 6
    assert app.state.pre_move_deep_health["status"] == "STOPPED"


async def test_zero_deep_limit_starts_no_executor(monkeypatch):
    def forbidden_executor(*args, **kwargs):
        pytest.fail("disabled deep analysis must not allocate a CPU process")

    monkeypatch.setattr(pre_move_runtime, "ProcessPoolExecutor", forbidden_executor)
    app = SimpleNamespace(state=SimpleNamespace())
    await pre_move_runtime.run_pre_move_deep_worker(app, SimpleNamespace(deep_watch_limit=0))
    assert app.state.pre_move_analysis == {}
    assert app.state.pre_move_deep_health == {"status": "DISABLED", "configured_limit": 0}
