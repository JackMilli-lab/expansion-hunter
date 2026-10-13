"""Check the benchmark's causal fixtures and real decoder, without network."""

from __future__ import annotations

import runpy
from datetime import timedelta
from pathlib import Path

from expansion_hunter.config import Settings
from expansion_hunter.fast_models import FastStage
from expansion_hunter.live_runtime import LiveObservationWorker
from expansion_hunter.models import Asset

BENCHMARK = runpy.run_path(str(Path(__file__).parents[1] / "benchmarks/live_worker_load.py"))


def test_load_fixture_has_known_pressure_resistance_before_breakout(tmp_path):
    start = BENCHMARK["START"]
    current = [start]
    worker = LiveObservationWorker(Settings(db_path=str(tmp_path / "fixture.sqlite3")), clock=lambda: current[0])
    worker.assets = {"TEST0000USDT": Asset(asset_id="synthetic:0", symbol="TEST0000",
                                        name="SYNTHETIC ONLY", exchange_symbol="TEST0000USDT")}
    try:
        BENCHMARK["seed_fixture"](worker, 240)
        state = worker.monitor.states["synthetic:0"]
        assert state.stage == FastStage.BREAKOUT_PRESSURE
        assert state.resistance.price == 100
        assert state.resistance.discovered_at <= start
        event = BENCHMARK["native_fixture"]("TEST0000USDT", "CANDLE", start + timedelta(minutes=1), 1,
                                            breakout=True)
        current[0] = event.processed_timestamp
        worker.apply(event)
        assert worker.monitor.states["synthetic:0"].stage == FastStage.BREAKOUT_CONFIRMED
        assert worker.monitor.states["synthetic:0"].resistance.price == 100
        assert worker.state.snapshot("TEST0000USDT", current[0])["closed_candles"]["1m"][-1]["source"].startswith("TEST_ONLY_SYNTHETIC")
    finally:
        worker.store.close()


def test_protocol_fixture_never_masquerades_as_a_real_source_or_completed_open_candle():
    start = BENCHMARK["START"]
    for kind in ("TRADE", "BOOK_TICKER", "DEPTH", "CANDLE"):
        event = BENCHMARK["native_fixture"]("TEST0000USDT", kind, start + timedelta(seconds=20), 1)
        assert event.source.startswith("TEST_ONLY_SYNTHETIC")
        assert event.received_timestamp <= event.processed_timestamp
        if kind == "CANDLE":
            assert event.candle.complete is False
            assert event.candle.close_time > event.received_timestamp


def test_percentiles_interpolate_and_missing_samples_remain_unknown():
    assert BENCHMARK["percentile"]([], .5) is None
    assert BENCHMARK["percentile"]([0, 100], .95) == 95
