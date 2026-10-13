"""Background-only event outcomes and observed-coverage miss accounting.

Live feature snapshots are immutable and separate from future outcome objects.
Missing horizons are censored, never negative examples. Misses cover only assets
with archived, contiguous observations; no claim is made about unobserved coins.
Rules and thresholds are explicit research hypotheses, not fitted performance.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from statistics import mean
from typing import Literal

from pydantic import Field, model_validator

from .analog import FactorQuality, FactorValue
from .fast_models import FastEvent
from .intelligence_store import IntelligenceStore
from .macro_models import MacroMarketObservation, MacroQuality
from .models import Model, aware

PRE_ALERTS = {"RADAR", "WATCH", "PRE_IGNITION", "LOADED", "BREAKOUT_PRESSURE", "BREAKDOWN_PRESSURE"}
BREAKOUT_ALERTS = {"1M_BREAKOUT_CONFIRMED", "BREAKOUT_CONFIRMED", "1M_BREAKDOWN_CONFIRMED", "BREAKDOWN_CONFIRMED"}
MEANINGFUL_ALERTS = PRE_ALERTS | BREAKOUT_ALERTS


class LearningSettings(Model):
    horizons_seconds: list[int] = Field(
        default_factory=lambda: [60, 300, 900, 3600, 14400, 86400, 259200, 604800]
    )
    expansion_return: float = Field(default=0.15, gt=0)
    expansion_onset_return: float = Field(default=0.05, gt=0)
    expansion_window_seconds: int = Field(default=86400, ge=60)
    false_positive_return: float = Field(default=0.10, gt=0)
    false_positive_horizon_seconds: int = Field(default=86400, ge=60)
    breakout_latency_seconds: float = Field(default=60, ge=0)
    miss_cooldown_seconds: int = Field(default=86400, ge=0)
    maximum_assets_per_pass: int = Field(default=50, ge=1, le=1000)
    maximum_events_per_pass: int = Field(default=100, ge=1, le=10000)
    maximum_observations_per_asset: int = Field(default=10000, ge=2, le=10000)
    minimum_alert_lead_seconds: float = Field(default=0, ge=0)
    version: str = "event-learning-v1"

    @model_validator(mode="after")
    def coherent_windows(self):
        if not self.horizons_seconds or len(self.horizons_seconds) != len(set(self.horizons_seconds)):
            raise ValueError("outcome horizons must be nonempty and unique")
        if any(horizon < 60 or horizon % 60 for horizon in self.horizons_seconds):
            raise ValueError("outcome horizons must be positive full-minute durations")
        if self.expansion_onset_return > self.expansion_return:
            raise ValueError("expansion onset threshold exceeds material expansion threshold")
        if self.expansion_window_seconds % 60:
            raise ValueError("expansion window must be a full-minute duration")
        return self


class ResearchEvent(Model):
    event_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    bias: Literal["LONG", "SHORT", "NEUTRAL"] = "LONG"
    event_timestamp: datetime
    data_received_timestamp: datetime
    decision_timestamp: datetime
    available_at: datetime
    reference_price: float = Field(gt=0)
    source: str = Field(min_length=1)
    features: dict[str, FactorValue] = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)
    data_quality: dict[str, str] = Field(default_factory=dict)
    regime: str | None = None
    cycle: str | None = None
    personal_watchlist: bool | None = None
    expansion_threshold: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def point_in_time(self):
        if (
            not self.event_timestamp
            <= self.data_received_timestamp
            <= self.decision_timestamp
            <= self.available_at
        ):
            raise ValueError("research event timestamp ordering is invalid")
        for name, feature in self.features.items():
            if name.lower().startswith(("future_", "outcome_", "label_", "time_to_")) or name.lower() in (
                "mfe",
                "mae",
            ):
                raise ValueError("future outcomes cannot be event features")
            if feature.value is not None and not (
                feature.event_timestamp <= self.decision_timestamp
                and feature.available_at <= self.decision_timestamp
            ):
                raise ValueError("feature was unavailable at event decision")
        return self


class EventOutcome(Model):
    event_id: str
    asset_id: str
    event_type: str
    bias: Literal["LONG", "SHORT", "NEUTRAL"] = "LONG"
    horizon_seconds: int = Field(gt=0)
    horizon_end: datetime
    evaluated_at: datetime
    available_at: datetime | None = None
    status: Literal["MATURED", "CENSORED"]
    reference_price: float
    future_close_return: float | None = None
    future_max_return: float | None = None
    future_min_return: float | None = None
    directional_close_return: float | None = None
    mfe: float | None = None
    mae: float | None = None
    mfe_interval_start: datetime | None = None
    mfe_interval_end: datetime | None = None
    mae_interval_start: datetime | None = None
    mae_interval_end: datetime | None = None
    time_to_expansion_seconds: float | None = None
    time_to_failure_seconds: float | None = None
    false_positive: bool | None = None
    expansion_threshold_hits: dict[str, bool] = Field(default_factory=dict)
    failed_breakout: bool | None = None
    failed_breakdown: bool | None = None
    source: str | None = None
    resolution_seconds: float | None = None
    regime: str | None = None
    cycle: str | None = None
    reasons: list[str] = Field(default_factory=list)
    formula_version: str = "event-learning-v1"

    @model_validator(mode="after")
    def known_complete_path(self):
        returns = (
            self.future_close_return,
            self.future_max_return,
            self.future_min_return,
            self.mfe,
            self.mae,
        )
        if self.status == "MATURED" and (
            any(value is None for value in returns)
            or self.available_at is None
            or self.source is None
            or self.resolution_seconds is None
            or self.evaluated_at < self.horizon_end
        ):
            raise ValueError("matured outcome requires complete returns, source and dated coverage")
        if self.status == "CENSORED" and any(value is not None for value in returns):
            raise ValueError("censored outcome cannot contain fabricated returns")
        return self


class ObservedExpansion(Model):
    expansion_id: str
    asset_id: str
    bias: Literal["LONG", "SHORT"] = "LONG"
    detected_at: datetime
    baseline_timestamp: datetime
    baseline_price: float
    expansion_start: datetime
    expansion_confirmed_at: datetime
    expansion_magnitude: float
    first_meaningful_alert: datetime | None
    first_breakout_alert: datetime | None
    lead_times_seconds: dict[str, float | None]
    had_early_warning: bool
    had_timely_breakout_alert: bool
    definition: str
    source: str
    coverage_scope: Literal["MONITORED_ARCHIVED_ONLY"] = "MONITORED_ARCHIVED_ONLY"


class MissRecord(Model):
    miss_id: str
    asset_id: str
    bias: Literal["LONG", "SHORT"] = "LONG"
    detected_at: datetime
    baseline_timestamp: datetime
    baseline_price: float
    expansion_start: datetime
    expansion_confirmed_at: datetime
    expansion_start_definition: str
    expansion_magnitude: float
    kind: Literal["PRE_WARNING_MISS", "TRIGGER_MISS", "FULL_MISS"]
    cause_classification: Literal["UNKNOWN"] = "UNKNOWN"
    coverage_scope: Literal["MONITORED_ARCHIVED_ONLY"] = "MONITORED_ARCHIVED_ONLY"
    first_system_detection: datetime | None = None
    first_breakout_alert: datetime | None = None
    pre_move_event_ids: list[str] = Field(default_factory=list)
    lead_times_seconds: dict[str, float | None] = Field(default_factory=dict)
    pre_move_features: dict = Field(default_factory=dict)
    pre_move_rank: float | None = None
    pre_move_stage: str | None = None
    rejection_reasons: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    data_quality: str = "AVAILABLE"
    source: str
    reason: str


def _observed_intervals(
    observations: list[MacroMarketObservation],
    start: datetime,
    end: datetime,
    as_of: datetime,
) -> tuple[list[MacroMarketObservation], str | None]:
    """Pick a fully covering native interval resolution; never infer tick paths."""
    available = [
        item
        for item in observations
        if item.data_received_timestamp <= as_of
        and item.market_event_timestamp <= as_of
        and item.data_quality == MacroQuality.AVAILABLE
        and item.resolution_seconds > 0
        and item.interval_start_timestamp is not None
        and item.interval_start_timestamp >= start
        and item.market_event_timestamp <= end
    ]
    for resolution in sorted({item.resolution_seconds for item in available}):
        if (end - start).total_seconds() % resolution:
            continue
        group = sorted(
            [item for item in available if item.resolution_seconds == resolution],
            key=lambda item: item.interval_start_timestamp,
        )
        expected = round((end - start).total_seconds() / resolution)
        if len(group) != expected or len({item.interval_start_timestamp for item in group}) != expected:
            continue
        if len({item.source for item in group}) != 1:
            continue
        if all(
            item.interval_start_timestamp == start + timedelta(seconds=index * resolution)
            and item.market_event_timestamp == start + timedelta(seconds=(index + 1) * resolution)
            and item.high is not None
            and item.low is not None
            for index, item in enumerate(group)
        ):
            return group, None
    return [], "missing, duplicate, stale, late, mixed-source, partial or non-contiguous interval coverage"


def calculate_event_outcome(
    event: ResearchEvent,
    observations: list[MacroMarketObservation],
    *,
    as_of: datetime,
    horizon_seconds: int,
    settings: LearningSettings | None = None,
) -> EventOutcome | None:
    """Return no outcome before maturity; complete interval paths give exact bounds.

    MFE/MAE include the zero excursion at the observed signal reference price.
    Intracandle extrema timestamps are intervals, not fabricated exact times.
    Time-to-expansion/failure uses completed closes rather than unseen tick paths.
    """
    settings = settings or LearningSettings()
    as_of = aware(as_of)
    if horizon_seconds <= 0 or horizon_seconds % 60:
        raise ValueError("outcome horizon must be a positive full-minute duration")
    if event.available_at > as_of:
        raise ValueError("research event unavailable at outcome evaluation")
    end = event.event_timestamp + timedelta(seconds=horizon_seconds)
    if end > as_of:
        return None
    result = EventOutcome(
        event_id=event.event_id,
        asset_id=event.asset_id,
        event_type=event.event_type,
        bias=event.bias,
        horizon_seconds=horizon_seconds,
        horizon_end=end,
        evaluated_at=as_of,
        reference_price=event.reference_price,
        status="CENSORED",
        regime=event.regime,
        cycle=event.cycle,
        formula_version=settings.version,
    )
    rows = [item for item in observations if item.asset_id == event.asset_id]
    intervals, reason = _observed_intervals(rows, event.event_timestamp, end, as_of)
    if reason:
        result.reasons.append(reason)
        return result
    highest = max(intervals, key=lambda item: item.high)
    lowest = min(intervals, key=lambda item: item.low)
    result.status = "MATURED"
    result.available_at = as_of
    result.source = intervals[0].source
    result.resolution_seconds = intervals[0].resolution_seconds
    result.future_close_return = intervals[-1].price / event.reference_price - 1
    result.future_max_return = highest.high / event.reference_price - 1
    result.future_min_return = lowest.low / event.reference_price - 1
    direction = -1 if event.bias == "SHORT" else 1
    favorable = -result.future_min_return if event.bias == "SHORT" else result.future_max_return
    adverse = -result.future_max_return if event.bias == "SHORT" else result.future_min_return
    favorable_bar, adverse_bar = (lowest, highest) if event.bias == "SHORT" else (highest, lowest)
    result.directional_close_return = direction * result.future_close_return
    result.mfe, result.mae = max(0.0, favorable), min(0.0, adverse)
    if result.mfe > 0:
        result.mfe_interval_start, result.mfe_interval_end = (
            favorable_bar.interval_start_timestamp,
            favorable_bar.market_event_timestamp,
        )
    if result.mae < 0:
        result.mae_interval_start, result.mae_interval_end = (
            adverse_bar.interval_start_timestamp,
            adverse_bar.market_event_timestamp,
        )
    threshold = event.expansion_threshold or settings.expansion_return
    expansion = next(
        (item for item in intervals if direction * (item.price / event.reference_price - 1) + 1e-12 >= threshold), None
    )
    if expansion:
        result.time_to_expansion_seconds = (
            expansion.market_event_timestamp - event.event_timestamp
        ).total_seconds()
    result.expansion_threshold_hits = {
        str(value): favorable + 1e-12 >= value for value in (0.10, 0.20, 0.30, 0.50, 1.0)
    }
    resistance = event.features.get("support" if event.bias == "SHORT" else "resistance")
    if event.event_type in BREAKOUT_ALERTS and resistance and resistance.usable(event.decision_timestamp):
        failure = next((item for item in intervals if (item.price > resistance.value if event.bias == "SHORT"
                                                      else item.price < resistance.value)), None)
        if event.bias == "SHORT":
            result.failed_breakdown = bool(failure)
        else:
            result.failed_breakout = bool(failure)
        if failure:
            result.time_to_failure_seconds = (
                failure.market_event_timestamp - event.event_timestamp
            ).total_seconds()
    if horizon_seconds == settings.false_positive_horizon_seconds and event.event_type in {
        "LOADED",
        "BREAKOUT_PRESSURE",
        "BREAKDOWN_PRESSURE",
        *BREAKOUT_ALERTS,
    }:
        result.false_positive = favorable + 1e-12 < settings.false_positive_return
    return result


class EventLearningEngine:
    """Call from a background worker, never directly in the 1m trigger hot path."""

    def __init__(self, store: IntelligenceStore, settings: LearningSettings | None = None):
        self.store = store
        self.settings = settings or LearningSettings()

    def record_event(self, event: ResearchEvent) -> bool:
        event = ResearchEvent.model_validate(event.model_dump())
        return self.store.append(
            "research_event",
            event.event_id,
            event.event_timestamp,
            event.available_at,
            event,
            event.asset_id,
        )

    def record_fast_event(self, event: FastEvent, context: dict | None = None) -> ResearchEvent:
        features = {}
        for name, value in event.metrics.items():
            if isinstance(value, (float, int)):
                features[name] = FactorValue(
                    value=float(value),
                    event_timestamp=event.market_event_timestamp,
                    available_at=event.evaluation_timestamp,
                    source="fast_ignition:derived",
                    unit="boolean" if isinstance(value, bool) else "raw feature-specific units",
                    quality=FactorQuality.DERIVED,
                )
        bias = getattr(event, "bias", "LONG")
        level = getattr(event, "support", None) if bias == "SHORT" else event.resistance
        if level:
            features["support" if bias == "SHORT" else "resistance"] = FactorValue(
                value=level.price,
                event_timestamp=level.event_timestamp,
                available_at=level.discovered_at,
                source="fast_ignition:known_5m_structure",
                unit="exchange quote price",
                quality=FactorQuality.DERIVED,
            )
        context = dict(context or {})
        cycle = context.get("cycle")
        if isinstance(cycle, dict):
            cycle = cycle.get("phase")
        if cycle is not None and not isinstance(cycle, str):
            cycle = None
        recorded = ResearchEvent(
            event_id=event.event_id,
            asset_id=event.asset_id,
            event_type=event.event_type,
            bias=bias,
            event_timestamp=event.market_event_timestamp,
            data_received_timestamp=event.data_received_timestamp,
            decision_timestamp=event.evaluation_timestamp,
            available_at=event.alert_created_timestamp,
            reference_price=event.price,
            source="fast_ignition",
            features=features,
            context=context,
            data_quality=event.data_quality,
            regime=event.regime,
            cycle=cycle,
            personal_watchlist=context.get("personal_watchlist"),
        )
        self.record_event(recorded)
        return recorded

    def _market_history(self, asset_id: str, as_of: datetime) -> list[MacroMarketObservation]:
        return [
            MacroMarketObservation.model_validate(item)
            for item in self.store.objects(
                "market_observation",
                as_of,
                asset_id=asset_id,
                limit=self.settings.maximum_observations_per_asset,
            )
        ]

    def evaluate(self, as_of: datetime) -> dict:
        """Write matured outcomes and misses with bounded archival work."""
        as_of = aware(as_of)
        events = [
            ResearchEvent.model_validate(item)
            for item in self.store.objects("research_event", as_of, limit=10000)
        ]
        known = {
            (item["event_id"], item["horizon_seconds"])
            for item in self.store.objects("research_outcome", as_of, limit=10000)
        }
        candidates = [
            event
            for event in events
            if any(
                event.event_timestamp + timedelta(seconds=horizon) <= as_of
                and (event.event_id, horizon) not in known
                for horizon in self.settings.horizons_seconds
            )
        ]
        # Rotate the bounded queue so permanently censored old events cannot starve newer signals.
        progress = self.store.states("event_learning_progress")
        cursor = progress[0] if progress and datetime.fromisoformat(progress[0]["as_of"]) <= as_of else None
        if cursor:
            key = (cursor["last_event_timestamp"], cursor["last_event_id"])
            candidates.sort(
                key=lambda item: (
                    (item.event_timestamp.isoformat(), item.event_id) <= key,
                    item.event_timestamp,
                    item.event_id,
                )
            )
        else:
            candidates.sort(key=lambda item: (item.event_timestamp, item.event_id))
        selected = candidates[: self.settings.maximum_events_per_pass]
        histories = {}
        matured, censored, pending = 0, 0, 0
        censor_reasons = {}
        issues = []
        evaluated_events = []
        for event in selected:
            if event.asset_id not in histories:
                if len(histories) >= self.settings.maximum_assets_per_pass:
                    issues.append("asset-per-pass budget exhausted; event evaluation deferred")
                    break
                histories[event.asset_id] = self._market_history(event.asset_id, as_of)
            evaluated_events.append(event)
            for horizon in self.settings.horizons_seconds:
                if (event.event_id, horizon) in known:
                    continue
                result = calculate_event_outcome(
                    event,
                    histories[event.asset_id],
                    as_of=as_of,
                    horizon_seconds=horizon,
                    settings=self.settings,
                )
                if result is None:
                    pending += 1
                elif result.status == "CENSORED":
                    censored += 1
                    for reason in result.reasons:
                        censor_reasons[reason] = censor_reasons.get(reason, 0) + 1
                else:
                    identity = hashlib.sha256(f"{event.event_id}|{horizon}".encode()).hexdigest()
                    self.store.append(
                        "research_outcome", identity, result.horizon_end, as_of, result, event.asset_id
                    )
                    matured += 1
        if evaluated_events:
            last = evaluated_events[-1]
            self.store.put_state(
                "event_learning_progress",
                "worker",
                as_of,
                {
                    "as_of": as_of.isoformat(),
                    "last_event_timestamp": last.event_timestamp.isoformat(),
                    "last_event_id": last.event_id,
                },
            )
        # Include observed assets without prior alerts; otherwise every miss would be hidden.
        observed = self.store.objects("market_observation", as_of, limit=10000)
        all_asset_ids = sorted({item["asset_id"] for item in observed})
        miss_cursor = self.store.states("miss_asset_progress")
        if miss_cursor and datetime.fromisoformat(miss_cursor[0]["as_of"]) <= as_of:
            last_asset = miss_cursor[0]["last_asset_id"]
            all_asset_ids.sort(key=lambda asset_id: (asset_id <= last_asset, asset_id))
        asset_ids = all_asset_ids[: self.settings.maximum_assets_per_pass]
        misses = []
        expansions = 0
        evaluated_assets = []
        for asset_id in asset_ids:
            if asset_id not in histories:
                if len(histories) >= self.settings.maximum_assets_per_pass:
                    issues.append("asset-per-pass budget exhausted; miss evaluation deferred")
                    continue
                histories[asset_id] = self._market_history(asset_id, as_of)
            evaluated_assets.append(asset_id)
            for bias in ("LONG", "SHORT"):
                miss, reason, expanded = self._detect_miss(asset_id, histories[asset_id], events, as_of, bias=bias)
                expansions += int(expanded)
                if miss:
                    misses.append(miss)
                elif reason:
                    issues.append(f"{asset_id} {bias}: {reason}")
        if evaluated_assets:
            self.store.put_state(
                "miss_asset_progress",
                "worker",
                as_of,
                {
                    "as_of": as_of.isoformat(),
                    "last_asset_id": evaluated_assets[-1],
                },
            )
        if not observed:
            issues.append("no archived market observations; outcome/miss coverage unavailable")
        return {
            "as_of": as_of.isoformat(),
            "status": "COMPLETE" if not issues else "PARTIAL",
            "events_available": len(events),
            "events_evaluated": len(evaluated_events),
            "events_deferred": len(candidates) - len(evaluated_events),
            "matured_outcomes": matured,
            "censored_outcomes": censored,
            "censor_reasons": censor_reasons,
            "pending_outcomes": pending,
            "observed_assets_evaluated": len(evaluated_assets),
            "observed_assets_deferred": len(all_asset_ids) - len(evaluated_assets),
            "misses_recorded": len(misses),
            "expansions_recorded": expansions,
            "misses": [item.model_dump(mode="json") for item in misses],
            "issues": list(dict.fromkeys(issues)),
            "coverage_scope": "MONITORED_ARCHIVED_ONLY",
            "data_quality": {
                "event_archive": "AVAILABLE" if events else "UNAVAILABLE",
                "market_archive": "AVAILABLE" if observed else "UNAVAILABLE",
            },
            "live_trading": False,
        }

    def _detect_miss(
        self,
        asset_id: str,
        history: list[MacroMarketObservation],
        events: list[ResearchEvent],
        as_of: datetime,
        *,
        bias: Literal["LONG", "SHORT"] = "LONG",
    ) -> tuple[MissRecord | None, str | None, bool]:
        direction = -1 if bias == "SHORT" else 1
        intervals = [
            item
            for item in history
            if item.resolution_seconds > 0 and item.data_quality == MacroQuality.AVAILABLE
        ]
        if not intervals:
            return None, "no available completed intervals for miss evaluation", False
        latest = max(intervals, key=lambda item: item.market_event_timestamp)
        end = latest.market_event_timestamp
        baseline_at = end - timedelta(seconds=self.settings.expansion_window_seconds)
        baselines = [item for item in intervals if item.market_event_timestamp == baseline_at]
        if not baselines:
            return None, "insufficient observed rolling-window baseline; no inferred miss", False
        baseline = max(baselines, key=lambda item: item.resolution_seconds)
        path, reason = _observed_intervals(intervals, baseline_at, end, as_of)
        if reason:
            return None, f"miss coverage incomplete: {reason}", False
        if baseline.source != path[0].source:
            return None, "baseline and expansion interval sources differ", False
        material = next(
            (
                item
                for item in path
                if direction * (item.price / baseline.price - 1) + 1e-12 >= self.settings.expansion_return
            ),
            None,
        )
        if material is None:
            return None, None, False
        prior_progress = next(
            (item for item in self.store.states("miss_progress")
             if item["asset_id"] == asset_id and item.get("bias", "LONG") == bias), None
        )
        if (
            prior_progress
            and datetime.fromisoformat(prior_progress.get("known_at", prior_progress["detected_event_at"]))
            <= as_of
            and end
            < datetime.fromisoformat(prior_progress["detected_event_at"])
            + timedelta(seconds=self.settings.miss_cooldown_seconds)
        ):
            return None, None, False
        onset = next(
            item
            for item in path
            if direction * (item.price / baseline.price - 1) + 1e-12 >= self.settings.expansion_onset_return
        )
        eligible = [
            event
            for event in events
            if event.asset_id == asset_id and event.bias == bias and baseline_at <= event.event_timestamp <= end
        ]
        earlier = [
            event
            for event in eligible
            if event.event_type in PRE_ALERTS
            and (
                event.available_at
                <= onset.market_event_timestamp - timedelta(seconds=self.settings.minimum_alert_lead_seconds)
            )
        ]
        confirmed = [
            event
            for event in eligible
            if event.event_type in BREAKOUT_ALERTS
            and (
                event.available_at
                <= onset.market_event_timestamp + timedelta(seconds=self.settings.breakout_latency_seconds)
            )
        ]
        kind = (
            "FULL_MISS"
            if not earlier and not confirmed
            else "PRE_WARNING_MISS"
            if not earlier
            else "TRIGGER_MISS"
        )
        prior = [event for event in eligible if event.available_at <= onset.market_event_timestamp]
        meaningful = [event for event in eligible if event.event_type in MEANINGFUL_ALERTS]
        first = min(meaningful, key=lambda event: event.available_at) if meaningful else None
        first_breakout = min(
            (event for event in eligible if event.event_type in BREAKOUT_ALERTS),
            key=lambda event: event.available_at,
            default=None,
        )
        lead_times = {}
        for event_type in sorted(MEANINGFUL_ALERTS):
            relevant = [event for event in eligible if event.event_type == event_type]
            earliest = min(relevant, key=lambda event: event.available_at) if relevant else None
            lead_times[event_type] = (
                (onset.market_event_timestamp - earliest.available_at).total_seconds() if earliest else None
            )
        identity_input = f"{asset_id}|{onset.market_event_timestamp.isoformat()}|{self.settings.version}"
        if bias == "SHORT":
            identity_input += "|SHORT"
        identity = hashlib.sha256(identity_input.encode()).hexdigest()
        definition = (f"first completed close >= {self.settings.expansion_onset_return:.4f} "
                      f"{'below' if bias == 'SHORT' else 'above'} rolling {self.settings.expansion_window_seconds}s baseline")
        expansion_record = ObservedExpansion(
            expansion_id=identity,
            asset_id=asset_id,
            bias=bias,
            detected_at=as_of,
            baseline_timestamp=baseline_at,
            baseline_price=baseline.price,
            expansion_start=onset.market_event_timestamp,
            expansion_confirmed_at=material.market_event_timestamp,
            expansion_magnitude=direction * (material.price / baseline.price - 1),
            first_meaningful_alert=first.available_at if first else None,
            first_breakout_alert=first_breakout.available_at if first_breakout else None,
            lead_times_seconds=lead_times,
            had_early_warning=bool(earlier),
            had_timely_breakout_alert=bool(confirmed),
            definition=definition,
            source=path[0].source,
        )
        self.store.append(
            "expansion_event", identity, material.market_event_timestamp, as_of, expansion_record, asset_id
        )
        self.store.put_state(
            "miss_progress",
            asset_id if bias == "LONG" else f"{asset_id}:SHORT",
            as_of,
            {"asset_id": asset_id, "bias": bias, "detected_event_at": end.isoformat(), "known_at": as_of.isoformat()},
        )
        if earlier and confirmed:
            return None, None, True
        discovery = self.store.asset_history(asset_id, before=onset.market_event_timestamp, limit=1)
        evidence = discovery[-1] if discovery else None
        miss = MissRecord(
            miss_id=identity,
            asset_id=asset_id,
            bias=bias,
            detected_at=as_of,
            baseline_timestamp=baseline_at,
            baseline_price=baseline.price,
            expansion_start=onset.market_event_timestamp,
            expansion_confirmed_at=material.market_event_timestamp,
            expansion_start_definition=definition,
            expansion_magnitude=direction * (material.price / baseline.price - 1),
            kind=kind,
            first_system_detection=first.available_at if first else None,
            first_breakout_alert=first_breakout.available_at if first_breakout else None,
            pre_move_event_ids=[event.event_id for event in prior],
            lead_times_seconds=lead_times,
            pre_move_features=evidence.model_dump(mode="json") if evidence else {},
            pre_move_rank=evidence.rank.current_rank if evidence else None,
            pre_move_stage=evidence.stage.value if evidence else None,
            rejection_reasons=evidence.reasons if evidence else [],
            missing_data=evidence.missing if evidence else ["pre-move discovery archive unavailable"],
            source=path[0].source,
            reason="Observed material expansion lacked an early warning, timely breakout alert, or both; causal diagnosis requires later research.",
        )
        self.store.append("miss_event", identity, material.market_event_timestamp, as_of, miss, asset_id)
        return miss, None, True

    def daily_report(self, as_of: datetime) -> dict:
        """Descriptive known-horizon statistics; never simulated or claimed PnL."""
        as_of = aware(as_of)
        start = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
        events = [
            ResearchEvent.model_validate(item)
            for item in self.store.objects("research_event", as_of, limit=10000)
        ]
        today = [event for event in events if start <= event.available_at <= as_of]
        outcomes = [
            EventOutcome.model_validate(item)
            for item in self.store.objects("research_outcome", as_of, limit=10000)
            if item["status"] == "MATURED"
        ]
        known = [item for item in outcomes if item.status == "MATURED" and item.false_positive is not None]
        misses = [
            MissRecord.model_validate(item) for item in self.store.objects("miss_event", as_of, limit=10000)
        ]
        today_misses = [item for item in misses if start <= item.detected_at <= as_of]
        types = sorted({event.event_type for event in today})
        regimes = sorted({event.regime or "UNAVAILABLE" for event in today})
        expansions = [
            ObservedExpansion.model_validate(item)
            for item in self.store.objects("expansion_event", as_of, limit=10000)
        ]
        today_expansions = [item for item in expansions if start <= item.detected_at <= as_of]
        lead_values = [
            value
            for item in today_expansions
            for value in item.lead_times_seconds.values()
            if value is not None
        ]
        outcome_regimes = sorted({item.regime or "UNAVAILABLE" for item in outcomes})
        return {
            "as_of": as_of.isoformat(),
            "day_start_utc": start.isoformat(),
            "events_today": len(today),
            "event_counts": {name: sum(event.event_type == name for event in today) for name in types},
            "regime_counts": {
                name: sum((event.regime or "UNAVAILABLE") == name for event in today) for name in regimes
            },
            "known_mature_false_positive_observations": len(known),
            "false_positive_rate": mean(item.false_positive for item in known) if known else None,
            "false_positive_scope": "all known matured configured-horizon observations published by as_of",
            "misses_today": len(today_misses),
            "expansions_today": len(today_expansions),
            "mean_available_alert_lead_seconds": mean(lead_values) if lead_values else None,
            "mean_lead_time_by_stage": {
                stage: mean(available)
                if (
                    available := [
                        item.lead_times_seconds[stage]
                        for item in today_expansions
                        if item.lead_times_seconds.get(stage) is not None
                    ]
                )
                else None
                for stage in sorted(MEANINGFUL_ALERTS)
            },
            "lead_times_by_expansion": {
                item.expansion_id: item.lead_times_seconds for item in today_expansions
            },
            "lead_times_by_miss": {item.miss_id: item.lead_times_seconds for item in today_misses},
            "regime_conditioned_outcomes": {
                regime: {
                    "matured_horizons": sum((item.regime or "UNAVAILABLE") == regime for item in outcomes),
                    "horizons": {
                        str(horizon): {
                            "sample_size": len(group),
                            "mean_future_close_return": mean(item.future_close_return for item in group),
                            "mean_mfe": mean(item.mfe for item in group),
                            "mean_mae": mean(item.mae for item in group),
                        }
                        for horizon in sorted(
                            {
                                item.horizon_seconds
                                for item in outcomes
                                if (item.regime or "UNAVAILABLE") == regime
                            }
                        )
                        if (
                            group := [
                                item
                                for item in outcomes
                                if (item.regime or "UNAVAILABLE") == regime
                                and item.horizon_seconds == horizon
                            ]
                        )
                    },
                }
                for regime in outcome_regimes
            },
            "outcome_count": len(outcomes),
            "coverage_scope": "MONITORED_ARCHIVED_ONLY",
            "false_positive_definition": f"directional favorable excursion below {self.settings.false_positive_return:.4f} over {self.settings.false_positive_horizon_seconds}s; only complete matured paths",
            "outcome_direction_counts": {
                bias: sum(item.bias == bias for item in outcomes) for bias in ("LONG", "SHORT", "NEUTRAL")
            },
            "warnings": [
                "Descriptive observed sample; overlapping signals are not independent observations.",
                "Unobserved/unarchived assets are outside miss-recall denominators.",
            ],
            "live_trading": False,
        }
