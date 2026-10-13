"""CPU isolation and exact arithmetic contracts; no network or trading."""

from __future__ import annotations

import asyncio
import math
import os
import pickle
import random
import statistics
import sys
import time
from concurrent.futures.process import BrokenProcessPool
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest

from expansion_hunter import fast, war_room
from expansion_hunter._numeric import float_mean
from expansion_hunter.config import Settings
from expansion_hunter.live_runtime import LiveObservationWorker, _calculate_analysis
from expansion_hunter.models import TIMEFRAMES, Asset, AssetSnapshot, Candle

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
ASSET = Asset(asset_id="test", symbol="TEST", name="Synthetic", exchange_symbol="TESTUSDT")


def candles(count=80, frame="1m"):
    seconds = TIMEFRAMES[frame]
    result = []
    for index in range(count):
        close_at = NOW - timedelta(seconds=(count - index - 1) * seconds)
        price = 100 + math.sin(index * 0.9) * 3
        result.append(Candle(
            open_time=close_at - timedelta(seconds=seconds), close_time=close_at,
            available_at=close_at, open=price, high=price + 0.4, low=price - 0.4,
            close=price, volume=100 + index % 7, source="synthetic_test_only",
        ))
    return result


def analysis_arguments():
    frames = {frame: candles(60, frame) for frame in TIMEFRAMES if frame in war_room.FRAMES}
    future = frames["1m"][-1].model_copy(update={
        "open_time": NOW, "close_time": NOW + timedelta(minutes=1),
        "available_at": NOW + timedelta(minutes=1),
    })
    frames["1m"].append(future)
    snapshot = AssetSnapshot(asset=ASSET, as_of=NOW, candles=frames)
    future_context = {"as_of": (NOW + timedelta(seconds=1)).isoformat(),
                      "cycle": {"regime": "FUTURE_MUST_NOT_APPEAR"}}
    return ("TESTUSDT", snapshot, None, None, None,
            {frame: ["synthetic_test_only"] for frame in frames}, NOW,
            None, None, future_context, "UNAVAILABLE")


@pytest.mark.parametrize("count", range(2, 21))
def test_float_mean_preserves_exact_rounding_for_ordinary_tied_and_extreme_windows(count):
    rng = random.Random(count)
    cases = [[9.1, 4.3, 8.1] * count, [0.75] * count, [1.5] * count,
             [math.nextafter(0.75, 0.0)] * count,
             [math.nextafter(0.75, math.inf)] * count,
             [sys.float_info.max] * count,
             [math.ulp(0.0), -math.ulp(0.0)] * count,
             [1e308, 1.0, -1e308], [sys.float_info.max, sys.float_info.max, -sys.float_info.max],
             [sys.float_info.max, math.ulp(0.0)], [-0.0] * count]
    for _ in range(100):
        cases.append([math.ldexp(rng.uniform(-1, 1), rng.randint(-1000, 1000))
                      for _ in range(count)])
        cases.append([rng.uniform(-100, 100) for _ in range(count)])
    for values in cases:
        assert float_mean(iter(values)).hex() == statistics.mean(values).hex()
    # This diagnostic proves why an unguarded fmean replacement is unsafe.
    assert statistics.fmean([9.1, 4.3, 8.1]) != float_mean([9.1, 4.3, 8.1])


@pytest.mark.parametrize("values", [
    [1, 2, 3], [2 ** 100, 2 ** 100 + 1], [1.0, 2],
    [Decimal("0.1"), Decimal("0.2")], [Fraction(1, 3), Fraction(2, 3)],
    [math.inf, 1.0], [-math.inf, 1.0], [math.inf, -math.inf], [math.nan, 1.0],
])
def test_float_mean_generic_and_nonfinite_inputs_keep_statistics_contract(values):
    expected, actual = statistics.mean(values), float_mean(iter(values))
    assert type(actual) is type(expected)
    if isinstance(expected, float) and math.isnan(expected):
        assert math.isnan(actual)
    else:
        assert actual == expected


def test_float_mean_empty_error_is_unchanged():
    with pytest.raises(statistics.StatisticsError, match="at least one data point"):
        float_mean(iter(()))


def legacy_atr(bars, period):
    if len(bars) <= period:
        return None
    values = [max(bar.high - bar.low, abs(bar.high - prior.close), abs(bar.low - prior.close))
              for prior, bar in zip(bars[:-1], bars[1:], strict=True)]
    value = statistics.mean(values[-period:])
    return value if value > 0 else None


@pytest.mark.parametrize("period", [5, 14, 20, 50])
def test_atr_window_keeps_predecessor_gap_and_original_rounding(period):
    source = candles(240)
    source[-period - 1] = source[-period - 1].model_copy(update={"close": 1000.0})
    assert fast._atr(source, period) == legacy_atr(source, period)
    assert fast._atr(source[:period], period) is None
    flat = [bar.model_copy(update={"high": 100.0, "low": 100.0, "close": 100.0}) for bar in source]
    assert fast._atr(flat, period) is None


def test_war_room_and_fast_metrics_match_legacy_arithmetic_exactly(monkeypatch):
    source = candles(240)
    actual_atr = fast._atr(source, 14)
    actual_fast = fast._metrics(source, 104.0, actual_atr)
    actual_war_room = war_room._metrics(source)
    monkeypatch.setattr(fast, "mean", statistics.mean)
    monkeypatch.setattr(fast, "_atr", legacy_atr)
    monkeypatch.setattr(war_room, "mean", statistics.mean)
    monkeypatch.setattr(war_room, "_atr", legacy_atr)
    assert actual_fast == fast._metrics(source, 104.0, actual_atr)
    assert actual_war_room == war_room._metrics(source)


def test_genuine_spawn_analysis_matches_direct_calculation_and_point_in_time(tmp_path):
    worker = LiveObservationWorker(Settings(db_path=str(tmp_path / "spawn.sqlite3")))
    args = analysis_arguments()
    expected = _calculate_analysis(*args)
    worker._start_analysis()
    executor = worker._analysis_executor
    try:
        assert executor._mp_context.get_start_method() == "spawn"
        assert executor._max_workers == worker.feed_settings.analysis_concurrency
        # Actual process serialization and imports, with no monkeypatched job.
        actual = executor.submit(_calculate_analysis, *pickle.loads(pickle.dumps(args))).result(timeout=30)
        assert actual == expected
        assert actual["market_regime"] == "UNAVAILABLE"
        assert actual["context_available_at"] is None
        assert actual["war_room"]["timeframes"]["1m"]["last_completed_candle"] == NOW.isoformat()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        worker._analysis_executor = None
        worker.store.close()


async def test_broken_spawn_worker_is_visible_and_never_falls_back(tmp_path, monkeypatch):
    worker = LiveObservationWorker(Settings(db_path=str(tmp_path / "broken.sqlite3")), clock=lambda: NOW)
    worker.assets = {"TESTUSDT": ASSET}
    worker._start_analysis()
    try:
        with pytest.raises(BrokenProcessPool):
            await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(
                worker._analysis_executor, os._exit, 17
            ), 30)

        def forbidden_fallback(*args, **kwargs):
            pytest.fail("A broken process must not run CPU analysis in the ingestion process")

        monkeypatch.setattr("expansion_hunter.live_runtime.analyze_live", forbidden_fallback)
        await worker._refresh_analysis("TESTUSDT")
        metrics = worker.metrics()["analysis_worker"]
        assert metrics["backend"] == "PROCESS_SPAWN"
        assert metrics["status"] == "FAILED" and metrics["failures"] == 1
        assert "BrokenProcessPool" in metrics["errors"]["TESTUSDT"]
        assert worker._analysis == {}
        error = worker.store.get_state("provider_error", "TESTUSDT:analysis")
        assert error["status"] == "FAILED" and "BrokenProcessPool" in error["error"]
    finally:
        await worker._finish_analysis()
        worker.store.close()


async def test_analysis_shutdown_joins_process_before_database_close(tmp_path):
    worker = LiveObservationWorker(Settings(db_path=str(tmp_path / "shutdown.sqlite3")))
    worker._start_analysis()
    executor = worker._analysis_executor
    future = executor.submit(time.sleep, 0.2)
    try:
        await worker._finish_analysis()
        assert future.done()
        assert future.result() is None
        assert executor._processes is None
        assert worker._analysis_executor is None
        # Shutdown completes while the parent still owns its usable connection.
        worker.store.put_state("test_shutdown", "one", NOW, {"joined": True})
        assert worker.store.get_state("test_shutdown", "one") == {"joined": True}
    finally:
        worker.store.close()
