"""Synthetic regressions for the discovery time of wick-based AVWAP evidence."""

from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.avwap import analyze_anchors
from expansion_hunter.models import Candle

START = datetime(2024, 1, 1, tzinfo=UTC)


def _bars(observations):
    result = []
    for index, (opened, high, low, close, volume) in enumerate(observations):
        open_time = START + timedelta(hours=index)
        close_time = open_time + timedelta(hours=1)
        result.append(
            Candle(
                open_time=open_time,
                close_time=close_time,
                available_at=close_time,
                open=opened,
                high=high,
                low=low,
                close=close,
                volume=volume,
                source="synthetic-test",
            )
        )
    return result


def _swing_report(bars, kind="low"):
    price = bars[1].low if kind == "low" else bars[1].high
    swings = [{"index": 1, "kind": kind, "price": price, "confirmed_index": 2}]
    return analyze_anchors(bars, swings, None, None)[0]


def test_new_event_anchor_cannot_reject_on_its_discovery_candle():
    observations = [(100, 101, 99, 100, 100)] * 20 + [(100, 120, 90, 91, 400)]
    bars = _bars(observations)
    reports = analyze_anchors(bars, [], None, None)
    assert {report.kind for report in reports} == {"capitulation", "high_volume"}
    assert all(report.confirmed_at == bars[-1].close_time for report in reports)
    assert all(report.rejection is False for report in reports)

    # An independently observed wick in the next bar can establish rejection.
    subsequent = _bars(observations + [(91, 115, 90, 91, 100)])
    assert all(report.rejection for report in analyze_anchors(subsequent, [], None, None))


@pytest.mark.parametrize("kind,pattern", [("low", "support"), ("high", "resistance")])
def test_mid_bar_discovery_cannot_use_that_bars_touch(kind, pattern):
    observations = [
        (100, 101, 99, 100, 100),
        (100, 101, 90, 95, 100),
        (95, 100, 93, 94, 100),
        (94, 105, 93, 104, 100),
        (104, 106, 96, 105, 100),
        (105, 110, 105, 108, 100),
    ]
    if kind == "high":
        observations = [
            (200 - opened, 200 - low, 200 - high, 200 - close, volume)
            for opened, high, low, close, volume in observations
        ]
    bars = _bars(observations)
    assert getattr(_swing_report(bars, kind), pattern) is True
    discovery = bars[4].open_time + timedelta(minutes=30)
    bars[2] = bars[2].model_copy(update={"available_at": discovery})
    report = _swing_report(bars, kind)
    assert report.confirmed_at == discovery
    assert getattr(report, pattern) is False
    if kind == "low":
        assert report.accepted_closes == 2


def test_mid_bar_discovery_cannot_use_that_bars_rejection_wick():
    bars = _bars(
        [
            (100, 101, 99, 100, 100),
            (100, 101, 90, 95, 100),
            (95, 100, 93, 94, 100),
            (94, 99, 93, 94, 100),
            (94, 120, 92, 93, 100),
        ]
    )
    assert _swing_report(bars).rejection is True
    discovery = bars[-1].open_time + timedelta(minutes=30)
    bars[2] = bars[2].model_copy(update={"available_at": discovery})
    report = _swing_report(bars)
    assert report.confirmed_at == discovery
    assert report.rejection is False


def test_post_discovery_closes_still_establish_reclaim_for_partial_first_bar():
    bars = _bars(
        [
            (100, 101, 99, 100, 100),
            (100, 101, 90, 95, 100),
            (95, 100, 93, 94, 100),
            (94, 105, 93, 104, 100),
            (104, 106, 103, 105, 100),
        ]
    )
    discovery = bars[3].open_time + timedelta(minutes=30)
    bars[2] = bars[2].model_copy(update={"available_at": discovery})
    report = _swing_report(bars)
    assert report.confirmed_at == discovery
    assert report.accepted_closes == 2
    assert report.reclaim is True
