"""Point-in-time Trade Alert V1 contracts; scores are uncalibrated hypotheses."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .models import Model
from .pre_ignition import PreIgnitionSettings
from .pre_move import PreMoveSettings

DEFAULT_WEIGHTS = {"tests": .15, "decay": .15, "compression": .15, "velocity": .10,
                   "acceleration": .05, "spot_volume": .15, "relative_strength": .10,
                   "distance": .10, "market_context": .05}


class TradeAlertSettings(Model):
    max_assets: int = Field(default=500, ge=1, le=500)
    min_quote_volume: float = Field(default=5_000_000, ge=0)
    symbols_per_connection: int = Field(default=100, ge=1, le=150)
    stream_mode: Literal["CANDLES", "LIGHT"] = "LIGHT"
    deep_watch_limit: int = Field(default=25, ge=0, le=50)
    deep_refresh_seconds: float = Field(default=60, ge=5, le=3600)
    pre_move: dict = Field(default_factory=lambda: PreMoveSettings().model_dump())
    pre_ignition: dict = Field(default_factory=lambda: PreIgnitionSettings().model_dump())
    legacy_pre_move_alerts: bool = True
    history_bars: int = Field(default=120, ge=36, le=1000)
    warmup_concurrency: int = Field(default=4, ge=1, le=10)
    warmup_retry_seconds: float = Field(default=30, ge=1)
    publish_seconds: float = Field(default=1, ge=.1, le=5)
    queue_size: int = Field(default=4096, ge=16, le=100000)
    excluded_bases: tuple[str, ...] = ("USDC", "USDT", "FDUSD", "TUSD", "USDP", "DAI", "BUSD",
        "USD1", "USDD", "USDE", "USDS", "RLUSD", "AEUR", "EUR", "EURI", "GUSD", "PAX")
    watch_score: float = Field(default=40, ge=0, le=100)
    alert_score: float = Field(default=65, ge=0, le=100)
    minimum_tests: int = Field(default=2, ge=1, le=20)
    minimum_structure: float = Field(default=.10, ge=0, le=1)
    minimum_taker_pressure: float = Field(default=.52, ge=.5, le=1)
    require_native_flow: bool = True
    proximity_atr: float = Field(default=.75, gt=0, le=5)
    breakout_buffer_atr: float = Field(default=.05, ge=0, le=1)
    breakout_buffer_ticks: int = Field(default=1, ge=0, le=20)
    late_atr: float = Field(default=1.5, gt=0)
    late_pct: float = Field(default=3, gt=0)
    alert_max_age_seconds: float = Field(default=120, ge=1, le=900)
    episode_seconds: float = Field(default=3600, ge=60)
    candle_stale_seconds: float = Field(default=90, ge=1)
    five_minute_stale_seconds: float = Field(default=360, ge=1)
    cooldown_seconds: float = Field(default=300, ge=0)
    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    horizon_seconds: tuple[int, ...] = (300, 900, 3600, 14400, 86400)
    cost_bps: float = Field(default=20, ge=0, description="Hypothetical round-trip fees+spread+slippage")
    expansion_pct: float = Field(default=5, gt=0)

    @model_validator(mode="after")
    def valid(self):
        if self.alert_score < self.watch_score:
            raise ValueError("alert threshold must not precede watch threshold")
        if set(self.weights) != set(DEFAULT_WEIGHTS) or any(v < 0 for v in self.weights.values()):
            raise ValueError("weights must contain the documented nonnegative components")
        if abs(sum(self.weights.values()) - 1) > 1e-9:
            raise ValueError("weights must sum to one")
        if any(h <= 0 for h in self.horizon_seconds):
            raise ValueError("outcome horizons must be positive")
        PreMoveSettings.model_validate(self.pre_move)
        PreIgnitionSettings.model_validate(self.pre_ignition)
        return self


class TradeAlertRecord(Model):
    event_id: str
    episode_id: str
    asset_id: str
    symbol: str
    phase: Literal["STRUCTURE_WATCH", "TRADE_ALERT", "BREAK_CONFIRMED", "INVALIDATED"]
    signal_state: str | None = None
    bias: Literal["LONG", "SHORT"]
    setup_type: Literal["CONTINUATION", "LONG_REVERSAL", "SHORT_REVERSAL"] = "CONTINUATION"
    event_timestamp: datetime
    received_at: datetime
    detected_at: datetime
    expires_at: datetime
    price: float = Field(gt=0)
    trigger_level: float = Field(gt=0)
    trigger_discovered_at: datetime
    distance_pct: float
    invalidation: float | None = Field(default=None, gt=0)
    targets: list[float] = Field(default_factory=list)
    score: float | None = Field(default=None, ge=0, le=100)
    components: dict = Field(default_factory=dict)
    metrics: dict = Field(default_factory=dict)
    data_quality: dict[str, str] = Field(default_factory=dict)
    btc_context: dict = Field(default_factory=dict)
    reason: str
    status: Literal["ACTIVE", "LATE", "MISSED", "INVALIDATED"] = "ACTIVE"
    risk: str = "UNCALIBRATED; observation only, SHORT is directional context, no spot short orders"
    detection_latency_ms: float = Field(ge=0)
    alert_latency_ms: float = Field(ge=0)
    processing_latency_ms: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def causal(self):
        if not self.event_timestamp <= self.received_at <= self.detected_at < self.expires_at:
            raise ValueError("alert timestamps are not causal")
        if self.trigger_discovered_at > self.detected_at:
            raise ValueError("trigger was not known at decision time")
        return self
