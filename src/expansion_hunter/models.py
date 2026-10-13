"""Versioned point-in-time contracts. All timestamps must be timezone aware.

observed_at = event time; available_at = when this system could first know it.
Neither current metadata nor revised data may enter a historical evaluation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1D": 86400, "1W": 604800}


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(UTC)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @field_validator("*", mode="after")
    @classmethod
    def aware_datetimes(cls, value):
        return aware(value) if isinstance(value, datetime) else value


class Status(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"
    UNRELIABLE = "UNRELIABLE"


class Metric(Model):
    value: float | None = None
    observed_at: datetime | None = None
    available_at: datetime | None = None
    source: str
    unit: str = ""
    status: Status = Status.UNAVAILABLE
    reason: str | None = None
    reliable: bool = True

    @field_validator("observed_at", "available_at")
    @classmethod
    def timestamps(cls, value: datetime | None) -> datetime | None:
        return aware(value) if value else None

    @model_validator(mode="after")
    def validate_available(self) -> Metric:
        if self.status == Status.AVAILABLE and (
            self.value is None or self.observed_at is None or self.available_at is None
        ):
            raise ValueError("available metric requires value, event time, and availability time")
        if self.observed_at and self.available_at and self.available_at < self.observed_at:
            raise ValueError("availability cannot precede observation")
        return self

    def usable(self, as_of: datetime, max_age_seconds: float = 86400) -> bool:
        return bool(
            self.status == Status.AVAILABLE
            and self.reliable
            and self.value is not None
            and self.observed_at
            and self.available_at
            and self.available_at <= as_of
            and 0 <= (as_of - self.observed_at).total_seconds() <= max_age_seconds
        )


class Candle(Model):
    open_time: datetime
    close_time: datetime
    available_at: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    quote_volume: float | None = Field(default=None, ge=0)
    taker_buy_volume: float | None = Field(default=None, ge=0)
    trade_count: int | None = Field(default=None, ge=0, strict=True)
    source: str

    @field_validator("open_time", "close_time", "available_at")
    @classmethod
    def timestamps(cls, value: datetime) -> datetime:
        return aware(value)

    @model_validator(mode="after")
    def validate_bar(self) -> Candle:
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("invalid OHLC bounds")
        if self.open_time >= self.close_time or self.available_at < self.close_time:
            raise ValueError("invalid candle event/availability times")
        if self.taker_buy_volume is not None and self.taker_buy_volume > self.volume:
            raise ValueError("taker buy base volume exceeds base volume")
        if self.quote_volume is not None:
            if self.volume == 0 and self.quote_volume != 0:
                raise ValueError("positive quote volume requires positive base volume")
            if self.volume > 0:
                traded_average = self.quote_volume / self.volume
                if not self.low * (1 - 1e-6) <= traded_average <= self.high * (1 + 1e-6):
                    raise ValueError("quote/base traded average is outside OHLC bounds")
        return self


class Asset(Model):
    asset_id: str
    symbol: str
    name: str
    rank: int | None = Field(default=None, ge=1)
    exchange_symbol: str | None = None
    quote_asset: str = "USDT"


class Catalyst(Model):
    category: Literal[
        "network_usage",
        "adoption",
        "scarcity",
        "institutional",
        "rwa",
        "inflows",
        "infrastructure",
        "partnership",
        "marketing",
        "social",
    ]
    description: str
    source_url: str
    observed_at: datetime
    available_at: datetime
    verified: bool = False
    quantitative: bool = False
    strength: float = Field(default=0, ge=0, le=1)

    @field_validator("observed_at", "available_at")
    @classmethod
    def timestamps(cls, value: datetime) -> datetime:
        return aware(value)

    @model_validator(mode="after")
    def causal(self) -> Catalyst:
        if self.available_at < self.observed_at:
            raise ValueError("catalyst availability precedes observation")
        return self


class AssetSnapshot(Model):
    schema_version: int = 1
    asset: Asset
    as_of: datetime
    candles: dict[str, list[Candle]] = Field(default_factory=dict)
    market: dict[str, Metric] = Field(default_factory=dict)
    derivatives: dict[str, Metric] = Field(default_factory=dict)
    catalysts: list[Catalyst] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)

    @field_validator("as_of")
    @classmethod
    def timestamp(cls, value: datetime) -> datetime:
        return aware(value)

    @field_validator("candles")
    @classmethod
    def timeframes(cls, value: dict[str, list[Candle]]) -> dict[str, list[Candle]]:
        if set(value) - set(TIMEFRAMES):
            raise ValueError("unsupported timeframe")
        return value


class AnchorReport(Model):
    kind: str
    anchor_time: datetime
    confirmed_at: datetime
    value: float | None
    distance_pct: float | None = None
    support: bool = False
    resistance: bool = False
    reclaim: bool = False
    rejection: bool = False
    accepted_closes: int = 0
    method: str = "OHLCV typical-price estimate"


class TechnicalSnapshot(Model):
    timeframe: str
    as_of: datetime
    bars: int
    metrics: dict[str, float | None] = Field(default_factory=dict)
    patterns: dict[str, bool | None] = Field(default_factory=dict)
    anchors: list[AnchorReport] = Field(default_factory=list)
    levels: dict[str, Any] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)


class Regime(StrEnum):
    RISK_ON = "RISK_ON"
    NEUTRAL = "NEUTRAL"
    RISK_OFF = "RISK_OFF"
    SHAKEOUT = "SHAKEOUT"
    BTC_EXPANSION = "BTC_EXPANSION"
    ALT_ROTATION = "ALT_ROTATION"
    ALT_EXPANSION = "ALT_EXPANSION"


class RegimeReport(Model):
    as_of: datetime | None = None
    available_at: datetime | None = None
    regime: Regime = Regime.NEUTRAL
    confidence: float = Field(default=0, ge=0, le=1)
    shakeout_score: float = Field(default=0, ge=0, le=100)
    risk_multiplier: float = Field(default=0, ge=0, le=1)
    metrics: dict[str, float | None] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def causal_regime(self):
        if self.as_of and self.available_at and self.available_at < self.as_of:
            raise ValueError("regime availability cannot precede its evaluation boundary")
        return self


class RuleEvidence(Model):
    rule: str
    archetype: str
    passed: bool | None
    weight: float = Field(ge=0)
    points: float = Field(ge=0)
    detail: str
    inputs: dict[str, float | bool | str | None] = Field(default_factory=dict)


class Stage(StrEnum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    COMPRESSION = "COMPRESSION"
    ACCUMULATION = "ACCUMULATION"
    IGNITION = "IGNITION"
    BREAKOUT = "BREAKOUT"
    RETEST = "RETEST"
    EXPANSION = "EXPANSION"
    NONE = "NONE"


class TradePlan(Model):
    action: Literal["WAIT", "WATCH", "ARMED", "ENTRY", "AVOID"] = "WAIT"
    entry: float | None = None
    invalidation: float | None = None
    target: float | None = None
    reward_risk: float | None = None
    risk_fraction: float = Field(default=0, ge=0, le=0.02)
    quantity: float | None = Field(default=None, ge=0)
    notional: float | None = Field(default=None, ge=0)
    reasons: list[str] = Field(default_factory=list)


class ScanResult(Model):
    schema_version: int = 1
    asset: Asset
    as_of: datetime
    stage: Stage
    expansion_score: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    archetypes: dict[str, float]
    evidence: list[RuleEvidence]
    technical: dict[str, TechnicalSnapshot]
    regime: RegimeReport
    trade: TradePlan
    market: dict[str, Metric] = Field(default_factory=dict)
    derivatives: dict[str, Metric] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class ScanBatch(Model):
    scan_id: str
    started_at: datetime
    as_of: datetime
    completed_at: datetime
    universe_size: int
    evaluated: int
    results: list[ScanResult]
    errors: dict[str, str] = Field(default_factory=dict)
    provider_issues: list[str] = Field(default_factory=list)
