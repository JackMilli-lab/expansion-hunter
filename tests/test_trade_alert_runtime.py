"""Injected native-shaped runtime fixtures; never real coverage or profit evidence."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from expansion_hunter.config import Settings
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.live_models import (
    DepthLevel,
    FeedConnection,
    FeedHealth,
    LiveCandle,
    LiveFeedSettings,
    LiveMarketEvent,
)
from expansion_hunter.models import TIMEFRAMES, Candle
from expansion_hunter.providers.binance_ws import BinancePublicStream
from expansion_hunter.providers.http import FetchedJSON
from expansion_hunter.trade_alert_models import TradeAlertRecord, TradeAlertSettings
from expansion_hunter.trade_alert_runtime import FRAMES, TradeAlertWorker

T = datetime(2026, 10, 8, 12, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = T

    def __call__(self):
        return self.now


class Provider:
    def __init__(self, clock, symbols=("BTC", "ABC", "USDC", "LOW")):
        self.clock, self.symbols = clock, symbols
        self.history_requests = []
        self.fail_frame = None
        self.fail_bootstrap = False

    async def _get(self, path, *, weight):
        if self.fail_bootstrap:
            raise RuntimeError("synthetic public-provider outage")
        if path.endswith("exchangeInfo"):
            data = {"symbols": [{"symbol": base + "USDT", "baseAsset": base, "quoteAsset": "USDT",
                "status": "TRADING", "isSpotTradingAllowed": True,
                "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01000000"}]}
                for base in self.symbols]}
        else:
            data = [{"symbol": base + "USDT", "lastPrice": "100", "quoteVolume": "100" if base == "LOW" else "10000000",
                     "closeTime": int(self.clock().timestamp() * 1000)} for base in self.symbols]
        return FetchedJSON(data=data, retrieved_at=self.clock())

    async def candles(self, asset, frame, limit, as_of):
        self.history_requests.append((asset.exchange_symbol, frame, limit))
        if frame == self.fail_frame:
            raise RuntimeError("synthetic candle outage")
        duration = TIMEFRAMES[frame]
        endpoint = datetime.fromtimestamp(int(as_of.timestamp() // duration) * duration, UTC)
        return [Candle(open_time=endpoint - timedelta(seconds=duration * (limit - index)),
                       close_time=endpoint - timedelta(seconds=duration * (limit - index - 1)),
                       available_at=self.clock(), open=100, high=100.5, low=99.5, close=100,
                       volume=100, quote_volume=10000, taker_buy_volume=75,
                       source=f"binance/spot/{asset.exchange_symbol}/{frame}") for index in range(limit)]


class Stream:
    def __init__(self, symbols, settings):
        self.symbols, self.settings = symbols, settings
        self.messages = []
        self.fail = False
        self.health = FeedHealth(connection=FeedConnection.CONNECTED, symbols=symbols, stale=False)

    async def events(self, stop):
        if self.fail:
            raise RuntimeError("synthetic stream outage")
        for event in self.messages:
            yield event
        await stop.wait()

    def stop(self):
        pass


class Engine:
    def __init__(self):
        self.calls = []
        self.restored = None
        self.observations = {}
        self.market_snapshots = []

    def restore_state(self, payload):
        self.restored = payload

    def export_state(self):
        return {"version": 1, "episodes": {"synthetic": {"calls": len(self.calls)}}}

    def evaluate(self, asset, candles, now, received_at, price, btc, tick_size, *, market_snapshot=None):
        self.calls.append((candles, btc, tick_size))
        self.market_snapshots.append(market_snapshot)
        if len(self.calls) > 1 or not candles["1m"] or not candles["5m"]:
            return []
        return [TradeAlertRecord(event_id="synthetic-runtime-alert", episode_id="synthetic-episode",
            asset_id=asset.asset_id, symbol=asset.exchange_symbol, phase="TRADE_ALERT", bias="LONG",
            event_timestamp=candles["1m"][-1].close_time, received_at=received_at, detected_at=now,
            expires_at=now + timedelta(seconds=120), price=price, trigger_level=100.2,
            trigger_discovered_at=candles["5m"][-1].close_time, distance_pct=.08,
            invalidation=99.5, targets=[101], score=70, reason="Synthetic runtime plumbing only",
            data_quality={"order_book": "UNAVAILABLE"}, detection_latency_ms=1000, alert_latency_ms=0)]


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    store = IntelligenceStore(str(tmp_path / "trade.sqlite3"))
    provider, engine = Provider(clock), Engine()
    worker = TradeAlertWorker(Settings(db_path=store.database_path),
                              TradeAlertSettings(max_assets=2, history_bars=36, stream_mode="CANDLES"),
                              store=store, provider=provider, engine=engine, stream_factory=Stream, clock=clock)
    yield worker, provider, engine, clock, store
    store.close()


def drain_writes(worker):
    while not worker._writes.empty():
        packet = worker._writes.get_nowait()
        worker._write(packet)
        worker._writes.task_done()


def event(clock, *, at=None, received=None):
    at, received = at or clock(), received or clock()
    close = at.replace(second=0, microsecond=0)
    return LiveMarketEvent(symbol="ABCUSDT", kind="CANDLE", exchange_timestamp=at,
        received_timestamp=received, processed_timestamp=received,
        candle=LiveCandle(timeframe="1m", open_time=close - timedelta(minutes=1), close_time=close,
            open=100, high=100.2, low=99.9, close=100.12, volume=100, quote_volume=10012,
            taker_buy_volume=75, complete=True))


async def consume_one(worker):
    task = asyncio.create_task(worker._consume(0))
    await asyncio.sleep(.01)
    worker.stop.set()
    await task


@pytest.mark.asyncio
async def test_native_universe_excludes_stablecoin_and_low_liquidity_retains_ticks(setup):
    worker, _, _, _, _ = setup
    await worker.bootstrap()
    assert set(worker.assets) == {"ABCUSDT", "BTCUSDT"}
    assert worker.tick_sizes == {"ABCUSDT": .01, "BTCUSDT": .01}
    assert worker.snapshot()["exclusion_reasons"]["STABLECOIN_BASE"] == 1
    assert worker.snapshot()["exclusion_reasons"]["INSUFFICIENT_QUOTE_VOLUME"] == 1
    assert worker._streams[0].settings.stream_mode == "CANDLES"
    assert worker._streams[0].settings.timeframes == list(FRAMES)


@pytest.mark.asyncio
async def test_500_configured_pairs_are_sharded_without_intensive_book_subscriptions(setup):
    worker, provider, _, _, _ = setup
    worker.config = TradeAlertSettings(max_assets=500)
    provider.symbols = tuple(f"COIN{index}" for index in range(500))
    await worker.bootstrap()
    assert len(worker.assets) == 500
    assert [len(stream.symbols) for stream in worker._streams] == [100] * 5
    assert all(stream.settings.stream_mode == "LIGHT" for stream in worker._streams)
    assert all(len(stream.symbols) * (len(FRAMES) + 2) <= 1024 for stream in worker._streams)
    assert worker.snapshot()["counts"]["ready"] == 0
    assert worker.snapshot()["counts"]["scanned"] == 0  # Configuration is not fresh-candle coverage.


@pytest.mark.asyncio
async def test_historical_cache_reuses_complete_windows_after_restart(setup):
    worker, provider, _, clock, store = setup
    await worker.bootstrap()
    await worker._warmup()
    drain_writes(worker)
    assert len(provider.history_requests) == len(FRAMES) * 2
    assert len(store.states("trade_alert_history")) == len(FRAMES) * 2
    restored = TradeAlertWorker(worker.settings, worker.config, store=store, provider=provider,
                                engine=Engine(), stream_factory=Stream, clock=clock)
    await restored.bootstrap()
    await restored._warmup()
    assert len(provider.history_requests) == len(FRAMES) * 2
    assert len(restored._history("ABCUSDT", "5m", clock())) == 36


@pytest.mark.asyncio
async def test_closed_native_event_is_evaluated_persisted_and_context_uses_btc_5m(setup, monkeypatch):
    worker, _, engine, clock, store = setup
    monkeypatch.delenv("COCKPIT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("COCKPIT_TELEGRAM_CHAT_ID", raising=False)
    await worker.bootstrap()
    await worker._warmup()
    clock.now += timedelta(seconds=61)
    worker._streams[0].messages = [event(clock)]
    await consume_one(worker)
    await worker._evaluate_loop()
    drain_writes(worker)
    records = store.objects("trade_alert", clock())
    assert len(records) == 1
    assert records[0]["data_quality"]["order_book"] == "UNAVAILABLE"
    assert store.objects("trade_alert_candle", clock())[0]["timeframe"] == "1m"
    assert engine.calls[0][2] == .01
    assert all((bar.close_time - bar.open_time).total_seconds() == 300 for bar in engine.calls[0][1])
    assert all(bar.available_at <= clock() for frame in engine.calls[0][0].values() for bar in frame)
    assert store.notification_counts().get("PENDING", 0) == 0
    restarted = TradeAlertWorker(worker.settings, worker.config, store=store, provider=worker.provider,
                                engine=Engine(), stream_factory=Stream, clock=clock)
    assert restarted.engine.restored["version"] == 1
    assert store.get_state("trade_alert_engine", "default") == engine.export_state()


@pytest.mark.parametrize("keys,expected", [(("active",), "active"), (("default",), "default"),
                                         (("active", "default"), "default")])
def test_restart_restores_default_checkpoint_with_safe_legacy_fallback(setup, keys, expected):
    worker, provider, _, clock, store = setup
    for key in keys:
        store.put_state("trade_alert_engine", key, clock(), {"version": 1, "episodes": {}, "key": key})
    engine = Engine()
    TradeAlertWorker(worker.settings, worker.config, store=store, provider=provider,
                     engine=engine, stream_factory=Stream, clock=clock)
    assert engine.restored["key"] == expected
    if "active" in keys:
        assert store.get_state("trade_alert_engine", "active")["key"] == "active"


@pytest.mark.asyncio
async def test_closed_no_signal_archives_first_observation_without_inventing_scores(setup):
    worker, _, engine, clock, store = setup
    await worker.bootstrap()
    await worker._warmup()
    engine.evaluate = lambda *_args, **_kwargs: []
    worker._evaluations.put_nowait(("ABCUSDT", clock(), 100, False))
    worker.stop.set()
    await worker._evaluate_loop()
    drain_writes(worker)
    records = store.objects("trade_alert_observation", clock())
    assert len(records) == 1
    first = records[0]
    assert first["observation_status"] == "UNAVAILABLE"
    assert first["data_quality"]["evaluation"] == "UNAVAILABLE"
    assert "score" not in first
    assert first["asset_id"] == "binance:spot:ABCUSDT"
    assert datetime.fromisoformat(first["event_timestamp"]) == T
    assert store.objects("trade_alert", clock()) == []
    clock.now += timedelta(seconds=1)
    worker._evaluations.put_nowait(("ABCUSDT", clock(), 100, False))
    worker._evaluations.put_nowait(("BTCUSDT", clock(), 100, True))
    await worker._evaluate_loop()
    drain_writes(worker)
    assert store.objects("trade_alert_observation", clock()) == [first]


@pytest.mark.asyncio
async def test_stale_source_message_cannot_become_fresh_alert_just_by_late_receipt(setup):
    worker, _, engine, clock, _ = setup
    await worker.bootstrap()
    clock.now += timedelta(minutes=3)
    worker._streams[0].messages = [event(clock, at=T, received=clock())]
    await consume_one(worker)
    assert worker._evaluations.empty()
    assert "ABCUSDT" not in worker._prices
    assert worker.snapshot()["counts"]["stale_messages"] == 1
    assert engine.calls == []


@pytest.mark.asyncio
async def test_missing_context_history_is_partial_with_visible_failure(setup):
    worker, provider, _, clock, _ = setup
    await worker.bootstrap()
    provider.fail_frame = "4h"
    await worker._warmup()
    assert worker._history("ABCUSDT", "4h", clock()) == []
    assert any(issue.startswith("4h:") for issue in worker._issues["ABCUSDT"])
    assert worker._history("ABCUSDT", "1m", clock())


@pytest.mark.asyncio
async def test_fresh_open_price_does_not_hide_stale_completed_candles(setup):
    worker, provider, _, clock, _ = setup
    await worker.bootstrap()
    await worker._warmup()
    clock.now += timedelta(minutes=4)
    worker._prices["ABCUSDT"] = (100, clock())
    provider.fail_frame = "1m"
    publisher = asyncio.create_task(worker._publish_loop())
    await asyncio.sleep(.01)
    worker.stop.set()
    await publisher
    await asyncio.gather(*list(worker._warmup_tasks), return_exceptions=True)
    row = next(row for row in worker.snapshot()["assets"] if row["symbol"] == "ABCUSDT")
    assert row["status"] == "STALE"
    assert row["data_quality"]["1m"] == "STALE"
    assert row["latest_signal_fresh"] is False


@pytest.mark.asyncio
async def test_native_provider_failure_is_reported_and_shutdown_does_not_launch_fake_feed(setup):
    worker, provider, _, _, _ = setup
    provider.fail_bootstrap = True
    await worker.run(seconds=.01)
    status = worker.snapshot()
    assert status["status"] == "STOPPED"
    assert "public-provider outage" in status["error"]
    assert status["counts"]["provider_failures"] == 1
    assert not worker.assets and not worker._streams


@pytest.mark.asyncio
async def test_stream_failure_and_queue_overflow_are_visible(setup):
    worker, _, _, _, _ = setup
    await worker.bootstrap()
    worker._streams[0].fail = True
    await worker._consume(0)
    assert worker.snapshot()["counts"]["provider_failures"] == 1
    for _index in range(worker.config.queue_size + 1):
        worker._enqueue(worker._evaluations, ("ABCUSDT", T, 100, False), "evaluation")
    assert worker.snapshot()["counts"]["evaluation_dropped"] == 1
    assert "queue full" in worker.snapshot()["error"]


@pytest.mark.asyncio
async def test_contiguous_tail_and_native_final_archive_dedup_preserve_first_receipt(setup):
    worker, _, _, clock, store = setup
    await worker.bootstrap()
    await worker._warmup()
    clock.now += timedelta(seconds=121)
    current = event(clock)
    assert worker._state("ABCUSDT").apply(current)
    assert len(worker._history("ABCUSDT", "1m", clock())) == 1
    worker._write(("candle", current))
    received = clock() + timedelta(seconds=1)
    repeated = current.model_copy(update={"received_timestamp": received, "processed_timestamp": received})
    worker._write(("candle", repeated))
    known = store.objects("trade_alert_candle", received)
    assert len(known) == 1
    assert datetime.fromisoformat(known[0]["candle"]["available_at"]) == current.received_timestamp


def test_light_mode_subscribes_six_frames_and_native_flow_without_depth():
    symbols = [f"COIN{index}USDT" for index in range(100)]
    stream = BinancePublicStream(symbols, LiveFeedSettings(timeframes=list(FRAMES), max_symbols=100,
                                                         stream_mode="LIGHT"))
    names = parse_qs(urlsplit(stream.stream_url("wss://data-stream.binance.vision/stream")).query)["streams"][0].split("/")
    assert len(names) == 800
    assert "coin0usdt@aggTrade" in names and "coin0usdt@bookTicker" in names
    assert "coin0usdt@kline_1d" in names
    assert not any("depth" in name for name in names)
    full = BinancePublicStream(["BTCUSDT"], LiveFeedSettings(stream_mode="FULL"))
    assert "depth20" in full.stream_url("wss://data-stream.binance.vision/stream")


@pytest.mark.asyncio
async def test_light_sharding_applies_native_1024_stream_budget_at_large_connection_limit(setup):
    worker, provider, _, _, _ = setup
    worker.config = TradeAlertSettings(max_assets=500, symbols_per_connection=150)
    provider.symbols = tuple(f"COIN{index}" for index in range(500))
    await worker.bootstrap()
    assert [len(stream.symbols) for stream in worker._streams] == [128, 128, 128, 116]
    assert all(len(stream.symbols) * 8 <= 1024 for stream in worker._streams)


@pytest.mark.asyncio
async def test_pre_move_lifecycle_is_published_persisted_and_restored_stale(setup):
    worker, provider, _, clock, store = setup
    await worker.bootstrap()
    await worker._warmup()
    observation = {"symbol": "ABCUSDT", "price": 99.9, "as_of": clock().isoformat(), "status": "EVALUATED",
        "data_quality": {"1m": "AVAILABLE", "5m": "AVAILABLE"},
        "directions": {"LONG": {"score": 70, "level": 100, "distance_atr": .1,
            "level_discovered_at": (clock() - timedelta(minutes=5)).isoformat(), "components": {},
            "metrics": {"range_compression": .6, "higher_low_strength": .3,
                        "resistance_test_count": 3, "taker_buy_ratio": .7}}}}
    worker._observations["binance:spot:ABCUSDT"] = observation
    worker._prices["ABCUSDT"] = (99.9, clock())
    publisher = asyncio.create_task(worker._publish_loop())
    await asyncio.sleep(.01)
    worker.stop.set()
    await publisher
    drain_writes(worker)
    row = next(row for row in worker.snapshot()["pre_move"] if row["bias"] == "LONG")
    assert row["state"] == "TRIGGER_IMMINENT"
    assert row["probability"] is None
    assert store.objects("trade_alert_pre_move_transition", clock())
    assert store.get_state("trade_alert_pre_move", "default")["version"] == 1
    restarted = TradeAlertWorker(worker.settings, worker.config, store=store, provider=provider,
                                engine=Engine(), stream_factory=Stream, clock=clock)
    restored = restarted.ranker.update(worker._observations, {}, clock())
    assert next(row for row in restored if row["bias"] == "LONG")["status"] == "STALE"


@pytest.mark.asyncio
async def test_native_trades_and_bbo_are_coalesced_and_pass_flow_without_raw_tick_archival(setup):
    worker, _, engine, clock, store = setup
    await worker.bootstrap()
    await worker._warmup()
    worker._streams[0].messages = [
        LiveMarketEvent(symbol="ABCUSDT", kind="TRADE", exchange_timestamp=clock(),
            received_timestamp=clock(), processed_timestamp=clock(), price=100, quantity=2,
            buyer_is_maker=False, sequence_id=1, sequence_supported=True),
        LiveMarketEvent(symbol="ABCUSDT", kind="TRADE", exchange_timestamp=clock(),
            received_timestamp=clock(), processed_timestamp=clock(), price=100.02, quantity=1,
            buyer_is_maker=True, sequence_id=2, sequence_supported=True),
        LiveMarketEvent(symbol="ABCUSDT", kind="BOOK_TICKER", received_timestamp=clock(),
            processed_timestamp=clock(), bid=DepthLevel(price=100, quantity=5),
            ask=DepthLevel(price=100.04, quantity=4), sequence_id=10)]
    await consume_one(worker)
    assert worker._evaluations.empty()
    worker.stop.clear()
    publisher = asyncio.create_task(worker._publish_loop())
    await asyncio.sleep(.01)
    worker.stop.set()
    await publisher
    assert worker._evaluations.qsize() == 1
    await worker._evaluate_loop()
    drain_writes(worker)
    assert len(engine.calls) == 1
    market = engine.market_snapshots[0]
    assert market["order_flow"]["delta"] == 1
    assert market["order_flow_windows"]["5m"]["delta"] == 1
    assert market["data_quality"]["best_bid_ask"] == "AVAILABLE"
    assert market["data_quality"]["order_book"] == "UNAVAILABLE"
    row = next(row for row in worker.snapshot()["assets"] if row["symbol"] == "ABCUSDT")
    assert row["price"] == pytest.approx(100.02)
    assert row["price_evidence"]["type"] == "ESTIMATED_BEST_BID_ASK_MIDPOINT"
    assert store.objects("trade_alert_candle", clock()) == []
    assert store.objects("trade_alert_observation", clock()) == []


@pytest.mark.asyncio
async def test_daily_closed_candle_is_archived_without_critical_evaluation(setup):
    worker, _, _, clock, store = setup
    await worker.bootstrap()
    close = clock().replace(hour=0, minute=0)
    worker._streams[0].messages = [LiveMarketEvent(symbol="ABCUSDT", kind="CANDLE",
        exchange_timestamp=clock(), received_timestamp=clock(), processed_timestamp=clock(),
        candle=LiveCandle(timeframe="1D", open_time=close - timedelta(days=1), close_time=close,
                         open=100, high=101, low=99, close=100, volume=100, complete=True))]
    await consume_one(worker)
    drain_writes(worker)
    assert worker._evaluations.empty()
    assert store.objects("trade_alert_candle", clock())[0]["timeframe"] == "1D"


@pytest.mark.asyncio
async def test_market_snapshot_does_not_leak_flow_received_after_decision_receipt(setup):
    worker, _, engine, clock, _ = setup
    await worker.bootstrap()
    await worker._warmup()
    cutoff = clock()
    clock.now += timedelta(seconds=1)
    worker._state("ABCUSDT").apply(LiveMarketEvent(symbol="ABCUSDT", kind="TRADE",
        exchange_timestamp=clock(), received_timestamp=clock(), processed_timestamp=clock(),
        price=100, quantity=100, buyer_is_maker=False, sequence_id=1, sequence_supported=True))
    worker._evaluations.put_nowait(("ABCUSDT", cutoff, 100, False))
    worker.stop.set()
    await worker._evaluate_loop()
    market = engine.market_snapshots[0]
    assert market["data_quality"]["spot_trades"] == "UNAVAILABLE"
    assert market["order_flow"]["delta"] is None
    assert market["order_flow_windows"]["1m"]["quality"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_pre_ignition_alert_is_before_break_persisted_notified_and_restart_deduped(setup, monkeypatch):
    worker, provider, _, clock, store = setup
    monkeypatch.setenv("COCKPIT_TELEGRAM_BOT_TOKEN", "12345:" + "x" * 20)
    monkeypatch.setenv("COCKPIT_TELEGRAM_CHAT_ID", "123456")
    await worker.bootstrap()
    await worker._warmup()
    worker._streams[0].messages = [LiveMarketEvent(symbol="ABCUSDT", kind="TRADE",
        exchange_timestamp=clock(), received_timestamp=clock(), processed_timestamp=clock(),
        price=99.7, quantity=2, buyer_is_maker=False, sequence_id=1, sequence_supported=True)]
    await consume_one(worker)
    asset_id = "binance:spot:ABCUSDT"
    discovered = (clock() - timedelta(minutes=5)).isoformat()
    observation = {"symbol": "ABCUSDT", "price": 99.7, "as_of": clock().isoformat(), "status": "EVALUATED",
        "data_quality": {"1m": "AVAILABLE", "5m": "AVAILABLE", "order_book": "UNAVAILABLE"},
        "directions": {"LONG": {"score": 60, "level": 100, "distance_atr": .3,
            "level_discovered_at": discovered, "components": {},
            "metrics": {"range_compression": .6, "higher_low_strength": .3, "trigger_atr": 1,
                        "resistance_test_count": 3, "taker_buy_ratio": .7}}}}
    worker._observations[asset_id] = observation
    worker._episode_state = {"version": 1, "episodes": {f"{asset_id}|LONG|CONTINUATION": {
        "episode_id": "synthetic-native-episode", "asset_id": asset_id, "bias": "LONG",
        "setup_type": "CONTINUATION", "discovered_at": discovered, "trigger": 100, "atr": 1,
        "phase": "STRUCTURE_WATCH", "status": "ACTIVE", "restart_stale": False,
        "invalidation": 99, "targets": [101]}}}
    unchanged_episode = copy.deepcopy(worker._episode_state)
    worker.stop.clear()
    publisher = asyncio.create_task(worker._publish_loop())
    await asyncio.sleep(.01)
    worker.stop.set()
    await publisher
    drain_writes(worker)
    records = store.objects("trade_alert", clock())
    assert len(records) == 1
    alert = records[0]
    assert alert["signal_state"] == "PRE_IGNITION"
    assert alert["phase"] == "STRUCTURE_WATCH"
    assert alert["price"] < alert["trigger_level"]
    assert worker._episode_state == unchanged_episode
    assert store.notification_counts()["PENDING"] == 1
    assert worker.snapshot()["pre_move_statealerts"][0]["event_id"] == alert["event_id"]
    row = next(row for row in worker._pre_move_rows if row["bias"] == "LONG")
    assert worker._emit_pre_move_state_alert(row, clock()) is None
    # A later price crossing cannot be turned into a retrospective early alert.
    clock.now += timedelta(seconds=1)
    worker._prices["ABCUSDT"] = (100.2, clock())
    crossed = dict(row, state="LOADED")
    assert worker._emit_pre_move_state_alert(crossed, clock()) is None
    restarted = TradeAlertWorker(worker.settings, worker.config, store=store, provider=provider,
                                engine=Engine(), stream_factory=Stream, clock=clock)
    await restarted.bootstrap()
    await restarted._warmup()
    restarted._streams[0].messages = [LiveMarketEvent(symbol="ABCUSDT", kind="TRADE",
        exchange_timestamp=clock(), received_timestamp=clock(), processed_timestamp=clock(),
        price=99.7, quantity=2, buyer_is_maker=False, sequence_id=1, sequence_supported=True)]
    await consume_one(restarted)
    restarted._observations[asset_id] = dict(observation, as_of=clock().isoformat())
    assert alert["event_id"] in restarted._state_alert_seen
    assert restarted._emit_pre_move_state_alert(row, clock()) is None
    drain_writes(restarted)
    assert len(store.objects("trade_alert", clock())) == 1
