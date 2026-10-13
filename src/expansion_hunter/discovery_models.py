"""Additive Phase 1 discovery contracts; rule scores are never probabilities."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from .models import Asset, AssetSnapshot, Model


class Freshness(StrEnum):
    FRESH = "FRESH"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    MISSING = "MISSING"


class DiscoveryStage(StrEnum):
    DORMANT = "DORMANT"
    RADAR = "RADAR"
    WATCH = "WATCH"
    PRE_IGNITION = "PRE_IGNITION"
    LOADED = "LOADED"
    IGNITION = "IGNITION"
    INVALIDATED = "INVALIDATED"


class DiscoveryFeature(Model):
    value: float | None = None
    unit: str = ""
    source_timestamp: datetime | None = None
    retrieved_at: datetime | None = None
    age_seconds: float | None = Field(default=None, ge=0)
    provider: str
    timeframe: str
    freshness: Freshness = Freshness.MISSING
    reason: str | None = None
    historical_percentile: float | None = Field(default=None, ge=0, le=1)
    cross_sectional_percentile: float | None = Field(default=None, ge=0, le=1)
    direction: Literal[-1, 1] = 1
    sample_count: int = Field(default=0, ge=0)
    critical: bool = False

    @model_validator(mode="after")
    def causal(self):
        if self.source_timestamp and self.retrieved_at and self.retrieved_at < self.source_timestamp:
            raise ValueError("retrieval precedes source observation")
        if self.value is not None and not (self.source_timestamp and self.retrieved_at):
            raise ValueError("observed feature requires source and retrieval timestamps")
        return self


class ScoreComponent(Model):
    name: str
    weight: float = Field(gt=0)
    normalized: float | None = Field(default=None, ge=0, le=1)
    contribution: float | None = Field(default=None, ge=0, le=100)
    reason: str
    inputs: dict[str, float | str | bool | None] = Field(default_factory=dict)


class DiscoveryScore(Model):
    value: float | None = Field(default=None, ge=0, le=100)
    coverage: float = Field(default=0, ge=0, le=1)
    lower_bound: float = Field(default=0, ge=0, le=100)
    upper_bound: float = Field(default=100, ge=0, le=100)
    calibration: Literal["UNCALIBRATED"] = "UNCALIBRATED"
    formula_version: str = "phase1-v1"
    components: list[ScoreComponent] = Field(default_factory=list)


class RankHistory(Model):
    current_rank: float | None = None
    previous_rank: float | None = None
    ranks: dict[str, float | None] = Field(default_factory=dict)
    rank_velocity: float | None = None
    rank_acceleration: float | None = None
    velocity_percentile: float | None = None
    cohort_size: int = 0
    comparable_cohort_size: int = 0
    reason: str | None = None


class DiscoveryObservation(Model):
    asset: Asset
    as_of: datetime
    features: dict[str, DiscoveryFeature] = Field(default_factory=dict)
    scores: dict[str, DiscoveryScore] = Field(default_factory=dict)
    ranking_mode: Literal["LONG", "BOTH"] = "LONG"
    bias: Literal["LONG", "SHORT", "NEUTRAL", "UNAVAILABLE"] = "UNAVAILABLE"
    rank: RankHistory = Field(default_factory=RankHistory)
    stage: DiscoveryStage = DiscoveryStage.DORMANT
    previous_stage: DiscoveryStage = DiscoveryStage.DORMANT
    disposition: Literal["COMPLETE", "PARTIAL", "EXCLUDED", "FAILED"] = "PARTIAL"
    eligible: bool = False
    deep_scanned: bool = False
    freshness: Freshness = Freshness.MISSING
    reasons: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    snapshot: AssetSnapshot | None = None
    levels: dict[str, float | None] = Field(default_factory=dict)
    monitoring_due_at: datetime | None = None
    last_light_at: datetime | None = None
    last_deep_at: datetime | None = None
    too_late: bool = False


class DiscoveryEvent(Model):
    event_id: str
    cycle_id: str
    asset_id: str
    timestamp: datetime
    event_type: DiscoveryStage
    previous_stage: DiscoveryStage
    reason: str
    high_priority: bool = False
    details: dict[str, float | str | bool | None] = Field(default_factory=dict)


class DiscoveryCycle(Model):
    schema_version: int = 2
    cycle_id: str
    started_at: datetime
    as_of: datetime
    completed_at: datetime
    database_path: str
    config_hash: str
    code_version: str = "phase1-v2-native-cohorts"
    config: dict = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    observations: list[DiscoveryObservation] = Field(default_factory=list)
    events: list[DiscoveryEvent] = Field(default_factory=list)
    lists: dict[str, list[str]] = Field(default_factory=dict)
    provider_issues: list[str] = Field(default_factory=list)
    statistics: dict[str, float | int | str | None] = Field(default_factory=dict)
    status: Literal["COMPLETE", "DEGRADED", "FAILED"] = "COMPLETE"
