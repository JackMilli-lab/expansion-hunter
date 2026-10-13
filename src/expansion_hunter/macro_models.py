"""Point-in-time macro contracts: schedules, consensus, initial releases and revisions.

Every receipt is recorded separately from the economic event time. A calendar
downloaded today is never represented as a calendar known yesterday.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from .models import Model


class MacroQuality(StrEnum):
    AVAILABLE = "AVAILABLE"
    ESTIMATED = "ESTIMATED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class MacroMode(StrEnum):
    NORMAL = "NORMAL"
    EVENT_RISK = "EVENT_RISK"
    EVENT_IMMINENT = "EVENT_IMMINENT"
    EVENT_RELEASED = "EVENT_RELEASED"
    EVENT_REACTION = "EVENT_REACTION"
    EVENT_COOLDOWN = "EVENT_COOLDOWN"


class ReleaseKind(StrEnum):
    INITIAL = "INITIAL"
    REVISION = "REVISION"


class MacroEvent(Model):
    event_id: str = Field(min_length=1)
    event_name: str = Field(min_length=1)
    country: str = "US"
    category: str
    importance: int = Field(default=3, ge=1, le=3)
    scheduled_timestamp: datetime
    available_at: datetime
    source: str
    source_quality: str = "AUTHORITATIVE"
    data_quality: MacroQuality = MacroQuality.AVAILABLE
    period: str | None = None
    unit: str | None = None


class MacroConsensus(Model):
    consensus_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    value: float | None = None
    unit: str
    consensus_timestamp: datetime
    engine_received_timestamp: datetime
    source: str
    previous_value: float | None = None
    previous_value_timestamp: datetime | None = None
    data_quality: MacroQuality = MacroQuality.AVAILABLE

    @model_validator(mode="after")
    def causal(self) -> MacroConsensus:
        if self.consensus_timestamp > self.engine_received_timestamp:
            raise ValueError("consensus cannot be received before its publication")
        if self.previous_value is not None and self.previous_value_timestamp is None:
            raise ValueError("previous value requires its point-in-time publication timestamp")
        if self.previous_value_timestamp and self.previous_value_timestamp > self.engine_received_timestamp:
            raise ValueError("previous value publication cannot follow consensus receipt")
        if self.data_quality == MacroQuality.AVAILABLE and self.value is None:
            raise ValueError("available consensus requires a value")
        return self


class MacroRelease(Model):
    release_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    kind: ReleaseKind = ReleaseKind.INITIAL
    value: float | None = None
    unit: str
    period: str | None = None
    actual_release_timestamp: datetime
    provider_received_timestamp: datetime
    engine_received_timestamp: datetime
    source: str
    source_quality: str = "AUTHORITATIVE"
    revision_of: str | None = None
    data_quality: MacroQuality = MacroQuality.AVAILABLE

    @model_validator(mode="after")
    def causal(self) -> MacroRelease:
        if (
            not self.actual_release_timestamp
            <= self.provider_received_timestamp
            <= self.engine_received_timestamp
        ):
            raise ValueError("release publication, provider receipt and engine receipt must be ordered")
        if self.kind == ReleaseKind.REVISION and not self.revision_of:
            raise ValueError("revision must identify the initial release")
        if self.kind == ReleaseKind.INITIAL and self.revision_of is not None:
            raise ValueError("initial release cannot revise another release")
        if self.data_quality == MacroQuality.AVAILABLE and self.value is None:
            raise ValueError("available release requires a value")
        return self


class MacroSurprise(Model):
    event_id: str
    as_of: datetime
    initial_release_id: str | None = None
    consensus_id: str | None = None
    actual_initial_value: float | None = None
    consensus_value: float | None = None
    previous_value_as_known_before_release: float | None = None
    unit: str | None = None
    surprise_absolute: float | None = None
    surprise_percent: float | None = None
    standardized_surprise: float | None = None
    historical_sample_count: int = 0
    data_quality: MacroQuality = MacroQuality.UNAVAILABLE
    reasons: list[str] = Field(default_factory=list)
    market_interpretation_context: str = "Raw event surprise; directional interpretation is unvalidated."


class MacroContext(Model):
    as_of: datetime
    mode: MacroMode = MacroMode.NORMAL
    event_id: str | None = None
    event_name: str | None = None
    scheduled_timestamp: datetime | None = None
    actual_release_timestamp: datetime | None = None
    seconds_to_release: float | None = None
    data_quality: MacroQuality = MacroQuality.UNAVAILABLE
    reasons: list[str] = Field(default_factory=list)


class MacroMarketObservation(Model):
    """One observed tick or one completed interval, never a reconstructed tick."""

    asset_id: str
    market_event_timestamp: datetime
    data_received_timestamp: datetime
    price: float = Field(gt=0)
    source: str
    interval_start_timestamp: datetime | None = None
    resolution_seconds: float = Field(default=0, ge=0)
    high: float | None = Field(default=None, gt=0)
    low: float | None = Field(default=None, gt=0)
    volume: float | None = Field(default=None, ge=0)
    quote_volume: float | None = Field(default=None, ge=0)
    spread_bps: float | None = Field(default=None, ge=0)
    book_depth: float | None = Field(default=None, ge=0)
    taker_delta: float | None = None
    cvd: float | None = None
    open_interest: float | None = Field(default=None, ge=0)
    funding: float | None = None
    liquidations: float | None = Field(default=None, ge=0)
    basis: float | None = None
    data_quality: MacroQuality = MacroQuality.AVAILABLE

    @model_validator(mode="after")
    def causal(self) -> MacroMarketObservation:
        if self.market_event_timestamp > self.data_received_timestamp:
            raise ValueError("market observation cannot be received before its event")
        if self.interval_start_timestamp and self.interval_start_timestamp >= self.market_event_timestamp:
            raise ValueError("interval start must precede completed interval time")
        if self.resolution_seconds > 0 and self.interval_start_timestamp is None:
            raise ValueError("interval observations require interval start")
        if self.interval_start_timestamp:
            actual = (self.market_event_timestamp - self.interval_start_timestamp).total_seconds()
            if abs(actual - self.resolution_seconds) > 0.001:
                raise ValueError("resolution must equal the completed interval duration")
        if self.high is not None and self.high < self.price:
            raise ValueError("high cannot be below close/price")
        if self.low is not None and self.low > self.price:
            raise ValueError("low cannot be above close/price")
        return self


class MacroReaction(Model):
    event_id: str
    asset_id: str
    as_of: datetime
    release_timestamp: datetime | None = None
    horizon_seconds: int = Field(gt=0)
    baseline_price: float | None = None
    baseline_event_timestamp: datetime | None = None
    end_price: float | None = None
    end_event_timestamp: datetime | None = None
    return_pct: float | None = None
    event_mfe_pct: float | None = None
    event_mae_pct: float | None = None
    event_direction: str | None = None
    volume: float | None = None
    volume_shock: float | None = None
    liquidity_shock: float | None = None
    first_move_direction: str | None = None
    first_move_size_pct: float | None = None
    reversed_first_move: bool | None = None
    samples: int = 0
    resolution_seconds: float | None = None
    data_quality: MacroQuality = MacroQuality.UNAVAILABLE
    reasons: list[str] = Field(default_factory=list)


class MacroProviderReport(Model):
    provider: str
    retrieved_at: datetime
    events: list[MacroEvent] = Field(default_factory=list)
    consensuses: list[MacroConsensus] = Field(default_factory=list)
    releases: list[MacroRelease] = Field(default_factory=list)
    calendar_quality: MacroQuality = MacroQuality.UNAVAILABLE
    release_quality: MacroQuality = MacroQuality.UNAVAILABLE
    consensus_quality: MacroQuality = MacroQuality.UNAVAILABLE
    issues: list[str] = Field(default_factory=list)


class MacroSettings(Model):
    risk_window_seconds: int = Field(default=3600, gt=0)
    imminent_window_seconds: int = Field(default=300, gt=0)
    released_window_seconds: int = Field(default=30, ge=0)
    reaction_window_seconds: int = Field(default=900, gt=0)
    cooldown_window_seconds: int = Field(default=3600, gt=0)
    overdue_window_seconds: int = Field(default=900, gt=0)
    minimum_importance: int = Field(default=3, ge=1, le=3)
    minimum_standardization_samples: int = Field(default=20, ge=3)
    maximum_tick_gap_seconds: float = Field(default=15, gt=0)
    maximum_baseline_age_seconds: float = Field(default=60, gt=0)

    @model_validator(mode="after")
    def ordered(self) -> MacroSettings:
        if self.imminent_window_seconds > self.risk_window_seconds:
            raise ValueError("imminent window cannot exceed risk window")
        if not self.released_window_seconds <= self.reaction_window_seconds <= self.cooldown_window_seconds:
            raise ValueError("released/reaction/cooldown windows must be ordered")
        return self
