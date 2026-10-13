"""Deterministic synthetic evidence; these fixtures are not market research."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.live_analysis import (
    PHI,
    LiveAnalysisSettings,
    analyze_live,
    fibonacci_research,
    order_book_analysis,
)
from expansion_hunter.live_models import DepthLevel, LiveMarketEvent
from expansion_hunter.models import TIMEFRAMES, Asset, AssetSnapshot, Candle

START = datetime(2024, 1, 1, tzinfo=UTC)
ASSET = Asset(asset_id="synthetic", symbol="SYN", name="Synthetic", exchange_symbol="SYNUSDT")


def candles(count=240, timeframe="5m", *, quote=True):
    seconds = TIMEFRAMES[timeframe]
    bars = []
    for index in range(count):
        price = 100 + 4 * math.sin(index * 0.3)
        opened = START + timedelta(seconds=index * seconds)
        bars.append(
            Candle(
                open_time=opened,
                close_time=opened + timedelta(seconds=seconds),
                available_at=opened + timedelta(seconds=seconds),
                open=price,
                high=price + 0.5,
                low=price - 0.5,
                close=price,
                volume=100,
                quote_volume=price * 100 if quote else None,
                taker_buy_volume=60,
                source="synthetic-test",
            )
        )
    return bars


def snapshot(bars, timeframe="5m", as_of=None):
    return AssetSnapshot(asset=ASSET, as_of=as_of or bars[-1].available_at, candles={timeframe: bars})


def book(*, when=START, gap=False, bids=None, asks=None):
    return LiveMarketEvent(
        symbol="SYNUSDT",
        kind="DEPTH",
        received_timestamp=when,
        processed_timestamp=when,
        source="synthetic-native-depth",
        depth_scope="TOP_20_SNAPSHOT",
        sequence_gap=gap,
        bids=[DepthLevel(price=p, quantity=q) for p, q in (bids or [(99.5, 1), (99.0, 1), (98.5, 10)])],
        asks=[DepthLevel(price=p, quantity=q) for p, q in (asks or [(100.5, 1), (101.0, 10), (102.5, 20)])],
    )


def test_empty_is_unavailable_without_fabricated_indicators_or_prices():
    result = analyze_live(AssetSnapshot(asset=ASSET, as_of=START))
    assert result["price"] is None
    assert result["overlays"] == []
    assert result["liquidity_map"] == []
    assert result["order_book"]["imbalance"] is None
    assert result["fibonacci"]["quality"] == "UNAVAILABLE"
    assert result["fibonacci"]["production_weight"] == 0
    assert result["data_quality"]["spot_candles"] == "UNAVAILABLE"


def test_existing_indicators_and_native_session_vwap_are_reused():
    bars = candles()
    result = analyze_live(snapshot(bars))
    metrics = result["indicators"]["5m"]["metrics"]
    for name in ("ema20", "ema50", "ema100", "ema200", "rsi14", "macd", "atr14"):
        assert metrics[name] is not None
    vwap = next(item for item in result["overlays"] if item["kind"] == "VWAP")
    assert vwap["price"] == pytest.approx(
        sum(bar.quote_volume for bar in bars) / sum(bar.volume for bar in bars)
    )
    assert vwap["quality"] == "DERIVED"
    ema = next(item for item in result["overlays"] if item["kind"] == "EMA200")
    assert len(ema["points"]) == 41


def test_future_candles_and_delayed_candles_do_not_change_analysis():
    bars = candles(241, "1h")
    point = bars[-2].available_at
    expected = analyze_live(snapshot(bars[:-1], "1h", point))
    assert analyze_live(snapshot(bars, "1h", point)) == expected
    delayed = bars[-2].model_copy(update={"available_at": point + timedelta(days=1)})
    delayed_result = analyze_live(snapshot(bars[:-2] + [delayed], "1h", point))
    assert delayed_result == analyze_live(snapshot(bars[:-2], "1h", point))


@pytest.mark.parametrize("bad", ["duplicate", "gap", "mixed_source"])
def test_invalid_candle_chronology_fails_closed(bad):
    bars = candles()
    if bad == "duplicate":
        bars.insert(20, bars[20])
    elif bad == "gap":
        del bars[20]
    else:
        bars[20] = bars[20].model_copy(update={"source": "other-test-source"})
    result = analyze_live(snapshot(bars))
    assert result["indicators"]["5m"]["quality"] == "UNAVAILABLE"
    assert result["overlays"] == []
    assert result["issues"]


def test_stale_candles_are_visible_and_do_not_create_current_fib_scores():
    bars = candles(240, "1h")
    result = analyze_live(snapshot(bars, "1h", bars[-1].close_time + timedelta(days=2)))
    assert result["indicators"]["1h"]["quality"] == "STALE"
    assert result["data_quality"]["spot_candles"] == "STALE"
    assert result["price"] is None
    assert result["fibonacci"]["scores"]["fib_confluence"] is None


def test_estimated_profile_has_real_prices_and_never_becomes_order_book():
    result = analyze_live(snapshot(candles()))
    poc = next(item for item in result["overlays"] if item["kind"] == "POC")
    assert 95 <= poc["price"] <= 105
    assert poc["category"] == "HISTORICAL_TRADE_VOLUME"
    assert poc["quality"] == "ESTIMATED"
    assert result["volume_profiles"]["5m"]["rolling"]["24h"]["quality"] == "UNAVAILABLE"
    assert all(level["category"] != "OBSERVED_ORDER_BOOK" for level in result["liquidity_map"])


def test_longer_profile_window_requires_real_full_history():
    result = analyze_live(snapshot(candles(300)))
    profile = result["volume_profiles"]["5m"]["rolling"]["24h"]
    assert profile["quality"] == "ESTIMATED"
    assert profile["bars"] == 288
    assert result["volume_profiles"]["5m"]["rolling"]["3d"]["quality"] == "UNAVAILABLE"


def test_native_depth_exact_prices_quantities_walls_and_partial_coverage():
    result = order_book_analysis(book(), START)
    native = {level["price"]: level for level in result["levels"]}
    assert native[101.0]["size"] == 10
    assert native[102.5]["notional"] == 2050
    assert native[98.5]["type"] == "BID_WALL"
    assert native[98.5]["category"] == "OBSERVED_ORDER_BOOK"
    assert result["bands"]["2%"]["bid_coverage"] == "PARTIAL_TOP_20"
    assert result["bands"]["2%"]["ask_coverage"] == "BOUNDARY_OBSERVED"
    assert result["liquidity_vacuum_score"] is None
    assert result["exchange_timestamp_available"] is False


def test_native_book_values_do_not_invent_usd_currency_or_stop_orders():
    result = analyze_live(AssetSnapshot(asset=ASSET, as_of=START), book())
    assert result["price"] == 100
    assert result["quote_asset"] == "USDT"
    assert result["data_quality"]["inferred_stops"] == "UNAVAILABLE"
    assert result["data_quality"]["derivatives"] == "UNAVAILABLE"
    assert all(level["size"] is not None for level in result["liquidity_map"])


def test_book_disappearance_is_invalidated_not_fake_consumption():
    first = order_book_analysis(book(), START)
    later = START + timedelta(seconds=1)
    changed = book(when=later, asks=[(100.5, 1), (102.5, 20)])
    second = order_book_analysis(changed, later, prior_levels=first["levels"])
    removed = next(level for level in second["levels"] if level["price"] == 101)
    assert removed["status"] == "INVALIDATED"
    assert "unknown" in removed["reason"]
    retained = next(level for level in second["levels"] if level["price"] == 102.5)
    assert retained["discovered_at"] == START.isoformat()
    assert all(level["status"] != "CONSUMED" for level in second["levels"])


def test_book_proximity_uses_atr_when_available():
    result = order_book_analysis(book(), START, price=100.4, atr=1)
    nearest = next(level for level in result["levels"] if level["price"] == 100.5)
    assert nearest["status"] == "TESTING"
    assert next(level for level in result["levels"] if level["price"] == 102.5)["status"] == "RESTING"


def test_missing_stale_and_future_book_are_explicit():
    assert order_book_analysis(None, START)["quality"] == "UNAVAILABLE"
    stale = order_book_analysis(book(), START + timedelta(seconds=16))
    assert stale["quality"] == "STALE"
    assert stale["levels"]
    future = order_book_analysis(book(when=START + timedelta(seconds=1)), START)
    assert future["quality"] == "UNAVAILABLE"
    assert future["levels"] == []
    assert order_book_analysis(book(gap=True), START)["quality"] == "STALE"


def test_native_exchange_age_does_not_become_fresh_from_late_receipt():
    event = book(when=START + timedelta(minutes=1)).model_copy(update={"exchange_timestamp": START})
    result = order_book_analysis(event, START + timedelta(minutes=1))
    assert result["quality"] == "STALE"
    assert result["age_seconds"] == 60


def test_invalid_or_non_depth_input_is_reported_without_crashing():
    assert order_book_analysis({"bids": []}, START)["quality"] == "UNAVAILABLE"
    ticker = LiveMarketEvent(
        symbol="SYNUSDT",
        kind="BOOK_TICKER",
        received_timestamp=START,
        processed_timestamp=START,
        bid=DepthLevel(price=99, quantity=1),
        ask=DepthLevel(price=101, quantity=1),
    )
    assert order_book_analysis(ticker, START)["quality"] == "UNAVAILABLE"


def test_structural_demand_and_equal_lows_have_no_invented_order_sizes():
    result = analyze_live(snapshot(candles()))
    demand = [level for level in result["liquidity_map"] if level["type"] == "DEMAND_ZONE"]
    assert demand
    assert all(level["size"] is None and level["category"] == "INFERRED_STRUCTURE" for level in demand)
    assert all(
        datetime.fromisoformat(level["discovered_at"]) > datetime.fromisoformat(level["event_timestamp"])
        for level in demand
    )
    equal = [level for level in result["liquidity_map"] if level["type"] in {"EQH", "EQL"}]
    assert equal
    assert all(level["strength"] >= 2 and level["size"] is None for level in equal)


def test_swings_cannot_generate_fib_before_right_hand_confirmation():
    bars = candles(20, "1h")
    prior = fibonacci_research({"1h": bars[:16]}, [], current_price=100, atr=1)
    latest = fibonacci_research({"1h": bars}, [], current_price=100, atr=1)
    for pair in prior["anchors"]:
        assert all(
            datetime.fromisoformat(anchor["discovery_timestamp"]) <= bars[15].available_at
            for anchor in pair["anchors"]
        )
    for pair in latest["anchors"]:
        assert all(
            datetime.fromisoformat(anchor["discovery_timestamp"]) <= bars[-1].available_at
            for anchor in pair["anchors"]
        )
    # A single confirmed extremum is not an objective opposite-anchor impulse.
    assert prior["levels"] == []


def test_fibonacci_relationships_and_conventions_are_explicit():
    result = analyze_live(snapshot(candles(240, "1h"), "1h"))["fibonacci"]
    assert result["phi"] == PHI
    assert result["levels"]
    by_ratio = {(level["kind"], level["ratio"]): level for level in result["levels"]}
    assert by_ratio[("RETRACEMENT", 0.5)]["basis"] == "TRADING_CONVENTION"
    assert by_ratio[("RETRACEMENT", 0.65)]["basis"] == "TRADING_CONVENTION"
    assert by_ratio[("EXTENSION", 4.236)]["price"] > 0
    assert result["production_weight"] == 0
    assert result["calibration"] == "UNCALIBRATED"
    assert result["placebo_validation"] == "NOT_YET_EVALUATED"
    assert result["golden_pockets"][0]["ratios"] == [0.618, 0.65]


def test_isolated_fibonacci_touch_cannot_gain_confluence_points():
    bars = candles(240, "1h")
    base = fibonacci_research({"1h": bars}, [], current_price=100, atr=1)
    assert all(level["confluence_score"] == 0 for level in base["levels"])
    assert all(level["cluster_score"] == 0 for level in base["levels"])


def test_correlated_ema_and_profile_matches_are_not_counted_repeatedly():
    bars = candles(240, "1h")
    base = fibonacci_research({"1h": bars}, [], atr=1)
    price = next(level["price"] for level in base["levels"] if level["ratio"] == 0.618)
    overlays = [
        {"kind": kind, "price": price, "timeframe": "1h", "quality": "DERIVED"}
        for kind in ("EMA20", "EMA50", "EMA100", "EMA200", "POC", "VAH", "HVN")
    ]
    result = fibonacci_research({"1h": bars}, overlays, current_price=price, atr=1)
    level = next(level for level in result["levels"] if level["ratio"] == 0.618)
    assert level["confluence_score"] == 50
    assert len(level["confluence"]) == 2
    assert result["production_weight"] == 0


def test_avwap_curves_are_visible_only_after_anchor_discovery():
    bars = candles()
    result = analyze_live(snapshot(bars))
    anchors = [overlay for overlay in result["overlays"] if overlay["kind"] == "AVWAP"]
    assert anchors
    for anchor in anchors:
        discovery = datetime.fromisoformat(anchor["discovered_at"])
        assert discovery <= bars[-1].available_at
        for point in anchor["points"]:
            opened = datetime.fromtimestamp(point["time"], UTC)
            assert opened + timedelta(minutes=5) >= discovery


def test_weekly_vwap_requires_real_monday_start():
    full = analyze_live(snapshot(candles()))
    assert full["weekly_vwap"]["value"] is not None
    assert full["weekly_vwap"]["quality"] == "DERIVED"
    shifted = [
        bar.model_copy(
            update={
                "open_time": bar.open_time + timedelta(hours=1),
                "close_time": bar.close_time + timedelta(hours=1),
                "available_at": bar.available_at + timedelta(hours=1),
            }
        )
        for bar in candles()
    ]
    partial = analyze_live(snapshot(shifted))
    assert partial["weekly_vwap"]["value"] is None
    assert partial["weekly_vwap"]["quality"] == "UNAVAILABLE"


def test_config_rejects_inverted_zones_and_distance_thresholds():
    with pytest.raises(ValueError):
        LiveAnalysisSettings(golden_pocket_low=0.7, golden_pocket_high=0.6)
    with pytest.raises(ValueError):
        LiveAnalysisSettings(testing_distance_atr=1, approaching_distance_atr=0.5)


@pytest.mark.parametrize('frame', ['1m', '5m', '15m', '1h', '4h', '1D'])
def test_fibonacci_research_exists_on_all_live_structure_timeframes(frame):
    bars = candles(120, frame)
    result = fibonacci_research({frame: bars}, [], atr=1.0, as_of=bars[-1].available_at)
    assert result['production_weight'] == 0
    levels = [level for level in result['levels'] if level['timeframe'] == frame]
    assert levels
    convention = next(level for level in levels if level['ratio'] == 0.705)
    assert convention['basis'] == 'TRADING_CONVENTION'
    for pair in result['anchors']:
        assert datetime.fromisoformat(pair['discovered_at']) <= bars[-1].available_at
