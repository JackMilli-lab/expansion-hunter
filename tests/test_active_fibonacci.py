"""Each active chart swing is causal, independent and explicitly unvalidated."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.live_analysis import (
    ACTIVE_FIB_EXTENSIONS,
    ACTIVE_FIB_RETRACEMENTS,
    ACTIVE_FIB_TIMEFRAMES,
    LiveAnalysisSettings,
    active_fibonacci_view,
    analyze_live,
    fibonacci_research,
    live_session_view,
)
from expansion_hunter.models import TIMEFRAMES, Asset, AssetSnapshot, Candle

POINT = datetime(2026, 10, 6, 0, tzinfo=UTC)
ASSET = Asset(asset_id="synthetic", symbol="SYN", name="Synthetic test only", exchange_symbol="SYNUSDT")


def source(frame="5m", *, short=False, offset=0):
    values = [109 + index * 0.01 for index in range(24)]
    for first, second in zip([110, 100, 120], [100, 120, 112], strict=True):
        values.extend(first + (second - first) * step / 5 for step in range(5))
    values.append(112)
    if short:
        values = [220 - price for price in values]
    seconds = TIMEFRAMES[frame]
    start = POINT - timedelta(seconds=len(values) * seconds)
    return [Candle(open_time=start + timedelta(seconds=index * seconds),
                   close_time=start + timedelta(seconds=(index + 1) * seconds),
                   available_at=start + timedelta(seconds=(index + 1) * seconds),
                   open=price + offset, high=price + offset + 0.1,
                   low=price + offset - 0.1, close=price + offset,
                   volume=100, source=f"synthetic_test_only/{frame}")
            for index, price in enumerate(values)]


def analysis(candles, *, as_of=POINT, config=None):
    snapshot = AssetSnapshot(asset=ASSET, as_of=as_of, candles=candles)
    return analyze_live(snapshot, config=config)


@pytest.mark.parametrize("frame", ACTIVE_FIB_TIMEFRAMES)
@pytest.mark.parametrize("short", [False, True])
def test_each_frame_has_own_directional_causal_active_fibonacci_and_golden_pocket(frame, short):
    candles = source(frame, short=short)
    result = analysis({frame: candles})
    view = result["active_fibonacci"][frame]
    assert view["enabled"] is True
    assert view["status"] == "AVAILABLE"
    assert view["quality"] == "DERIVED"
    assert view["bias"] == ("SHORT" if short else "LONG")
    assert view["direction"] == ("DOWN" if short else "UP")
    assert view["context"]["phase"] == "ACTIVE_RETRACEMENT"
    assert view["context"]["own_timeframe"] == frame
    assert [level["ratio"] for level in view["retracements"]] == list(ACTIVE_FIB_RETRACEMENTS)
    assert [level["ratio"] for level in view["extensions"]] == list(ACTIVE_FIB_EXTENSIONS)
    assert all(level["timeframe"] == frame and level["pair_id"] == view["pair_id"] for level in view["levels"])
    assert view["golden_pocket"]["ratios"] == [0.618, 0.65]
    assert view["golden_pocket"]["lower_price"] < view["golden_pocket"]["upper_price"]
    assert view["golden_pocket"]["timeframe"] == frame
    assert view["confidence_quality"]["anchor_confirmation"] == "CONFIRMED"
    assert view["confidence_quality"]["probability"] is None
    assert view["production_weight"] == 0
    assert view["calibration"] == "UNCALIBRATED"
    assert view["source"] == f"synthetic_test_only/{frame}"
    assert view["source_timestamp"] == candles[-1].close_time.isoformat()
    for anchor in view["anchors"]:
        assert anchor["timeframe"] == frame
        assert datetime.fromisoformat(anchor["anchor_timestamp"]) < datetime.fromisoformat(anchor["discovery_timestamp"]) <= POINT
    for missing_frame in ACTIVE_FIB_TIMEFRAMES:
        if missing_frame != frame:
            assert result["active_fibonacci"][missing_frame]["status"] == "INSUFFICIENT_DATA"
    json.dumps(view, allow_nan=False)


def test_long_short_mirror_exact_retracements_extensions_and_pocket():
    long = analysis({"5m": source()})["active_fibonacci"]["5m"]
    short = analysis({"5m": source(short=True)})["active_fibonacci"]["5m"]
    for first, second in zip(long["levels"], short["levels"], strict=True):
        assert first["kind"] == second["kind"]
        assert first["ratio"] == second["ratio"]
        assert first["price"] + second["price"] == pytest.approx(220)
    assert long["golden_pocket"]["lower_price"] + short["golden_pocket"]["upper_price"] == pytest.approx(220)
    assert long["golden_pocket"]["upper_price"] + short["golden_pocket"]["lower_price"] == pytest.approx(220)


def test_six_selected_charts_never_borrow_the_4h_swing_or_prices():
    candles = {frame: source(frame, offset=index * 25) for index, frame in enumerate(ACTIVE_FIB_TIMEFRAMES)}
    result = analysis(candles)
    views = result["active_fibonacci"]
    assert set(views) == set(ACTIVE_FIB_TIMEFRAMES)
    assert len({views[frame]["pair_id"] for frame in ACTIVE_FIB_TIMEFRAMES}) == 6
    assert len({views[frame]["golden_pocket"]["lower_price"] for frame in ACTIVE_FIB_TIMEFRAMES}) == 6
    for frame in ACTIVE_FIB_TIMEFRAMES:
        assert views[frame]["context"]["current_price"] == candles[frame][-1].close
        assert all(level["timeframe"] == frame for level in views[frame]["levels"])


def test_full_research_relationships_remain_backwards_compatible():
    candles = source()
    result = analysis({"5m": candles})
    full = result["fibonacci"]
    assert {0.236, 0.382, 0.5, 0.618, 0.65, 0.705, 0.786} <= {level["ratio"] for level in full["levels"] if level["kind"] == "RETRACEMENT"}
    assert {1.0, 1.272, 1.414, 1.618, 2.0, 2.618, 4.236} <= {level["ratio"] for level in full["levels"] if level["kind"] == "EXTENSION"}
    assert full["placebo_validation"] == "NOT_YET_EVALUATED"
    assert full["production_weight"] == 0
    assert 0.236 not in {level["ratio"] for level in result["active_fibonacci"]["5m"]["levels"]}


@pytest.mark.parametrize("bad", ["future", "late", "gap", "mixed"])
def test_active_fibonacci_respects_point_in_time_and_history_integrity(bad):
    candles = source()
    point = candles[-1].available_at
    baseline = analysis({"5m": candles})["active_fibonacci"]
    if bad == "future":
        extra = candles[-1].model_copy(update={"open_time": point, "close_time": point + timedelta(minutes=5), "available_at": point + timedelta(minutes=5), "high": 10000})
        assert analysis({"5m": candles + [extra]})["active_fibonacci"] == baseline
    elif bad == "late":
        late = candles[-1].model_copy(update={"available_at": point + timedelta(hours=1)})
        assert analysis({"5m": candles[:-1] + [late]})["active_fibonacci"] == analysis({"5m": candles[:-1]})["active_fibonacci"]
    else:
        changed = list(candles)
        if bad == "gap":
            changed.pop(20)
        else:
            changed[-1] = changed[-1].model_copy(update={"source": "unrelated_exchange"})
        view = analysis({"5m": changed})["active_fibonacci"]["5m"]
        assert view["status"] == "INSUFFICIENT_DATA"
        assert view["levels"] == []
        assert view["golden_pocket"] is None


def test_unconfirmed_extreme_cannot_become_an_active_swing_anchor():
    candles = source()
    before_high_confirmation = candles[:37]
    after_high_confirmation = candles[:38]
    before = analysis({"5m": before_high_confirmation}, as_of=before_high_confirmation[-1].available_at)
    after = analysis({"5m": after_high_confirmation}, as_of=after_high_confirmation[-1].available_at)
    latest_high = candles[34].open_time.isoformat()
    assert not any(anchor["anchor_timestamp"] == latest_high for anchor in before["active_fibonacci"]["5m"]["anchors"])
    assert after["active_fibonacci"]["5m"]["anchors"][-1]["anchor_timestamp"] == latest_high


def test_active_projection_cannot_accept_premature_or_future_research_anchor():
    candles = source()
    full = fibonacci_research({"5m": candles}, [], as_of=POINT, atr=1)
    for bad in ("premature", "future", "other_frame"):
        changed = copy.deepcopy(full)
        anchor = changed["anchors"][0]["anchors"][-1]
        if bad == "premature":
            anchor["discovery_timestamp"] = (datetime.fromisoformat(anchor["anchor_timestamp"]) + timedelta(minutes=5)).isoformat()
        elif bad == "future":
            anchor["discovery_timestamp"] = (POINT + timedelta(seconds=1)).isoformat()
        else:
            anchor["timeframe"] = "4h"
        view = active_fibonacci_view({"5m": candles}, changed, POINT)["5m"]
        assert view["status"] == "INSUFFICIENT_DATA"
        assert view["anchors"] == []


@pytest.mark.parametrize("short", [False, True])
def test_loss_of_confirmed_origin_does_not_redraw_a_convenient_swing(short):
    candles = source(short=short)
    first = analysis({"5m": candles})["active_fibonacci"]["5m"]
    direction = 1 if short else -1
    opened = candles[-1].close_time
    close = first["context"]["origin_price"] + direction * 2
    extra = Candle(open_time=opened, close_time=opened + timedelta(minutes=5), available_at=opened + timedelta(minutes=5),
                   open=candles[-1].close, close=close, high=max(close, candles[-1].close) + 0.1,
                   low=min(close, candles[-1].close) - 0.1, volume=100, source=candles[-1].source)
    result = analysis({"5m": candles + [extra]}, as_of=extra.available_at)["active_fibonacci"]["5m"]
    assert result["status"] == "INVALIDATED"
    assert result["context"]["phase"] == "ORIGIN_LOST"
    assert result["bias"] == "UNAVAILABLE"
    assert result["swing_bias"] == first["bias"]
    assert result["anchors"] == first["anchors"]
    assert result["pair_id"] == first["pair_id"]


def test_stale_and_insufficient_sources_never_fake_an_active_map():
    stale = analysis({"1m": source("1m")}, as_of=POINT + timedelta(minutes=10))["active_fibonacci"]["1m"]
    assert stale["status"] == "STALE"
    assert stale["quality"] == "STALE"
    assert stale["levels"] == []
    assert stale["golden_pocket"] is None
    missing = analysis({})["active_fibonacci"]
    assert all(view["status"] == "INSUFFICIENT_DATA" and view["bias"] == "UNAVAILABLE" for view in missing.values())


def test_auto_fibonacci_default_on_and_explicit_disable_preserves_research():
    assert LiveAnalysisSettings().auto_fibonacci_enabled is True
    result = analysis({"5m": source()}, config=LiveAnalysisSettings(auto_fibonacci_enabled=False))
    assert result["fibonacci"]["anchors"] and result["fibonacci"]["levels"]
    assert all(view["status"] == "DISABLED" and view["enabled"] is False and not view["levels"] for view in result["active_fibonacci"].values())


def test_view_retains_underlying_confluence_and_convention_labels():
    candles = source()
    result = analysis({"5m": candles})
    view = result["active_fibonacci"]["5m"]
    for active in view["levels"]:
        original = next(level for level in result["fibonacci"]["levels"] if level["pair_id"] == active["pair_id"] and level["kind"] == active["kind"] and level["ratio"] == active["ratio"])
        assert active == original
    assert next(level for level in view["retracements"] if level["ratio"] == 0.705)["basis"] == "TRADING_CONVENTION"
    assert next(level for level in view["extensions"] if level["ratio"] == 1.414)["basis"] == "SQRT_2_TRADING_CONVENTION"


def complete_session_bars(frame="5m"):
    count = 360 if frame == "5m" else 1800
    duration = timedelta(seconds=TIMEFRAMES[frame])
    start = POINT - count * duration
    return [Candle(open_time=start + index * duration, close_time=start + (index + 1) * duration,
                   available_at=start + (index + 1) * duration, open=100, close=100,
                   high=101, low=99, volume=10, quote_volume=1000,
                   source=f"native_test_only/{frame}") for index in range(count)]


def test_measured_sessions_are_integrated_with_real_candle_source_metadata():
    candles = source()
    result = analysis({"5m": candles})
    sessions = result["sessions"]
    assert sessions["timeframe"] == "5m"
    assert sessions["source_timestamp"] == POINT.isoformat()
    assert sessions["received_timestamp"] == POINT.isoformat()
    assert sessions["sources"] == [candles[-1].source]
    assert sessions["production_weight"] == 0
    assert sessions["liquidity_type"] == "HISTORICAL_STRUCTURAL"
    json.dumps(sessions, allow_nan=False)


def test_sessions_prefer_full_5m_reference_to_truncated_minute_window():
    result = live_session_view({"1m": source("1m"), "5m": complete_session_bars()}, POINT)
    assert result["timeframe"] == "5m"
    complete = [row for row in result["sessions"] if row["completed"] and row["quality"] == "AVAILABLE"]
    assert {row["session"] for row in complete} == {"ASIA", "LONDON", "NEW_YORK"}
    assert all(row["coverage"] == 1 and row["vwap"] == 100 and row["vwap_quality"] == "NATIVE_VOLUME_RATIO" for row in complete)


def test_sessions_use_minute_source_when_it_has_complete_reference_coverage():
    result = live_session_view({"1m": complete_session_bars("1m"), "5m": complete_session_bars()}, POINT)
    assert result["timeframe"] == "1m"


def test_session_context_rejects_future_late_and_incoherent_data():
    candles = complete_session_bars()
    baseline = live_session_view({"5m": candles}, POINT)
    future = candles[-1].model_copy(update={
        "open_time": POINT, "close_time": POINT + timedelta(minutes=5),
        "available_at": POINT + timedelta(minutes=5), "high": 9999,
    })
    assert live_session_view({"5m": candles + [future]}, POINT) == baseline
    late = candles[-1].model_copy(update={"available_at": POINT + timedelta(minutes=1)})
    assert live_session_view({"5m": candles[:-1] + [late]}, POINT) == live_session_view({"5m": candles[:-1]}, POINT)
    incoherent = live_session_view({"5m": candles[:12] + candles[13:]}, POINT)
    assert incoherent["data_quality"] == "UNAVAILABLE"
    assert incoherent["sessions"] == [] and incoherent["session_events"] == []


def test_sessions_expose_staleness_without_fabricating_fresh_context():
    result = live_session_view({"1m": source("1m")}, POINT + timedelta(minutes=10))
    assert result["data_quality"] == "STALE"
    assert result["age_seconds"] == 600
    assert result["source_timestamp"] == POINT.isoformat()
    unavailable = live_session_view({}, POINT)
    assert unavailable["data_quality"] == "UNAVAILABLE"
    assert unavailable["timeframe"] is None and unavailable["age_seconds"] is None
