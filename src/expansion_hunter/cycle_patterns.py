"""Generic, predeclared market-cycle pattern hypotheses for offline research.

These labels are operational definitions, not universal cycles or proven edges.
The production scanner does not import this module. Missing evidence stays
unknown and every component retains its point-in-time source and threshold.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .analog import AnalogVector, FactorQuality
from .models import Model


class PatternRule(Model):
    feature: str = Field(min_length=1)
    operator: Literal["gte", "lte"]
    threshold: float
    unit: str
    weight: float = Field(default=1, gt=0)
    required: bool = True
    reason: str = Field(min_length=1)


def _rule(feature, operator, threshold, unit, reason, *, weight=1, required=True):
    return PatternRule(
        feature=feature,
        operator=operator,
        threshold=threshold,
        unit=unit,
        reason=reason,
        weight=weight,
        required=required,
    )


def default_pattern_rules() -> dict[str, list[PatternRule]]:
    """Thresholds are explicit initial hypotheses; none were fitted to winners.

    Returns/drawdown are fractions, strengths are 0–1 and MA distances are
    fractions of the MA. Positive drawdown means depth below a then-known high.
    BOS/CHoCH and sweeps must come from confirmed, timestamped upstream logic.
    """
    return {
        "REVIVAL": [
            _rule("prior_trend_return", "lte", -0.20, "fraction", "prolonged preceding decline"),
            _rule("drawdown_fraction", "gte", 0.40, "fraction", "large drawdown from then-known high"),
            _rule("base_duration_days", "gte", 14, "days", "multi-week base existed at decision"),
            _rule("higher_low_strength", "gte", 0.65, "0-1", "rising confirmed lows"),
            _rule("resistance_tests", "gte", 3, "count", "multiple settled tests of known resistance"),
            _rule("rejection_decay", "gte", 0.35, "0-1", "rejections weaken at the same frozen zone"),
            _rule("relative_strength_btc", "gte", 0, "fraction", "not underperforming BTC in aligned window"),
            _rule(
                "short_crowding",
                "gte",
                0.70,
                "percentile",
                "dated short-crowding context if reliable",
                required=False,
            ),
        ],
        "FORCE_PATTERN": [
            _rule("pressure_duration_days", "gte", 30, "days", "long-duration ascending resistance pressure"),
            _rule("resistance_tests", "gte", 4, "count", "repeated tests of a major known level"),
            _rule("higher_low_strength", "gte", 0.70, "0-1", "constructive confirmed lows"),
            _rule("distance_resistance_atr", "lte", 0.75, "ATR", "price near resistance in volatility units"),
            _rule("market_congruence", "gte", 65, "0-100", "broad-market alignment measured separately"),
            _rule("breadth_above_ema50", "gte", 0.55, "fraction", "majority of dated universe above EMA50"),
            _rule(
                "buy_side_liquidity_distance_atr",
                "lte",
                2,
                "ATR",
                "nearby inferred or observed buy-side context",
                required=False,
            ),
        ],
        "ARMAGEDDON": [
            _rule("prior_bull_return", "gte", 0.50, "fraction", "substantial preceding bull expansion"),
            _rule("overextension_atr", "gte", 3, "ATR", "previous overextension measured before transition"),
            _rule("bearish_divergence", "gte", 1, "boolean", "deterministic confirmed bearish divergence"),
            _rule("distance_ma50", "lte", 0, "fraction", "price loses contemporaneous 50-day MA"),
            _rule("bearish_choch", "gte", 1, "boolean", "confirmed change of structure"),
            _rule("confirmed_lower_low", "gte", 1, "boolean", "lower low confirmable at decision"),
            _rule(
                "oi_crowding",
                "gte",
                0.80,
                "percentile",
                "reliable dated OI crowding if available",
                required=False,
            ),
        ],
        "SUCKER_PUNCH": [
            _rule("market_risk_off", "gte", 1, "boolean", "broader bearish regime at decision"),
            _rule("rally_return", "gte", 0.08, "fraction", "countertrend rally has already occurred"),
            _rule("distance_supply_atr", "lte", 0.75, "ATR", "rally reaches predefined supply"),
            _rule("resistance_rejection", "gte", 0.60, "0-1", "observed rejection at known supply"),
            _rule("relative_strength_btc", "lte", 0, "fraction", "weak aligned relative strength"),
            _rule("avwap_rejection", "gte", 1, "boolean", "point-in-time AVWAP rejection", required=False),
            _rule(
                "fib_confluence",
                "gte",
                60,
                "0-100",
                "research-only confluence, no assumed Fibonacci edge",
                required=False,
            ),
        ],
        "WHIPLASH": [
            _rule("sell_side_sweep", "gte", 1, "boolean", "price sweeps a previously known low"),
            _rule("support_reclaim", "gte", 1, "boolean", "closed price reclaims the swept level"),
            _rule("reclaim_speed_bars", "lte", 3, "count", "quick closed-candle reclaim"),
            _rule("volume_awakening", "gte", 60, "0-100", "activity response around observed sweep"),
            _rule("exhaustion_strength", "gte", 0.60, "0-1", "predeclared rejection/exhaustion evidence"),
            _rule(
                "observed_long_liquidation_percentile",
                "gte",
                0.90,
                "percentile",
                "observed liquidations only, not inferred clusters",
                required=False,
            ),
        ],
        "KNOCKOUT": [
            _rule("drawdown_fraction", "gte", 0.70, "fraction", "extreme drawdown from then-known high"),
            _rule(
                "capitulation_volume_percentile",
                "gte",
                0.95,
                "percentile",
                "dated capitulation activity anomaly",
            ),
            _rule("sell_side_sweep", "gte", 1, "boolean", "sweep below a major known low"),
            _rule("support_reclaim", "gte", 1, "boolean", "closed price reclaims swept support"),
            _rule("reclaim_speed_bars", "lte", 3, "count", "sharp reclaim after the sweep"),
            _rule(
                "demand_reaction", "gte", 0.65, "0-1", "observed demand response without future continuation"
            ),
            _rule(
                "observed_long_liquidation_percentile",
                "gte",
                0.95,
                "percentile",
                "reliable observed liquidation exhaustion",
                required=False,
            ),
        ],
        "POST_KNOCKOUT_RECOVERY": [
            _rule(
                "prior_knockout_known", "gte", 1, "boolean", "an earlier knockout observation already existed"
            ),
            _rule("higher_low_strength", "gte", 0.65, "0-1", "subsequent lows now confirmed higher"),
            _rule("support_reclaim", "gte", 1, "boolean", "support remains reclaimed at current decision"),
            _rule("relative_strength_btc", "gte", 0, "fraction", "recovery not underperforming BTC"),
            _rule(
                "volatility_compression", "gte", 0.60, "0-1", "volatility settles after the observed shock"
            ),
            _rule("avwap_support", "gte", 1, "boolean", "acceptance above known AVWAP", required=False),
        ],
    }


class PatternSettings(Model):
    rules: dict[str, list[PatternRule]] = Field(default_factory=default_pattern_rules)
    minimum_coverage: float = Field(default=0.70, gt=0, le=1)
    presence_threshold: float = Field(default=70, ge=0, le=100)
    allow_estimates: bool = False
    version: str = "cycle-pattern-hypotheses-v1"

    @model_validator(mode="after")
    def explicit_rules(self):
        if not self.rules or any(not name or not rules for name, rules in self.rules.items()):
            raise ValueError("every research pattern needs a name and explicit rules")
        for rules in self.rules.values():
            if len({(rule.feature, rule.operator) for rule in rules}) != len(rules):
                raise ValueError("duplicate research pattern rule")
            if not any(rule.required for rule in rules):
                raise ValueError("research pattern requires at least one mandatory rule")
            for rule in rules:
                if rule.feature.lower().startswith(("future_", "outcome_", "label_", "time_to_")):
                    raise ValueError("future outcomes cannot define current pattern features")
        return self


class PatternEvidence(Model):
    feature: str
    operator: str
    threshold: float
    unit: str
    weight: float
    required: bool
    value: float | None
    passed: bool | None
    source: str | None
    event_timestamp: datetime | None
    available_at: datetime | None
    quality: FactorQuality
    reason: str


class PatternObservation(Model):
    name: str
    event_id: str
    asset_id: str
    decision_time: datetime
    version: str
    status: Literal["PRESENT", "ABSENT", "UNKNOWN"]
    evidence_score: float | None = Field(default=None, ge=0, le=100)
    coverage: float = Field(ge=0, le=1)
    lower_bound: float = Field(ge=0, le=100)
    upper_bound: float = Field(ge=0, le=100)
    evidence: list[PatternEvidence]
    reasons: list[str]
    research_only: Literal[True] = True
    predictive_value: Literal["UNVALIDATED_HYPOTHESIS"] = "UNVALIDATED_HYPOTHESIS"
    production_weight: Literal[0] = 0


def research_cycle_patterns(
    vector: AnalogVector,
    settings: PatternSettings | None = None,
) -> list[PatternObservation]:
    """Evaluate present knowledge only; later continuation is an outcome label."""
    settings = settings or PatternSettings()
    if vector.available_at > vector.decision_time:
        raise ValueError("pattern snapshot was unavailable at decision")
    observations = []
    for name, rules in settings.rules.items():
        evidence = []
        total_weight = sum(rule.weight for rule in rules)
        for rule in rules:
            factor = vector.factors.get(rule.feature)
            usable = bool(
                factor
                and factor.usable(vector.decision_time, allow_estimates=settings.allow_estimates)
                and factor.unit == rule.unit
                and (rule.unit not in {"0-1", "percentile", "boolean"} or 0 <= factor.value <= 1)
                and (rule.unit != "0-100" or 0 <= factor.value <= 100)
                and (rule.unit not in {"count", "days", "ATR"} or factor.value >= 0)
            )
            value = factor.value if usable else None
            passed = (
                None
                if value is None
                else (value >= rule.threshold if rule.operator == "gte" else value <= rule.threshold)
            )
            evidence.append(
                PatternEvidence(
                    feature=rule.feature,
                    operator=rule.operator,
                    threshold=rule.threshold,
                    unit=rule.unit,
                    weight=rule.weight,
                    required=rule.required,
                    value=value,
                    passed=passed,
                    source=factor.source if factor else None,
                    event_timestamp=factor.event_timestamp if factor else None,
                    available_at=factor.available_at if factor else None,
                    quality=factor.quality if usable else FactorQuality.UNAVAILABLE,
                    reason=rule.reason
                    if usable
                    else f"{rule.reason}; missing, late, stale or unit-incompatible evidence",
                )
            )
        known_weight = sum(item.weight for item in evidence if item.passed is not None)
        passed_weight = sum(item.weight for item in evidence if item.passed is True)
        coverage = known_weight / total_weight
        lower_bound = 100 * passed_weight / total_weight
        upper_bound = 100 * (passed_weight + total_weight - known_weight) / total_weight
        score = 100 * passed_weight / known_weight if known_weight else None
        missing_required = [item.feature for item in evidence if item.required and item.passed is None]
        failed_required = [item.feature for item in evidence if item.required and item.passed is False]
        reasons = []
        if missing_required or coverage < settings.minimum_coverage:
            status = "UNKNOWN"
            reasons.append("insufficient point-in-time evidence")
            reasons.extend(f"required feature unavailable: {feature}" for feature in missing_required)
        elif failed_required or score < settings.presence_threshold:
            status = "ABSENT"
            reasons.extend(f"required condition failed: {feature}" for feature in failed_required)
            if score < settings.presence_threshold:
                reasons.append("observed evidence below explicit presence threshold")
        else:
            status = "PRESENT"
            reasons.append("predeclared measurable hypothesis conditions currently present")
        observations.append(
            PatternObservation(
                name=name,
                event_id=vector.event_id,
                asset_id=vector.asset_id,
                decision_time=vector.decision_time,
                version=settings.version,
                status=status,
                evidence_score=score,
                coverage=coverage,
                lower_bound=lower_bound,
                upper_bound=upper_bound,
                evidence=evidence,
                reasons=reasons,
            )
        )
    return observations
