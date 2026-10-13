"""Native, read-only Binance global mini-ticker observation stream.

The exchange emits changed symbols approximately once per second, not a fresh
observation of every asset each second. Rolling 24h values are not 1m candles,
trade flow, book liquidity or sequence-complete history.
"""

from __future__ import annotations

import asyncio
import json
import random
import ssl
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlencode

from pydantic import ConfigDict, Field, model_validator

from ..live_models import FeedConnection, FeedHealth, LiveFeedSettings, normalize_symbols
from ..models import Model, aware, utcnow
from .binance_ws import BinancePublicStream, _time

MAX_UNIVERSE_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_BATCH_SYMBOLS = 10000


class UniverseTicker(Model):
    model_config = ConfigDict(frozen=True)

    symbol: str
    price: float = Field(gt=0)
    open24h: float = Field(gt=0)
    high24h: float = Field(gt=0)
    low24h: float = Field(gt=0)
    basevolume24h: float = Field(ge=0)
    quotevolume24h: float = Field(ge=0)
    event_timestamp: datetime
    received_timestamp: datetime
    processing_timestamp: datetime
    source: Literal["binance-public-miniTicker"] = "binance-public-miniTicker"
    quality: Literal["NATIVE"] = "NATIVE"
    sequence_supported: Literal[False] = False

    @model_validator(mode="after")
    def native_contract(self) -> UniverseTicker:
        normalize_symbols([self.symbol])
        if self.symbol != self.symbol.upper():
            raise ValueError("Native mini-ticker symbol must be uppercase")
        if not self.low24h <= min(self.open24h, self.price) <= max(self.open24h, self.price) <= self.high24h:
            raise ValueError("Native rolling 24h price bounds are inconsistent")
        if not self.event_timestamp <= self.received_timestamp <= self.processing_timestamp:
            raise ValueError("Native mini-ticker timestamps are noncausal or the local clock is behind")
        if not self.basevolume24h and self.quotevolume24h:
            raise ValueError("Native quote volume requires base volume")
        return self

    @property
    def feed_latency_ms(self) -> float:
        return (self.received_timestamp - self.event_timestamp).total_seconds() * 1000

    @property
    def processing_latency_ms(self) -> float:
        return (self.processing_timestamp - self.received_timestamp).total_seconds() * 1000


def _rows(raw: str | bytes | list | dict) -> list:
    if isinstance(raw, str | bytes):
        size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
        if size > MAX_UNIVERSE_MESSAGE_BYTES:
            raise ValueError("Global mini-ticker message exceeds bounded size")
        payload = json.loads(raw)
    else:
        payload = raw
    if isinstance(payload, dict):
        if payload.get("stream", "!miniTicker@arr") != "!miniTicker@arr":
            raise ValueError("Unexpected combined stream")
        payload = payload.get("data")
    if not isinstance(payload, list) or len(payload) > MAX_BATCH_SYMBOLS:
        raise ValueError("Expected a bounded native mini-ticker array")
    return payload


def _ticker(row: dict, received_at: datetime, processed_at: datetime) -> UniverseTicker:
    if not isinstance(row, dict) or row.get("e") != "24hrMiniTicker":
        raise ValueError("Expected a native 24hrMiniTicker object")
    timestamp = row["E"]
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
        raise ValueError("Native event time must be positive epoch milliseconds")
    numbers = {}
    for name, native in (("price", "c"), ("open24h", "o"), ("high24h", "h"),
                         ("low24h", "l"), ("basevolume24h", "v"), ("quotevolume24h", "q")):
        value = row[native]
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise ValueError("Native numeric mini-ticker value is malformed")
        numbers[name] = float(value)
    return UniverseTicker(symbol=row["s"], **numbers, event_timestamp=_time(timestamp),
                          received_timestamp=received_at, processing_timestamp=processed_at)


def decode_universe_message(raw: str | bytes | list | dict, received_at: datetime,
                            processed_at: datetime) -> list[UniverseTicker]:
    """Strict decoder for native arrays; no invented missing fields or clocks."""
    values = [_ticker(row, received_at, processed_at) for row in _rows(raw)]
    if len({value.symbol for value in values}) != len(values):
        raise ValueError("Duplicate symbols in one native mini-ticker message")
    return values


class BinanceUniverseStream(BinancePublicStream):
    """Reuse public transport cancellation/heartbeat primitives for one stream."""

    def __init__(self, settings: LiveFeedSettings | None = None, *,
                 clock: Callable[[], datetime] = utcnow,
                 connect_factory: Callable[..., Any] | None = None) -> None:
        super().__init__([], settings or LiveFeedSettings(max_message_bytes=MAX_UNIVERSE_MESSAGE_BYTES),
                         clock=clock, connect_factory=connect_factory)
        self._health = FeedHealth(provider="binance-public-miniTicker", issues=[
            "NATIVE_STREAM_HAS_NO_SEQUENCE_IDS",
            "CHANGED_SYMBOLS_ONLY; unchanged assets retain their actual observation age",
            "ROLLING_24H_TICKER; not a candle/order-flow/order-book feed",
        ])
        self._last_event_times: dict[str, datetime] = {}
        self.malformed_rows = 0

    @property
    def health(self) -> FeedHealth:
        report = super().health
        if report.last_exchange_timestamp is None or (
            aware(self.clock()) - report.last_exchange_timestamp
        ).total_seconds() > self.settings.stale_after_seconds:
            report.stale = True
        return report

    def stream_url(self, endpoint: str) -> str:
        return endpoint + "?" + urlencode({"streams": "!miniTicker@arr"}, safe="!/@")

    def set_symbols(self, symbols) -> None:
        raise ValueError("Global mini-ticker subscriptions are not per-symbol; filter the native batch downstream")

    async def events(self, stop_event: asyncio.Event | None = None) -> AsyncIterator[list[UniverseTicker]]:
        stop_event = stop_event or asyncio.Event()
        if self._connect_factory is None:
            from websockets.asyncio.client import connect

            self._connect_factory = connect
        failures, endpoint_index = 0, 0
        try:
            while not self._stop.is_set() and not stop_event.is_set():
                endpoint = self.settings.websocket_urls[endpoint_index % len(self.settings.websocket_urls)]
                self._health.endpoint = endpoint
                self._health.connection = FeedConnection.RECONNECTING if failures else FeedConnection.CONNECTING
                connected_at = None
                try:
                    proxy = self.settings.proxy if self.settings.proxy else (
                        True if self.settings.respect_proxy_environment else None)
                    async with self._connect_factory(
                        self.stream_url(endpoint), ssl=ssl.create_default_context(), proxy=proxy,
                        open_timeout=self.settings.connect_timeout_seconds,
                        ping_interval=self.settings.ping_interval_seconds,
                        ping_timeout=self.settings.ping_timeout_seconds, close_timeout=3,
                        max_size=min(self.settings.max_message_bytes, MAX_UNIVERSE_MESSAGE_BYTES),
                        max_queue=self.settings.receive_queue_size,
                    ) as websocket:
                        connected_at = aware(self.clock())
                        self._health.connection = FeedConnection.CONNECTED
                        self._health.connected_at = connected_at
                        self._health.stream_epoch += 1
                        self._health.last_message_timestamp = None
                        self._health.last_error = None
                        while not self._stop.is_set() and not stop_event.is_set():
                            raw = await self._receive(websocket, stop_event)
                            if raw is None:
                                if not self._stop.is_set() and not stop_event.is_set():
                                    await self._heartbeat(websocket, stop_event)
                                continue
                            received, processed = aware(self.clock()), aware(self.clock())
                            self._health.messages_received += 1
                            try:
                                rows = _rows(raw)
                            except (ValueError, TypeError, KeyError, OverflowError):
                                self._health.malformed_messages += 1
                                self._health.last_error = "Malformed bounded native mini-ticker array"
                                continue
                            batch, bad_rows = [], 0
                            for row in rows:
                                try:
                                    ticker = _ticker(row, received, processed)
                                except (ValueError, TypeError, KeyError, IndexError, OverflowError):
                                    bad_rows += 1
                                    continue
                                previous = self._last_event_times.get(ticker.symbol)
                                if previous is not None and ticker.event_timestamp <= previous:
                                    self._health.ignored_messages += 1
                                    continue
                                if previous is None and len(self._last_event_times) >= MAX_BATCH_SYMBOLS:
                                    bad_rows += 1
                                    continue
                                self._last_event_times[ticker.symbol] = ticker.event_timestamp
                                batch.append(ticker)
                            if bad_rows:
                                self.malformed_rows += bad_rows
                                self._health.malformed_messages += 1
                                self._health.last_error = "Malformed/noncausal native mini-ticker rows isolated"
                            if not batch:
                                self._health.ignored_messages += 1
                                continue
                            completed = aware(self.clock())
                            if completed < received:
                                raise ValueError("Local processing clock moved backwards")
                            batch = [ticker.model_copy(update={"processing_timestamp": completed}) for ticker in batch]
                            latest = max(batch, key=lambda item: item.event_timestamp)
                            self._health.last_message_timestamp = received
                            self._health.last_exchange_timestamp = latest.event_timestamp
                            self._health.last_processing_timestamp = completed
                            self._health.last_feed_latency_ms = latest.feed_latency_ms
                            self._health.last_processing_latency_ms = latest.processing_latency_ms
                            yield batch
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Exception URLs may carry proxy secrets; expose only class.
                    self._health.last_error = type(error).__name__
                    self._health.connection = FeedConnection.DISCONNECTED
                    self._health.reconnect_count += 1
                    if connected_at and (aware(self.clock()) - connected_at).total_seconds() >= self.settings.stable_reset_seconds:
                        failures = 0
                    failures += 1
                    endpoint_index += 1
                    wait = min(self.settings.reconnect_max_seconds,
                               self.settings.reconnect_initial_seconds * 2 ** min(failures - 1, 20))
                    wait *= 1 + random.uniform(-self.settings.reconnect_jitter_fraction,
                                              self.settings.reconnect_jitter_fraction)
                    await self._pause(min(wait, self.settings.reconnect_max_seconds), stop_event)
        finally:
            self._health.connection = FeedConnection.STOPPED
