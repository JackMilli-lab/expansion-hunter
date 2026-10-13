"""Serializable fast-watch contracts. Scores express uncalibrated evidence only."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from .models import Asset, Candle, Model


class FastStage(StrEnum):
    WATCH = "WATCH"
    PRE_IGNITION = "PRE_IGNITION"
    LOADED = "LOADED"
    BREAKOUT_PRESSURE = "BREAKOUT_PRESSURE"
    BREAKOUT_ATTEMPT = "BREAKOUT_ATTEMPT"
    BREAKOUT_CONFIRMED = "1M_BREAKOUT_CONFIRMED"
    BREAKDOWN_PRESSURE = "BREAKDOWN_PRESSURE"
    BREAKDOWN_ATTEMPT = "BREAKDOWN_ATTEMPT"
    BREAKDOWN_CONFIRMED = "1M_BREAKDOWN_CONFIRMED"
    LIQUIDITY_SWEEP = "LIQUIDITY_SWEEP"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    FAILED_BREAKDOWN = "FAILED_BREAKDOWN"
    ACCEPTED = "5M_ACCEPTED"
    ACCEPTED_DOWN = "5M_ACCEPTED_DOWN"
    EXPANSION_CONFIRMED = "15M_EXPANSION_CONFIRMED"
    EXPANSION_CONFIRMED_DOWN = "15M_EXPANSION_CONFIRMED_DOWN"
    COOLDOWN = "COOLDOWN"
    DEMOTED = "DEMOTED"


class FastSettings(Model):
    history_bars: int = Field(default=120, ge=32, le=1000)
    resistance_lookback: int = Field(default=36, ge=20, le=300)
    swing_left: int = Field(default=2, ge=1, le=10)
    swing_right: int = Field(default=2, ge=1, le=10)
    atr_period: int = Field(default=14, ge=5, le=50)
    zone_atr: float = Field(default=0.15, gt=0, le=1)
    proximity_atr: float = Field(default=0.75, gt=0, le=5)
    pressure_score: float = Field(default=60, ge=0, le=100)
    pre_ignition_score: float = Field(default=45, ge=0, le=100)
    loaded_score: float = Field(default=55, ge=0, le=100)
    minimum_tests: int = Field(default=2, ge=1, le=20)
    breakout_buffer_atr: float = Field(default=0.05, ge=0, le=1)
    breakout_buffer_ticks: int = Field(default=1, ge=0, le=20)
    failure_buffer_atr: float = Field(default=0.05, ge=0, le=1)
    resistance_change_atr: float = Field(default=0.5, gt=0, le=5)
    candle_stale_seconds: float = Field(default=90, gt=0)
    trade_stale_seconds: float = Field(default=15, gt=0)
    five_minute_stale_seconds: float = Field(default=360, gt=0)
    cooldown_seconds: float = Field(default=300, ge=0)
    late_candle_seconds: float = Field(default=90, ge=0)
    acceptance_closes: int = Field(default=1, ge=1, le=5)
    structural_closes: int = Field(default=1, ge=1, le=5)
    quality_rvol_moderate: float = Field(default=1.2, gt=0)
    quality_rvol_strong: float = Field(default=2, gt=0)
    quality_body_ratio: float = Field(default=0.5, ge=0, le=1)
    quality_close_location: float = Field(default=0.7, ge=0, le=1)
    short_enabled: bool = True
    direction_min_structure_atr: float = Field(default=0.10, ge=0, le=2)
    direction_score_margin: float = Field(default=5, ge=0, le=100)

    @model_validator(mode="after")
    def sufficient_history(self):
        if self.history_bars < self.resistance_lookback:
            raise ValueError("history must cover resistance lookback")
        return self


class FastResistance(Model):
    price: float = Field(gt=0)
    lower: float = Field(gt=0)
    upper: float = Field(gt=0)
    event_timestamp: datetime
    discovered_at: datetime
    atr: float = Field(gt=0)
    timeframe: str = "5m"
    kind: str
    tests: int = Field(ge=0)
    reason: str

    @model_validator(mode="after")
    def causal(self):
        if self.discovered_at < self.event_timestamp:
            raise ValueError("resistance discovery precedes event")
        if not self.lower <= self.price <= self.upper:
            raise ValueError("resistance outside its zone")
        return self


class FastEvent(Model):
    event_id: str
    asset_id: str
    symbol: str
    event_type: str
    previous_stage: FastStage
    market_event_timestamp: datetime
    candle_close_timestamp: datetime | None = None
    data_received_timestamp: datetime
    evaluation_timestamp: datetime
    alert_created_timestamp: datetime
    alert_delivered_timestamp: datetime | None = None
    detection_latency_ms: float = Field(ge=0)
    alert_latency_ms: float = Field(ge=0)
    processing_latency_ms: float = Field(ge=0)
    price: float = Field(gt=0)
    resistance: FastResistance | None = None
    support: FastResistance | None = None
    bias: Literal["LONG", "SHORT"] = "LONG"
    reason: str
    quality: str = "UNCALIBRATED"
    metrics: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    data_quality: dict[str, str] = Field(default_factory=dict)
    regime: str | None = None
    macro_context: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def causal(self):
        if not (
            self.market_event_timestamp
            <= self.data_received_timestamp
            <= self.evaluation_timestamp
            <= self.alert_created_timestamp
        ):
            raise ValueError("event timestamps are out of order")
        if self.alert_delivered_timestamp and self.alert_delivered_timestamp < self.alert_created_timestamp:
            raise ValueError("delivery precedes alert creation")
        return self


class FastState(Model):
    asset: Asset
    stage: FastStage = FastStage.WATCH
    resistance: FastResistance | None = None
    support: FastResistance | None = None
    bias: Literal["LONG", "SHORT"] = "LONG"
    bias_reason: str = "Legacy LONG context; SHORT requires completed structural evidence"
    pressure_metrics: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    trigger_metrics: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    candles: dict[str, list[Candle]] = Field(default_factory=dict)
    last_processed: dict[str, datetime] = Field(default_factory=dict)
    last_evaluation_at: datetime | None = None
    last_received_at: datetime | None = None
    last_trade_at: datetime | None = None
    price: float | None = Field(default=None, gt=0)
    promoted_at: datetime | None = None
    demoted_at: datetime | None = None
    confirmed_at: datetime | None = None
    accepted_at: datetime | None = None
    structural_at: datetime | None = None
    attempt_at: datetime | None = None
    cooldown_until: datetime | None = None
    maximum_excursion_pct: float | None = None
    accepted_closes: int = 0
    structural_closes: int = 0
    dedupe_keys: list[str] = Field(default_factory=list)
    stale: bool = True
    data_quality: dict[str, str] = Field(default_factory=dict)
    regime: str | None = None
    macro_context: dict = Field(default_factory=dict)

    @property
    def trigger_level(self) -> FastResistance | None:
        """Actual predefined support for SHORT, legacy resistance for LONG."""
        return self.support if self.bias == "SHORT" else self.resistance


class FastEvaluation(Model):
    state: FastState
    events: list[FastEvent] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
