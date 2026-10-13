"""Deterministic synthetic protocol fixtures; these are not a live-data claim."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.live_feed import LiveFeedState
from expansion_hunter.live_models import FeedConnection, LiveFeedSettings, normalize_symbols
from expansion_hunter.models import Candle
from expansion_hunter.providers.binance_ws import BinancePublicStream, decode_message

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)


def millis(timestamp):
    return int(timestamp.timestamp() * 1000)


def trade(identifier=1, price="100", quantity="2", buyer_is_maker=False, timestamp=NOW):
    return {
        "stream": "btcusdt@aggTrade",
        "data": {
            "e": "aggTrade",
            "s": "BTCUSDT",
            "a": identifier,
            "p": price,
            "q": quantity,
            "m": buyer_is_maker,
            "T": millis(timestamp),
            "E": millis(timestamp + timedelta(milliseconds=1)),
        },
    }


def ticker(identifier=1):
    return {
        "stream": "btcusdt@bookTicker",
        "data": {
            "s": "BTCUSDT",
            "u": identifier,
            "b": "99.9",
            "B": "10",
            "a": "100.1",
            "A": "12",
        },
    }


def depth(identifier=1):
    return {
        "stream": "btcusdt@depth20@100ms",
        "data": {
            "lastUpdateId": identifier,
            "bids": [["98.5", "10"]],
            "asks": [["101", "20"], ["102.5", "30"]],
        },
    }


def kline(opened=NOW, frame="1m", complete=False, close="101", timestamp=None):
    duration = {"1m": 60, "5m": 300, "15m": 900}[frame]
    boundary = opened + timedelta(seconds=duration)
    timestamp = timestamp or (boundary if complete else opened + timedelta(seconds=30))
    return {
        "stream": f"btcusdt@kline_{frame}",
        "data": {
            "e": "kline",
            "s": "BTCUSDT",
            "E": millis(timestamp),
            "k": {
                "t": millis(opened),
                "T": millis(boundary) - 1,
                "i": frame,
                "o": "100",
                "h": "102",
                "l": "99",
                "c": close,
                "v": "10",
                "q": "1000",
                "V": "6",
                "Q": "600",
                "n": 12,
                "x": complete,
            },
        },
    }


def event(payload, received=NOW, epoch=0):
    return decode_message(payload, received, received + timedelta(milliseconds=2), stream_epoch=epoch)


def test_native_trade_timestamps_and_latencies():
    item = event(trade(timestamp=NOW - timedelta(milliseconds=25)))
    assert item.price == 100
    assert item.quantity == 2
    assert item.buyer_is_maker is False
    assert item.feed_latency_ms == 25
    assert item.processing_latency_ms == 2
    assert item.sequence_supported


def test_best_bid_ask_has_no_invented_exchange_timestamp():
    item = event(ticker())
    assert item.exchange_timestamp is None
    assert item.feed_latency_ms is None
    assert item.bid.price == 99.9
    assert item.ask.quantity == 12
    assert not item.sequence_supported


def test_partial_depth_exact_prices_and_scope():
    item = event(depth())
    assert [level.price for level in item.asks] == [101, 102.5]
    assert item.bids[0].price == 98.5
    assert item.depth_scope == "TOP_20_SNAPSHOT"
    assert not item.sequence_supported
    assert item.exchange_timestamp is None


@pytest.mark.parametrize("frame,seconds", [("1m", 60), ("5m", 300), ("15m", 900)])
def test_open_candle_is_never_closed_history(frame, seconds):
    feed = LiveFeedState()
    item = event(kline(frame=frame), received=NOW + timedelta(seconds=30))
    assert feed.apply(item)
    assert feed.candles("BTCUSDT", frame) == []
    assert feed.open_candle("BTCUSDT", frame).close_time == NOW + timedelta(seconds=seconds)
    with pytest.raises(ValueError, match="open candle"):
        item.candle.as_closed(item.received_timestamp)


@pytest.mark.parametrize("frame,seconds", [("1m", 60), ("5m", 300), ("15m", 900)])
def test_exact_candle_boundary_and_completion(frame, seconds):
    feed = LiveFeedState()
    closed_at = NOW + timedelta(seconds=seconds)
    payload = kline(frame=frame, complete=True)
    with pytest.raises(ValueError, match="before its close"):
        event(payload, received=closed_at - timedelta(microseconds=1))
    assert feed.apply(event(payload, received=closed_at))
    assert feed.candles("BTCUSDT", frame)[0].close_time == closed_at
    assert feed.open_candle("BTCUSDT", frame) is None
    assert feed.candles("BTCUSDT", frame, closed_at - timedelta(microseconds=1)) == []


def test_finalized_candle_cannot_repaint_after_duplicate_or_correction():
    feed = LiveFeedState()
    payload = kline(complete=True)
    assert feed.apply(event(payload, received=NOW + timedelta(seconds=60)))
    assert not feed.apply(event(payload, received=NOW + timedelta(seconds=61)))
    assert not feed.apply(event(kline(complete=True, close="100"), received=NOW + timedelta(seconds=62)))
    assert feed.candles("BTCUSDT", "1m")[0].close == 101


def test_out_of_order_open_update_and_old_epoch_are_suppressed():
    feed = LiveFeedState()
    assert feed.apply(event(trade(10), received=NOW + timedelta(seconds=1), epoch=2))
    assert not feed.apply(event(trade(11), received=NOW, epoch=2))
    assert not feed.apply(event(trade(12), received=NOW + timedelta(seconds=2), epoch=1))
    assert feed.events_rejected == 2


def test_sequence_duplicate_and_epoch_reset():
    feed = LiveFeedState()
    assert feed.apply(event(trade(10), epoch=1))
    assert not feed.apply(event(trade(10), epoch=1))
    assert feed.apply(event(trade(1), received=NOW + timedelta(seconds=1), epoch=2))
    flow = feed.snapshot("BTCUSDT", NOW + timedelta(seconds=1))["order_flow"]
    assert flow["aggregate_trade_count"] == 1
    assert flow["cvd"] == 2


def test_native_cvd_and_gap_reset_are_explicit():
    feed = LiveFeedState()
    feed.apply(event(trade(1)))
    feed.apply(event(trade(2, quantity="3", buyer_is_maker=True), received=NOW + timedelta(seconds=1)))
    flow = feed.snapshot("BTCUSDT", NOW + timedelta(seconds=1))["order_flow"]
    assert flow["cvd"] == -1
    assert flow["taker_buy_volume"] == 2
    assert flow["taker_sell_volume"] == 3
    assert flow["historical_cvd"] == "UNAVAILABLE"
    feed.apply(event(trade(4), received=NOW + timedelta(seconds=2)))
    flow = feed.snapshot("BTCUSDT", NOW + timedelta(seconds=2))["order_flow"]
    assert flow["cvd"] == 2
    assert flow["continuous_since_session_start"] is False
    assert feed.sequence_gaps == 1


def test_depth_update_id_jump_is_not_misreported_as_full_book_gap():
    feed = LiveFeedState()
    feed.apply(event(depth(1)))
    feed.apply(event(depth(900), received=NOW + timedelta(seconds=1)))
    assert feed.sequence_gaps == 0
    assert feed.snapshot("BTCUSDT", NOW + timedelta(seconds=1))["depth"]["sequence_supported"] is False


def test_per_feed_staleness_missing_futures_and_native_price():
    feed = LiveFeedState(LiveFeedSettings(stale_after_seconds=5))
    feed.apply(event(trade(1)))
    feed.apply(event(ticker(), received=NOW + timedelta(seconds=6)))
    snapshot = feed.snapshot("BTCUSDT", NOW + timedelta(seconds=6))
    assert snapshot["price"] == 100
    assert snapshot["price_quality"] == "STALE"
    assert snapshot["data_quality"]["spot_trades"] == "STALE"
    assert snapshot["data_quality"]["best_bid_ask"] == "AVAILABLE"
    assert snapshot["data_quality"]["order_book"] == "UNAVAILABLE"
    assert snapshot["data_quality"]["futures"] == "UNAVAILABLE"


def test_delayed_exchange_events_are_stale_even_if_received_now():
    feed = LiveFeedState(LiveFeedSettings(stale_after_seconds=5))
    feed.apply(event(trade(timestamp=NOW - timedelta(seconds=10))))
    assert feed.snapshot("BTCUSDT", NOW)["data_quality"]["spot_trades"] == "STALE"


def test_missing_symbol_has_explicit_unavailable_data():
    snapshot = LiveFeedState().snapshot("XRPUSDT", NOW)
    assert snapshot["data_quality"]["spot_cvd"] == "UNAVAILABLE"


def test_candle_history_and_state_are_bounded():
    feed = LiveFeedState(LiveFeedSettings(candle_capacity=32))
    for index in range(40):
        opened = NOW + timedelta(minutes=index)
        feed.apply(event(kline(opened, complete=True), received=opened + timedelta(minutes=1)))
    assert len(feed.candles("BTCUSDT", "1m")) == 32


def test_missing_closed_candles_record_gap():
    feed = LiveFeedState()
    feed.apply(event(kline(complete=True), received=NOW + timedelta(minutes=1)))
    feed.apply(event(kline(NOW + timedelta(minutes=2), complete=True), received=NOW + timedelta(minutes=3)))
    assert feed.snapshot("BTCUSDT", NOW + timedelta(minutes=3))["candle_gaps"]["1m"] == [
        [(NOW + timedelta(minutes=1)).isoformat(), (NOW + timedelta(minutes=2)).isoformat()]
    ]


def test_rest_warmup_respects_availability_and_preserves_live_finalized_values():
    feed = LiveFeedState()
    feed.apply(event(kline(complete=True), received=NOW + timedelta(minutes=1)))
    changed = Candle(
        open_time=NOW,
        close_time=NOW + timedelta(minutes=1),
        available_at=NOW + timedelta(minutes=2),
        open=100,
        high=102,
        low=99,
        close=100,
        volume=10,
        source="native-public-rest",
    )
    with pytest.raises(ValueError, match="future or unavailable"):
        feed.seed_candles("BTCUSDT", "1m", [changed], NOW + timedelta(minutes=1))
    feed.seed_candles("BTCUSDT", "1m", [changed], NOW + timedelta(minutes=2))
    assert feed.candles("BTCUSDT", "1m")[0].close == 101


def test_invalid_payload_never_gains_invented_values():
    payload = trade()
    del payload["data"]["q"]
    with pytest.raises(KeyError):
        event(payload)
    assert decode_message({"result": None, "id": 1}, NOW, NOW) is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "ws://stream.binance.com/stream",
        "wss://evil.example/stream",
        "wss://user:secret@stream.binance.com/stream",
        "wss://stream.binance.com/ws",
    ],
)
def test_only_public_allowlisted_endpoints(endpoint):
    with pytest.raises(ValueError):
        LiveFeedSettings(websocket_urls=[endpoint])


def test_combined_url_capacity_and_public_streams_only():
    stream = BinancePublicStream(["btcusdt", "XRPUSDT"])
    url = stream.stream_url(stream.settings.websocket_urls[0])
    assert "btcusdt@aggTrade" in url and "xrpusdt@depth20@100ms" in url
    assert "kline_1m" in url and "kline_5m" in url and "kline_15m" in url
    assert "listenKey" not in url and "signature" not in url
    with pytest.raises(ValueError):
        normalize_symbols(["BTCUSDT", "XRPUSDT"], maximum=1)


class FakeSocket:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    async def recv(self):
        if self.payloads:
            value = self.payloads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        await asyncio.Future()

    async def ping(self):
        pong = asyncio.get_running_loop().create_future()
        pong.set_result(0.001)
        return pong


class Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value

    async def __aexit__(self, *args):
        return False


def tiny_settings(**kwargs):
    return LiveFeedSettings(
        reconnect_initial_seconds=0.01,
        reconnect_max_seconds=0.02,
        reconnect_jitter_fraction=0,
        heartbeat_seconds=0.01,
        **kwargs,
    )


async def test_reconnect_fallback_health_and_graceful_stop():
    calls = []
    sockets = [ConnectionError("synthetic disconnect"), FakeSocket([trade()])]

    def connector(url, **kwargs):
        calls.append((url, kwargs))
        return Context(sockets.pop(0))

    stream = BinancePublicStream(["BTCUSDT"], tiny_settings(), clock=lambda: NOW, connect_factory=connector)
    stop = asyncio.Event()
    async for item in stream.events(stop):
        assert item.kind == "TRADE"
        assert stream.health.connection == FeedConnection.CONNECTED
        assert not stream.health.stale
        stop.set()
    assert stream.health.connection == FeedConnection.STOPPED
    assert stream.health.reconnect_count == 1
    assert "data-stream.binance.vision" in calls[0][0]
    assert "stream.binance.com" in calls[1][0]
    assert calls[1][1]["ping_interval"] == 20


async def test_adapter_duplicates_gaps_and_malformed_message_isolation():
    socket = FakeSocket(["not JSON", trade(1), trade(1), trade(3), depth(20)])
    stream = BinancePublicStream(
        ["BTCUSDT"],
        tiny_settings(),
        clock=lambda: NOW,
        connect_factory=lambda *args, **kwargs: Context(socket),
    )
    kinds = []
    async for item in stream.events():
        kinds.append(item.kind)
        if item.kind == "TRADE" and item.sequence_id == 3:
            assert item.sequence_gap
        if item.kind == "DEPTH":
            stream.stop()
    assert kinds == ["TRADE", "TRADE", "DEPTH"]
    assert stream.health.malformed_messages == 1
    assert stream.health.ignored_messages == 1
    assert stream.health.sequence_gaps == 1


async def test_stop_interrupts_blocked_recv():
    stream = BinancePublicStream(
        ["BTCUSDT"],
        tiny_settings(),
        clock=lambda: NOW,
        connect_factory=lambda *args, **kwargs: Context(FakeSocket([])),
    )
    stop = asyncio.Event()

    async def consume():
        async for _ in stream.events(stop):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=0.2)
    assert stream.health.connection == FeedConnection.STOPPED


async def test_watchlist_subscription_changes_do_not_count_as_provider_failure():
    calls = []

    def connector(url, **kwargs):
        calls.append(url)
        return Context(FakeSocket([trade()]))

    stream = BinancePublicStream(["BTCUSDT"], tiny_settings(), clock=lambda: NOW, connect_factory=connector)
    count = 0
    async for _ in stream.events():
        count += 1
        if count == 1:
            stream.set_symbols(["BTCUSDT", "XRPUSDT"])
        else:
            stream.stop()
    assert "xrpusdt@aggTrade" in calls[1]
    assert stream.health.reconnect_count == 0
    assert stream.health.stream_epoch == 2


async def test_quiet_connected_feed_becomes_stale_and_heartbeat_does_not_fake_data():
    current = [NOW]
    stream = BinancePublicStream(
        ["BTCUSDT"],
        tiny_settings(stale_after_seconds=1),
        clock=lambda: current[0],
        connect_factory=lambda *args, **kwargs: Context(FakeSocket([trade()])),
    )
    iterator = stream.events()
    await anext(iterator)
    current[0] += timedelta(seconds=2)
    assert stream.health.stale
    assert stream.health.connection == FeedConnection.CONNECTED
    assert stream.health.last_message_timestamp == NOW
    await iterator.aclose()
    assert stream.health.connection == FeedConnection.STOPPED


async def test_confirmed_heartbeat_records_pong_without_inventing_market_messages():
    current = [NOW]
    stream = BinancePublicStream(
        ["BTCUSDT"],
        tiny_settings(),
        clock=lambda: current[0],
        connect_factory=lambda *args, **kwargs: Context(FakeSocket([])),
    )
    stop = asyncio.Event()

    async def consume():
        async for _ in stream.events(stop):
            pass

    task = asyncio.create_task(consume())
    # Wait for the observed pong rather than assuming a busy CI host schedules
    # receive timeout, cleanup and heartbeat within a fixed 40 milliseconds.
    for _ in range(100):
        if stream.health.last_heartbeat_timestamp is not None:
            break
        await asyncio.sleep(0.01)
    assert stream.health.last_heartbeat_timestamp == NOW
    assert stream.health.last_heartbeat_check_timestamp == NOW
    assert stream.health.last_message_timestamp is None
    assert stream.health.stale
    stop.set()
    await asyncio.wait_for(task, 0.2)


def test_light_snapshot_and_health_do_not_copy_entire_candle_history():
    feed = LiveFeedState()
    feed.apply(event(kline(complete=True), received=NOW + timedelta(minutes=1)))
    assert feed.snapshot("BTCUSDT", NOW + timedelta(minutes=1), include_history=False)["closed_candles"] == {}
    assert feed.health(NOW + timedelta(minutes=1))["per_symbol_data_quality"]["BTCUSDT"]["1m"] == "AVAILABLE"


def test_unreliable_crossed_book_and_non_boolean_native_side_are_rejected():
    payload = ticker()
    payload["data"]["b"] = "200"
    with pytest.raises(ValueError, match="crossed"):
        event(payload)
    payload = trade()
    payload["data"]["m"] = "false"
    with pytest.raises(ValueError, match="boolean"):
        event(payload)
