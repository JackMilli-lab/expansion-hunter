"""Deterministic discovery stage rules and rate-budgeted adaptive monitoring.

These thresholds are uncalibrated setup hypotheses, never probabilities or order
instructions. Expired evidence cannot maintain a high-priority state. The
scheduler exposes deferred work rather than silently pretending it was scanned.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256

from .discovery_config import DiscoverySettings
from .discovery_models import (
    DiscoveryEvent,
    DiscoveryObservation,
    DiscoveryStage,
    Freshness,
)
from .models import TIMEFRAMES, aware

_ORDER = {
    DiscoveryStage.DORMANT: 0,
    DiscoveryStage.INVALIDATED: 0,
    DiscoveryStage.RADAR: 1,
    DiscoveryStage.WATCH: 2,
    DiscoveryStage.PRE_IGNITION: 3,
    DiscoveryStage.LOADED: 4,
    DiscoveryStage.IGNITION: 5,
}


def _score(observation: DiscoveryObservation, key: str, coverage: float) -> float | None:
    score = observation.scores.get(key)
    if score is None or score.value is None or score.coverage < coverage:
        return None
    return score.value


def _feature(observation: DiscoveryObservation, key: str) -> float | None:
    value = observation.features.get(key)
    if not value or value.value is None or value.freshness != Freshness.FRESH:
        return None
    if not value.retrieved_at or value.retrieved_at > observation.as_of:
        return None
    if not value.source_timestamp or value.source_timestamp > observation.as_of:
        return None
    return value.value


def _critical_reason(observation: DiscoveryObservation) -> str | None:
    if observation.freshness in {Freshness.STALE, Freshness.MISSING}:
        return f"critical data {observation.freshness}; setup evidence expired"
    for key, feature in observation.features.items():
        if not feature.critical:
            continue
        if feature.value is None or feature.freshness in {Freshness.STALE, Freshness.MISSING}:
            return f"critical feature {key} {feature.freshness}; setup evidence expired"
        if not feature.retrieved_at or feature.retrieved_at > observation.as_of:
            return f"critical feature {key} not yet available at the observation boundary"
        if not feature.source_timestamp or feature.source_timestamp > observation.as_of:
            return f"critical feature {key} has an invalid source timestamp"
    return None


def _all_critical_fresh(observation: DiscoveryObservation) -> bool:
    critical = [feature for feature in observation.features.values() if feature.critical]
    return bool(critical) and observation.freshness == Freshness.FRESH and all(
        feature.freshness == Freshness.FRESH and feature.value is not None
        for feature in critical
    )


def _interval(settings: DiscoverySettings, stage: DiscoveryStage) -> int:
    return settings.intervals.get(stage.value, settings.intervals.get("DORMANT", 900))


def transition(
    observation: DiscoveryObservation,
    previous: DiscoveryObservation | None,
    settings: DiscoverySettings,
    cycle_id: str,
) -> tuple[DiscoveryObservation, DiscoveryEvent | None]:
    """Choose one current state and explain every promotion/demotion.

    Missing advanced scores remain unknown. A fresh, rapid rank improvement may
    enter RADAR before the absolute score threshold. Hard invalidation compares
    current price with *previously known* support, never a newly derived level.
    """
    result = observation.model_copy(deep=True)
    prior_stage = previous.stage if previous else DiscoveryStage.DORMANT
    result.previous_stage = prior_stage
    coverage = settings.minimum_score_coverage
    light = _score(result, "light_discovery", coverage)
    if light is None:
        light = _score(result, "anomaly", coverage)
    energy = _score(result, "stored_energy", coverage)
    absorption = _score(result, "absorption", settings.loaded_minimum_coverage)
    trigger = _score(result, "trigger_sensitivity", settings.loaded_minimum_coverage)
    readiness = _score(result, "expansion_readiness", settings.loaded_minimum_coverage)
    awakening = _score(result, "volume_awakening", coverage)
    velocity = result.rank.rank_velocity
    fast_rank = bool(
        velocity is not None and velocity > 0
        and result.rank.velocity_percentile is not None
        and result.rank.velocity_percentile >= settings.rank_velocity_percentile
    )
    critical_reason = _critical_reason(result)
    fresh = not critical_reason and _all_critical_fresh(result)
    stage = DiscoveryStage.DORMANT
    reason = "no current discovery promotion conditions"

    short = result.ranking_mode == "BOTH" and result.bias == "SHORT"
    boundary_name = "resistance" if short else "support"
    same_direction = settings.ranking_mode == "LONG" or bool(
        previous and previous.ranking_mode == "BOTH" and previous.bias == result.bias
        and result.bias in {"LONG", "SHORT"}
    )
    previous_boundary = None
    if previous and same_direction and _ORDER[prior_stage] >= _ORDER[DiscoveryStage.RADAR]:
        previous_boundary = previous.levels.get(boundary_name)
        if previous_boundary is None:
            feature = previous.features.get(boundary_name)
            if feature and feature.retrieved_at and feature.retrieved_at <= previous.as_of:
                previous_boundary = feature.value
    price = _feature(result, "price")
    invalidated = bool(
        fresh and previous_boundary is not None and price is not None
        and (price > previous_boundary if short else price < previous_boundary)
    )

    if invalidated:
        stage = DiscoveryStage.INVALIDATED
        reason = (f"current price {price:g} above previously known resistance {previous_boundary:g}" if short
                  else f"current price {price:g} below previously known support {previous_boundary:g}")
    elif not result.eligible or result.disposition == "EXCLUDED":
        reason = "asset currently ineligible; " + "; ".join(result.reasons)
    elif critical_reason:
        reason = critical_reason
    elif result.disposition == "FAILED":
        reason = "collection failed; previous setup cannot be promoted on missing evidence"
    else:
        if (light is not None and light >= settings.radar_threshold) or fast_rank:
            stage = DiscoveryStage.RADAR
            reason = "light discovery threshold met" if not fast_rank else "rapid comparable-cohort rank improvement"
        if light is not None and light >= settings.watch_threshold:
            stage = DiscoveryStage.WATCH
            reason = "fresh lightweight anomalies align at WATCH threshold"
        if (
            fresh and not result.too_late
            and light is not None and light >= settings.radar_threshold
            and energy is not None and energy >= settings.pre_ignition_energy
        ):
            stage = DiscoveryStage.PRE_IGNITION
            reason = "Stored Energy threshold met with known component coverage"
        loaded_energy = _score(result, "stored_energy", settings.loaded_minimum_coverage)
        if (
            fresh and not result.too_late
            and light is not None and light >= settings.watch_threshold
            and loaded_energy is not None and loaded_energy >= settings.loaded_energy
            and absorption is not None and absorption >= settings.loaded_absorption
            and trigger is not None and trigger >= settings.loaded_trigger
            and readiness is not None and readiness >= settings.loaded_readiness
        ):
            stage = DiscoveryStage.LOADED
            reason = "Stored Energy, Absorption, Trigger Sensitivity and Readiness thresholds met; UNCALIBRATED"
        # An hourly RVOL alone cannot prove intrahour ignition. At least one
        # causally available 1m/5m/15m observation must actually be abnormal.
        micro_rvol = [
            value for key in ("rvol_1m", "rvol_5m", "rvol_15m")
            if (value := _feature(result, key)) is not None
        ]
        extension = _feature(result, "atr_distance")
        structural_watch = (
            _ORDER[stage] >= _ORDER[DiscoveryStage.WATCH]
            or _ORDER[prior_stage] >= _ORDER[DiscoveryStage.WATCH]
        )
        if (
            fresh and not result.too_late and structural_watch
            and (settings.ranking_mode == "LONG" or result.bias == "LONG"
                 or (short and readiness is not None))
            and awakening is not None and awakening >= settings.ignition_awakening
            and micro_rvol and max(micro_rvol) >= settings.ignition_rvol
            and extension is not None and extension <= 2
        ):
            stage = DiscoveryStage.IGNITION
            reason = "fresh micro-timeframe volume awakening before excessive ATR extension; UNCALIBRATED"
        if not fresh and _ORDER[stage] > _ORDER[DiscoveryStage.WATCH]:
            stage = DiscoveryStage.WATCH
            reason = "degraded critical data caps the setup at WATCH"
        if result.too_late:
            reason += "; TOO_LATE: high-priority setup/ignition promotion suppressed"

    result.stage = stage
    if stage != prior_stage:
        direction = "promotion" if _ORDER[stage] > _ORDER[prior_stage] else "demotion"
        result.reasons.append(f"{direction}: {prior_stage.value} -> {stage.value}: {reason}")
    elif reason not in result.reasons:
        result.reasons.append(reason)

    interval = _interval(settings, stage)
    new_deep_at = result.last_deep_at
    previous_deep_at = previous.last_deep_at if previous else None
    new_deep_success = bool(new_deep_at and (not previous_deep_at or new_deep_at > previous_deep_at))
    successful_deep = (
        (new_deep_success or (result.deep_scanned and new_deep_at is None))
        and result.disposition != "FAILED" and not critical_reason
    )
    failed_deep = result.deep_scanned and (result.disposition == "FAILED" or bool(critical_reason))
    if successful_deep:
        # Real observation completion, rather than the cycle's requested start.
        result.monitoring_due_at = (new_deep_at or result.as_of) + timedelta(seconds=interval)
    elif failed_deep:
        result.monitoring_due_at = result.as_of + timedelta(seconds=max(60, interval))
        result.reasons.append("deep refresh failed; bounded retry backoff applied")
    elif previous and previous.monitoring_due_at:
        result.monitoring_due_at = min(
            previous.monitoring_due_at, result.as_of + timedelta(seconds=interval)
        )
    else:
        # Newly promoted assets can enter the deep queue immediately.
        result.monitoring_due_at = result.as_of

    if stage == prior_stage or stage == DiscoveryStage.DORMANT:
        return result, None
    high_priority = stage in {DiscoveryStage.LOADED, DiscoveryStage.IGNITION}
    # Redundant guard makes event generation safe if stage logic is extended.
    if high_priority and not fresh:
        return result, None
    identity = f"{cycle_id}|{result.asset.asset_id}|{prior_stage.value}|{stage.value}"
    event = DiscoveryEvent(
        event_id=sha256(identity.encode()).hexdigest(), cycle_id=cycle_id,
        asset_id=result.asset.asset_id, timestamp=result.as_of, event_type=stage,
        previous_stage=prior_stage, reason=reason, high_priority=high_priority,
        details={
            "bias": result.bias, "ranking_mode": result.ranking_mode,
            "rank": result.rank.current_rank, "previous_rank": result.rank.previous_rank,
            "rank_velocity": result.rank.rank_velocity,
            "stored_energy": energy, "absorption": absorption,
            "trigger_sensitivity": trigger, "expansion_readiness": readiness,
            "calibration": "UNCALIBRATED", "live_trading": False,
        },
    )
    return result, event


class DiscoveryScheduler:
    """Select only due work under asset/request-weight budgets, with fair aging."""

    def __init__(self, settings: DiscoverySettings):
        self.settings = settings
        self.deferred: list[dict[str, str | int | float]] = []
        self.used_weight = 0

    def due(
        self,
        observations: list[DiscoveryObservation],
        now: datetime,
        limit: int,
        weight_budget: int,
        cost_per_asset: int = 80,
    ) -> list[DiscoveryObservation]:
        now = aware(now)
        if cost_per_asset < 1:
            raise ValueError("deep request cost must be positive")
        if limit < 0 or weight_budget < 0:
            raise ValueError("monitoring budgets must be nonnegative")
        self.deferred = []
        self.used_weight = 0
        candidates = []
        for observation in observations:
            if not observation.eligible or observation.disposition == "EXCLUDED":
                continue
            if observation.as_of > now:
                self.deferred.append({"asset_id": observation.asset.asset_id, "reason": "future observation"})
                continue
            due_at = observation.monitoring_due_at or observation.as_of
            if due_at > now:
                continue
            interval = _interval(self.settings, observation.stage)
            age = max(0.0, (now - due_at).total_seconds()) / interval
            # Unbounded age eventually outranks recently due high-priority work.
            urgency = age + _ORDER[observation.stage] / 2
            candidates.append((urgency, due_at, observation))
        candidates.sort(key=lambda row: (-row[0], row[1], row[2].asset.asset_id))
        selected = []
        for _, due_at, observation in candidates:
            if len(selected) >= limit or self.used_weight + cost_per_asset > weight_budget:
                self.deferred.append({
                    "asset_id": observation.asset.asset_id,
                    "reason": "asset limit" if len(selected) >= limit else "provider request-weight budget",
                    "overdue_seconds": max(0, (now - due_at).total_seconds()),
                    "cost": cost_per_asset,
                })
                continue
            selected.append(observation)
            self.used_weight += cost_per_asset
        return selected

    def estimated_deep_cost(self) -> int:
        """Conservative Binance-native weights; actual transports still rate-limit."""
        candle_weight = 2 * len(self.settings.deep_timeframes)
        # Depth snapshot plus market/spot ticker and optional derivative reads.
        return candle_weight + 50 + 8

    def data_age_budget(self, timeframe: str) -> int:
        return TIMEFRAMES.get(timeframe, 60) + self.settings.poll_interval_seconds * 2
