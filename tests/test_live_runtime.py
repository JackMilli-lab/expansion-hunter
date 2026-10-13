"""Native-format synthetic events exercise the actual worker and fast monitor.

These deterministic integration tests do not demonstrate external connectivity.
"""

from __future__ import annotations

import asyncio
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.config import Settings
from expansion_hunter.fast_models import FastSettings, FastStage
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.live_analysis import analyze_live
from expansion_hunter.live_models import FeedHealth, LiveFeedSettings
from expansion_hunter.live_runtime import LiveObservationWorker, chart_snapshot, fast_snapshot
from expansion_hunter.models import TIMEFRAMES, Asset, Candle
from expansion_hunter.providers.binance_ws import decode_message

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
ASSET = Asset(asset_id="unknown", symbol="UNKNOWN", name="Unknown asset", exchange_symbol="BTCUSDT")


def millis(when):
    return int(when.timestamp() * 1000)


def native_trade(when, price=100.08, identifier=1, quantity=2, maker=False):
    payload = {
        "stream": "btcusdt@aggTrade",
        "data": {
            "e": "aggTrade",
            "s": "BTCUSDT",
            "a": identifier,
            "p": str(price),
            "q": str(quantity),
            "m": maker,
            "T": millis(when),
            "E": millis(when),
        },
    }
    return decode_message(payload, when + timedelta(milliseconds=25), when + timedelta(milliseconds=27))


def native_candle(
    close_at,
    frame="1m",
    *,
    close=100.12,
    high=100.2,
    low=99.75,
    opened_price=99.9,
    volume=100,
    complete=True,
    received=None,
):
    open_at = close_at - timedelta(seconds=TIMEFRAMES[frame])
    received = received or close_at + timedelta(milliseconds=250)
    wire_frame = "1d" if frame == "1D" else frame
    data = {
        "e": "kline",
        "s": "BTCUSDT",
        "E": millis(received),
        "k": {
            "t": millis(open_at),
            "T": millis(close_at) - 1,
            "i": wire_frame,
            "o": str(opened_price),
            "h": str(high),
            "l": str(low),
            "c": str(close),
            "v": str(volume),
            "q": str(volume * close),
            "V": str(volume * 0.6),
            "Q": str(volume * close * 0.6),
            "n": 15,
            "x": complete,
        },
    }
    return decode_message(
        {"stream": f"btcusdt@kline_{wire_frame}", "data": data},
        received,
        received + timedelta(milliseconds=2),
    )


def native_depth(when, identifier=1, *, asks=None):
    data = {
        "lastUpdateId": identifier,
        "bids": [["98.50", "20"]],
        "asks": asks if asks is not None else [["101.00", "30"], ["102.50", "50"]],
    }
    return decode_message({"stream": "btcusdt@depth20@100ms", "data": data}, when, when)


def native_ticker(when):
    return decode_message(
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "s": "BTCUSDT",
                "u": 1,
                "b": "99.99",
                "B": "10",
                "a": "100.01",
                "A": "12",
            },
        },
        when,
        when,
    )


def history_bar(close_at, frame, *, close=99.9, high=100, low=99.5):
    return Candle(
        open_time=close_at - timedelta(seconds=TIMEFRAMES[frame]),
        close_time=close_at,
        available_at=close_at,
        open=close - 0.04,
        high=high,
        low=low,
        close=close,
        volume=100,
        source="binance-public-rest:TEST_ONLY",
    )


def seed_pressure(worker):
    one = [history_bar(NOW - timedelta(minutes=20 - i), "1m") for i in range(21)]
    five = []
    for i in range(36):
        close = 99.9 if i == 35 else 99.7
        high = 100 if i in (4, 12, 20, 28) else max(close + 0.02, 99.8)
        five.append(
            history_bar(
                NOW - timedelta(minutes=(35 - i) * 5), "5m", close=close, high=high, low=98.9 + i * 0.018
            )
        )
    worker.state.seed_candles("BTCUSDT", "1m", one, NOW)
    worker.state.seed_candles("BTCUSDT", "5m", five, NOW)
    worker._evaluate("BTCUSDT", NOW)


@pytest.fixture
def worker(tmp_path):
    current = [NOW]
    instance = LiveObservationWorker(
        Settings(db_path=str(tmp_path / "worker.sqlite3")),
        fast_settings=FastSettings(pressure_score=50, loaded_score=45, pre_ignition_score=40),
        clock=lambda: current[0],
    )
    instance.assets = {"BTCUSDT": ASSET}
    instance.test_clock = current
    try:
        yield instance
    finally:
        instance.store.close()


def apply(worker, event):
    worker.test_clock[0] = event.processed_timestamp
    worker._last_publish.clear()
    worker.apply(event)


def test_native_worker_emits_pressure_attempt_and_immediate_1m_confirmation(worker):
    seed_pressure(worker)
    assert worker.monitor.states[ASSET.asset_id].stage == FastStage.BREAKOUT_PRESSURE
    before = worker.store.objects("fast_event", NOW)
    assert {event["event_type"] for event in before} == {"PRE_IGNITION", "LOADED", "BREAKOUT_PRESSURE"}
    resistance = worker.monitor.states[ASSET.asset_id].resistance.price
    apply(worker, native_trade(NOW + timedelta(seconds=10), resistance + 0.08))
    assert worker.monitor.states[ASSET.asset_id].stage == FastStage.BREAKOUT_ATTEMPT
    assert worker.monitor.states[ASSET.asset_id].confirmed_at is None
    apply(worker, native_candle(NOW + timedelta(minutes=1), close=resistance + 0.12))
    state = worker.monitor.states[ASSET.asset_id]
    assert state.stage == FastStage.BREAKOUT_CONFIRMED
    assert state.resistance.price == resistance
    events = worker.store.objects("fast_event", worker.test_clock[0])
    assert [row["event_type"] for row in events][-2:] == ["BREAKOUT_ATTEMPT", "1M_BREAKOUT_CONFIRMED"]
    confirmed = events[-1]
    assert confirmed["detection_latency_ms"] == 252
    assert confirmed["processing_latency_ms"] == 2
    assert confirmed["data_quality"]["futures"] == "UNAVAILABLE"
    assert (
        worker.store.objects("research_event", worker.test_clock[0])[-1]["event_id"] == confirmed["event_id"]
    )
    assert worker.store.objects("market_observation", worker.test_clock[0])[-1]["resolution_seconds"] == 60
    assert worker.store._db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 5


def test_worker_5m_and_15m_acceptance_follow_immediate_1m_signal(worker):
    seed_pressure(worker)
    resistance = worker.monitor.states[ASSET.asset_id].resistance.price
    apply(worker, native_candle(NOW + timedelta(minutes=1), close=resistance + 0.12))
    for minute in range(2, 6):
        apply(
            worker,
            native_candle(
                NOW + timedelta(minutes=minute),
                close=resistance + 0.2,
                low=100.05,
                high=100.3,
                opened_price=100.1,
            ),
        )
    apply(
        worker,
        native_candle(
            NOW + timedelta(minutes=5), "5m", close=resistance + 0.2, low=99.8, high=100.3, opened_price=99.9
        ),
    )
    assert worker.monitor.states[ASSET.asset_id].stage == FastStage.ACCEPTED
    for minute in range(6, 16):
        apply(
            worker,
            native_candle(
                NOW + timedelta(minutes=minute),
                close=resistance + 0.3,
                low=100.1,
                high=100.4,
                opened_price=100.2,
            ),
        )
    apply(
        worker,
        native_candle(
            NOW + timedelta(minutes=15),
            "15m",
            close=resistance + 0.3,
            low=99.8,
            high=100.4,
            opened_price=99.9,
        ),
    )
    events = worker.store.objects("fast_event", worker.test_clock[0])
    assert [row["event_type"] for row in events][-3:] == [
        "1M_BREAKOUT_CONFIRMED",
        "5M_ACCEPTED",
        "15M_EXPANSION_CONFIRMED",
    ]


def test_worker_native_wick_is_sweep_and_never_confirmed(worker):
    seed_pressure(worker)
    apply(worker, native_candle(NOW + timedelta(minutes=1), close=99.92, high=100.12))
    assert worker.monitor.states[ASSET.asset_id].stage == FastStage.LIQUIDITY_SWEEP
    events = worker.store.objects("fast_event", worker.test_clock[0])
    assert events[-1]["event_type"] == "LIQUIDITY_SWEEP"
    assert all(row["event_type"] != "1M_BREAKOUT_CONFIRMED" for row in events)


def test_worker_rejects_duplicate_native_close_and_preserves_event_archive(worker):
    seed_pressure(worker)
    item = native_candle(NOW + timedelta(minutes=1))
    apply(worker, item)
    count = len(worker.store.objects("fast_event", worker.test_clock[0]))
    apply(worker, item)
    assert len(worker.store.objects("fast_event", worker.test_clock[0])) == count
    assert worker.state.events_rejected == 1


def test_worker_exact_book_prices_and_native_order_flow_are_persisted(worker):
    apply(worker, native_trade(NOW, 100, quantity=3))
    apply(worker, native_depth(NOW + timedelta(seconds=1)))
    live = worker.store.states("live_asset")[0]
    assert {row["price"] for row in live["liquidity_map"]} == {98.5, 101, 102.5}
    assert all(row["category"] == "OBSERVED_ORDER_BOOK" for row in live["liquidity_map"])
    assert all(row["quality"] == "NATIVE" for row in live["liquidity_map"])
    assert next(level for level in live["liquidity_map"] if level["price"] == 101)["notional"] == 3030
    assert live["order_flow"]["cvd"] == 3
    assert live["order_flow"]["source"] == "NATIVE_OBSERVED_AGGREGATE_TRADES"
    assert live["order_flow"]["historical_cvd"] == "UNAVAILABLE"
    assert live["data_quality"]["futures"] == "UNAVAILABLE"


def test_missing_depth_produces_no_invented_liquidity_levels(worker):
    apply(worker, native_trade(NOW, 100))
    live = worker.store.states("live_asset")[0]
    assert live["liquidity_map"] == []
    assert live["data_quality"]["order_book"] == "UNAVAILABLE"


@pytest.mark.parametrize("frame", ["1m", "5m", "15m", "1h", "4h", "1D"])
def test_worker_chart_history_separates_native_open_and_closed_bars(worker, frame):
    duration = TIMEFRAMES[frame]
    open_at = datetime.fromtimestamp(int(NOW.timestamp()) // duration * duration, tz=UTC)
    close_at = open_at + timedelta(seconds=duration)
    open_event = native_candle(close_at, frame, complete=False, received=open_at + timedelta(seconds=20))
    apply(worker, open_event)
    live = worker.store.states("live_asset")[0]
    assert frame in live["open_candles"]
    assert live["closed_candles"].get(frame, []) == []
    apply(worker, native_candle(close_at, frame))
    live = worker.store.states("live_asset")[0]
    assert frame not in live["open_candles"]
    assert live["closed_candles"][frame][-1]["close"] == 100.12
    assert live["closed_candles"][frame][-1]["source"] == "binance-public-ws"


def test_worker_restore_is_stale_and_cannot_emit_attempt_until_fresh(worker):
    seed_pressure(worker)
    restored = LiveObservationWorker(worker.settings, clock=lambda: NOW + timedelta(seconds=10))
    try:
        restored.assets = {"BTCUSDT": ASSET}
        assert restored.monitor.states[ASSET.asset_id].stale
        old_count = len(restored.store.objects("fast_event", NOW + timedelta(hours=1)))
        restored.apply(native_trade(NOW + timedelta(seconds=10)))
        assert len(restored.store.objects("fast_event", NOW + timedelta(hours=1))) == old_count
    finally:
        restored.store.close()


def test_personal_watchlist_capacity_and_priority_are_real_membership(tmp_path):
    settings = Settings(db_path=str(tmp_path / "personal.sqlite3"))
    archive = IntelligenceStore(settings.db_path)
    archive.watchlist_add("ripple", "XRPUSDT", priority=99, pinned=True, at=NOW)
    archive.close()
    instance = LiveObservationWorker(settings, LiveFeedSettings(max_symbols=1))
    try:
        assert list(instance.membership()) == ["XRPUSDT"]
        assert instance.membership()["XRPUSDT"].asset_id == "ripple"
        assert instance.monitor.states == {}
        instance.symbols = ["ETHUSDT"]
        with pytest.raises(ValueError, match="capacity"):
            instance.membership()
    finally:
        instance.store.close()


def test_fast_snapshot_does_not_include_future_seeded_history(worker):
    past = history_bar(NOW, "1m")
    future = history_bar(NOW + timedelta(minutes=1), "1m", close=100.12, high=100.2)
    worker.state.seed_candles("BTCUSDT", "1m", [past, future], NOW + timedelta(minutes=1))
    snapshot = fast_snapshot(ASSET, worker.state, NOW)
    assert [bar.close_time for bar in snapshot.candles["1m"]] == [NOW]


def test_best_bid_ask_without_depth_is_never_reported_as_order_book(worker):
    seed_pressure(worker)
    apply(worker, native_ticker(NOW + timedelta(seconds=10)))
    result = worker.monitor.evaluate(ASSET, fast_snapshot(ASSET, worker.state, NOW + timedelta(seconds=10)))
    assert result.state.data_quality["order_book"] == "UNAVAILABLE"


def test_native_depth_is_available_only_while_fresh(worker):
    seed_pressure(worker)
    apply(worker, native_ticker(NOW + timedelta(seconds=10)))
    apply(worker, native_depth(NOW + timedelta(seconds=10)))
    fresh = worker.monitor.evaluate(ASSET, fast_snapshot(ASSET, worker.state, NOW + timedelta(seconds=10)))
    assert fresh.state.data_quality["order_book"] == "AVAILABLE"
    stale = worker.monitor.evaluate(ASSET, fast_snapshot(ASSET, worker.state, NOW + timedelta(seconds=30)))
    assert stale.state.data_quality["order_book"] in {"STALE", "UNAVAILABLE"}


async def test_worker_stream_failure_shuts_down_and_preserves_database(tmp_path, monkeypatch):
    class FailingStream:
        health = FeedHealth(last_error="synthetic public provider failure")

        async def events(self, stop):
            raise ConnectionError("synthetic public provider failure")
            yield  # pragma: no cover

    async def no_network(*args):
        await asyncio.sleep(0)

    settings = Settings(db_path=str(tmp_path / "failure.sqlite3"))
    instance = LiveObservationWorker(settings, symbols=["BTCUSDT"], stream=FailingStream(), clock=lambda: NOW)
    monkeypatch.setattr(instance, "warmup", no_network)
    monkeypatch.setattr(instance, "_housekeeping", no_network)
    with pytest.raises(ConnectionError, match="public provider failure"):
        await instance.run()
    assert instance.stop.is_set()
    reopened = IntelligenceStore(settings.db_path)
    try:
        assert reopened.states("feed_health")[0]["last_error"] == "synthetic public provider failure"
        assert reopened._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()


async def test_slow_warmup_coin_does_not_delay_other_coin_and_duplicate_is_suppressed(worker, monkeypatch):
    blocked = asyncio.Event()
    entered = asyncio.Event()
    ready = asyncio.Event()
    calls = []
    other = Asset(asset_id="other", symbol="ETH", name="Other synthetic coin", exchange_symbol="ETHUSDT")
    worker.assets["ETHUSDT"] = other

    class Spot:
        def __init__(self, *args):
            pass

        async def candles(self, asset, frame, limit, cutoff):
            calls.append((asset.exchange_symbol, frame))
            if asset.exchange_symbol == "BTCUSDT" and frame == "1m":
                entered.set()
                await blocked.wait()
            return []

    def evaluated(symbol, at):
        if symbol == "ETHUSDT":
            ready.set()

    monkeypatch.setattr("expansion_hunter.live_runtime.BinanceSpotProvider", Spot)
    monkeypatch.setattr(worker, "_evaluate", evaluated)
    monkeypatch.setattr(worker, "_schedule_analysis", lambda symbol: None)
    task = worker._start_warmup(None, ["BTCUSDT", "ETHUSDT"])
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(ready.wait(), 2)
        assert not task.done(), "Slow coin should remain pending while the other is ready"
        # A changed broad cohort may ask for an already warming symbol again.
        await worker.warmup(None, ["BTCUSDT"])
        assert calls.count(("BTCUSDT", "1m")) == 1
    finally:
        blocked.set()
        await task
    assert not worker._warming
    assert not worker._warmup_tasks


async def test_cancelled_background_warmup_releases_capacity(worker, monkeypatch):
    entered = asyncio.Event()

    class Spot:
        def __init__(self, *args):
            pass

        async def candles(self, *args):
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr("expansion_hunter.live_runtime.BinanceSpotProvider", Spot)
    task = worker._start_warmup(None, ["BTCUSDT"])
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not worker._warming
    assert worker._warmup_slots._value == worker.feed_settings.warmup_concurrency


def seed_chart_history(worker, count=120, frames=("1m", "5m", "15m", "1h", "4h", "1D")):
    """Synthetic prices, native adapter source contracts, actual indicator computation."""
    for frame in frames:
        seconds = TIMEFRAMES[frame]
        last = datetime.fromtimestamp(int(NOW.timestamp()) // seconds * seconds, tz=UTC)
        source = f"binance/spot/BTCUSDT/{frame}"
        bars = []
        for index in range(count):
            closed = last - timedelta(seconds=(count - index - 1) * seconds)
            price = 100 + 0.4 * math.sin(index * 0.3)
            volume = 100 + index % 7
            bars.append(
                Candle(
                    open_time=closed - timedelta(seconds=seconds),
                    close_time=closed,
                    available_at=closed,
                    open=price - 0.02,
                    high=price + 0.15,
                    low=price - 0.15,
                    close=price,
                    volume=volume,
                    quote_volume=volume * price,
                    taker_buy_volume=volume * 0.6,
                    source=source,
                )
            )
        worker.state.seed_candles("BTCUSDT", frame, bars, NOW)


async def await_task(task, timeout=5):
    # Runtime housekeeping normally supplies periodic event-loop timers. Supply
    # equivalent timers in this isolated test so sandbox thread wakeups cannot
    # stall the idle loop. The real analysis and its thread are not replaced.
    deadline = asyncio.get_running_loop().time() + timeout
    while not task.done():
        if asyncio.get_running_loop().time() > deadline:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise TimeoutError("Synthetic integration task exceeded bounded timeout")
        await asyncio.sleep(0.01)
    return await task


async def bounded_analysis(worker):
    await await_task(asyncio.create_task(worker._refresh_analysis("BTCUSDT")))


@pytest.fixture
async def chart_executor():
    """Real threads with bounded, synchronous cleanup in the restricted sandbox.

    Python's idle-loop default-executor teardown requires a cross-thread socket
    wakeup that is unreliable here. Clear only this test's executor after joining
    its real work; no production calculation or market result is mocked.
    """
    loop = asyncio.get_running_loop()
    prior = loop._default_executor
    executor = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(executor)
    try:
        yield
    finally:
        executor.shutdown(wait=True)
        loop._default_executor = prior


async def test_refresh_analysis_calculates_real_indicators_scores_and_publishes_overlays(
    worker, chart_executor
):
    # This fixture tests measurable LONG hypotheses, not an unknown direction.
    # Explicit broad context is required; chart monitoring grants no score bias.
    worker._discovery_bias = {"BTCUSDT": "LONG"}
    seed_chart_history(worker)
    apply(worker, native_trade(NOW, 100))
    apply(worker, native_depth(NOW + timedelta(seconds=1)))
    await bounded_analysis(worker)
    assert worker.store.states("provider_error") == []
    live = worker.store.states("live_asset")[0]
    assert live["indicators"]["5m"]["quality"] == "DERIVED"
    metrics = live["indicators"]["5m"]["metrics"]
    assert metrics["ema20"] is not None and metrics["ema100"] is not None
    assert metrics["ema200"] is None  # Honest warmup limitation: 120 observations.
    assert metrics["atr14"] > 0
    kinds = {overlay["kind"] for overlay in live["overlays"]}
    assert {"EMA20", "VWAP", "AVWAP", "POC", "VAH", "VAL"} <= kinds
    assert live["volume_profiles"]["5m"]["available_window"]["quality"] == "ESTIMATED"
    assert live["volume_profiles"]["5m"]["rolling"]["24h"]["quality"] == "UNAVAILABLE"
    assert live["fibonacci"]["production_weight"] == 0
    for name in (
        "stored_energy",
        "absorption",
        "trigger_sensitivity",
        "expansion_readiness",
        "volume_awakening",
    ):
        score = live["scores"][name]
        assert score["calibration"] == "UNCALIBRATED"
        assert score["components"]
        assert score["value"] is None or 0 <= score["value"] <= 100
    assert live["scores"]["stored_energy"]["value"] is not None
    assert live["analysis_at"] == worker.test_clock[0].isoformat()
    assert live["transport_provenance"]["5m"] == ["binance/spot/BTCUSDT/5m"]
    structural = [level for level in live["liquidity_map"] if level["category"] == "INFERRED_STRUCTURE"]
    assert structural and all(level["size"] is None for level in structural)
    assert any(level["category"] == "OBSERVED_ORDER_BOOK" for level in live["liquidity_map"])


async def test_refresh_analysis_keeps_unknown_directional_scores_unavailable(worker, chart_executor):
    seed_chart_history(worker)
    apply(worker, native_trade(NOW, 100))
    apply(worker, native_depth(NOW + timedelta(seconds=1)))
    await bounded_analysis(worker)
    live = worker.store.states("live_asset")[0]
    assert live["score_bias"] == "UNAVAILABLE"
    for name in ("stored_energy", "absorption", "trigger_sensitivity", "expansion_readiness"):
        assert live["scores"][name]["value"] is None
        assert any("direction" in component["reason"].lower() or "bias" in component["reason"].lower()
                   for component in live["scores"][name]["components"])


async def test_live_tick_and_new_book_keep_computed_chart_overlays_and_scores(worker, chart_executor):
    seed_chart_history(worker)
    apply(worker, native_trade(NOW, 100))
    apply(worker, native_depth(NOW + timedelta(seconds=1)))
    await bounded_analysis(worker)
    before = worker.store.states("live_asset")[0]
    apply(worker, native_trade(NOW + timedelta(seconds=2), 100.1, identifier=2))
    apply(
        worker, native_depth(NOW + timedelta(seconds=3), identifier=2, asks=[["101", "29"], ["102.5", "50"]])
    )
    after = worker.store.states("live_asset")[0]
    assert after["price"] == 100.1
    for field in ("indicators", "overlays", "scores", "analysis_at", "transport_provenance"):
        assert after[field] == before[field]
    structural = [level for level in after["liquidity_map"] if level["category"] != "OBSERVED_ORDER_BOOK"]
    assert structural == [
        level for level in before["liquidity_map"] if level["category"] != "OBSERVED_ORDER_BOOK"
    ]
    wall = next(
        level
        for level in after["liquidity_map"]
        if level["category"] == "OBSERVED_ORDER_BOOK" and level["price"] == 101
    )
    assert wall["size"] == 29
    assert after["order_flow"]["aggregate_trade_count"] == 2


def test_chart_snapshot_folds_same_binance_venue_transports_without_rewriting_raw_evidence(worker):
    seed_chart_history(worker, frames=("1m",))
    rest_before = [bar.model_dump() for bar in worker.state.candles("BTCUSDT", "1m")]
    item = native_candle(NOW + timedelta(minutes=1))
    apply(worker, item)
    raw = worker.state.candles("BTCUSDT", "1m")
    snapshot = chart_snapshot(ASSET, worker.state, worker.test_clock[0])
    assert {bar.source for bar in snapshot.candles["1m"]} == {"binance/spot/BTCUSDT/1m"}
    assert {bar.source for bar in raw} == {"binance/spot/BTCUSDT/1m", "binance-public-ws"}
    assert [bar.model_dump() for bar in worker.state.candles("BTCUSDT", "1m")[:-1]] == rest_before
    for derived, original in zip(snapshot.candles["1m"], raw, strict=True):
        assert derived.model_dump(exclude={"source"}) == original.model_dump(exclude={"source"})
    assert raw[-1].available_at == item.received_timestamp
    assert analyze_live(snapshot)["indicators"]["1m"]["quality"] == "DERIVED"
    archived = worker.store.states("live_asset")[0]["closed_candles"]["1m"][-1]
    assert archived["source"] == "binance-public-ws"
    assert datetime.fromisoformat(archived["available_at"]) == item.received_timestamp


def test_chart_snapshot_does_not_fold_foreign_provider_into_native_binance(worker):
    seed_chart_history(worker, frames=("1m",))
    raw = worker.state.candles("BTCUSDT", "1m")
    raw[20] = raw[20].model_copy(update={"source": "foreign-provider:synthetic-test"})
    worker.state.remove_symbol("BTCUSDT")
    worker.state.seed_candles("BTCUSDT", "1m", raw, NOW)
    apply(worker, native_candle(NOW + timedelta(minutes=1)))
    snapshot = chart_snapshot(ASSET, worker.state, worker.test_clock[0])
    assert {bar.source for bar in snapshot.candles["1m"]} == {
        "binance/spot/BTCUSDT/1m",
        "binance-public-ws",
        "foreign-provider:synthetic-test",
    }
    analysis = analyze_live(snapshot)
    assert analysis["indicators"]["1m"]["quality"] == "UNAVAILABLE"
    assert analysis["overlays"] == [] and analysis["issues"]


async def test_slow_actual_chart_analysis_cannot_delay_native_1m_confirmation(
    worker, monkeypatch, chart_executor
):
    import expansion_hunter.live_runtime as runtime

    seed_pressure(worker)
    real_analysis = runtime.analyze_live
    began = threading.Event()
    release = threading.Event()

    def slow_analysis(snapshot, depth):
        began.set()
        if not release.wait(timeout=5):
            raise TimeoutError("Synthetic test barrier was not released")
        return real_analysis(snapshot, depth)

    monkeypatch.setattr(runtime, "analyze_live", slow_analysis)
    worker._schedule_analysis("BTCUSDT")
    task = worker._analysis_tasks["BTCUSDT"]
    try:
        assert await await_task(asyncio.create_task(asyncio.to_thread(began.wait, 2)))
        worker._schedule_analysis("BTCUSDT")
        assert worker._analysis_tasks["BTCUSDT"] is task  # Bounded one outstanding calculation per symbol.
        apply(worker, native_candle(NOW + timedelta(minutes=1)))
        assert not task.done()
        assert worker.monitor.states[ASSET.asset_id].stage == FastStage.BREAKOUT_CONFIRMED
        events = worker.store.objects("fast_event", worker.test_clock[0])
        assert events[-1]["event_type"] == "1M_BREAKOUT_CONFIRMED"
        assert datetime.fromisoformat(events[-1]["candle_close_timestamp"]) == NOW + timedelta(minutes=1)
    finally:
        release.set()
        await await_task(task)
    assert "BTCUSDT" in worker._analysis


async def test_war_room_is_published_archived_and_restored_without_duplicate_history(worker, chart_executor):
    seed_chart_history(worker)
    await bounded_analysis(worker)
    live = worker.store.get_state('live_asset', 'BTCUSDT')
    assert live['war_room']['mode'] == 'DECISION_SUPPORT'
    assert set(live['war_room']['timeframes']) == {'1m', '5m', '15m', '1h', '4h', '1D'}
    assert live['war_room']['expected_path']['probability'] is None
    archived = worker.store.objects('structure_transition', worker.test_clock[0])
    assert archived
    await bounded_analysis(worker)
    assert worker.store.objects('structure_transition', worker.test_clock[0]) == archived
    restored = LiveObservationWorker(worker.settings, clock=worker.clock)
    try:
        assert restored._war_room['BTCUSDT'] == live['war_room']
    finally:
        restored.store.close()
