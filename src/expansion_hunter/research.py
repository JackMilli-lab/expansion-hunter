"""Outcome-blind matched cohorts; descriptive research, never an order gateway.

Matching scales and candidate features are fixed before observing outcomes.
Archived universe/metadata declarations are required attestations, not proof of
data lineage. No current top-500 list can establish historical non-survivorship.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from statistics import mean
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator

from .models import TIMEFRAMES, AssetSnapshot, Candle, Metric, Model, RegimeReport
from .regime import matched_returns
from .replay import point_in_time
from .technical import analyze

# Scale units are log10 dollars/turnover, fractions, percentage points and days.
# Explanatory demand, relative strength, derivatives and adoption are not matched
# away. These scales must not be estimated on a subsequently selected holdout.
MATCH_SCALES = {
    "log_market_cap": 1.0,
    "log_turnover": 1.0,
    "log_liquidity": 1.0,
    "daily_bb_width": 0.1,
    "four_hour_bb_width": 0.1,
    "ema_cluster_pct": 3.0,
    "range_age_days": 30.0,
    "rsi14": 10.0,
    "resistance_tests": 3.0,
    "distance_resistance_pct": 5.0,
    "volume_contraction": 0.5,
}
EFFECT_FEATURES = (
    "spot_relative_volume",
    "relative_strength_7d",
    "aggressive_buy_share",
    "oi_change_24h",
    "funding_8h_equivalent",
    "futures_spot_volume_ratio",
    "verified_quantitative_catalyst_strength",
)
Label = Literal["winner", "breakout_only", "failure", "censored"]


class StudyObservation(Model):
    snapshot: AssetSnapshot
    pre_event_source: str = Field(min_length=1)
    point_in_time_metadata: bool
    future_bars: list[Candle] = Field(default_factory=list)
    future_timeframe: Literal["1D", "4h"] = "1D"
    future_source: str = Field(min_length=1)
    # Optional archived engine score: cannot be reconstructed from future data.
    score: Metric | None = None

    @model_validator(mode="after")
    def historical_metadata(self):
        if not self.point_in_time_metadata:
            raise ValueError("current metadata is not point-in-time research evidence")
        if self.snapshot.as_of.timestamp() % TIMEFRAMES[self.future_timeframe] != 0:
            raise ValueError("research as_of must align with the UTC future_timeframe candle-open boundary")
        return self


class StudyCohort(Model):
    as_of: datetime
    universe_asset_ids: list[str] = Field(min_length=1, max_length=500)
    universe_available_at: datetime
    universe_source: str = Field(min_length=1)
    point_in_time_universe: bool
    includes_delisted: bool
    regime: RegimeReport
    observations: list[StudyObservation] = Field(min_length=1)

    @model_validator(mode="after")
    def causal_universe(self):
        if not self.point_in_time_universe or not self.includes_delisted:
            raise ValueError(
                "archived point-in-time universe including delisted assets is required; survivor-only lists rejected"
            )
        if self.universe_available_at > self.as_of:
            raise ValueError("universe was not available at cohort as_of")
        if (
            self.regime.as_of != self.as_of
            or self.regime.available_at is None
            or self.regime.available_at > self.as_of
        ):
            raise ValueError("cohort regime requires matching point-in-time timestamps")
        universe = set(self.universe_asset_ids)
        ids = [o.snapshot.asset.asset_id for o in self.observations]
        if len(universe) != len(self.universe_asset_ids) or len(ids) != len(set(ids)):
            raise ValueError("duplicate universe or observation asset identifiers")
        if any(
            o.snapshot.as_of != self.as_of or o.snapshot.asset.asset_id not in universe
            for o in self.observations
        ):
            raise ValueError("observations must belong to the same archived universe and as_of")
        return self


class StudyCase(Model):
    asset_id: str
    as_of: datetime


class StudyDataset(Model):
    schema_version: int = 1
    cohorts: list[StudyCohort] = Field(min_length=1)
    cases: list[StudyCase] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_boundaries(self):
        if len({c.as_of for c in self.cohorts}) != len(self.cohorts):
            raise ValueError("duplicate cohort as_of")
        if len({(c.asset_id, c.as_of) for c in self.cases}) != len(self.cases):
            raise ValueError("duplicate case")
        return self


class ResearchSettings(Model):
    controls_per_case: int = Field(default=50, ge=1, le=499)
    horizon_days: int = Field(default=30, ge=1, le=180)
    explosive_return: float = Field(default=0.5, gt=0)
    acceptance_closes: int = Field(default=2, ge=2, le=10)
    acceptance_buffer: float = Field(default=0.005, ge=0, le=0.1)
    max_match_distance: float = Field(default=2.0, gt=0)
    min_history_bars: int = Field(default=220, ge=220)
    metric_max_age_seconds: int = Field(default=86400, ge=1)
    train_end: datetime | None = None
    minimum_cases_per_split: int = Field(default=5, ge=1)
    discover_cases: bool = True
    score_threshold: float = Field(default=70, ge=0, le=100)
    minimum_labeled_control_fraction: float = Field(default=0.9, gt=0, le=1)
    minimum_non_breakout_controls: int = Field(default=20, ge=1)


class Outcome(Model):
    label: Label
    horizon_end: datetime
    available_at: datetime | None = None
    accepted_breakout: bool | None = None
    maximum_favorable_return: float | None = None
    maximum_adverse_return: float | None = None
    final_return: float | None = None
    reason: str | None = None


class MatchedAsset(Model):
    asset_id: str
    distance: float
    outcome: Outcome
    match_features: dict[str, float]
    distinguishing_features: dict[str, float | None]


class CaseReport(Model):
    asset_id: str
    as_of: datetime
    split: Literal["train", "holdout", "purged", "unsplit"]
    outcome: Outcome
    match_features: dict[str, float]
    distinguishing_features: dict[str, float | None]
    matches: list[MatchedAsset]
    candidate_distances: dict[str, float]
    eligible_controls: int
    matched_failures: int
    control_shortfall: int
    non_breakout_shortfall: int
    warnings: list[str] = Field(default_factory=list)


class CohortStatistics(Model):
    as_of: datetime
    split: str
    universe_size: int
    eligible: int
    excluded: dict[str, str]
    label_counts: dict[str, int]
    base_rate: float | None
    score_coverage: int
    score_flagged: int
    false_positive_rate: float | None
    false_discovery_rate: float | None


class FeatureEffect(Model):
    feature: str
    train_case_count: int
    holdout_case_count: int
    train_mean_paired_difference: float | None
    holdout_mean_paired_difference: float | None
    holdout_same_sign: bool | None
    sufficient_samples: bool


class StudyReport(Model):
    schema_version: int = 1
    status: Literal["insufficient_data", "descriptive_only", "holdout_comparison"]
    settings: ResearchSettings
    matching_scales: dict[str, float]
    cases: list[CaseReport]
    cohort_statistics: list[CohortStatistics]
    feature_effects: list[FeatureEffect]
    warnings: list[str]
    automatic_orders_enabled: Literal[False] = False


def _metric(snapshot: AssetSnapshot, key: str, settings: ResearchSettings, derivatives=False):
    source = snapshot.derivatives if derivatives else snapshot.market
    metric = source.get(key)
    return metric.value if metric and metric.usable(snapshot.as_of, settings.metric_max_age_seconds) else None


def _features(
    observation: StudyObservation, settings: ResearchSettings, benchmark: AssetSnapshot | None = None
):
    snapshot = point_in_time(observation.snapshot, observation.snapshot.as_of)
    technical = {tf: analyze(snapshot.candles.get(tf, []), tf, snapshot.as_of) for tf in ("1D", "4h")}
    fatal = {
        "no_closed_available_candles",
        "duplicate_candle_timestamps",
        "candle_duration_does_not_match_timeframe",
        "non_contiguous_candles",
        "mixed_candle_sources",
        "stale_latest_candle",
        "insufficient_indicator_warmup_minimum_220_bars",
        "insufficient_calendar_history_for_structures_minimum_21_days",
    }
    for tf, t in technical.items():
        issues = [issue for issue in t.issues if issue in fatal]
        if t.bars < settings.min_history_bars or issues:
            return None, f"{tf}: insufficient, stale or invalid closed history ({', '.join(issues)})"
        if not all("spot" in bar.source.lower() for bar in snapshot.candles[tf]):
            return None, f"{tf}: spot-volume source provenance is required for compression matching"
    daily, four = technical["1D"].metrics, technical["4h"].metrics
    cap = _metric(snapshot, "market_cap", settings)
    turnover = _metric(snapshot, "turnover_24h", settings)
    if turnover is None:
        turnover = _metric(snapshot, "turnover", settings)
    liquidity = _metric(snapshot, "depth_usd", settings)
    if liquidity is None:
        liquidity = _metric(snapshot, "liquidity_usd", settings)
    if any(value is None or value <= 0 for value in (cap, turnover, liquidity)):
        return None, "usable historical market cap, turnover and exchange liquidity are required"
    features = {
        "log_market_cap": math.log10(cap),
        "log_turnover": math.log10(turnover),
        "log_liquidity": math.log10(liquidity),
        "daily_bb_width": daily.get("bb_width"),
        "four_hour_bb_width": four.get("bb_width"),
        "ema_cluster_pct": four.get("ema_cluster_pct"),
        "range_age_days": daily.get("range_age_days"),
        "rsi14": four.get("rsi14"),
        "resistance_tests": four.get("resistance_tests"),
        "distance_resistance_pct": four.get("distance_resistance_pct"),
        "volume_contraction": four.get("volume_contraction"),
    }
    absent = [key for key, value in features.items() if value is None or not math.isfinite(value)]
    if absent:
        return None, "missing fixed matching features: " + ", ".join(absent)
    if not 0 <= features["distance_resistance_pct"] <= 15 or (four.get("extension_atr") or 0) >= 3:
        return None, "already breaking out/extended or too far from resistance for pre-ignition cohort"
    resistance = daily.get("breakout_level")
    if resistance is None or resistance <= 0:
        return None, "no causally observed daily resistance"
    if four["close"] > resistance:
        return None, "price already above the frozen daily resistance before the study entry"
    # Only positive, verified quantitative developments enter adoption evidence.
    verified = [
        c.strength
        for c in snapshot.catalysts
        if c.verified
        and c.quantitative
        and c.strength >= 0.5
        and c.category not in {"marketing", "social", "partnership"}
        and urlparse(c.source_url).scheme in {"http", "https"}
        and urlparse(c.source_url).hostname
        and snapshot.as_of - timedelta(days=180) <= c.observed_at <= snapshot.as_of
    ]
    bars = snapshot.candles["4h"]
    total_volume = sum(b.volume for b in bars[-6:])
    buys_known = all(b.taker_buy_volume is not None for b in bars[-6:])
    returns = matched_returns(snapshot, point_in_time(benchmark, snapshot.as_of)) if benchmark else None
    relative_strength = (
        returns[0] - returns[1] if returns else _metric(snapshot, "relative_strength_7d", settings)
    )
    funding = snapshot.derivatives.get("funding_rate")
    interval = snapshot.derivatives.get("funding_interval_hours")
    rate, cadence = (
        _metric(snapshot, "funding_rate", settings, True),
        _metric(snapshot, "funding_interval_hours", settings, True),
    )
    normalized = (
        rate * 8 / cadence
        if rate is not None
        and cadence is not None
        and 0 < cadence <= 24
        and funding.observed_at
        and interval.observed_at
        and abs((funding.observed_at - interval.observed_at).total_seconds()) <= 60
        else None
    )
    distinguishing = {
        "spot_relative_volume": four.get("relative_volume"),
        "relative_strength_7d": relative_strength,
        "aggressive_buy_share": sum(b.taker_buy_volume for b in bars[-6:]) / total_volume
        if buys_known and total_volume
        else None,
        "oi_change_24h": _metric(snapshot, "open_interest_change_pct", settings, True),
        "funding_8h_equivalent": normalized,
        "futures_spot_volume_ratio": _metric(snapshot, "futures_spot_volume_ratio", settings, True),
        "verified_quantitative_catalyst_strength": max(verified) if verified else None,
    }
    score = observation.score
    value = (
        score.value
        if score and score.usable(snapshot.as_of, settings.metric_max_age_seconds) and 0 <= score.value <= 100
        else None
    )
    return {
        "features": features,
        "distinguishing": distinguishing,
        "entry": four["close"],
        "resistance": resistance,
        "score": value,
    }, None


def _outcome(observation: StudyObservation, row: dict, settings: ResearchSettings) -> Outcome:
    boundary = observation.snapshot.as_of
    end = boundary + timedelta(days=settings.horizon_days)
    seconds = TIMEFRAMES[observation.future_timeframe]
    bars = sorted(
        [b for b in observation.future_bars if b.open_time >= boundary and b.close_time <= end],
        key=lambda b: b.open_time,
    )
    expected = settings.horizon_days * 86400 // seconds
    problem = None
    if len(bars) != expected:
        problem = "incomplete outcome horizon; missing data or unknown delisting is censored"
    elif len({b.source for b in bars}) != 1 or any(b.source != observation.future_source for b in bars):
        problem = "outcome source provenance mismatch"
    elif (
        abs((bars[0].open_time - boundary).total_seconds()) > 1.01
        or abs((end - bars[-1].close_time).total_seconds()) > 1.01
    ):
        problem = "outcome horizon boundary gap"
    elif any(abs((b.close_time - b.open_time).total_seconds() - seconds) > 1.01 for b in bars) or any(
        (b.open_time - a.open_time).total_seconds() != seconds for a, b in zip(bars, bars[1:], strict=False)
    ):
        problem = "duplicate, irregular or gapped future candles"
    if problem:
        return Outcome(label="censored", horizon_end=end, reason=problem)
    threshold = row["resistance"] * (1 + settings.acceptance_buffer)
    run = 0
    accepted = False
    for bar in bars:
        run = run + 1 if bar.close > threshold else 0
        accepted = accepted or run >= settings.acceptance_closes
    favorable = max(b.high for b in bars) / row["entry"] - 1
    label = (
        "winner"
        if accepted and favorable >= settings.explosive_return
        else "breakout_only"
        if accepted
        else "failure"
    )
    return Outcome(
        label=label,
        horizon_end=end,
        accepted_breakout=accepted,
        available_at=max(b.available_at for b in bars),
        maximum_favorable_return=favorable,
        maximum_adverse_return=min(b.low for b in bars) / row["entry"] - 1,
        final_return=bars[-1].close / row["entry"] - 1,
    )


def _split(when: datetime, settings: ResearchSettings, available_at: datetime | None = None) -> str:
    if settings.train_end is None:
        return "unsplit"
    if when + timedelta(days=settings.horizon_days) <= settings.train_end:
        return "purged" if available_at is not None and available_at > settings.train_end else "train"
    return "holdout" if when > settings.train_end else "purged"


def _effects(cases: list[CaseReport], settings: ResearchSettings) -> list[FeatureEffect]:
    effects = []
    for feature in EFFECT_FEATURES:
        values = {"train": [], "holdout": []}
        for case in cases:
            if case.split not in values or case.outcome.label != "winner":
                continue
            controls = [
                m.distinguishing_features[feature]
                for m in case.matches
                if m.outcome.label == "failure" and m.distinguishing_features[feature] is not None
            ]
            own = case.distinguishing_features[feature]
            if controls and own is not None:
                values[case.split].append(own - mean(controls))
        train = mean(values["train"]) if values["train"] else None
        holdout = mean(values["holdout"]) if values["holdout"] else None
        sufficient = min(len(values["train"]), len(values["holdout"])) >= settings.minimum_cases_per_split
        effects.append(
            FeatureEffect(
                feature=feature,
                train_case_count=len(values["train"]),
                holdout_case_count=len(values["holdout"]),
                train_mean_paired_difference=train,
                holdout_mean_paired_difference=holdout,
                holdout_same_sign=(train * holdout > 0)
                if sufficient and train is not None and holdout is not None
                else None,
                sufficient_samples=sufficient,
            )
        )
    return effects


def run_study(dataset: StudyDataset, settings: ResearchSettings | None = None) -> StudyReport:
    """Freeze matching for every eligible observation before reading any future.

    Winners are discovery labels, not predictors. Manual cases remain descriptive
    and cannot replace all-candidate prevalence or an independently frozen test.
    Missing optional explanatory data remains null, never an invented zero.
    """
    settings = settings or ResearchSettings()
    warnings = [
        "Universe and metadata provenance flags are declarations, not independently verified lineage; audit source artifacts.",
        "Nearest controls are selected before outcomes; other winners remain controls to preserve the observed base rate.",
        "Feature differences are descriptive associations, not causal effects or validated trading profitability.",
        "Excluded universe members and censored outcomes can bias observed eligible-cohort base rates; these are not full-market prevalence estimates.",
        "Repeated asset/date observations, overlapping horizons and reused controls are dependent; no independent-observation confidence claims.",
        "Missing catalyst coverage is unknown, not evidence that no adoption occurred. No feature weights or order permissions are learned.",
    ]
    if dataset.cases:
        warnings.append(
            "Manual famous-winner cases have selection bias; cohort base rates describe only the supplied archived candidate coverage."
        )
    if settings.train_end is None:
        warnings.append(
            "No frozen train_end supplied: holdout comparisons unavailable; report is descriptive only."
        )
    prepared = {}
    statistics = []
    for cohort in sorted(dataset.cohorts, key=lambda c: c.as_of):
        rows, excluded = {}, {}
        observations = {o.snapshot.asset.asset_id: o for o in cohort.observations}
        benchmark = observations["bitcoin"].snapshot if "bitcoin" in observations else None
        for asset_id in cohort.universe_asset_ids:
            if asset_id not in observations:
                excluded[asset_id] = "archived universe member has no supplied observation"
                continue
            row, issue = _features(observations[asset_id], settings, benchmark)
            if issue:
                excluded[asset_id] = issue
            else:
                rows[asset_id] = row
        # All ranks and distances are frozen before accessing future_bars.
        plans = {}
        for asset_id, row in rows.items():
            distances = {
                other: math.sqrt(
                    mean(
                        ((row["features"][key] - value["features"][key]) / scale) ** 2
                        for key, scale in MATCH_SCALES.items()
                    )
                )
                for other, value in rows.items()
                if other != asset_id
            }
            nearest = [
                other
                for other in sorted(distances, key=lambda other: (distances[other], other))
                if distances[other] <= settings.max_match_distance
            ]
            plans[asset_id] = (distances, nearest[: settings.controls_per_case], len(nearest))
        outcomes = {
            asset_id: _outcome(observations[asset_id], row, settings) for asset_id, row in rows.items()
        }
        prepared[cohort.as_of] = (rows, plans, outcomes)
        counts = {
            label: sum(o.label == label for o in outcomes.values())
            for label in ("winner", "breakout_only", "failure", "censored")
        }
        known = [key for key, outcome in outcomes.items() if outcome.label != "censored"]
        scored = [key for key in known if rows[key]["score"] is not None]
        flagged = [key for key in scored if rows[key]["score"] >= settings.score_threshold]
        negatives = [key for key in scored if outcomes[key].label != "winner"]
        false = sum(outcomes[key].label != "winner" for key in flagged)
        label_available = max((o.available_at for o in outcomes.values() if o.available_at), default=None)
        statistics.append(
            CohortStatistics(
                as_of=cohort.as_of,
                split=_split(cohort.as_of, settings, label_available),
                universe_size=len(cohort.universe_asset_ids),
                eligible=len(rows),
                excluded=excluded,
                label_counts=counts,
                base_rate=counts["winner"] / len(known) if known else None,
                score_coverage=len(scored),
                score_flagged=len(flagged),
                false_positive_rate=false / len(negatives) if negatives else None,
                false_discovery_rate=false / len(flagged) if flagged else None,
            )
        )
    selected = list(dataset.cases)
    if not selected and settings.discover_cases:
        previous = {}
        for when, (_, _, outcomes) in prepared.items():
            for asset_id, outcome in sorted(outcomes.items()):
                if outcome.label == "winner" and (asset_id not in previous or when > previous[asset_id]):
                    selected.append(StudyCase(asset_id=asset_id, as_of=when))
                    previous[asset_id] = outcome.horizon_end
        warnings.append(
            "Discovered winner episodes use a fixed horizon embargo per asset; discovery still conditions on future outcomes."
        )
    reports = []
    for case in selected:
        group = prepared.get(case.as_of)
        if group is None or case.asset_id not in group[0]:
            warnings.append(
                f"Case {case.asset_id} at {case.as_of.isoformat()} is absent or ineligible; no matching study produced."
            )
            continue
        rows, plans, outcomes = group
        row = rows[case.asset_id]
        distances, nearest, eligible = plans[case.asset_id]
        matches = [
            MatchedAsset(
                asset_id=other,
                distance=distances[other],
                outcome=outcomes[other],
                match_features=rows[other]["features"],
                distinguishing_features=rows[other]["distinguishing"],
            )
            for other in nearest
        ]
        failures = sum(m.outcome.label == "failure" for m in matches)
        issues = []
        if len(matches) < settings.controls_per_case:
            issues.append("Insufficient nearby pre-event controls within the fixed distance limit.")
        if failures < settings.controls_per_case:
            issues.append(
                "Fewer than the requested number of non-breakout lookalikes; outcome-filtered matching is prohibited."
            )
        if (
            matches
            and sum(m.outcome.label != "censored" for m in matches) / len(matches)
            < settings.minimum_labeled_control_fraction
        ):
            issues.append("Insufficient complete outcome coverage among the preselected controls.")
        if outcomes[case.asset_id].label != "winner":
            issues.append("Specified case does not satisfy the locked explosive-winner outcome definition.")
        label_available = max(
            (
                o.available_at
                for o in [outcomes[case.asset_id], *(m.outcome for m in matches)]
                if o.available_at
            ),
            default=None,
        )
        reports.append(
            CaseReport(
                asset_id=case.asset_id,
                as_of=case.as_of,
                split=_split(case.as_of, settings, label_available),
                outcome=outcomes[case.asset_id],
                match_features=row["features"],
                distinguishing_features=row["distinguishing"],
                matches=matches,
                candidate_distances=distances,
                eligible_controls=eligible,
                matched_failures=failures,
                control_shortfall=max(0, settings.controls_per_case - len(matches)),
                non_breakout_shortfall=max(0, settings.controls_per_case - failures),
                warnings=issues,
            )
        )
    effects = _effects(reports, settings) if settings.train_end else []
    split_counts = {
        split: sum(c.split == split and c.outcome.label == "winner" for c in reports)
        for split in ("train", "holdout")
    }
    status = "descriptive_only"
    if not reports or any(
        c.control_shortfall
        or c.matched_failures < min(settings.minimum_non_breakout_controls, settings.controls_per_case)
        or sum(m.outcome.label != "censored" for m in c.matches) / max(1, len(c.matches))
        < settings.minimum_labeled_control_fraction
        for c in reports
    ):
        status = "insufficient_data"
    elif (
        settings.train_end
        and min(split_counts.values()) >= settings.minimum_cases_per_split
        and any(e.sufficient_samples for e in effects)
    ):
        status = "holdout_comparison"
    else:
        warnings.append("Insufficient independent training/holdout cases for a comparative validation claim.")
    if settings.train_end and any(s.split == "purged" for s in statistics):
        warnings.append(
            "Training observations whose outcome horizon overlaps train_end or whose required labels became available later were purged from feature comparison."
        )
    if not any(s.score_coverage for s in statistics):
        warnings.append(
            "No usable archived pre-event engine scores: score false-positive rates are unavailable."
        )
    return StudyReport(
        status=status,
        settings=settings,
        matching_scales=MATCH_SCALES.copy(),
        cases=reports,
        cohort_statistics=statistics,
        feature_effects=effects,
        warnings=warnings,
    )
