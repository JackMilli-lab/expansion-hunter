"""Synthetic mathematical fixtures, never presented as real market observations."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.avwap import analyze_anchors, anchored_vwap
from expansion_hunter.models import TIMEFRAMES, Candle
from expansion_hunter.technical import analyze, confirmed_swings

START = datetime(2024, 1, 1, tzinfo=UTC)


def series(count=240, timeframe="4h", prices=None, volumes=None, flat=False):
    duration = timedelta(seconds=TIMEFRAMES[timeframe])
    result = []
    for i in range(count):
        price = prices[i] if prices is not None else 100 + (0 if flat else math.sin(i * 0.3) * 0.5)
        opened = START + i * duration
        result.append(
            Candle(
                open_time=opened,
                close_time=opened + duration,
                available_at=opened + duration,
                open=price,
                high=price + 0.4,
                low=price - 0.4,
                close=price,
                volume=volumes[i] if volumes is not None else 100,
                source="synthetic-test",
            )
        )
    return result


def test_prefix_unchanged_by_future_or_delayed_observations():
    bars = series(241)
    as_of = bars[-2].available_at
    expected = analyze(bars[:-1], "4h", as_of)
    assert analyze(bars, "4h", as_of) == expected
    delayed = bars[-1].model_copy(update={"available_at": as_of + timedelta(days=3)})
    assert analyze(bars[:-1] + [delayed], "4h", as_of) == expected


@pytest.mark.parametrize("failure", ["duplicate", "gap", "duration"])
def test_bad_chronology_fails_closed(failure):
    bars = series()
    if failure == "duplicate":
        bars.insert(50, bars[50])
    elif failure == "gap":
        del bars[50]
    else:
        bars[50] = bars[50].model_copy(update={"close_time": bars[50].close_time - timedelta(minutes=10)})
    snapshot = analyze(bars, "4h", bars[-1].available_at)
    assert snapshot.issues
    assert not snapshot.metrics
    assert all(value is None for value in snapshot.patterns.values())


def test_indicator_seed_and_flat_market_neutrality():
    bars = series(flat=True)
    snapshot = analyze(bars, "4h", bars[-1].available_at)
    for period in (10, 20, 50, 100, 200):
        assert snapshot.metrics[f"ema{period}"] == pytest.approx(100)
    assert snapshot.metrics["rsi14"] == 50
    assert snapshot.metrics["atr14"] == pytest.approx(0.8)
    assert snapshot.metrics["macd"] == pytest.approx(0)
    assert snapshot.metrics["realized_vol"] == 0
    assert snapshot.metrics["bb_width_percentile"] == pytest.approx(0.5)
    assert snapshot.patterns["compression"] is False
    assert snapshot.metrics["resistance_tests"] == 1


def test_relative_volume_excludes_current_bar_from_baseline():
    volumes = [10] * 239 + [1000]
    bars = series(volumes=volumes)
    snapshot = analyze(bars, "4h", bars[-1].available_at)
    assert snapshot.metrics["relative_volume"] == 100
    assert snapshot.metrics["spot_volume_expansion"] == 100
    assert snapshot.metrics["aggressive_sell_share"] is None
    assert snapshot.patterns["absorption"] is None


def test_short_calendar_history_cannot_be_multiweek_structure():
    bars = series(300, "1m")
    snapshot = analyze(bars, "1m", bars[-1].available_at)
    assert snapshot.metrics["ema200"] is not None
    assert snapshot.metrics["range_age_days"] is None
    assert all(value is None for value in snapshot.patterns.values())
    assert "insufficient_calendar_history_for_structures_minimum_21_days" in snapshot.issues


def test_weekly_range_search_has_enough_calendar_horizon():
    bars = series(240, "1W")
    snapshot = analyze(bars, "1W", bars[-1].available_at)
    assert snapshot.levels["range_defined"] is True
    assert snapshot.metrics["range_age_days"] >= 32 * 7


def test_insufficient_warmup_is_unknown_not_zero():
    bars = series(10)
    snapshot = analyze(bars, "4h", bars[-1].available_at)
    assert snapshot.metrics["ema10"] is not None
    assert snapshot.metrics["ema20"] is None
    assert snapshot.metrics["rsi14"] is None
    assert snapshot.metrics["atr14"] is None
    assert all(value is None for value in snapshot.patterns.values())


def test_stale_bars_have_features_but_no_signals():
    bars = series()
    snapshot = analyze(bars, "4h", bars[-1].available_at + timedelta(days=2))
    assert snapshot.metrics["close"] is not None
    assert all(value is None for value in snapshot.patterns.values())
    assert "stale_latest_candle" in snapshot.issues


def test_fixed_prior_range_and_two_close_breakout_acceptance():
    bars = series()
    level = max(c.high for c in bars[:-2])
    bars[-2] = bars[-2].model_copy(
        update={"open": level + 0.4, "high": level + 1, "low": level + 0.3, "close": level + 0.6}
    )
    bars[-1] = bars[-1].model_copy(
        update={"open": level + 0.6, "high": level + 1, "low": level - 0.1, "close": level + 0.7}
    )
    snapshot = analyze(bars, "4h", bars[-1].available_at)
    assert snapshot.metrics["range_high"] == level
    assert snapshot.patterns["accepted_breakout"] is True
    assert snapshot.patterns["retest"] is True
    assert snapshot.levels["breakout_level"] == level


def test_wick_above_resistance_is_never_accepted_breakout():
    bars = series()
    bars[-1] = bars[-1].model_copy(update={"high": 125})
    snapshot = analyze(bars, "4h", bars[-1].available_at)
    assert snapshot.patterns["breakout"] is False
    assert snapshot.patterns["accepted_breakout"] is False


def test_breakout_level_stays_fixed_through_later_retest():
    bars = series(245)
    level = max(c.high for c in bars[:238])
    for i in range(238, 245):
        bars[i] = bars[i].model_copy(
            update={"open": level + 0.7, "high": level + 1.2, "low": level + 0.4, "close": level + 0.8}
        )
    bars[-1] = bars[-1].model_copy(update={"low": level - 0.1})
    early = analyze(bars[:240], "4h", bars[239].available_at)
    late = analyze(bars, "4h", bars[-1].available_at)
    assert early.levels["breakout_level"] == level
    assert late.levels["breakout_level"] == level
    assert late.patterns["accepted_breakout"] is True
    assert late.patterns["retest"] is True


def test_session_vwap_requires_utc_midnight_coverage():
    bars = series(400, "1m")
    # Six hours of observations beginning at noon is not the full UTC session.
    shift = timedelta(hours=12)
    partial = [
        c.model_copy(
            update={
                "open_time": c.open_time + shift,
                "close_time": c.close_time + shift,
                "available_at": c.available_at + shift,
            }
        )
        for c in bars
    ]
    snapshot = analyze(partial, "1m", partial[-1].available_at)
    assert snapshot.metrics["vwap"] is None
    assert snapshot.metrics["window_vwap"] is not None
    assert "partial_session_vwap_unavailable" in snapshot.issues
    full = analyze(bars, "1m", bars[-1].available_at)
    assert full.metrics["vwap"] is not None


def test_native_quote_volume_produces_traded_session_vwap():
    bars = [c.model_copy(update={"quote_volume": c.volume * 100.2}) for c in series(flat=True)]
    snapshot = analyze(bars, "4h", bars[-1].available_at)
    assert snapshot.metrics["vwap"] == pytest.approx(100.2)
    assert snapshot.levels["vwap_method"] == "exchange quote/base traded volume; UTC session"


def test_native_anchored_vwap_and_mixed_quote_data_remain_prefix_causal():
    bars = [c.model_copy(update={"quote_volume": c.volume * 100.2}) for c in series(5, "1h", flat=True)]
    known = anchored_vwap(bars, 0)
    assert known[-1] == pytest.approx(100.2)
    future = series(6, "1h", flat=True)[-1]
    expanded = anchored_vwap(bars + [future], 0)
    assert expanded[: len(bars)] == known


def test_swings_require_right_hand_confirmation():
    prices = [100, 101, 103, 108, 104, 103, 102]
    bars = series(7, "1D", prices=prices)
    assert not [s for s in confirmed_swings(bars[:-1]) if s["index"] == 3]
    swing = next(s for s in confirmed_swings(bars) if s["kind"] == "high")
    assert swing["index"] == 3
    assert swing["confirmed_index"] == 6
    assert swing["confirmed_at"] == bars[6].available_at.isoformat()


def test_52_week_levels_require_calendar_coverage_and_ath_is_unknown():
    short = series()
    snapshot = analyze(short, "4h", short[-1].available_at)
    assert snapshot.metrics["distance_52w_high_pct"] is None
    daily = series(400, "1D")
    snapshot = analyze(daily, "1D", daily[-1].available_at)
    assert snapshot.metrics["distance_52w_high_pct"] is not None
    assert snapshot.metrics["distance_52w_low_pct"] is not None
    assert snapshot.metrics["distance_ath_pct"] is None
    assert snapshot.metrics["vwap"] is None


def test_profile_is_labelled_approximation_and_handles_zero_volume():
    bars = series(volumes=[0] * 240)
    snapshot = analyze(bars, "4h", bars[-1].available_at)
    profile = snapshot.levels["volume_profile"]
    assert profile["reliable_trade_distribution"] is False
    assert "approximation" in profile["method"]
    assert profile["poc"] is None
    assert snapshot.metrics["relative_volume"] is None
    assert all(anchor.value is None for anchor in snapshot.anchors)


def avwap_fixture():
    bars = series(5, "1h", flat=True)
    observations = [
        (100, 101, 99, 100),
        (100, 101, 90, 95),
        (95, 100, 93, 94),
        (94, 105, 93, 104),
        (104, 106, 103, 105),
    ]
    for i, (opened, high, low, close) in enumerate(observations):
        bars[i] = bars[i].model_copy(update={"open": opened, "high": high, "low": low, "close": close})
    swings = [{"index": 1, "kind": "low", "price": 90, "confirmed_index": 2}]
    return bars, swings


def test_avwap_acceptance_and_running_weighted_math():
    bars, swings = avwap_fixture()
    curve = anchored_vwap(bars, 1)
    assert curve[0] is None
    assert curve[1] == pytest.approx((101 + 90 + 95) / 3)
    one = analyze_anchors(bars[:4], swings, None, None)[0]
    assert one.reclaim is False
    two = analyze_anchors(bars, swings, None, None)[0]
    assert two.reclaim is True
    assert two.accepted_closes == 2
    assert two.confirmed_at == bars[2].available_at


def test_avwap_wick_rejection_and_range_anchor_cannot_backdate_acceptance():
    bars, swings = avwap_fixture()
    bars[-1] = bars[-1].model_copy(update={"open": 94, "high": 120, "low": 92, "close": 93})
    report = analyze_anchors(bars, swings, None, None)[0]
    assert report.reclaim is False
    assert report.rejection is True
    bars, _ = avwap_fixture()
    report = analyze_anchors(bars, [], 1, 101)[0]
    assert report.kind == "range_start"
    assert report.accepted_closes <= 1
    assert report.reclaim is False


def test_range_avwap_accepts_only_after_causal_range_recognition():
    bars = series(flat=True)
    for i, price in ((237, 99), (238, 101), (239, 101)):
        bars[i] = bars[i].model_copy(
            update={"open": price, "high": price + 0.4, "low": price - 0.4, "close": price}
        )
    snapshot = analyze(bars, "4h", bars[-1].available_at)
    anchor = next(a for a in snapshot.anchors if a.kind == "range_start")
    assert anchor.confirmed_at == bars[125].available_at  # First observable 21-day base.
    assert anchor.reclaim is True
    assert anchor.accepted_closes == 2
    retrieved_at = bars[-1].close_time + timedelta(seconds=1)
    downloaded = [b.model_copy(update={"available_at": retrieved_at}) for b in bars]
    delayed = analyze(downloaded, "4h", retrieved_at)
    delayed_anchor = next(a for a in delayed.anchors if a.kind == "range_start")
    assert delayed_anchor.confirmed_at == retrieved_at
    assert delayed_anchor.accepted_closes == 0
    assert delayed_anchor.reclaim is False
