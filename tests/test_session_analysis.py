from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from expansion_hunter.models import Candle
from expansion_hunter.session_analysis import DEFAULT_SESSIONS, analyze_sessions, session_bounds


def bar(at, *, high=102, low=98, close=100, available_at=None, quote=True):
    return Candle(open_time=at, close_time=at + timedelta(hours=1), available_at=available_at or at + timedelta(hours=1),
        open=100, high=high, low=low, close=close, volume=10, quote_volume=1000 if quote else None,
        source="Explicit synthetic session test fixture")


def test_session_windows_follow_london_and_new_york_dst():
    london, new_york = DEFAULT_SESSIONS[1:]
    assert session_bounds(date(2026, 1, 15), london)[0].hour == 8
    assert session_bounds(date(2026, 7, 15), london)[0].hour == 7
    assert session_bounds(date(2026, 1, 15), new_york)[0].hour == 13
    assert session_bounds(date(2026, 7, 15), new_york)[0].hour == 12
    # US and UK change on different dates; never use a hardcoded shared offset.
    assert session_bounds(date(2026, 3, 15), london)[0].hour == 8
    assert session_bounds(date(2026, 3, 15), new_york)[0].hour == 12


def test_no_candles_is_unavailable_not_zero_volume_or_liquidity():
    value = analyze_sessions([], datetime(2026, 7, 15, 12, tzinfo=UTC))
    assert value["data_quality"] == "UNAVAILABLE" and value["sessions"] == []


def test_closed_session_actual_ohlcv_and_native_quote_vwap():
    start = datetime(2026, 7, 15, tzinfo=UTC)
    bars = [bar(start + timedelta(hours=n)) for n in range(8)]
    value = analyze_sessions(bars, start + timedelta(hours=8), days=1)
    asia = next(item for item in value["sessions"] if item["session"] == "ASIA")
    assert asia["completed"] and asia["quality"] == "AVAILABLE"
    assert asia["volume"] == 80 and asia["quote_volume"] == 8000 and asia["vwap"] == 100
    assert asia["vwap_quality"] == "NATIVE_VOLUME_RATIO" and asia["high"] == 102 and asia["low"] == 98


def test_future_closed_or_late_received_bars_never_enter_features():
    start = datetime(2026, 7, 15, tzinfo=UTC)
    future = bar(start + timedelta(hours=2), high=1000)
    late = bar(start, high=500, available_at=start + timedelta(days=1))
    current = bar(start + timedelta(hours=1))
    value = analyze_sessions([future, late, current], start + timedelta(hours=2), days=1)
    asia = next(item for item in value["sessions"] if item["session"] == "ASIA")
    assert asia["high"] == 102 and asia["bars"] == 1 and not asia["completed"]
    assert value["session_events"] == []


def test_candle_typical_price_proxy_is_labeled_estimated():
    start = datetime(2026, 7, 15, tzinfo=UTC)
    value = analyze_sessions([bar(start, quote=False)], start + timedelta(hours=1), days=1)
    asia = next(item for item in value["sessions"] if item["session"] == "ASIA")
    assert asia["quote_volume"] is None and asia["vwap_quality"] == "ESTIMATED_CANDLE_TYPICAL_PRICE"


def test_sweep_uses_completed_known_session_level():
    start = datetime(2026, 7, 15, tzinfo=UTC)
    bars = [bar(start + timedelta(hours=n)) for n in range(8)]
    sweep = bar(start + timedelta(hours=8), high=103, close=101)
    value = analyze_sessions([*bars, sweep], start + timedelta(hours=9), days=1)
    event = next(item for item in value["session_events"] if item["reference_session"] == "ASIA")
    assert event["event"] == "SWEEP_RECLAIM" and event["level"] == 102
    assert datetime.fromisoformat(event["reference_discovered_at"]) <= sweep.open_time
    assert value["production_weight"] == 0 and value["research_status"] == "UNVALIDATED"


def test_late_session_reference_cannot_explain_an_earlier_received_break():
    start = datetime(2026, 7, 15, tzinfo=UTC)
    bars = [bar(start + timedelta(hours=n)) for n in range(8)]
    bars[-1] = bars[-1].model_copy(update={"available_at": start + timedelta(hours=10)})
    attempt = bar(start + timedelta(hours=8), high=105, close=104)
    value = analyze_sessions([*bars, attempt], start + timedelta(hours=10), days=1)
    assert not any(item["reference_session"] == "ASIA" for item in value["session_events"])


def test_duplicate_bars_are_visible_failure_not_double_volume():
    start = datetime(2026, 7, 15, tzinfo=UTC)
    with pytest.raises(ValueError, match="Duplicate"):
        analyze_sessions([bar(start), bar(start)], start + timedelta(hours=1))
