"""Bounded synthetic stress contracts, not exchange latency/coverage claims."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import pytest
from test_live_runtime import (
    ASSET,
    NOW,
    await_task,
    native_candle,
    native_trade,
    seed_chart_history,
    seed_pressure,
)
from test_live_runtime import (
    worker as runtime_worker,
)

from expansion_hunter.config import Settings
from expansion_hunter.discovery_models import (
    DiscoveryCycle,
    DiscoveryObservation,
    DiscoveryStage,
    RankHistory,
)
from expansion_hunter.fast_models import FastStage
from expansion_hunter.live_feed import LiveFeedState
from expansion_hunter.live_models import LiveFeedSettings
from expansion_hunter.live_repair import detect_repair_frames
from expansion_hunter.live_runtime import LiveObservationWorker
from expansion_hunter.models import Asset


def membership_cycle(worker, identity, rows):
    """Explicit synthetic eligibility/rank publication, never live evidence."""
    when = worker.clock()
    worker.store.save_cycle(DiscoveryCycle(
        cycle_id=identity, started_at=when, as_of=when, completed_at=when,
        database_path=worker.store.database_path, config_hash="TEST_ONLY",
        observations=[DiscoveryObservation(asset=asset, as_of=when, eligible=eligible,
            stage=stage, disposition="PARTIAL" if eligible else "EXCLUDED",
            rank=RankHistory(current_rank=rank))
            for asset, eligible, stage, rank in rows],
    ))


def test_pressure_and_confirmed_setup_survive_only_broad_rank_demotion(worker):
    seed_pressure(worker)
    marker = worker.store.get_state("fast_retention", "BTCUSDT")
    assert marker["stage"] == "BREAKOUT_PRESSURE"
    membership_cycle(worker, "rank-demoted", [(ASSET, True, DiscoveryStage.DORMANT, 500)])
    assert list(worker.membership()) == ["BTCUSDT"]
    assert worker.metrics()["active_setup_retention"]["retained_symbols"] == ["BTCUSDT"]
    event = native_candle(NOW + timedelta(minutes=1))
    worker.test_clock[0] = event.processed_timestamp
    worker.apply(event)
    membership_cycle(worker, "still-low-ranked", [(ASSET, True, DiscoveryStage.DORMANT, 500)])
    assert worker.monitor.states[ASSET.asset_id].stage == FastStage.BREAKOUT_CONFIRMED
    assert worker.membership()["BTCUSDT"].asset_id == ASSET.asset_id
    # Monitoring retention must not change broad rank or assign score bonuses.
    saved = worker.store.latest_summary().observations[0]
    assert saved.rank.current_rank == 500 and saved.scores == {}


def test_retention_is_bounded_and_not_refreshed_by_identical_ticks(worker):
    seed_pressure(worker)
    membership_cycle(worker, "dormant", [(ASSET, True, DiscoveryStage.DORMANT, 500)])
    started = worker._retention["BTCUSDT"]["started_at"]
    event = native_trade(NOW + timedelta(seconds=20), price=99.9)
    worker.test_clock[0] = event.processed_timestamp
    worker.apply(event)
    assert worker._retention["BTCUSDT"]["started_at"] == started
    assert "BTCUSDT" in worker.membership()
    state = worker.monitor.states[ASSET.asset_id]
    worker.test_clock[0] = NOW + timedelta(seconds=worker.feed_settings.active_setup_retention_seconds)
    state.last_evaluation_at = worker.clock()  # Fresh evidence alone cannot reset material transition TTL.
    assert worker.membership() == {}


@pytest.mark.parametrize("failure", ["ineligible", "stale", "old_evaluation", "failed", "unavailable"])
def test_active_setup_retention_never_bypasses_safety(worker, failure):
    seed_pressure(worker)
    membership_cycle(worker, "dormant", [(ASSET, failure != "ineligible", DiscoveryStage.DORMANT, 500)])
    state = worker.monitor.states[ASSET.asset_id]
    if failure == "stale":
        state.stale = True
    elif failure == "old_evaluation":
        worker.test_clock[0] = NOW + timedelta(seconds=worker.fast_settings.candle_stale_seconds + 1)
    elif failure == "failed":
        state.stage = FastStage.FAILED_BREAKOUT
    elif failure == "unavailable":
        state.data_quality["spot_candles"] = "UNAVAILABLE"
    assert worker.membership() == {}


def test_retained_setup_has_priority_over_new_ranked_candidates_within_cap(worker):
    worker.feed_settings.max_symbols = 1
    worker.feed_settings.benchmark_symbols = []
    seed_pressure(worker)
    newcomer = Asset(asset_id="new", symbol="NEW", name="Synthetic newcomer", exchange_symbol="NEWUSDT")
    membership_cycle(worker, "new-top-rank", [(newcomer, True, DiscoveryStage.WATCH, 1),
                                               (ASSET, True, DiscoveryStage.DORMANT, 500)])
    assert list(worker._subscriptions(worker.membership())) == ["BTCUSDT"]
    assert worker.store.latest_summary().observations[0].rank.current_rank == 1


def test_setup_retention_restores_but_requires_fresh_post_restart_evaluation(worker):
    seed_pressure(worker)
    membership_cycle(worker, "dormant", [(ASSET, True, DiscoveryStage.DORMANT, 500)])
    restarted = LiveObservationWorker(worker.settings, clock=worker.clock)
    try:
        assert restarted._retention["BTCUSDT"] == worker._retention["BTCUSDT"]
        assert restarted.membership() == {}  # Restored closed candles are initially stale.
        state = restarted.monitor.states[ASSET.asset_id]
        state.stale = False
        state.data_quality["spot_candles"] = "AVAILABLE"
        state.last_evaluation_at = worker.clock()
        assert list(restarted.membership()) == ["BTCUSDT"]
    finally:
        restarted.store.close()


@pytest.mark.asyncio
async def test_native_gap_repair_restores_next_1m_trigger_without_retrospective_confirmation(worker, monkeypatch):
    """Native protocol fixtures exercise recovery, not external connectivity."""
    monkeypatch.setattr(worker, "_schedule_analysis", lambda symbol: None)
    monkeypatch.setattr(worker, "_schedule_repair", lambda symbol: None)
    seed_pressure(worker)
    # The repair adapter requires actual native instrument provenance. These
    # explicitly synthetic fixtures use the exact accepted provider contract.
    original = worker.state
    worker.state = LiveFeedState(worker.feed_settings)
    monitored = worker.monitor.states[ASSET.asset_id]
    for frame in ("1m", "5m"):
        bars = [bar.model_copy(update={"source": f"binance/spot/BTCUSDT/{frame}"})
                for bar in original.candles("BTCUSDT", frame, NOW)]
        worker.state.seed_candles("BTCUSDT", frame, bars, NOW)
        monitored.candles[frame] = [bar.model_copy() for bar in bars]
    lost_close = native_candle(NOW + timedelta(minutes=2))
    worker.test_clock[0] = lost_close.processed_timestamp
    worker.apply(lost_close)
    assert monitored.stale, "A real gap must continue suspending the fast trigger"
    worker.test_clock[0] = NOW + timedelta(minutes=2, seconds=10)
    plan = next(plan for plan in detect_repair_frames(ASSET, worker.state, monitored, worker.clock())
                if plan.timeframe == "1m")
    missing = native_candle(NOW + timedelta(minutes=1)).candle.as_closed(
        NOW + timedelta(minutes=1, milliseconds=250), "binance/spot/BTCUSDT/1m"
    )

    class NativeHistoryProvider:
        async def candles(self, asset, frame, limit, as_of):
            assert asset == ASSET and frame == "1m" and limit <= 120
            return [missing]

    worker._repair_provider = NativeHistoryProvider()
    monkeypatch.setattr(worker, "_schedule_analysis", lambda symbol: None)
    await worker._repair_candles("BTCUSDT", [plan])
    assert not monitored.stale, (worker.store.get_state("candle_repair", "BTCUSDT"),
                                 monitored.data_quality,
                                 [(bar.open_time, bar.close_time) for bar in monitored.candles["1m"]][-4:])
    assert monitored.stage == FastStage.BREAKOUT_PRESSURE
    assert worker.store.get_state("candle_repair", "BTCUSDT")["status"] == "AVAILABLE"
    assert worker.store.get_state("candle_repair", "BTCUSDT")["bars_added"] == 1
    assert not any(event["event_type"] == "1M_BREAKOUT_CONFIRMED"
                   for event in worker.store.objects("fast_event", worker.clock()))
    repaired = next(bar for bar in worker.state.candles("BTCUSDT", "1m")
                    if bar.close_time == NOW + timedelta(minutes=1))
    assert repaired.available_at == worker.clock()
    next_native = native_candle(NOW + timedelta(minutes=3))
    worker.test_clock[0] = next_native.processed_timestamp
    worker.apply(next_native)
    assert monitored.stage == FastStage.BREAKOUT_CONFIRMED
    assert monitored.confirmed_at == NOW + timedelta(minutes=3)


@pytest.mark.asyncio
async def test_candle_repair_failure_remains_visible_and_does_not_claim_fresh_data(worker, monkeypatch):
    seed_pressure(worker)
    worker.test_clock[0] = NOW + timedelta(minutes=2, seconds=10)
    plans = detect_repair_frames(ASSET, worker.state, worker.monitor.states[ASSET.asset_id], worker.clock())

    class FailedProvider:
        async def candles(self, *args):
            raise RuntimeError("explicit provider fixture failure")

    worker._repair_provider = FailedProvider()
    monkeypatch.setattr(worker, "_schedule_analysis", lambda symbol: None)
    await worker._repair_candles("BTCUSDT", plans)
    status = worker.store.get_state("candle_repair", "BTCUSDT")
    assert status["status"] == "DEGRADED" and status["issues"]
    assert status["bars_added"] == 0
    assert worker.monitor.states[ASSET.asset_id].stale


def test_tick_publication_reuses_history_and_open_candles_do_not_invalidate_it(worker, monkeypatch):
    seed_chart_history(worker, count=240)
    calls = []
    real = worker.state.candles

    def tracked(symbol, frame, as_of=None, **kwargs):
        calls.append(frame)
        return real(symbol, frame, as_of, **kwargs)

    monkeypatch.setattr(worker.state, "candles", tracked)
    worker.apply(native_trade(NOW, 100))
    cached = worker._history_cache["BTCUSDT"]["5m"][1]
    assert len(calls) == 6
    for index in range(2, 22):
        worker._last_publish.clear()
        worker.apply(native_trade(NOW + timedelta(milliseconds=index), 100.01, identifier=index))
    assert len(calls) == 6, "Ordinary ticks must not recopy completed histories"
    worker._last_publish.clear()
    worker.apply(native_candle(NOW + timedelta(minutes=1), complete=False,
                               received=NOW + timedelta(seconds=20)))
    assert worker._history_cache["BTCUSDT"]["5m"][1] is cached
    assert len(calls) == 6
    saved = worker.store.get_state("live_asset", "BTCUSDT")
    assert len(saved["closed_candles"]["5m"]) == 240
    assert "1m" in saved["open_candles"]


def test_closed_history_version_changes_only_after_accepted_finalized_bar(worker):
    seed_chart_history(worker, frames=("1m",))
    version = worker.state.history_version("BTCUSDT", "1m")
    open_event = native_candle(NOW + timedelta(minutes=1), complete=False,
                               received=NOW + timedelta(seconds=20))
    assert worker.state.apply(open_event)
    assert worker.state.history_version("BTCUSDT", "1m") == version
    closed = native_candle(NOW + timedelta(minutes=1))
    assert worker.state.apply(closed)
    assert worker.state.history_version("BTCUSDT", "1m") == version + 1
    assert not worker.state.apply(closed)
    assert worker.state.history_version("BTCUSDT", "1m") == version + 1


def test_candle_copy_isolated_and_point_in_time_limit_applied_after_filter(worker):
    seed_chart_history(worker, frames=("1m",))
    copies = worker.state.candles("BTCUSDT", "1m", NOW, limit=2)
    copies[0].source = "TEST_MUTATION"
    assert worker.state.candles("BTCUSDT", "1m", NOW, limit=2)[0].source != "TEST_MUTATION"
    assert worker.state.candles("BTCUSDT", "1m", NOW, limit=0) == []
    with pytest.raises(ValueError, match="negative"):
        worker.state.candles("BTCUSDT", "1m", limit=-1)
    worker.state.apply(native_candle(NOW + timedelta(minutes=1)))
    assert worker.state.candles("BTCUSDT", "1m", NOW, limit=1)[0].close_time == NOW


def test_seeded_final_history_is_not_repainted_by_caller_mutation(worker):
    seed_chart_history(worker, frames=("1m",))
    caller_bars = worker.state.candles("BTCUSDT", "1m", NOW)
    worker.state.seed_candles("ETHUSDT", "1m", caller_bars, NOW)
    caller_bars[-1].source = "TEST_MUTATION_AFTER_SEED"
    assert worker.state.candles("ETHUSDT", "1m", NOW)[-1].source != "TEST_MUTATION_AFTER_SEED"


def test_cached_chart_cannot_reveal_later_availability_at_earlier_decision(worker):
    seed_chart_history(worker, frames=("1m",))
    worker.state.apply(native_candle(NOW + timedelta(minutes=1)))
    worker._publish("BTCUSDT", NOW)
    known = worker.store.get_state("live_asset", "BTCUSDT")["closed_candles"]["1m"]
    assert datetime.fromisoformat(known[-1]["close_time"]) == NOW
    later = NOW + timedelta(minutes=1, seconds=1)
    worker._publish("BTCUSDT", later)
    known = worker.store.get_state("live_asset", "BTCUSDT")["closed_candles"]["1m"]
    assert datetime.fromisoformat(known[-1]["close_time"]) == NOW + timedelta(minutes=1)
    assert all(datetime.fromisoformat(row["available_at"]) <= later for row in known)


def test_empty_membership_remains_empty_and_benchmarks_are_context_subscriptions(tmp_path):
    instance = LiveObservationWorker(Settings(db_path=str(tmp_path / "empty.sqlite3")))
    try:
        assert instance.membership() == {}
        assert set(instance._subscriptions({})) == {"BTCUSDT", "ETHUSDT"}
        assert instance._context_symbols == {"BTCUSDT", "ETHUSDT"}
        assert instance.monitor.states == {}
    finally:
        instance.store.close()


def test_context_subscription_cannot_emit_promotions_or_breakout_alerts(worker):
    worker._context_symbols = {"BTCUSDT"}
    seed_pressure(worker)
    assert worker.monitor.states == {}
    worker.apply(native_trade(NOW + timedelta(seconds=10), 101))
    worker.apply(native_candle(NOW + timedelta(minutes=1)))
    assert worker.store.objects("fast_event", NOW + timedelta(hours=1)) == []
    assert worker.store.get_state("live_asset", "BTCUSDT")["context_only"]
    # Native market observations remain available for context/research.
    assert len(worker.store.objects("market_observation", NOW + timedelta(hours=1))) == 1


def test_benchmark_capacity_never_evicts_personal_priority(tmp_path):
    instance = LiveObservationWorker(
        Settings(db_path=str(tmp_path / "capacity.sqlite3")), LiveFeedSettings(max_symbols=2)
    )
    ripple = Asset(asset_id="ripple", symbol="XRP", name="Ripple", exchange_symbol="XRPUSDT")
    tail = Asset(asset_id="tail", symbol="TAIL", name="Synthetic tail", exchange_symbol="TAILUSDT")
    try:
        instance._personal_symbols = {"XRPUSDT"}
        selected = instance._subscriptions({"XRPUSDT": ripple, "TAILUSDT": tail})
        assert list(selected) == ["XRPUSDT", "BTCUSDT"]
        assert instance._capacity_exclusions == ["TAILUSDT"]
        instance.assets = selected
        assert instance.metrics()["benchmark_context"]["unavailable_due_capacity"] == ["ETHUSDT"]
    finally:
        instance.store.close()


def test_native_large_trade_flow_survives_duplicate_rejection_without_identity_claim(worker):
    event = native_trade(NOW, 100, quantity=1500)
    worker.apply(event)
    worker.apply(event)
    live = worker.store.get_state("live_asset", "BTCUSDT")
    flow = live["large_trade_flow"]
    assert flow["whale_identity"] == "UNAVAILABLE"
    assert flow["institutional_identity"] == "UNAVAILABLE"
    assert flow["large_trade_count"] == 1
    assert live["session_context"]["research_weight"] == 0
    assert worker.state.events_rejected == 1


def test_overview_summary_is_small_causal_and_keeps_nearest_both_sides(worker):
    seed_chart_history(worker, count=240)
    worker.apply(native_trade(NOW, 100))
    live = worker.store.get_state("live_asset", "BTCUSDT")
    summary = worker.store.get_state("live_asset_summary", "BTCUSDT")
    assert summary["price"] == live["price"]
    assert summary["source"] == "binance-public-ws"
    assert summary["quote_asset"] == ASSET.quote_asset
    assert datetime.fromisoformat(summary["price_source_timestamp"]) == NOW
    assert datetime.fromisoformat(summary["price_received_timestamp"]) == NOW + timedelta(milliseconds=25)
    assert summary["last_exchange_timestamp"] == live["last_exchange_timestamp"]
    assert summary["data_quality"] == live["data_quality"]
    assert "closed_candles" not in summary and "overlays" not in summary
    assert len(worker.store._json(summary)) < len(worker.store._json(live)) / 10
    example = {"price": 100, "liquidity_map": [
        {"price": 100 + index, "status": "RESTING", "quality": "NATIVE", "size": index,
         "large_internal_metadata": "unused" * 10000} for index in range(-20, 21) if index != 0
    ]}
    result = worker._live_summary(example)
    assert len(result["liquidity_map"]) == 8
    assert {level["price"] for level in result["liquidity_map"]} == {96, 97, 98, 99, 101, 102, 103, 104}
    assert all("large_internal_metadata" not in level for level in result["liquidity_map"])


async def stop_writer(worker):
    await await_task(asyncio.create_task(worker._finish_writer()), timeout=5)


async def test_blocked_database_writer_does_not_delay_1m_confirmation(worker, monkeypatch):
    seed_pressure(worker)
    entered = threading.Event()
    release = threading.Event()
    real = worker.store.put_state

    def slow_write(kind, *args):
        if kind == "fast_state":
            entered.set()
            if not release.wait(4):
                raise TimeoutError("Synthetic persistence barrier expired")
        return real(kind, *args)

    monkeypatch.setattr(worker.store, "put_state", slow_write)
    monkeypatch.setattr(worker, "_schedule_analysis", lambda symbol: None)
    worker._start_writer()
    try:
        worker.test_clock[0] = NOW + timedelta(minutes=1, seconds=1)
        worker.apply(native_candle(NOW + timedelta(minutes=1)))
        assert worker.monitor.states[ASSET.asset_id].stage == FastStage.BREAKOUT_CONFIRMED
        deadline = asyncio.get_running_loop().time() + 2
        while not entered.is_set():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        # Ordinary ingestion still progresses while the writer is genuinely
        # blocked on its own real thread, with no monkeypatched calculation.
        worker.apply(native_trade(NOW + timedelta(minutes=1, seconds=2), 100.2, identifier=2))
        assert worker.state.events_accepted == 2
        assert worker.metrics()["persistence"]["critical_pending"] >= 2
        assert worker.metrics()["includes_publish_preparation"] is True
    finally:
        release.set()
        await stop_writer(worker)
    events = worker.store.objects("fast_event", NOW + timedelta(hours=1))
    assert events[-1]["event_type"] == "1M_BREAKOUT_CONFIRMED"
    assert len(worker.store.objects("market_observation", NOW + timedelta(hours=1))) == 1
    assert worker.metrics()["persistence"]["critical_pending"] == 0


async def test_replaceable_chart_writes_coalesce_but_archival_jobs_remain_fifo(worker):
    worker._start_writer()
    for index in range(10):
        worker._put_state("synthetic_cache", "one", NOW + timedelta(seconds=index), {"index": index})
        worker._enqueue_write(worker.store.append, "synthetic_archive", str(index),
                              NOW, NOW + timedelta(seconds=index), {"index": index})
    assert len(worker._latest_writes) == 1
    assert len(worker._critical_writes) == 10
    await stop_writer(worker)
    assert worker.store.get_state("synthetic_cache", "one") == {"index": 9}
    assert [row["index"] for row in worker.store.objects("synthetic_archive", NOW + timedelta(hours=1))] == list(range(10))
    assert worker.metrics()["persistence"]["cache_updates_coalesced"] == 9


async def test_demotion_then_repromotion_persists_in_timestamp_order(worker):
    seed_pressure(worker)
    demoted = worker.monitor.states[ASSET.asset_id].model_copy(deep=True)
    demoted.stage = FastStage.DEMOTED
    promoted = demoted.model_copy(deep=True)
    promoted.stage = FastStage.WATCH
    worker._start_writer()
    worker._put_state("fast_state", ASSET.asset_id, NOW + timedelta(seconds=1), demoted)
    worker._enqueue_write(worker.store.put_state, "fast_state", ASSET.asset_id,
                          NOW + timedelta(seconds=2), promoted)
    await stop_writer(worker)
    assert worker.store.get_state("fast_state", ASSET.asset_id)["stage"] == "WATCH"
    assert worker.metrics()["persistence"]["failed"] is False


async def test_queued_fast_snapshot_cannot_be_repainted_by_next_candle(worker, monkeypatch):
    seed_pressure(worker)
    recorded_stages = []
    real = worker.store.put_state

    def recorded(kind, identity, at, payload):
        if kind == "fast_state":
            recorded_stages.append(payload.stage)
        return real(kind, identity, at, payload)

    monkeypatch.setattr(worker.store, "put_state", recorded)
    monkeypatch.setattr(worker, "_schedule_analysis", lambda symbol: None)
    worker._start_writer()
    worker.test_clock[0] = NOW + timedelta(minutes=1, seconds=1)
    worker.apply(native_candle(NOW + timedelta(minutes=1)))
    worker.test_clock[0] = NOW + timedelta(minutes=2, seconds=1)
    worker.apply(native_candle(NOW + timedelta(minutes=2), close=99.8, high=100.12, low=99.5))
    assert worker.monitor.states[ASSET.asset_id].stage == FastStage.FAILED_BREAKOUT
    await stop_writer(worker)
    assert recorded_stages == [FastStage.BREAKOUT_CONFIRMED, FastStage.FAILED_BREAKOUT]


async def test_sustained_buffered_stream_allows_other_tasks_to_progress_without_drops(worker, monkeypatch):
    total = 2048

    class BufferedStream:
        async def events(self, stop):
            # Deliberately no awaits: an already-filled native receive buffer
            # has the same scheduling semantics under sustained exchange load.
            for index in range(total):
                yield native_trade(NOW + timedelta(milliseconds=index), identifier=index + 1)

    worker.stream = BufferedStream()
    monkeypatch.setattr(worker, "_publish", lambda *args: None)
    progress = []

    async def health_task():
        while worker.state.events_accepted < total:
            progress.append(worker.state.events_accepted)
            await asyncio.sleep(0)

    observer = asyncio.create_task(health_task())
    await worker._consume_events()
    await observer
    assert any(0 < count < total for count in progress), "Other tasks must progress before buffer exhaustion"
    assert worker.state.events_accepted == total
    assert worker.state.events_rejected == 0
    assert worker.metrics()["cooperative_ingestion_yields"] > 0
    assert worker._latencies.maxlen == 10000


async def test_sqlite_busy_retry_is_visible_and_does_not_duplicate_archive(worker, monkeypatch):
    real = worker.store.append
    attempts = []

    def busy_once(*args):
        attempts.append(True)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args)

    worker._start_writer()
    monkeypatch.setattr(worker.store, "append", busy_once)
    worker._enqueue_write(worker.store.append, "synthetic_archive", "one", NOW, NOW, {"value": 1})
    await stop_writer(worker)
    assert worker.metrics()["persistence"]["busy_retries"] == 1
    assert worker.metrics()["persistence"]["last_error"] is None
    assert worker.store.objects("synthetic_archive", NOW) == [{"value": 1}]


async def test_critical_queue_saturation_stops_ingestion_without_silent_discard(worker):
    worker.feed_settings.persistence_queue_size = 8
    worker._start_writer()
    for index in range(8):
        worker._enqueue_write(worker.store.append, "synthetic_archive", str(index),
                              NOW, NOW, {"index": index})
    assert worker.stop.is_set()
    assert worker.metrics()["persistence"]["overloaded"] is True
    assert len(worker._critical_writes) == 8
    await stop_writer(worker)
    assert len(worker.store.objects("synthetic_archive", NOW)) == 8
    assert worker.metrics()["persistence"]["status"] == "DEGRADED"


async def test_permanent_writer_failure_is_visible_and_not_reported_durable(worker):
    def failed():
        raise ValueError("Synthetic immutable collision")

    worker._start_writer()
    worker._enqueue_write(failed)
    with pytest.raises(ValueError, match="immutable collision"):
        await stop_writer(worker)
    metrics = worker.metrics()["persistence"]
    assert metrics["failed"] is True and metrics["status"] == "FAILED"
    assert metrics["critical_pending"] == 1
    assert metrics["writes_completed"] == 0
    assert worker.stop.is_set()


async def test_analysis_tasks_globally_bounded_and_new_close_coalesces_latest_refresh(worker, monkeypatch):
    # Use real tasks and slot scheduling, replacing only the expensive offline
    # calculation with a deterministic thread barrier to prove concurrency.
    entered = threading.Event()
    release = threading.Event()
    active = 0
    peak = 0
    lock = threading.Lock()
    worker.assets["ETHUSDT"] = Asset(asset_id="ethereum", symbol="ETH", name="Ethereum", exchange_symbol="ETHUSDT")
    for symbol in worker.assets:
        worker._analysis_requested[symbol] = worker._history_revision(symbol)

    def bounded_calculation(*args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        entered.set()
        if not release.wait(4):
            raise TimeoutError("Synthetic analysis barrier expired")
        with lock:
            active -= 1
        return {"as_of": NOW.isoformat()}

    monkeypatch.setattr("expansion_hunter.live_runtime.analyze_live", bounded_calculation)
    monkeypatch.setattr("expansion_hunter.live_runtime.deep_scores", lambda *args, **kwargs: ({}, {}))
    monkeypatch.setattr("expansion_hunter.live_runtime.analyze_war_room", lambda symbol, *args: {"symbol": symbol, "timeframes": {}})
    worker._schedule_analysis("BTCUSDT")
    worker._schedule_analysis("ETHUSDT")
    tasks = list(worker._analysis_tasks.values())
    try:
        deadline = time.monotonic() + 2
        while not entered.is_set():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        assert peak == 1
        assert not worker._analysis_tasks["ETHUSDT"].done()
    finally:
        release.set()
        for task in tasks:
            await await_task(task)
    assert peak == 1 and active == 0
    assert set(worker._analysis) == {"BTCUSDT", "ETHUSDT"}


def test_settings_validate_explicit_runtime_budgets():
    with pytest.raises(ValueError):
        LiveFeedSettings(analysis_concurrency=0)
    with pytest.raises(ValueError):
        LiveFeedSettings(persistence_queue_size=1)
    with pytest.raises(ValueError):
        LiveFeedSettings(benchmark_symbols=["DOGEUSDT"])
    assert LiveFeedSettings(benchmark_symbols=[]).benchmark_symbols == []
    assert LiveFeedState().history_version("BTCUSDT", "1m") == 0
@pytest.fixture
def worker(tmp_path):
    yield from runtime_worker.__wrapped__(tmp_path)
