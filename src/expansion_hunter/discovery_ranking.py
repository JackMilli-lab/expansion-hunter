"""Causal cross-sectional discovery ranking and comparable-cohort migration.

The deterministic weights are uncalibrated hypotheses, never probabilities.
Missing evidence earns no contribution and remains visible in score coverage.
Rank velocity uses percentile points/hour; acceleration uses points/hour².
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import timedelta
from statistics import median

from .discovery_config import DiscoverySettings
from .discovery_models import (
    DiscoveryCycle,
    DiscoveryFeature,
    DiscoveryObservation,
    DiscoveryScore,
    Freshness,
    RankHistory,
    ScoreComponent,
)

FORMULA_VERSION = "phase1-v2-native-cohorts"
DIRECTIONAL_FORMULA_VERSION = "phase1-v3-native-directional-cohorts"
SIGNED_FEATURES = {"price_change", "relative_strength_btc", "relative_strength_market"}
# This immutable hypothesis is shared by current and archived recomputation.
FEATURE_WEIGHTS = {
    "price_change": 5.0,
    "relative_volume": 10.0,
    "volume_acceleration": 15.0,
    "turnover": 5.0,
    "volatility": 10.0,
    "range_compression": 15.0,
    "distance_recent_high": 10.0,
    "relative_strength_btc": 10.0,
    "relative_strength_market": 10.0,
    "liquidity_quality": 10.0,
}
RANK_WINDOWS = {
    "15m": timedelta(minutes=15), "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1), "3h": timedelta(hours=3),
    "6h": timedelta(hours=6), "24h": timedelta(hours=24),
}


def ranking_formula_version(settings: DiscoverySettings) -> str:
    """A two-sided rank is never compared with a legacy long-only rank."""
    return DIRECTIONAL_FORMULA_VERSION if settings.ranking_mode == "BOTH" else FORMULA_VERSION


def _names(settings: DiscoverySettings) -> tuple[str, ...]:
    return tuple(FEATURE_WEIGHTS) + (("distance_recent_low",) if settings.ranking_mode == "BOTH" else ())


def _weights(settings: DiscoverySettings) -> dict[str, float]:
    weights = dict(FEATURE_WEIGHTS)
    configured = settings.score_weights.get("light_discovery", {})
    if set(configured) - set(weights):
        raise ValueError("unknown light_discovery feature weights: " + str(sorted(set(configured) - set(weights))))
    weights.update(configured)
    return weights


def _ranking_signature(settings: DiscoverySettings) -> tuple:
    """Only changes that affect broad score comparability enter this signature."""
    return (settings.ranking_mode, settings.light_timeframe, settings.light_history_bars, settings.minimum_cross_section,
            settings.minimum_history_samples, tuple(sorted(_weights(settings).items())))


def ranking_history_compatible(cycle: DiscoveryCycle, settings: DiscoverySettings) -> bool:
    """Require the same ranking direction/formula and baseline configuration."""
    if cycle.code_version != ranking_formula_version(settings):
        return False
    if settings.ranking_mode == "LONG":
        # Preserve existing legacy archives, whose fixtures may predate configs.
        return True
    archived = cycle.config.get("discovery")
    if not isinstance(archived, dict) or archived.get("ranking_mode") != "BOTH":
        return False
    if any(prior.ranking_mode != "BOTH" for prior in cycle.observations):
        return False
    try:
        return _ranking_signature(DiscoverySettings.model_validate(archived)) == _ranking_signature(settings)
    except (TypeError, ValueError):
        return False


def _usable(feature: DiscoveryFeature | None, observation: DiscoveryObservation) -> bool:
    return bool(
        feature is not None and feature.value is not None and math.isfinite(feature.value)
        and feature.freshness in (Freshness.FRESH, Freshness.DEGRADED)
        and feature.source_timestamp is not None and feature.retrieved_at is not None
        and feature.source_timestamp <= observation.as_of
        and feature.retrieved_at <= observation.as_of
    )


def _eligible(observation: DiscoveryObservation) -> bool:
    return (
        observation.eligible and observation.disposition not in ("EXCLUDED", "FAILED")
        and observation.freshness in (Freshness.FRESH, Freshness.DEGRADED)
        and not any(f.critical and not _usable(f, observation) for f in observation.features.values())
    )


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    """Midrank percentile: ties are identical, endpoints are 0/1, all ties .5."""
    ordered = sorted(values.values())
    count = len(ordered)
    if count < 2:
        return {}
    return {
        asset_id: 1 - ((bisect_left(ordered, value) + bisect_right(ordered, value) - 1) / 2)
        / (count - 1)
        for asset_id, value in values.items()
    }


def _raw_percentiles(values: dict[str, float]) -> dict[str, float]:
    return {key: 1 - value for key, value in _percentiles(values).items()}


def _beneficial(percentile: float, direction: int) -> float:
    return percentile if direction == 1 else 1 - percentile


def _provider_family(provider: str) -> str:
    """Group the same native venue/measurement across different instruments.

    Provenance strings include the instrument by design; it must not create a
    singleton cross-sectional cohort. Keep origin, method, benchmark tag, frame
    and units intact. Never collapse unrelated providers or estimated sources.
    """
    return re.sub(r"binance/spot/[A-Z0-9]+/", "binance/spot/<instrument>/", provider)


def _history(cycles: list[DiscoveryCycle], current: DiscoveryObservation, settings: DiscoverySettings) -> list[DiscoveryCycle]:
    return sorted(
        (cycle for cycle in cycles if cycle.as_of < current.as_of
         and cycle.completed_at <= current.as_of and ranking_history_compatible(cycle, settings)),
        key=lambda cycle: cycle.as_of,
    )


def _populate_market_strength(observations: list[DiscoveryObservation]) -> None:
    """Leave-one-out market return from genuinely aligned usable returns."""
    groups: dict[tuple, list[DiscoveryObservation]] = defaultdict(list)
    for observation in observations:
        feature = observation.features.get("price_change")
        if _eligible(observation) and _usable(feature, observation):
            groups[(feature.timeframe, feature.source_timestamp, feature.unit)].append(observation)
    for group in groups.values():
        for observation in group:
            own = observation.features["price_change"]
            peers = [other.features["price_change"].value for other in group
                     if other.asset.asset_id != observation.asset.asset_id]
            if not peers:
                continue
            observation.features["relative_strength_market"] = DiscoveryFeature(
                value=own.value - median(peers), unit=own.unit,
                source_timestamp=own.source_timestamp,
                retrieved_at=max(other.features["price_change"].retrieved_at for other in group),
                provider="derived:leave-one-out-market", timeframe=own.timeframe,
                freshness=max((other.features["price_change"].freshness for other in group),
                              key=lambda f: f == Freshness.DEGRADED),
                sample_count=len(peers), direction=1,
                reason="Asset return less aligned leave-one-out market median; peers exclude this asset",
            )


def _populate_historical(
    observations: list[DiscoveryObservation], cycles: list[DiscoveryCycle], settings: DiscoverySettings,
) -> None:
    indexed: dict[str, list[tuple[DiscoveryCycle, DiscoveryObservation]]] = defaultdict(list)
    for cycle in cycles:
        for prior in cycle.observations:
            indexed[prior.asset.asset_id].append((cycle, prior))
    for observation in observations:
        for name in _names(settings):
            feature = observation.features.get(name)
            if not _usable(feature, observation) or feature.historical_percentile is not None:
                continue
            samples = {}
            for cycle, prior in indexed[observation.asset.asset_id]:
                candidate = prior.features.get(name)
                if (ranking_history_compatible(cycle, settings) and cycle.completed_at <= observation.as_of
                        and cycle.as_of < observation.as_of and prior.as_of <= cycle.as_of
                        and prior.as_of < observation.as_of and _usable(candidate, prior)
                        and candidate.provider == feature.provider and candidate.timeframe == feature.timeframe
                        and candidate.direction == feature.direction and candidate.unit == feature.unit):
                    # Polling one closed candle repeatedly cannot manufacture a baseline.
                    key = candidate.source_timestamp
                    if key not in samples or candidate.retrieved_at > samples[key][0]:
                        samples[key] = (candidate.retrieved_at, candidate.value)
            values = [sample[1] for sample in samples.values()]
            if len(values) >= settings.minimum_history_samples:
                feature.historical_percentile = (
                    sum(value < feature.value for value in values)
                    + 0.5 * sum(value == feature.value for value in values)
                ) / len(values)
                feature.sample_count = max(feature.sample_count, len(values))


def _score(
    observation: DiscoveryObservation, cross: dict[str, dict[str, float]],
    settings: DiscoverySettings,
    mask: set[str] | None = None,
    *, bias: str = "LONG",
) -> DiscoveryScore:
    weights = _weights(settings)
    total_weight = sum(weights.values())
    components = []
    for slot, weight in weights.items():
        name = "distance_recent_low" if bias == "SHORT" and slot == "distance_recent_high" else slot
        feature = observation.features.get(name)
        usable = _usable(feature, observation) and (mask is None or name in mask)
        direction = feature.direction if feature else None
        if direction is not None and bias == "SHORT" and name in SIGNED_FEATURES:
            direction *= -1
        for channel in ("historical", "cross_sectional"):
            percentile = None
            if usable:
                percentile = (
                    feature.historical_percentile if channel == "historical"
                    else cross.get(name, {}).get(observation.asset.asset_id)
                )
            normalized = _beneficial(percentile, direction) if percentile is not None else None
            components.append(ScoreComponent(
                name=f"{name}:{channel}", weight=weight / 2, normalized=normalized,
                contribution=(100 * weight / 2 * normalized / total_weight if normalized is not None else None),
                reason="Observed causal percentile" if normalized is not None else "Evidence unavailable",
                inputs={"percentile": percentile, "direction": direction, "bias": bias},
            ))
    lower = sum(component.contribution or 0 for component in components)
    coverage = sum(component.weight for component in components if component.normalized is not None) / total_weight
    return DiscoveryScore(
        value=lower if coverage else None, coverage=coverage,
        lower_bound=lower, upper_bound=min(100, lower + 100 * (1 - coverage)),
        formula_version=ranking_formula_version(settings), components=components,
    )


def _directional_scores(observation, cross, settings, mask=None) -> tuple[DiscoveryScore, DiscoveryScore | None, str]:
    long = _score(observation, cross, settings, mask, bias="LONG")
    if settings.ranking_mode == "LONG":
        return long, None, "LONG" if long.value is not None else "UNAVAILABLE"
    short = _score(observation, cross, settings, mask, bias="SHORT")
    if long.value is None and short.value is None:
        return long, short, "UNAVAILABLE"
    if math.isclose(long.lower_bound, short.lower_bound, rel_tol=0, abs_tol=1e-12):
        return long, short, "NEUTRAL"
    return long, short, "SHORT" if short.lower_bound > long.lower_bound else "LONG"


def _selected_score(long: DiscoveryScore, short: DiscoveryScore | None) -> DiscoveryScore:
    return short if short and short.lower_bound > long.lower_bound else long


def _cross_sections(
    observations: list[DiscoveryObservation], settings: DiscoverySettings, mask: set[str] | None = None,
) -> dict[str, dict[str, float]]:
    cross = {}
    for name in _names(settings):
        if mask is not None and name not in mask:
            continue
        # Different return windows, sources and units never share a percentile.
        groups: dict[tuple, dict[str, float]] = defaultdict(dict)
        for observation in observations:
            feature = observation.features.get(name)
            if _eligible(observation) and _usable(feature, observation):
                endpoint = feature.source_timestamp if name in (
                    "price_change", "relative_strength_btc", "relative_strength_market",
                ) else None
                groups[(_provider_family(feature.provider), feature.timeframe, feature.unit, feature.direction, endpoint)][
                    observation.asset.asset_id
                ] = feature.value
        cross[name] = {}
        for values in groups.values():
            if len(values) >= settings.minimum_cross_section:
                cross[name].update(_raw_percentiles(values))
    return cross


def _midranks(scores: dict[str, float]) -> dict[str, float]:
    ordered = sorted(scores.values(), reverse=True)
    ascending = sorted(ordered)
    return {
        asset_id: len(ordered) - (bisect_left(ascending, value) + bisect_right(ascending, value) - 1) / 2
        for asset_id, value in scores.items()
    }


def _common_ranks(
    frames: list[list[DiscoveryObservation]], settings: DiscoverySettings,
) -> tuple[list[dict[str, float]], int, str | None]:
    maps = [{observation.asset.asset_id: observation for observation in frame
             if _eligible(observation) and observation.rank.current_rank is not None} for frame in frames]
    ids = set.intersection(*(set(frame) for frame in maps))
    largest = max((len(frame) for frame in maps), default=0)
    if len(ids) < settings.minimum_cross_section or not largest or len(ids) / largest < settings.minimum_rank_overlap:
        return [], len(ids), "Rank migration withheld: insufficient comparable universe overlap"
    mask = set()
    for name in _names(settings):
        comparable = True
        for asset_id in ids:
            features = [frame[asset_id].features.get(name) for frame in maps]
            if not all(_usable(feature, frame[asset_id]) for feature, frame in zip(features, maps, strict=True)):
                comparable = False
                break
            signatures = {(f.provider, f.timeframe, f.unit, f.direction,
                           f.historical_percentile is not None) for f in features}
            if len(signatures) != 1:
                comparable = False
                break
        if comparable:
            mask.add(name)
    if not mask:
        return [], len(ids), "Rank migration withheld: feature, source or historical-evidence mask changed"
    ranks = []
    for frame in maps:
        cohort = [frame[asset_id] for asset_id in sorted(ids)]
        cross = _cross_sections(cohort, settings, mask)
        scores = {}
        for observation in cohort:
            long, short, _ = _directional_scores(observation, cross, settings, mask)
            scores[observation.asset.asset_id] = _selected_score(long, short).lower_bound
        ranks.append(_midranks(scores))
    return ranks, len(ids), None


def rank_observations(
    observations: list[DiscoveryObservation], history: list[DiscoveryCycle], settings: DiscoverySettings,
) -> list[DiscoveryObservation]:
    """Populate only current broad rank; migration cannot feed back into that rank."""
    if not observations:
        return []
    _populate_market_strength(observations)
    _populate_historical(observations, history, settings)
    cross = _cross_sections(observations, settings)
    rankable = {}
    for observation in observations:
        observation.rank = RankHistory(ranks={window: None for window in RANK_WINDOWS})
        for name in _names(settings):
            feature = observation.features.get(name)
            if feature:
                feature.cross_sectional_percentile = cross.get(name, {}).get(observation.asset.asset_id)
        long, short, bias = _directional_scores(observation, cross, settings,
                                               mask=None if _eligible(observation) else set())
        score = _selected_score(long, short)
        observation.ranking_mode = settings.ranking_mode
        observation.bias = bias
        observation.scores["light_discovery"] = score.model_copy(deep=True)
        if settings.ranking_mode == "BOTH":
            observation.scores["light_discovery_long"] = long
            observation.scores["light_discovery_short"] = short
        else:
            observation.scores.pop("light_discovery_long", None)
            observation.scores.pop("light_discovery_short", None)
        if _eligible(observation) and any(observation.asset.asset_id in group for group in cross.values()):
            rankable[observation.asset.asset_id] = score.lower_bound
        elif _eligible(observation):
            observation.rank.reason = "Insufficient comparable cross-sectional observations"
        else:
            observation.rank.reason = "Ineligible, failed, missing or stale critical observations"
    ranks = _midranks(rankable)
    for observation in observations:
        observation.rank.current_rank = ranks.get(observation.asset.asset_id)
        observation.rank.cohort_size = len(rankable)
    # Historical rank fields are the actual persisted historical ranks. Migration
    # uses reconstructed common-cohort ranks and can differ from this display.
    for observation in observations:
        cycles = _history(history, observation, settings)
        priors = []
        for cycle in cycles:
            prior = next((item for item in cycle.observations
                          if item.asset.asset_id == observation.asset.asset_id
                          and item.as_of <= cycle.as_of), None)
            if prior:
                priors.append((cycle, prior))
        if priors:
            observation.rank.previous_rank = priors[-1][1].rank.current_rank
        for window, offset in RANK_WINDOWS.items():
            target = observation.as_of - offset
            before = [(cycle, item) for cycle, item in priors if item.as_of <= target]
            if before:
                _, prior = max(before, key=lambda pair: pair[1].as_of)
                tolerance = max(timedelta(seconds=settings.broad_interval_seconds * 2), offset / 4)
                if target - prior.as_of <= tolerance:
                    observation.rank.ranks[window] = prior.rank.current_rank
    # Migration uses whole cycles, never per-asset selections with mismatched clocks.
    current_time = min(item.as_of for item in observations)
    historical_cycles = sorted(
        (cycle for cycle in history if cycle.as_of < current_time and cycle.completed_at <= current_time
         and ranking_history_compatible(cycle, settings)), key=lambda cycle: cycle.as_of,
    )
    if not historical_cycles and any(cycle.as_of < current_time and cycle.completed_at <= current_time
                                     for cycle in history):
        for observation in observations:
            if observation.rank.current_rank is not None:
                observation.rank.reason = "Rank migration withheld: ranking formula/mode or baseline configuration changed"
    if historical_cycles:
        previous = historical_cycles[-1]
        comparable, count, reason = _common_ranks([previous.observations, observations], settings)
        elapsed = (current_time - previous.as_of).total_seconds() / 3600
        for observation in observations:
            observation.rank.comparable_cohort_size = count
            asset_id = observation.asset.asset_id
            if comparable and asset_id in comparable[0] and elapsed > 0:
                observation.rank.rank_velocity = 100 * (comparable[0][asset_id] - comparable[1][asset_id]) / (
                    count - 1
                ) / elapsed
                observation.rank.reason = "Migration recomputed on common universe, feature mask and source"
            elif observation.rank.current_rank is not None:
                observation.rank.reason = reason or "Asset absent from comparable historical cohort"
    if len(historical_cycles) >= 2:
        older, previous = historical_cycles[-2:]
        comparable, count, _ = _common_ranks([older.observations, previous.observations, observations], settings)
        dt_old = (previous.as_of - older.as_of).total_seconds() / 3600
        dt_now = (current_time - previous.as_of).total_seconds() / 3600
        if comparable and min(dt_old, dt_now) > 0:
            for observation in observations:
                asset_id = observation.asset.asset_id
                if asset_id in comparable[0]:
                    old_velocity = (comparable[0][asset_id] - comparable[1][asset_id]) / (count - 1) / dt_old
                    velocity = (comparable[1][asset_id] - comparable[2][asset_id]) / (count - 1) / dt_now
                    observation.rank.rank_acceleration = 100 * (velocity - old_velocity) / ((dt_old + dt_now) / 2)
    velocities = {item.asset.asset_id: item.rank.rank_velocity for item in observations
                  if item.rank.rank_velocity is not None}
    velocity_percentiles = _raw_percentiles(velocities) if len(velocities) >= settings.minimum_cross_section else {}
    for observation in observations:
        observation.rank.velocity_percentile = velocity_percentiles.get(observation.asset.asset_id)
    return sorted(observations, key=lambda item: (
        item.rank.current_rank if item.rank.current_rank is not None else math.inf, item.asset.asset_id,
    ))


def dark_horses(observations: list[DiscoveryObservation], settings: DiscoverySettings) -> list[str]:
    """Surface low-ranked rapid improvers with at least two self-history confirmations."""
    selected = []
    for observation in observations:
        rank = observation.rank
        if not (_eligible(observation) and rank.current_rank is not None
                and rank.previous_rank is not None and rank.previous_rank >= settings.dark_horse_previous_rank
                and rank.rank_velocity is not None and rank.rank_velocity > 0
                and rank.velocity_percentile is not None
                and rank.velocity_percentile >= settings.rank_velocity_percentile):
            continue
        confirmed = 0
        for names in (("relative_strength_btc", "relative_strength_market"),
                      ("relative_volume", "volume_acceleration", "turnover"),
                      ("range_compression", "volatility")):
            confirmed += any(
                _usable(observation.features.get(name), observation)
                and observation.features[name].historical_percentile is not None
                and _beneficial(observation.features[name].historical_percentile,
                                observation.features[name].direction *
                                (-1 if observation.bias == "SHORT" and name in SIGNED_FEATURES else 1)) > 0.5
                for name in names
            )
        if confirmed >= 2:
            selected.append(observation)
    selected.sort(key=lambda item: (-item.rank.rank_velocity, item.asset.asset_id))
    return [item.asset.asset_id for item in selected]
