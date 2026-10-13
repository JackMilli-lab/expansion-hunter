"""Research pattern fixtures do not establish profitable cycle archetypes."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from expansion_hunter.analog import AnalogVector, FactorQuality, FactorValue
from expansion_hunter.cycle_patterns import (
    PatternRule,
    PatternSettings,
    default_pattern_rules,
    research_cycle_patterns,
)

NOW = datetime(2025, 1, 20, tzinfo=UTC)


def snapshot_for(name, *, settings=None):
    rules = (settings or PatternSettings()).rules[name]
    return AnalogVector(
        event_id="test-event",
        asset_id="synthetic-test-asset",
        decision_time=NOW,
        available_at=NOW,
        factors={
            rule.feature: FactorValue(
                value=rule.threshold,
                event_timestamp=NOW,
                available_at=NOW,
                source="synthetic_test_only: cycle-pattern protocol",
                unit=rule.unit,
                quality=FactorQuality.DERIVED,
            )
            for rule in rules
        },
    )


@pytest.mark.parametrize("name", list(default_pattern_rules()))
def test_each_requested_archetype_has_executable_measurable_hypothesis(name):
    observation = next(item for item in research_cycle_patterns(snapshot_for(name)) if item.name == name)
    assert observation.status == "PRESENT"
    assert observation.coverage == 1
    assert observation.evidence_score == 100
    assert observation.research_only is True
    assert observation.production_weight == 0
    assert observation.predictive_value == "UNVALIDATED_HYPOTHESIS"
    assert all(item.source.startswith("synthetic_test_only") for item in observation.evidence)


def test_no_input_data_is_unknown_not_a_negative_pattern():
    vector = AnalogVector(
        event_id="empty",
        asset_id="empty",
        decision_time=NOW,
        available_at=NOW,
    )
    observations = research_cycle_patterns(vector)
    assert len(observations) == 7
    assert all(item.status == "UNKNOWN" for item in observations)
    assert all(item.evidence_score is None for item in observations)
    assert all(item.lower_bound == 0 and item.upper_bound == 100 for item in observations)


def test_missing_required_feature_does_not_become_fake_zero():
    vector = snapshot_for("REVIVAL")
    del vector.factors["drawdown_fraction"]
    observation = next(item for item in research_cycle_patterns(vector) if item.name == "REVIVAL")
    assert observation.status == "UNKNOWN"
    evidence = next(item for item in observation.evidence if item.feature == "drawdown_fraction")
    assert evidence.value is None and evidence.passed is None
    assert evidence.quality == FactorQuality.UNAVAILABLE


def test_optional_missing_derivatives_does_not_disable_complete_spot_hypothesis():
    vector = snapshot_for("REVIVAL")
    del vector.factors["short_crowding"]
    observation = next(item for item in research_cycle_patterns(vector) if item.name == "REVIVAL")
    assert observation.status == "PRESENT"
    assert observation.coverage == pytest.approx(7 / 8)
    assert observation.evidence_score == 100
    assert observation.lower_bound == pytest.approx(87.5)
    assert observation.upper_bound == 100


def test_known_failed_mandatory_rule_is_absent_with_evidence():
    vector = snapshot_for("REVIVAL")
    vector.factors["resistance_tests"].value = 1
    observation = next(item for item in research_cycle_patterns(vector) if item.name == "REVIVAL")
    assert observation.status == "ABSENT"
    assert "required condition failed: resistance_tests" in observation.reasons


def test_invalid_bounded_evidence_is_unknown_not_extremely_strong():
    vector = snapshot_for("REVIVAL")
    vector.factors["higher_low_strength"].value = 50
    observation = next(item for item in research_cycle_patterns(vector) if item.name == "REVIVAL")
    assert observation.status == "UNKNOWN"


@pytest.mark.parametrize("defect", ["future_availability", "future_event", "stale", "wrong_unit"])
def test_later_or_unusable_features_never_define_current_pattern(defect):
    vector = snapshot_for("REVIVAL")
    factor = vector.factors["drawdown_fraction"]
    if defect == "future_availability":
        factor.available_at = NOW + timedelta(minutes=1)
    elif defect == "future_event":
        factor.event_timestamp = NOW + timedelta(minutes=1)
        factor.available_at = factor.event_timestamp
    elif defect == "stale":
        factor.quality = FactorQuality.STALE
    else:
        factor.unit = "percent instead of fraction"
    observation = next(item for item in research_cycle_patterns(vector) if item.name == "REVIVAL")
    assert observation.status == "UNKNOWN"
    assert next(item for item in observation.evidence if item.feature == "drawdown_fraction").value is None


def test_unseen_future_continuation_cannot_change_current_archetype():
    vector = snapshot_for("KNOCKOUT")
    before = research_cycle_patterns(vector)
    vector.factors["future_return_7d"] = FactorValue(
        value=2,
        event_timestamp=NOW + timedelta(days=7),
        available_at=NOW + timedelta(days=7),
        source="synthetic future outcome (not a pattern input)",
        unit="fraction",
        quality=FactorQuality.NATIVE,
    )
    assert before == research_cycle_patterns(vector)


def test_estimates_require_explicit_research_permission():
    vector = snapshot_for("WHIPLASH")
    vector.factors["support_reclaim"].quality = FactorQuality.ESTIMATED
    before = next(item for item in research_cycle_patterns(vector) if item.name == "WHIPLASH")
    after = next(
        item
        for item in research_cycle_patterns(vector, PatternSettings(allow_estimates=True))
        if item.name == "WHIPLASH"
    )
    assert before.status == "UNKNOWN"
    assert after.status == "PRESENT"


def test_additional_archetypes_are_explicit_rules_not_hardcoded_winners():
    rules = {
        "CUSTOM_RESEARCH": [
            PatternRule(
                feature="range_pressure",
                operator="gte",
                threshold=0.7,
                unit="0-1",
                reason="predeclared upper-range acceptance hypothesis",
            )
        ]
    }
    settings = PatternSettings(rules=rules)
    observation = research_cycle_patterns(snapshot_for("CUSTOM_RESEARCH", settings=settings), settings)[0]
    assert observation.name == "CUSTOM_RESEARCH"
    assert observation.status == "PRESENT"
    assert observation.production_weight == 0


def test_no_future_labels_allowed_as_new_rules():
    with pytest.raises(ValidationError, match="future outcomes"):
        PatternSettings(
            rules={
                "BAD": [
                    PatternRule(
                        feature="future_return_24h",
                        operator="gte",
                        threshold=0.2,
                        unit="fraction",
                        reason="invalid hindsight",
                    )
                ]
            }
        )


def test_rules_are_reproducible_and_snapshot_is_not_mutated():
    vector = snapshot_for("FORCE_PATTERN")
    original = vector.model_dump()
    assert research_cycle_patterns(vector) == research_cycle_patterns(vector)
    assert vector.model_dump() == original


def test_snapshot_published_after_decision_fails_closed():
    vector = snapshot_for("REVIVAL").model_copy(update={"available_at": NOW + timedelta(seconds=1)})
    with pytest.raises(ValueError, match="unavailable at decision"):
        research_cycle_patterns(vector)
