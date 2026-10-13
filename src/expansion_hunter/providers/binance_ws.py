"""Read-only native Binance public streams with bounded reconnects and health.

Depth is the exchange's partial TOP-20 snapshot stream, not a synchronized full
diff-depth book. Its update IDs do not establish continuity of unseen levels.
"""

from __future__ import annotations

import asyncio
import json
import random
import ssl
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from ..live_models import (
    DepthLevel,
    FeedConnection,
    FeedHealth,
    LiveCandle,
    LiveFeedSettings,
    LiveMarketEvent,
    normalize_symbols,
)
from ..models import utcnow


def _time(milliseconds: int | str) -> datetime:
    return datetime.fromtimestamp(int(milliseconds) / 1000, tz=UTC)


def _levels(values: list[list[str]]) -> list[DepthLevel]:
    return [DepthLevel(price=float(pair[0]), quantity=float(pair[1])) for pair in values]


def decode_message(
    raw: str | bytes | dict[str, Any],
    received_at: datetime,
    processed_at: datetime,
    *,
    stream_epoch: int = 0,
) -> LiveMarketEvent | None:
    """Decode native values; malformed payloads raise rather than acquire zeros."""
    message = json.loads(raw) if isinstance(raw, str | bytes) else raw
    stream = message.get("stream", "")
    data = message.get("data", message)
    if not isinstance(data, dict):
        raise ValueError("expected an exchange object")
    symbol = str(data.get("s") or stream.split("@", 1)[0]).upper()
    event_type = data.get("e")
    common = {
        "symbol": symbol,
        "received_timestamp": received_at,
        "processed_timestamp": processed_at,
        "stream_epoch": stream_epoch,
    }
    if event_type == "aggTrade":
        if not isinstance(data["m"], bool):
            raise ValueError("native maker-side flag must be boolean")
        return LiveMarketEvent(
            **common,
            kind="TRADE",
            exchange_timestamp=_time(data["T"]),
            sequence_id=int(data["a"]),
            sequence_supported=True,
            price=float(data["p"]),
            quantity=float(data["q"]),
            buyer_is_maker=data["m"],
        )
    if event_type == "bookTicker" or ("b" in data and "a" in data and "u" in data):
        return LiveMarketEvent(
            **common,
            kind="BOOK_TICKER",
            sequence_id=int(data["u"]),
            # Spot bookTicker normally has no exchange event timestamp.
            exchange_timestamp=_time(data["E"]) if "E" in data else None,
            bid=DepthLevel(price=float(data["b"]), quantity=float(data["B"])),
            ask=DepthLevel(price=float(data["a"]), quantity=float(data["A"])),
        )
    if "lastUpdateId" in data and "bids" in data and "asks" in data:
        bids, asks = _levels(data["bids"]), _levels(data["asks"])
        if len(bids) > 20 or len(asks) > 20:
            raise ValueError("partial depth exceeds advertised TOP-20 scope")
        return LiveMarketEvent(
            **common,
            kind="DEPTH",
            sequence_id=int(data["lastUpdateId"]),
            exchange_timestamp=_time(data["E"]) if "E" in data else None,
            bids=bids,
            asks=asks,
            depth_scope="TOP_20_SNAPSHOT",
            sequence_supported=False,
        )
    if event_type == "kline":
        candle = data["k"]
        if not isinstance(candle["x"], bool):
            raise ValueError("native kline completion flag must be boolean")
        return LiveMarketEvent(
            **common,
            kind="CANDLE",
            exchange_timestamp=_time(data["E"]),
            candle=LiveCandle(
                timeframe="1D" if candle["i"] == "1d" else candle["i"],
                open_time=_time(candle["t"]),
                # Binance T is inclusive; use exclusive boundaries throughout the project.
                close_time=_time(candle["T"]) + timedelta(milliseconds=1),
                open=float(candle["o"]),
                high=float(candle["h"]),
                low=float(candle["l"]),
                close=float(candle["c"]),
                volume=float(candle["v"]),
                quote_volume=float(candle["q"]),
                taker_buy_volume=float(candle["V"]),
                taker_buy_quote_volume=float(candle["Q"]),
                trade_count=candle["n"],
                complete=candle["x"],
            ),
        )
    return None


class BinancePublicStream:
    def __init__(
        self,
        symbols: list[str] | set[str],
        settings: LiveFeedSettings | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
        connect_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings or LiveFeedSettings()
        self.clock = clock
        self._symbols = normalize_symbols(symbols, self.settings.max_symbols)
        self._generation = 0
        self._connect_factory = connect_factory
        self._health = FeedHealth(symbols=self._symbols)
        self._stop = asyncio.Event()
        self._sequences: dict[tuple[str, str], int] = {}

    @property
    def health(self) -> FeedHealth:
        health = self._health.model_copy(deep=True)
        timestamp = health.last_message_timestamp
        health.stale = (
            health.connection != FeedConnection.CONNECTED
            or timestamp is None
            or (self.clock() - timestamp).total_seconds() > self.settings.stale_after_seconds
        )
        return health

    def set_symbols(self, symbols: list[str] | set[str]) -> None:
        symbols = normalize_symbols(symbols, self.settings.max_symbols)
        if symbols != self._symbols:
            self._symbols = symbols
            self._health.symbols = symbols
            self._generation += 1

    def stop(self) -> None:
        self._stop.set()

    def stream_url(self, endpoint: str) -> str:
        streams = []
        for symbol in self._symbols:
            prefix = symbol.lower()
            if self.settings.stream_mode in {"FULL", "LIGHT"}:
                streams.extend([f"{prefix}@aggTrade", f"{prefix}@bookTicker"])
            if self.settings.stream_mode == "FULL":
                streams.append(f"{prefix}@depth20@100ms")
            streams.extend(f"{prefix}@kline_{'1d' if frame == '1D' else frame}" for frame in self.settings.timeframes)
        if len(streams) > 1024:
            raise ValueError("Binance permits at most 1024 streams per connection; split symbols into shards")
        return endpoint + "?" + urlencode({"streams": "/".join(streams)}, safe="/@")

    async def _pause(self, seconds: float, stop_event: asyncio.Event) -> None:
        own = asyncio.create_task(self._stop.wait())
        external = asyncio.create_task(stop_event.wait())
        try:
            await asyncio.wait([own, external], timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            own.cancel()
            external.cancel()
            await asyncio.gather(own, external, return_exceptions=True)

    async def _receive(self, websocket: Any, stop_event: asyncio.Event) -> Any | None:
        receiving = asyncio.create_task(websocket.recv())
        own = asyncio.create_task(self._stop.wait())
        external = asyncio.create_task(stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                [receiving, own, external],
                timeout=self.settings.heartbeat_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receiving in done:
                return receiving.result()
            self._health.last_heartbeat_check_timestamp = self.clock()
            return None
        finally:
            for task in (receiving, own, external):
                if not task.done():
                    task.cancel()
            await asyncio.gather(receiving, own, external, return_exceptions=True)

    async def _heartbeat(self, websocket: Any, stop_event: asyncio.Event) -> None:
        pong = await websocket.ping()
        own = asyncio.create_task(self._stop.wait())
        external = asyncio.create_task(stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                [pong, own, external],
                timeout=self.settings.ping_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if pong in done:
                pong.result()
                self._health.last_heartbeat_timestamp = self.clock()
            elif not self._stop.is_set() and not stop_event.is_set():
                raise TimeoutError("public WebSocket heartbeat timed out")
        finally:
            own.cancel()
            external.cancel()
            await asyncio.gather(own, external, return_exceptions=True)

    async def events(self, stop_event: asyncio.Event | None = None) -> AsyncIterator[LiveMarketEvent]:
        """Reconnect independently of broad discovery; backpressure is bounded.

        Library ping/pong performs connection heartbeats. A receive timeout still
        marks feed stale when no native messages arrive; pongs never fabricate data.
        """
        stop_event = stop_event or asyncio.Event()
        if self._connect_factory is None:
            from websockets.asyncio.client import connect

            self._connect_factory = connect
        failures = 0
        endpoint_index = 0
        try:
            while not self._stop.is_set() and not stop_event.is_set():
                if not self._symbols:
                    self._health.connection = FeedConnection.DISCONNECTED
                    await self._pause(self.settings.heartbeat_seconds, stop_event)
                    continue
                generation = self._generation
                endpoint = self.settings.websocket_urls[endpoint_index % len(self.settings.websocket_urls)]
                self._health.endpoint = endpoint
                self._health.connection = (
                    FeedConnection.RECONNECTING if failures else FeedConnection.CONNECTING
                )
                connected_at = None
                try:
                    proxy = (
                        self.settings.proxy
                        if self.settings.proxy
                        else (True if self.settings.respect_proxy_environment else None)
                    )
                    async with self._connect_factory(
                        self.stream_url(endpoint),
                        ssl=ssl.create_default_context(),
                        proxy=proxy,
                        open_timeout=self.settings.connect_timeout_seconds,
                        ping_interval=self.settings.ping_interval_seconds,
                        ping_timeout=self.settings.ping_timeout_seconds,
                        close_timeout=3,
                        max_size=self.settings.max_message_bytes,
                        max_queue=self.settings.receive_queue_size,
                    ) as websocket:
                        connected_at = self.clock()
                        self._health.connection = FeedConnection.CONNECTED
                        self._health.connected_at = connected_at
                        self._health.stream_epoch += 1
                        self._health.last_message_timestamp = None
                        self._health.last_error = None
                        self._sequences.clear()
                        while (
                            not self._stop.is_set()
                            and not stop_event.is_set()
                            and generation == self._generation
                        ):
                            raw = await self._receive(websocket, stop_event)
                            if raw is None:
                                if (
                                    not self._stop.is_set()
                                    and not stop_event.is_set()
                                    and generation == self._generation
                                ):
                                    await self._heartbeat(websocket, stop_event)
                                continue
                            received = self.clock()
                            self._health.messages_received += 1
                            try:
                                event = decode_message(
                                    raw, received, self.clock(), stream_epoch=self._health.stream_epoch
                                )
                            except (ValueError, TypeError, KeyError, IndexError, OverflowError):
                                self._health.malformed_messages += 1
                                self._health.last_error = "Malformed or non-causal native message"
                                continue
                            if event is None or event.symbol not in self._symbols:
                                self._health.ignored_messages += 1
                                continue
                            key = (event.symbol, event.kind)
                            if event.sequence_id is not None:
                                previous = self._sequences.get(key)
                                if previous is not None and event.sequence_id <= previous:
                                    self._health.ignored_messages += 1
                                    continue
                                if (
                                    event.sequence_supported
                                    and previous is not None
                                    and event.sequence_id != previous + 1
                                ):
                                    event.sequence_gap = True
                                    self._health.sequence_gaps += 1
                                    if "AGGREGATE_TRADE_GAP" not in self._health.issues:
                                        self._health.issues.append("AGGREGATE_TRADE_GAP")
                                self._sequences[key] = event.sequence_id
                            self._health.last_message_timestamp = received
                            self._health.last_exchange_timestamp = event.exchange_timestamp
                            self._health.last_processing_timestamp = event.processed_timestamp
                            self._health.last_feed_latency_ms = event.feed_latency_ms
                            self._health.last_processing_latency_ms = event.processing_latency_ms
                            yield event
                        if generation != self._generation:
                            # Watchlist changes reconnect without penalizing provider health.
                            failures = 0
                            continue
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Proxy credentials, response headers and private values are never logged.
                    self._health.last_error = type(error).__name__
                    self._health.connection = FeedConnection.DISCONNECTED
                    self._health.reconnect_count += 1
                    if (
                        connected_at
                        and (self.clock() - connected_at).total_seconds()
                        >= self.settings.stable_reset_seconds
                    ):
                        failures = 0
                    failures += 1
                    endpoint_index += 1
                    wait = min(
                        self.settings.reconnect_max_seconds,
                        self.settings.reconnect_initial_seconds * 2 ** min(failures - 1, 20),
                    )
                    wait *= 1 + random.uniform(
                        -self.settings.reconnect_jitter_fraction, self.settings.reconnect_jitter_fraction
                    )
                    await self._pause(min(wait, self.settings.reconnect_max_seconds), stop_event)
        finally:
            self._health.connection = FeedConnection.STOPPED
