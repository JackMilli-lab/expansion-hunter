"""Direction-aware observed research; synthetic prices do not imply an edge."""
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.analog import FactorQuality, FactorValue
from expansion_hunter.event_learning import (
    EventLearningEngine,
    LearningSettings,
    ResearchEvent,
    calculate_event_outcome,
)
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.macro_models import MacroMarketObservation

START = datetime(2025, 1, 1, tzinfo=UTC)


def path(prices):
    previous = 100
    result = []
    for index, price in enumerate(prices):
        at = START + timedelta(minutes=index)
        result.append(MacroMarketObservation(asset_id="test", market_event_timestamp=at,
            data_received_timestamp=at, interval_start_timestamp=at - timedelta(minutes=1),
            resolution_seconds=60, price=price, high=max(previous, price) + 1,
            low=min(previous, price) - 1, volume=100, source="synthetic_test_only"))
        previous = price
    return result


def signal(bias="SHORT", kind="LOADED"):
    return ResearchEvent(event_id=f"test-{bias}", asset_id="test", event_type=kind, bias=bias,
        event_timestamp=START, data_received_timestamp=START, decision_timestamp=START,
        available_at=START, reference_price=100, source="synthetic_test_only")


def test_short_mfe_mae_are_directional_but_market_return_remains_raw():
    result = calculate_event_outcome(signal(), path([100, 95, 90, 85]),
        as_of=START + timedelta(minutes=3), horizon_seconds=180,
        settings=LearningSettings(false_positive_horizon_seconds=180))
    assert result.bias == "SHORT"
    assert result.future_close_return == pytest.approx(-.15)
    assert result.directional_close_return == pytest.approx(.15)
    assert result.mfe == pytest.approx(.16)
    assert result.mae == pytest.approx(-.01)
    assert result.time_to_expansion_seconds == 180
    assert result.false_positive is False


def test_breakdown_failure_uses_predefined_support():
    event = signal(kind="1M_BREAKDOWN_CONFIRMED")
    event.features["support"] = FactorValue(value=100, event_timestamp=START,
        available_at=START, source="synthetic_test_only", quality=FactorQuality.DERIVED)
    result = calculate_event_outcome(event, path([100, 99, 101, 102]),
        as_of=START + timedelta(minutes=3), horizon_seconds=180)
    assert result.failed_breakdown is True
    assert result.failed_breakout is None
    assert result.time_to_failure_seconds == 120


def test_short_miss_is_detected_without_alert_or_retrospective_promotion(tmp_path):
    with_store = IntelligenceStore(str(tmp_path / "test.sqlite3"))
    try:
        engine = EventLearningEngine(with_store, LearningSettings(expansion_window_seconds=180))
        at = START + timedelta(minutes=3)
        miss, reason, detected = engine._detect_miss("test", path([100, 95, 90, 85]), [], at, bias="SHORT")
        assert reason is None and detected
        assert miss.bias == "SHORT" and miss.kind == "FULL_MISS"
        assert miss.expansion_magnitude == pytest.approx(.15)
        assert with_store.states("fast_state") == []
        assert with_store.objects("expansion_event", at)[0]["bias"] == "SHORT"
        assert engine._detect_miss("test", path([100, 95, 90, 85]), [], at, bias="SHORT") == (None, None, False)
    finally:
        with_store.close()


@pytest.mark.parametrize("move_bias,wrong_bias,prices", [
    ("SHORT", "LONG", [100, 95, 90, 85]), ("LONG", "SHORT", [100, 105, 110, 115])])
def test_opposite_direction_alert_cannot_disguise_a_miss(tmp_path, move_bias, wrong_bias, prices):
    store = IntelligenceStore(str(tmp_path / "test.sqlite3"))
    try:
        engine = EventLearningEngine(store, LearningSettings(expansion_window_seconds=180))
        miss, _, _ = engine._detect_miss("test", path(prices), [signal(wrong_bias)],
            START + timedelta(minutes=3), bias=move_bias)
        assert miss.kind == "FULL_MISS"
        assert miss.first_system_detection is None
    finally:
        store.close()


def test_short_research_never_uses_future_received_intervals():
    history = path([100, 95, 90, 85])
    history[-1].data_received_timestamp += timedelta(hours=1)
    result = calculate_event_outcome(signal(), history,
        as_of=START + timedelta(minutes=3), horizon_seconds=180)
    assert result.status == "CENSORED"
    assert result.mfe is None and result.directional_close_return is None
