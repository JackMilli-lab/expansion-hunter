"""Outcome-blind historical analogues with auditable point-in-time normalization.

Distances are descriptive research similarities, never calibrated probabilities.
Reference features, not subsequent returns, fit the normalizer. Outcome access is
a separate operation after matching; no model fitting or threshold optimization
is performed here. Downloaded-today history must retain its actual availability.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta
from enum import StrEnum
from statistics import mean, pstdev
from typing import Literal

from pydantic import Field, model_validator

from .models import TIMEFRAMES, Candle, Model, aware


class FactorQuality(StrEnum):
    AVAILABLE = "AVAILABLE"
    NATIVE = "NATIVE"
    DERIVED = "DERIVED"
    ESTIMATED = "ESTIMATED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class FactorValue(Model):
    value: float | None = None
    event_timestamp: datetime
    available_at: datetime
    source: str = Field(min_length=1)
    unit: str = ""
    quality: FactorQuality = FactorQuality.UNAVAILABLE
    reason: str | None = None
    role: Literal["OBSERVED_FEATURE"] = "OBSERVED_FEATURE"

    @model_validator(mode="after")
    def causal(self):
        if self.available_at < self.event_timestamp:
            raise ValueError("factor availability precedes its event")
        if self.quality not in (FactorQuality.UNAVAILABLE, FactorQuality.STALE) and self.value is None:
            raise ValueError("available factor requires a numeric value")
        return self

    def usable(self, decision_time: datetime, *, allow_estimates: bool = False) -> bool:
        return bool(
            self.value is not None
            and self.event_timestamp <= decision_time
            and self.available_at <= decision_time
            and self.quality not in (FactorQuality.STALE, FactorQuality.UNAVAILABLE)
            and (allow_estimates or self.quality != FactorQuality.ESTIMATED)
        )


class AnalogVector(Model):
    event_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    decision_time: datetime
    available_at: datetime
    factors: dict[str, FactorValue] = Field(default_factory=dict)
    reference_price: FactorValue | None = None
    regime: str | None = None
    cycle: str | None = None
    macro_context: str | None = None
    schema_version: str = "analog-v1"


DEFAULT_FACTORS = (
    "price_change",
    "volatility",
    "relative_volume",
    "volume_acceleration",
    "drawdown",
    "trend",
    "rsi14",
    "macd",
    "ema_distance",
    "vwap_distance",
    "avwap_distance",
    "range_position",
    "range_compression",
    "resistance_pressure",
    "relative_strength_btc",
    "breadth",
    "funding",
    "oi_change",
    "liquidity_quality",
    "fib_confluence",
)


class AnalogSettings(Model):
    feature_names: list[str] = Field(default_factory=lambda: list(DEFAULT_FACTORS), min_length=1)
    metric: Literal["euclidean", "cosine"] = "euclidean"
    normalization: Literal["zscore", "robust_zscore"] = "robust_zscore"
    minimum_reference_samples: int = Field(default=3, ge=3)
    minimum_features: int = Field(default=3, ge=1)
    minimum_coverage: float = Field(default=0.75, gt=0, le=1)
    maximum_matches: int = Field(default=10, ge=1, le=1000)
    maximum_matches_per_asset: int = Field(default=1, ge=1)
    same_regime: bool = False
    same_cycle: bool = False
    allow_estimates: bool = False
    minimum_separation_seconds: int = Field(default=86400, ge=0)

    @model_validator(mode="after")
    def observed_only(self):
        if len(self.feature_names) != len(set(self.feature_names)):
            raise ValueError("duplicate matching feature")
        for name in self.feature_names:
            lowered = name.lower()
            if lowered.startswith(("future_", "outcome_", "label_", "time_to_")) or lowered in ("mfe", "mae"):
                raise ValueError("future outcome labels may not be matching features")
        return self


class NormalizationFactor(Model):
    feature: str
    center: float
    scale: float = Field(gt=0)
    unit: str
    sample_count: int = Field(ge=3)
    reference_event_ids: list[str]
    latest_available_at: datetime


class AnalogMatch(Model):
    event_id: str
    asset_id: str
    decision_time: datetime
    available_at: datetime
    reference_price: FactorValue | None = None
    regime: str | None = None
    cycle: str | None = None
    macro_context: str | None = None
    distance: float = Field(ge=0)
    similarity: float = Field(ge=0, le=1)
    coverage: float = Field(ge=0, le=1)
    common_features: list[str]
    normalized_reference: dict[str, float]


class AnalogReport(Model):
    query_event_id: str
    query_asset_id: str
    decision_time: datetime
    settings: AnalogSettings
    status: Literal["MATCHED", "INSUFFICIENT_DATA"]
    label: Literal["HISTORICAL RESEARCH — NOT FORECAST"] = "HISTORICAL RESEARCH — NOT FORECAST"
    similarity_is_probability: Literal[False] = False
    normalizer: dict[str, NormalizationFactor] = Field(default_factory=dict)
    normalized_query: dict[str, float] = Field(default_factory=dict)
    matches: list[AnalogMatch] = Field(default_factory=list)
    excluded: dict[str, list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _candidate_reasons(reference: AnalogVector, query: AnalogVector, settings: AnalogSettings) -> list[str]:
    reasons = []
    if reference.event_id == query.event_id:
        reasons.append("query event excluded")
    if reference.decision_time >= query.decision_time:
        reasons.append("reference decision is not before query")
    if reference.available_at > query.decision_time:
        reasons.append("reference was unavailable at query decision")
    if reference.available_at > reference.decision_time:
        reasons.append("reference snapshot was not available at its own decision")
    if reference.schema_version != query.schema_version:
        reasons.append("feature schema versions differ")
    if settings.same_regime and (query.regime is None or reference.regime != query.regime):
        reasons.append("regime unavailable or different")
    if settings.same_cycle and (query.cycle is None or reference.cycle != query.cycle):
        reasons.append("cycle unavailable or different")
    return reasons


def match_analogs(
    query: AnalogVector,
    references: list[AnalogVector],
    settings: AnalogSettings | None = None,
) -> AnalogReport:
    """Fit only dated reference features, then find deterministic nearest states.

    A candidate factor must have existed at its own decision, not merely at the
    present query. Missing/constant features are excluded, never filled with zero.
    Outcome fields cannot be provided through the typed matching interface.
    """
    settings = settings or AnalogSettings()
    if query.available_at > query.decision_time:
        raise ValueError("query was unavailable at its decision time")
    event_ids = [reference.event_id for reference in references]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("duplicate reference event IDs")
    excluded: dict[str, list[str]] = {}
    candidates = []
    for reference in sorted(references, key=lambda item: (item.decision_time, item.event_id)):
        reasons = _candidate_reasons(reference, query, settings)
        if reasons:
            excluded[reference.event_id] = reasons
        else:
            candidates.append(reference)
    normalizer = {}
    warnings = []
    for name in settings.feature_names:
        query_factor = query.factors.get(name)
        if query_factor is None or not query_factor.usable(
            query.decision_time, allow_estimates=settings.allow_estimates
        ):
            warnings.append(f"{name}: query feature unavailable at decision")
            continue
        usable = [
            (reference, reference.factors[name])
            for reference in candidates
            if name in reference.factors
            and reference.factors[name].usable(
                reference.decision_time, allow_estimates=settings.allow_estimates
            )
            and reference.factors[name].unit == query_factor.unit
        ]
        if len(usable) < settings.minimum_reference_samples:
            warnings.append(f"{name}: insufficient dated, unit-compatible reference samples")
            continue
        values = [factor.value for _, factor in usable]
        if settings.normalization == "zscore":
            center, scale = mean(values), pstdev(values)
        else:
            center = _quantile(values, 0.5)
            scale = 1.4826 * _quantile([abs(value - center) for value in values], 0.5)
        if scale <= 1e-12:
            warnings.append(f"{name}: constant or zero-scale reference feature excluded")
            continue
        normalizer[name] = NormalizationFactor(
            feature=name,
            center=center,
            scale=scale,
            unit=query_factor.unit,
            sample_count=len(usable),
            reference_event_ids=[reference.event_id for reference, _ in usable],
            latest_available_at=max(factor.available_at for _, factor in usable),
        )
    normalized_query = {
        name: (query.factors[name].value - factor.center) / factor.scale
        for name, factor in normalizer.items()
    }
    report = AnalogReport(
        query_event_id=query.event_id,
        query_asset_id=query.asset_id,
        decision_time=query.decision_time,
        settings=settings,
        status="INSUFFICIENT_DATA",
        normalizer=normalizer,
        normalized_query=normalized_query,
        excluded=excluded,
        warnings=warnings,
    )
    query_coverage = sum(
        bool(
            name in query.factors
            and query.factors[name].usable(
                query.decision_time,
                allow_estimates=settings.allow_estimates,
            )
        )
        for name in settings.feature_names
    ) / len(settings.feature_names)
    if query_coverage < settings.minimum_coverage:
        report.warnings.append("insufficient declared query-feature coverage")
        return report
    if len(normalizer) < settings.minimum_features:
        report.warnings.append("insufficient usable normalized features")
        return report
    prospective = []
    for reference in candidates:
        common = [
            name
            for name in normalizer
            if name in reference.factors
            and reference.factors[name].unit == normalizer[name].unit
            and reference.factors[name].usable(
                reference.decision_time, allow_estimates=settings.allow_estimates
            )
        ]
        coverage = len(common) / len(normalizer)
        if len(common) < settings.minimum_features or coverage < settings.minimum_coverage:
            report.excluded[reference.event_id] = ["insufficient common feature coverage"]
            continue
        normalized_reference = {
            name: (reference.factors[name].value - normalizer[name].center) / normalizer[name].scale
            for name in common
        }
        q = [normalized_query[name] for name in common]
        r = [normalized_reference[name] for name in common]
        if settings.metric == "euclidean":
            # RMS distance avoids rewarding a vector just for having fewer fields.
            distance = math.sqrt(mean((left - right) ** 2 for left, right in zip(q, r, strict=True)))
            similarity = 1 / (1 + distance)
        else:
            norm = math.sqrt(sum(value * value for value in q) * sum(value * value for value in r))
            if norm <= 1e-12:
                report.excluded[reference.event_id] = ["cosine undefined for zero-length normalized vector"]
                continue
            cosine = max(-1.0, min(1.0, sum(left * right for left, right in zip(q, r, strict=True)) / norm))
            distance, similarity = 1 - cosine, (1 + cosine) / 2
        prospective.append(
            AnalogMatch(
                event_id=reference.event_id,
                asset_id=reference.asset_id,
                decision_time=reference.decision_time,
                available_at=reference.available_at,
                reference_price=reference.reference_price,
                regime=reference.regime,
                cycle=reference.cycle,
                macro_context=reference.macro_context,
                distance=distance,
                similarity=similarity,
                coverage=coverage,
                common_features=common,
                normalized_reference=normalized_reference,
            )
        )
    for candidate in sorted(prospective, key=lambda item: (item.distance, item.event_id)):
        same_asset = [match for match in report.matches if match.asset_id == candidate.asset_id]
        if len(same_asset) >= settings.maximum_matches_per_asset or any(
            abs((candidate.decision_time - match.decision_time).total_seconds())
            < settings.minimum_separation_seconds
            for match in same_asset
        ):
            report.excluded[candidate.event_id] = ["asset concentration or overlapping-state deduplication"]
            continue
        report.matches.append(candidate)
        if len(report.matches) >= settings.maximum_matches:
            break
    if report.matches:
        report.status = "MATCHED"
    else:
        report.warnings.append("no eligible historical matches")
    return report


class AnalogOutcome(Model):
    event_id: str
    asset_id: str
    horizon_seconds: int = Field(gt=0)
    horizon_end: datetime
    evaluated_at: datetime
    status: Literal["MATURED", "PENDING", "CENSORED"]
    available_at: datetime | None = None
    close_return: float | None = None
    mfe: float | None = None
    mae: float | None = None
    expansion_threshold_hits: dict[str, bool] = Field(default_factory=dict)
    reason: str | None = None
    source: str | None = None


def attach_outcomes(
    report: AnalogReport,
    histories: dict[str, list[Candle]],
    *,
    as_of: datetime,
    timeframe: str = "1m",
    horizons_seconds: tuple[int, ...] = (900, 3600, 14400, 86400, 259200, 604800),
) -> list[AnalogOutcome]:
    """Evaluate only matched events after full horizons, without changing matches.

    Histories are keyed by matched event ID. Candle outcomes require exact UTC
    alignment and full closed contiguous coverage; partial bars are censored.
    Trade-level data is required for exact outcomes of sub-candle event times.
    """
    as_of = aware(as_of)
    if as_of < report.decision_time:
        raise ValueError("outcome evaluation precedes matching decision")
    if timeframe not in TIMEFRAMES:
        raise ValueError("unsupported outcome timeframe")
    duration = TIMEFRAMES[timeframe]
    if any(horizon <= 0 or horizon % duration for horizon in horizons_seconds):
        raise ValueError("outcome horizons must be positive multiples of candle duration")
    output = []
    for match in report.matches:
        for horizon in horizons_seconds:
            end = match.decision_time + timedelta(seconds=horizon)
            result = AnalogOutcome(
                event_id=match.event_id,
                asset_id=match.asset_id,
                horizon_seconds=horizon,
                horizon_end=end,
                evaluated_at=as_of,
                status="CENSORED",
            )
            if end > as_of:
                result.status, result.reason = "PENDING", "future outcome horizon has not matured"
                output.append(result)
                continue
            price = match.reference_price
            if price is None or not price.usable(match.decision_time) or price.value <= 0:
                result.reason = "usable point-in-time reference price unavailable"
                output.append(result)
                continue
            bars = sorted(
                [
                    bar
                    for bar in histories.get(match.event_id, [])
                    if bar.open_time >= match.decision_time
                    and bar.close_time <= end
                    and bar.available_at <= as_of
                ],
                key=lambda bar: bar.open_time,
            )
            expected = horizon // duration
            valid = (
                match.decision_time.timestamp() % duration == 0
                and len(bars) == expected
                and len({bar.open_time for bar in bars}) == expected
                and len({bar.source for bar in bars}) == 1
                and all(
                    bar.open_time == match.decision_time + timedelta(seconds=index * duration)
                    and bar.close_time == bar.open_time + timedelta(seconds=duration)
                    for index, bar in enumerate(bars)
                )
            )
            if not valid:
                result.reason = "incomplete, gapped, duplicate, mixed-source or unaligned outcome candles"
            else:
                result.status = "MATURED"
                result.available_at = max(bar.available_at for bar in bars)
                result.source = bars[0].source
                result.close_return = bars[-1].close / price.value - 1
                result.mfe = max(bar.high for bar in bars) / price.value - 1
                result.mae = min(bar.low for bar in bars) / price.value - 1
                result.expansion_threshold_hits = {
                    str(threshold): result.mfe + 1e-12 >= threshold
                    for threshold in (0.10, 0.20, 0.30, 0.50, 1.0)
                }
            output.append(result)
    return output


def benchmark_metrics(
    query: AnalogVector,
    references: list[AnalogVector],
    settings: AnalogSettings | None = None,
) -> dict[str, dict[str, float | int | str]]:
    """Measure executable matching time/coverage; this does not measure an edge."""
    settings = settings or AnalogSettings()
    measured = {}
    for metric in ("euclidean", "cosine"):
        started = time.perf_counter()
        report = match_analogs(query, references, settings.model_copy(update={"metric": metric}))
        measured[metric] = {
            "duration_seconds": time.perf_counter() - started,
            "matches": len(report.matches),
            "status": report.status,
            "usable_features": len(report.normalizer),
            "reference_count": len(references),
        }
    return measured
