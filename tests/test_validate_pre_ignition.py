"""Research-cache availability safeguards, not market-value fixtures."""
from datetime import UTC, datetime, timedelta

from scripts.validate_pre_ignition import pressure_direction


def test_cached_pressure_cannot_become_new_current_five_minute_evidence():
    at = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)
    row = {"as_of": (at-timedelta(minutes=1)).isoformat(), "trigger_discovered_at": (at-timedelta(minutes=2)).isoformat(),
           "trigger": 101., "price": 100., "distance_atr": 1., "metrics": {}, "bias": "LONG"}
    assert pressure_direction(row, at, at, 100.) is None


def test_actual_new_price_remeasures_distance_without_moving_known_level():
    at = datetime(2026, 1, 1, 12, 4, tzinfo=UTC)
    row = {"as_of": (at-timedelta(minutes=1)).isoformat(), "trigger_discovered_at": (at-timedelta(minutes=2)).isoformat(),
           "trigger": 101., "price": 100., "distance_atr": 1., "metrics": {}, "bias": "LONG"}
    result = pressure_direction(row, at, at.replace(minute=0), 100.5)
    assert result["level"] == 101.
    assert result["distance_atr"] == .5
    assert result["level_discovered_at"] == row["trigger_discovered_at"]
    assert result["pressure_snapshot_timestamp"] == row["as_of"]


def test_future_snapshot_is_not_replayed_before_its_timestamp():
    at = datetime(2026, 1, 1, 12, 4, tzinfo=UTC)
    row = {"as_of": (at+timedelta(seconds=1)).isoformat(), "trigger_discovered_at": at.isoformat(),
           "trigger": 101., "price": 100., "distance_atr": 1., "metrics": {}, "bias": "LONG"}
    assert pressure_direction(row, at, at.replace(minute=0), 100.) is None
