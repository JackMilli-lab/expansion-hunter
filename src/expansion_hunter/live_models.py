"""Public feed contracts. Open candles are never presented as completed history."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .models import TIMEFRAMES, Candle, Model


class FeedConnection(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    STOPPED = "STOPPED"


class LiveFeedSettings(Model):
    stream_mode: str = Field(default="FULL", pattern="^(FULL|LIGHT|CANDLES)$")
    websocket_urls: list[str] = Field(
        default_factory=lambda: [
            "wss://data-stream.binance.vision/stream",
            "wss://stream.binance.com:9443/stream",
        ]
    )
    timeframes: list[str] = Field(default_factory=lambda: ["1m", "5m", "15m"])
    max_symbols: int = Field(default=50, ge=1, le=150)
    warmup_concurrency: int = Field(default=2, ge=1, le=10)
    analysis_concurrency: int = Field(default=1, ge=1, le=4)
    persistence_queue_size: int = Field(default=2048, ge=8, le=100000)
    persistence_retry_seconds: float = Field(default=0.25, gt=0, le=10)
    persistence_busy_timeout_ms: int = Field(default=250, ge=1, le=5000)
    persistence_shutdown_seconds: float = Field(default=30, ge=1, le=180)
    chart_publish_seconds: float = Field(default=1, ge=0.1, le=60)
    ingestion_yield_events: int = Field(default=64, ge=1, le=4096)
    ingestion_yield_seconds: float = Field(default=0.005, ge=0.0001, le=0.1)
    benchmark_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    active_setup_retention_seconds: float = Field(default=1200, ge=0, le=86400)
    candle_repair_concurrency: int = Field(default=1, ge=1, le=4)
    candle_repair_retry_seconds: float = Field(default=10, ge=1, le=300)
    candle_repair_max_bars: int = Field(default=120, ge=32, le=120)
    candle_repair_timeout_seconds: float = Field(default=15, ge=1, le=60)
    candle_capacity: int = Field(default=512, ge=32, le=10000)
    stale_after_seconds: float = Field(default=15, gt=0)
    heartbeat_seconds: float = Field(default=10, gt=0)
    ping_interval_seconds: float = Field(default=20, gt=0)
    ping_timeout_seconds: float = Field(default=20, gt=0)
    connect_timeout_seconds: float = Field(default=10, gt=0)
    reconnect_initial_seconds: float = Field(default=1, ge=0.01)
    reconnect_max_seconds: float = Field(default=60, ge=0.01)
    reconnect_jitter_fraction: float = Field(default=0.2, ge=0, le=1)
    stable_reset_seconds: float = Field(default=30, ge=0)
    max_message_bytes: int = Field(default=2097152, ge=1024)
    receive_queue_size: int = Field(default=64, ge=1, le=4096)
    proxy: str | None = None
    respect_proxy_environment: bool = True

    @field_validator("websocket_urls")
    @classmethod
    def public_endpoints(cls, values: list[str]) -> list[str]:
        if not values:
            raise ValueError("at least one public WebSocket endpoint is required")
        for value in values:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "wss"
                or parsed.hostname
                not in {
                    "data-stream.binance.vision",
                    "stream.binance.com",
                    "stream.binance.us",
                }
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path != "/stream"
                or parsed.port not in {None, 443, 9443}
            ):
                raise ValueError("only approved public Binance combined-stream endpoints are supported")
        return values

    @field_validator("timeframes")
    @classmethod
    def supported_frames(cls, values: list[str]) -> list[str]:
        if not values or len(values) != len(set(values)) or set(values) - {"1m", "5m", "15m", "1h", "4h", "1D"}:
            raise ValueError("live feed supports unique 1m/5m/15m/1h/4h/1D timeframes")
        return values

    @field_validator("benchmark_symbols")
    @classmethod
    def public_benchmarks(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or set(values) - {"BTCUSDT", "ETHUSDT"}:
            raise ValueError("benchmark context supports unique BTCUSDT/ETHUSDT public spot symbols")
        return values

    @model_validator(mode="after")
    def backoff_order(self) -> LiveFeedSettings:
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            raise ValueError("maximum reconnect delay must exceed the initial delay")
        return self


def normalize_symbols(symbols: list[str] | set[str], maximum: int = 150) -> list[str]:
    result = sorted({symbol.upper() for symbol in symbols})
    if len(result) > maximum or any(not re.fullmatch(r"[A-Z0-9]{5,24}", symbol) for symbol in result):
        raise ValueError("invalid public symbols or configured monitoring capacity exceeded")
    return result


class DepthLevel(Model):
    price: float = Field(gt=0)
    quantity: float = Field(ge=0)


class LiveCandle(Model):
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    quote_volume: float | None = Field(default=None, ge=0)
    taker_buy_volume: float | None = Field(default=None, ge=0)
    taker_buy_quote_volume: float | None = Field(default=None, ge=0)
    trade_count: int | None = Field(default=None, ge=0, strict=True)
    complete: bool = False

    @model_validator(mode="after")
    def bounds(self) -> LiveCandle:
        if self.timeframe not in {"1m", "5m", "15m", "1h", "4h", "1D"}:
            raise ValueError("unsupported live candle timeframe")
        if (self.close_time - self.open_time).total_seconds() != TIMEFRAMES[self.timeframe]:
            raise ValueError("candle duration does not match timeframe")
        if int(self.open_time.timestamp()) % TIMEFRAMES[self.timeframe] or self.open_time.microsecond:
            raise ValueError("candle is not aligned to its exchange boundary")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("invalid OHLC bounds")
        if self.taker_buy_volume is not None and self.taker_buy_volume > self.volume:
            raise ValueError("taker buy volume exceeds total volume")
        if self.taker_buy_quote_volume is not None and self.quote_volume is not None:
            if self.taker_buy_quote_volume > self.quote_volume:
                raise ValueError("taker buy quote volume exceeds total quote volume")
        if self.quote_volume is not None:
            if not self.volume and self.quote_volume:
                raise ValueError("quote volume requires base volume")
            if self.volume and not self.low * (1 - 1e-6) <= self.quote_volume / self.volume <= self.high * (
                1 + 1e-6
            ):
                raise ValueError("traded average is outside candle range")
        return self

    def as_closed(self, received_at: datetime, source: str = "binance-public-ws") -> Candle:
        if not self.complete:
            raise ValueError("an open candle cannot become completed history")
        return Candle(
            open_time=self.open_time,
            close_time=self.close_time,
            available_at=received_at,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            quote_volume=self.quote_volume,
            taker_buy_volume=self.taker_buy_volume,
            trade_count=self.trade_count,
            source=source,
        )


class LiveMarketEvent(Model):
    symbol: str
    kind: Literal["TRADE", "BOOK_TICKER", "DEPTH", "CANDLE"]
    exchange_timestamp: datetime | None = None
    received_timestamp: datetime
    processed_timestamp: datetime
    source: str = "binance-public-ws"
    stream_epoch: int = Field(default=0, ge=0)
    sequence_id: int | None = Field(default=None, ge=0)
    sequence_gap: bool = False
    sequence_supported: bool = False
    price: float | None = Field(default=None, gt=0)
    quantity: float | None = Field(default=None, ge=0)
    buyer_is_maker: bool | None = None
    bid: DepthLevel | None = None
    ask: DepthLevel | None = None
    bids: list[DepthLevel] = Field(default_factory=list)
    asks: list[DepthLevel] = Field(default_factory=list)
    depth_scope: Literal["TOP_20_SNAPSHOT"] | None = None
    candle: LiveCandle | None = None

    @model_validator(mode="after")
    def payload_contract(self) -> LiveMarketEvent:
        normalize_symbols([self.symbol])
        if self.processed_timestamp < self.received_timestamp:
            raise ValueError("processing timestamp precedes local receipt")
        if self.kind == "TRADE" and (
            self.price is None or self.quantity is None or self.buyer_is_maker is None
        ):
            raise ValueError("trade event requires native price, quantity and maker side")
        if self.kind == "BOOK_TICKER" and (self.bid is None or self.ask is None):
            raise ValueError("book ticker requires bid and ask")
        if self.kind == "BOOK_TICKER" and self.bid and self.ask and self.bid.price > self.ask.price:
            raise ValueError("crossed best bid/ask")
        if self.kind == "DEPTH" and self.depth_scope is None:
            raise ValueError("depth snapshot scope must be explicit")
        if self.kind == "DEPTH":
            bid_prices = [level.price for level in self.bids]
            ask_prices = [level.price for level in self.asks]
            if (
                len(set(bid_prices)) != len(bid_prices)
                or len(set(ask_prices)) != len(ask_prices)
                or bid_prices != sorted(bid_prices, reverse=True)
                or ask_prices != sorted(ask_prices)
            ):
                raise ValueError("depth levels must be unique and exchange ordered")
            if bid_prices and ask_prices and bid_prices[0] > ask_prices[0]:
                raise ValueError("crossed partial depth snapshot")
        if self.kind == "CANDLE":
            if self.candle is None:
                raise ValueError("candle event requires a candle")
            if self.received_timestamp < self.candle.open_time:
                raise ValueError("candle was received before its open boundary")
            if self.candle.complete and self.received_timestamp < self.candle.close_time:
                raise ValueError("completed candle was received before its close boundary")
        return self

    @property
    def feed_latency_ms(self) -> float | None:
        if self.exchange_timestamp is None:
            return None
        return (self.received_timestamp - self.exchange_timestamp).total_seconds() * 1000

    @property
    def processing_latency_ms(self) -> float:
        return (self.processed_timestamp - self.received_timestamp).total_seconds() * 1000


class FeedHealth(Model):
    provider: str = "binance-public-ws"
    connection: FeedConnection = FeedConnection.DISCONNECTED
    endpoint: str | None = None
    symbols: list[str] = Field(default_factory=list)
    stream_epoch: int = 0
    connected_at: datetime | None = None
    last_message_timestamp: datetime | None = None
    last_exchange_timestamp: datetime | None = None
    last_processing_timestamp: datetime | None = None
    last_heartbeat_timestamp: datetime | None = None
    last_heartbeat_check_timestamp: datetime | None = None
    stale: bool = True
    reconnect_count: int = 0
    messages_received: int = 0
    malformed_messages: int = 0
    ignored_messages: int = 0
    sequence_gaps: int = 0
    last_feed_latency_ms: float | None = None
    last_processing_latency_ms: float | None = None
    last_error: str | None = None
    issues: list[str] = Field(default_factory=list)
