"""Synthetic native-protocol fixtures, never presented as public smoke data."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.live_models import FeedConnection, LiveFeedSettings
from expansion_hunter.providers.universe_stream import (
    MAX_UNIVERSE_MESSAGE_BYTES,
    BinanceUniverseStream,
    decode_universe_message,
)

NOW = datetime(2026, 10, 6, 1, tzinfo=UTC)


def native(symbol="BTCUSDT", *, at=NOW, **changes):
    return {"e": "24hrMiniTicker", "E": int(at.timestamp() * 1000), "s": symbol,
            "c": "100", "o": "99", "h": "101", "l": "98", "v": "20", "q": "1990", **changes}


def settings(**changes):
    return LiveFeedSettings(reconnect_initial_seconds=0.01, reconnect_max_seconds=0.02,
                            reconnect_jitter_fraction=0, heartbeat_seconds=0.01, **changes)


class Socket:
    def __init__(self, rows):
        self.rows = list(rows)
        self.pings = 0

    async def recv(self):
        if self.rows:
            value = self.rows.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        await asyncio.Future()

    async def ping(self):
        self.pings += 1
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future


class Context:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        if isinstance(self.socket, Exception):
            raise self.socket
        return self.socket

    async def __aexit__(self, *args):
        return False


@pytest.mark.parametrize("wrapper", ["array", "combined", "json", "bytes"])
def test_native_rolling_fields_timestamps_immutability_and_no_candle_flow(wrapper):
    payload = [native()]
    if wrapper == "combined":
        payload = {"stream": "!miniTicker@arr", "data": payload}
    elif wrapper in {"json", "bytes"}:
        payload = json.dumps(payload)
        if wrapper == "bytes":
            payload = payload.encode()
    item = decode_universe_message(payload, NOW + timedelta(milliseconds=25), NOW + timedelta(milliseconds=27))[0]
    assert item.price == 100 and item.open24h == 99 and item.high24h == 101 and item.low24h == 98
    assert item.basevolume24h == 20 and item.quotevolume24h == 1990
    assert item.source == "binance-public-miniTicker" and item.quality == "NATIVE"
    assert not item.sequence_supported
    assert item.feed_latency_ms == 25 and item.processing_latency_ms == 2
    assert item.event_timestamp == NOW
    assert not hasattr(item, "candle") and not hasattr(item, "cvd") and not hasattr(item, "order_book")
    with pytest.raises(ValueError, match="frozen"):
        item.price = 1


@pytest.mark.parametrize("changes", [
    {"c": "0"}, {"c": "nan"}, {"v": "-1"}, {"q": "-1"}, {"v": "0"},
    {"h": "90"}, {"l": "110"}, {"s": "not a symbol"}, {"s": "btcusdt"},
    {"e": "aggTrade"}, {"E": True}, {"E": -1}, {"E": int((NOW + timedelta(seconds=1)).timestamp() * 1000)},
    {"v": True}, {"q": None},
])
def test_invalid_native_values_are_rejected_without_fake_replacements(changes):
    with pytest.raises((ValueError, TypeError)):
        decode_universe_message([native(**changes)], NOW, NOW)


def test_missing_fields_wrong_array_duplicate_symbol_and_processing_clock_fail():
    missing = native()
    del missing["q"]
    with pytest.raises(KeyError):
        decode_universe_message([missing], NOW, NOW)
    for payload in (native(), {"data": {}}, {"stream": "!ticker@arr", "data": [native()]}, [native(), native()]):
        with pytest.raises(ValueError):
            decode_universe_message(payload, NOW, NOW)
    with pytest.raises(ValueError, match="timestamps"):
        decode_universe_message([native()], NOW, NOW - timedelta(milliseconds=1))
    with pytest.raises(ValueError, match="bounded size"):
        decode_universe_message(" " * (MAX_UNIVERSE_MESSAGE_BYTES + 1), NOW, NOW)


def test_global_public_subscription_is_one_stream_and_has_no_private_endpoints():
    stream = BinanceUniverseStream()
    url = stream.stream_url(stream.settings.websocket_urls[0])
    assert url == "wss://data-stream.binance.vision/stream?streams=!miniTicker@arr"
    assert "listenKey" not in url and "signature" not in url and "@kline" not in url
    assert stream.health.symbols == [] and stream.health.sequence_gaps == 0
    assert "NATIVE_STREAM_HAS_NO_SEQUENCE_IDS" in stream.health.issues
    with pytest.raises(ValueError, match="Global"):
        stream.set_symbols(["BTCUSDT"])


async def test_row_failure_isolation_duplicates_out_of_order_and_changed_symbols_only():
    socket = Socket([
        "invalid JSON",
        [native(), native("ETHUSDT", c="0")],
        [native(), native(at=NOW - timedelta(milliseconds=1)), native("ETHUSDT")],
    ])
    stream = BinanceUniverseStream(settings(), clock=lambda: NOW,
                                   connect_factory=lambda *args, **kwargs: Context(socket))
    emitted = []
    async for batch in stream.events():
        emitted.append([item.symbol for item in batch])
        if len(emitted) == 2:
            stream.stop()
    assert emitted == [["BTCUSDT"], ["ETHUSDT"]]
    assert stream.health.malformed_messages == 2 and stream.malformed_rows == 1
    assert stream.health.ignored_messages == 2 and stream.health.sequence_gaps == 0
    assert stream.health.connection == FeedConnection.STOPPED


async def test_reconnect_rotates_native_hosts_retains_duplicate_guard_and_masks_errors():
    calls = []
    sockets = [ConnectionError("private proxy credential must not leak"), Socket([[native()]])]

    def connector(url, **kwargs):
        calls.append((url, kwargs))
        return Context(sockets.pop(0))

    stream = BinanceUniverseStream(settings(), clock=lambda: NOW, connect_factory=connector)
    stop = asyncio.Event()
    async for batch in stream.events(stop):
        assert batch[0].symbol == "BTCUSDT"
        assert stream.health.connection == FeedConnection.CONNECTED and not stream.health.stale
        stop.set()
    assert stream.health.reconnect_count == 1
    assert "data-stream.binance.vision" in calls[0][0] and "stream.binance.com" in calls[1][0]
    assert calls[0][1]["max_size"] <= MAX_UNIVERSE_MESSAGE_BYTES
    assert calls[0][1]["proxy"] is True
    assert "private" not in json.dumps(stream.health.model_dump(mode="json"))


async def test_received_delayed_exchange_batch_remains_stale():
    stream = BinanceUniverseStream(settings(stale_after_seconds=1), clock=lambda: NOW,
                                   connect_factory=lambda *args, **kwargs: Context(Socket([[native(at=NOW - timedelta(seconds=3))]])))
    iterator = stream.events()
    batch = await anext(iterator)
    assert batch[0].feed_latency_ms == 3000
    assert stream.health.stale
    assert stream.health.last_message_timestamp == NOW
    await iterator.aclose()


async def test_stale_no_message_heartbeat_and_blocked_receive_stop_are_honest():
    socket = Socket([])
    stream = BinanceUniverseStream(settings(), clock=lambda: NOW,
                                   connect_factory=lambda *args, **kwargs: Context(socket))
    stop = asyncio.Event()

    async def consume():
        async for _ in stream.events(stop):
            pytest.fail("Heartbeat must not invent market data")

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.04)
    assert socket.pings > 0 and stream.health.last_heartbeat_timestamp == NOW
    assert stream.health.last_message_timestamp is None and stream.health.stale
    stop.set()
    await asyncio.wait_for(task, timeout=0.3)
    assert stream.health.connection == FeedConnection.STOPPED


async def test_reconnect_never_replaces_newer_received_symbol_with_old_native_time():
    sockets = [Socket([[native()], ConnectionError("disconnect")]),
               Socket([[native(at=NOW - timedelta(seconds=1))], [native("ETHUSDT")]])]
    stream = BinanceUniverseStream(settings(), clock=lambda: NOW,
                                   connect_factory=lambda *args, **kwargs: Context(sockets.pop(0)))
    batches = []
    async for batch in stream.events():
        batches.append([item.symbol for item in batch])
        if len(batches) == 2:
            stream.stop()
    assert batches == [["BTCUSDT"], ["ETHUSDT"]]
    assert stream.health.ignored_messages == 2 and stream.health.reconnect_count == 1


async def test_stop_interrupts_backoff_without_next_connection():
    calls = []

    def fail(url, **kwargs):
        calls.append(url)
        return Context(ConnectionError("fixture failure"))

    stream = BinanceUniverseStream(LiveFeedSettings(reconnect_initial_seconds=30, reconnect_max_seconds=30),
                                   clock=lambda: NOW, connect_factory=fail)

    async def consume():
        async for _ in stream.events():
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    stream.stop()
    await asyncio.wait_for(task, timeout=0.3)
    assert len(calls) == 1 and stream.health.connection == FeedConnection.STOPPED


def test_native_batch_can_cover_more_than_fast_watch_capacity():
    result = decode_universe_message([native(f"ASSET{index:04}USDT") for index in range(1000)], NOW, NOW)
    assert len(result) == 1000
    assert len({row.symbol for row in result}) == 1000
