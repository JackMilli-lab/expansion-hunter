"""Configurable discovery budgets and uncalibrated rule hypotheses."""

from typing import Literal

from pydantic import Field, model_validator

from .models import Model


class DiscoverySettings(Model):
    universe_size: int = Field(default=1000, ge=1, le=10000)
    ranking_mode: Literal["LONG", "BOTH"] = "LONG"
    light_timeframe: str = "15m"
    light_history_bars: int = Field(default=128, ge=32, le=1000)
    deep_timeframes: list[str] = Field(default_factory=lambda: ["1m", "5m", "15m", "1h", "4h", "1D"])
    deep_history_bars: int = Field(default=240, ge=220, le=1000)
    abnormal_limit: int = Field(default=250, ge=1)
    deep_watchlist_size: int = Field(default=50, ge=1)
    deep_per_cycle: int = Field(default=10, ge=0)
    deep_weight_budget: int = Field(default=800, ge=0)
    broad_interval_seconds: int = Field(default=300, ge=60)
    poll_interval_seconds: int = Field(default=15, ge=1)
    max_cycle_seconds: float = Field(default=240, gt=0)
    collection_timeout_seconds: float = Field(default=30, gt=0)
    minimum_history_samples: int = Field(default=20, ge=3)
    minimum_cross_section: int = Field(default=10, ge=2)
    minimum_rank_overlap: float = Field(default=0.8, gt=0, le=1)
    minimum_score_coverage: float = Field(default=0.6, gt=0, le=1)
    loaded_minimum_coverage: float = Field(default=0.75, gt=0, le=1)
    min_quote_volume_24h: float = Field(default=1_000_000, ge=0)
    max_spread_bps: float = Field(default=50, gt=0)
    radar_threshold: float = Field(default=55, ge=0, le=100)
    watch_threshold: float = Field(default=65, ge=0, le=100)
    pre_ignition_energy: float = Field(default=65, ge=0, le=100)
    loaded_energy: float = Field(default=75, ge=0, le=100)
    loaded_absorption: float = Field(default=60, ge=0, le=100)
    loaded_trigger: float = Field(default=70, ge=0, le=100)
    loaded_readiness: float = Field(default=75, ge=0, le=100)
    ignition_rvol: float = Field(default=2, gt=0)
    ignition_awakening: float = Field(default=65, ge=0, le=100)
    rank_velocity_percentile: float = Field(default=0.9, ge=0, le=1)
    dark_horse_previous_rank: int = Field(default=50, ge=1)
    resistance_reaction_bars: int = Field(default=6, ge=2, le=48)
    feature_fresh_frame_multiple: float = Field(default=2, gt=0)
    feature_degraded_frame_multiple: float = Field(default=4, gt=0)
    market_fresh_seconds: float = Field(default=300, gt=0)
    market_degraded_seconds: float = Field(default=900, gt=0)
    score_weights: dict[str, dict[str, float]] = Field(default_factory=dict)
    intervals: dict[str, int] = Field(default_factory=lambda: {
        "DORMANT": 900, "RADAR": 600, "WATCH": 300, "PRE_IGNITION": 120,
        "LOADED": 60, "IGNITION": 30, "INVALIDATED": 900,
    })

    @model_validator(mode="after")
    def valid_frames(self):
        from .models import TIMEFRAMES

        if self.light_timeframe not in TIMEFRAMES or set(self.deep_timeframes) - set(TIMEFRAMES):
            raise ValueError("unsupported discovery timeframe")
        if not {"4h", "1D"}.issubset(self.deep_timeframes):
            raise ValueError("deep analysis preserves 4h/daily baseline context")
        if any(v < 1 for v in self.intervals.values()):
            raise ValueError("monitoring intervals must be positive")
        if self.feature_degraded_frame_multiple < self.feature_fresh_frame_multiple:
            raise ValueError("degraded threshold must not precede fresh threshold")
        if self.market_degraded_seconds < self.market_fresh_seconds:
            raise ValueError("degraded market threshold must not precede fresh threshold")
        if any(weight <= 0 for weights in self.score_weights.values() for weight in weights.values()):
            raise ValueError("score weights must be positive")
        return self
