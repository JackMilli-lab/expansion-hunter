"""Synthetic, causal mathematical tests; no historical winner claims."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.discovery_config import DiscoverySettings
from expansion_hunter.discovery_features import (
    _score,
    _structures,
    deep_scores,
    light_features,
    volume_awakening,
)
from expansion_hunter.discovery_models import DiscoveryFeature, DiscoveryObservation, Freshness
from expansion_hunter.models import TIMEFRAMES, Asset, AssetSnapshot, Candle, Metric, Status

END = datetime(2025, 1, 3, tzinfo=UTC)


def bars(count=128, frame="15m", prices=None, volumes=None):
    interval = timedelta(seconds=TIMEFRAMES[frame])
    result = []
    for i in range(count):
        price = prices[i] if prices is not None else 100 + math.sin(i * .31)
        opened = END - (count - i) * interval
        volume = volumes[i] if volumes is not None else 100 + i % 7
        result.append(Candle(open_time=opened, close_time=opened + interval,
                             available_at=opened + interval, open=price, high=price + .3,
                             low=price - .3, close=price, volume=volume,
                             quote_volume=price * volume, taker_buy_volume=volume * .45,
                             source="synthetic-test/spot"))
    return result


def snapshot(candles=None, market=None, as_of=END):
    return AssetSnapshot(asset=Asset(asset_id="unknown-token", symbol="XYZ", name="Unknown"),
                         as_of=as_of, candles=candles or {"15m": bars()}, market=market or {})


def metric(value, unit="USD", source="coingecko/coins/markets", age=0):
    return Metric(value=value, observed_at=END - timedelta(seconds=age), available_at=END,
                  source=source, unit=unit, status=Status.AVAILABLE)


def absorption_bars():
    result = bars(prices=[99] * 32 + [96] * 96, volumes=[100] * 128)
    for i in range(32):
        result[i] = result[i].model_copy(update={"high": 100., "low": 98., "quote_volume": None})
    for touch, low, recovery in ((50, 88, 5), (70, 93, 3), (90, 97, 2)):
        result[touch] = result[touch].model_copy(update={"open": 99.5, "high": 100.,
                                                       "low": 99., "close": 99.5,
                                                       "quote_volume": None})
        for step in range(1, recovery):
            result[touch + step] = result[touch + step].model_copy(update={
                "open": low + .5, "high": low + 1, "low": float(low), "close": low + .5,
                "quote_volume": None})
        for step in range(recovery, 7):
            result[touch + step] = result[touch + step].model_copy(update={
                "open": 99.5, "high": 99.8, "low": 99., "close": 99.5, "quote_volume": None})
    return result


def test_rvol_baseline_excludes_current_bar_and_works_with_intraday_history():
    data = bars(volumes=[10] * 127 + [1000])
    features = light_features(snapshot({"15m": data}), DiscoverySettings())
    assert features["rvol"].value == 100
    assert features["rvol"].historical_percentile == 1
    assert features["rvol"].sample_count == 107
    assert features["price_change"].freshness == Freshness.FRESH
    assert features["volatility"].value is not None
    assert features["ema_convergence"].value is not None
    assert (data[-1].close_time - data[0].open_time).days < 21


def test_flat_historical_percentile_is_neutral_and_zero_volume_is_unknown():
    features = light_features(snapshot({"15m": bars(prices=[100] * 128, volumes=[0] * 128)}),
                              DiscoverySettings())
    assert features["volatility"].historical_percentile == .5
    assert features["rvol"].value is None
    assert features["rvol"].freshness == Freshness.MISSING
    assert features["volume_acceleration"].value is None


def test_future_and_delayed_candles_do_not_change_light_or_deep_state():
    data = absorption_bars()
    original = snapshot({"15m": data})
    future = data[-1].model_copy(update={"open_time": END, "close_time": END + timedelta(minutes=15),
                                        "available_at": END + timedelta(minutes=15), "close": 10_000,
                                        "high": 10_001, "low": 95, "quote_volume": None})
    later = snapshot({"15m": data + [future]})
    settings = DiscoverySettings()
    assert light_features(original, settings) == light_features(later, settings)
    assert deep_scores(original, settings) == deep_scores(later, settings)
    unavailable = future.model_copy(update={"close_time": END, "open_time": END - timedelta(minutes=15)})
    assert light_features(snapshot({"15m": data + [unavailable]}), settings) == light_features(original, settings)


@pytest.mark.parametrize("kind", ["gap", "duplicate", "mixed"])
def test_corrupt_light_chronology_fails_closed(kind):
    data = bars()
    if kind == "gap":
        del data[30]
    elif kind == "duplicate":
        data.append(data[30])
    else:
        data[30] = data[30].model_copy(update={"source": "other-venue"})
    features = light_features(snapshot({"15m": data}), DiscoverySettings())
    assert features["price_change"].value is None
    assert features["rvol"].freshness == Freshness.MISSING
    assert features["rvol"].reason


def test_freshness_uses_event_time_and_does_not_make_old_data_fresh_at_retrieval():
    data = bars()
    data = [b.model_copy(update={"available_at": END + timedelta(hours=4)}) for b in data]
    observations = light_features(snapshot({"15m": data}, as_of=END + timedelta(hours=4)), DiscoverySettings())
    assert observations["price_change"].freshness == Freshness.STALE
    assert observations["price_change"].age_seconds == 14_400
    assert observations["price_change"].retrieved_at == END + timedelta(hours=4)
    scores, levels = deep_scores(snapshot({"15m": data}, as_of=END + timedelta(hours=4)), DiscoverySettings())
    assert scores["stored_energy"].value is None
    assert scores["trigger_sensitivity"].value is None
    assert levels["price"] is None


def test_turnover_requires_dated_aligned_usd_coingecko_data():
    settings = DiscoverySettings()
    market = {"market_cap": metric(1_000_000), "volume_24h": metric(100_000, "USD; aggregated reported volume")}
    assert light_features(snapshot(market=market), settings)["turnover"].value == .1
    market["volume_24h"] = metric(100_000, "USDT", "binance/spot/ticker24h")
    assert light_features(snapshot(market=market), settings)["turnover"].value is None
    market["volume_24h"] = metric(100_000, "USD; aggregated", age=120)
    assert light_features(snapshot(market=market), settings)["turnover"].value is None
    old = {"market_cap": metric(1_000_000, age=3600), "volume_24h": metric(100_000, "USD; aggregated", age=3600)}
    turnover = light_features(snapshot(market=old), settings)["turnover"]
    assert turnover.value == .1
    assert turnover.freshness == Freshness.STALE


def test_repeated_archive_polling_cannot_inflate_history_sample_count():
    settings = DiscoverySettings(minimum_history_samples=3)
    market = {"market_cap": metric(1_000_000), "volume_24h": metric(100_000, "USD; aggregated")}
    current = snapshot(market=market)
    template = light_features(current, settings)["turnover"]
    old = template.model_copy(update={"value": .05, "source_timestamp": END - timedelta(minutes=10),
                                      "retrieved_at": END - timedelta(minutes=10)})
    repeated = [DiscoveryObservation(asset=current.asset, as_of=END - timedelta(seconds=500 - i),
                                     features={"turnover": old}) for i in range(100)]
    assert light_features(current, settings, historical=repeated)["turnover"].sample_count == 1
    assert light_features(current, settings, historical=repeated)["turnover"].historical_percentile is None
    future = DiscoveryFeature(value=100, provider=template.provider, unit=template.unit,
                              timeframe=template.timeframe, freshness=Freshness.FRESH,
                              source_timestamp=END + timedelta(minutes=1), retrieved_at=END + timedelta(minutes=1))
    repeated.append(DiscoveryObservation(asset=current.asset, as_of=END - timedelta(seconds=1), features={"turnover": future}))
    assert light_features(current, settings, historical=repeated)["turnover"].sample_count == 1


def test_broad_liquidity_proxy_does_not_assume_spread_when_only_volume_is_known():
    market = {"quote_volume_24h": metric(10_000_000, "USDT", "binance/spot")}
    result = light_features(snapshot(market=market), DiscoverySettings())["liquidity_quality"]
    assert result.value == 50
    assert "1/2" in result.reason


def test_relative_strength_requires_identical_endpoints_not_equal_bar_counts():
    settings = DiscoverySettings()
    left = snapshot({"15m": bars(prices=[100 + i * .1 for i in range(128)])})
    right = snapshot({"15m": bars(prices=[100] * 128)})
    feature = light_features(left, settings, right)["relative_strength_btc"]
    assert feature.value > 0
    stale = right.model_copy(update={"candles": {"15m": right.candles["15m"][:-1]}})
    assert light_features(left, settings, stale)["relative_strength_btc"].value is None


def test_absorption_is_unknown_without_three_settled_reactions():
    scores, _ = deep_scores(snapshot({"15m": bars(prices=[100] * 128)}), DiscoverySettings())
    assert scores["absorption"].value is None
    data = absorption_bars()[:80]
    assert _structures(data, DiscoverySettings())["rejection_decay"] is None


def test_absorption_settled_rejections_reward_decay_and_rising_lows():
    data = absorption_bars()
    structure = _structures(data, DiscoverySettings())
    assert structure["tests"] == 3
    assert structure["rejection_decay"] == pytest.approx(.75)
    assert structure["higher_lows"] == 1
    assert structure["recovery_improvement"] == pytest.approx(.75)
    scores, _ = deep_scores(snapshot({"15m": data}), DiscoverySettings())
    assert scores["absorption"].value is not None
    assert scores["absorption"].value > 60
    # A new test with an unobserved reaction is excluded, not credited as zero rejection.
    late = data[-1].model_copy(update={"high": 100., "quote_volume": None})
    changed = data[:-1] + [late]
    assert _structures(changed, DiscoverySettings())["tests"] == 3


def test_missing_sell_side_evidence_stays_missing_in_absorption_components():
    data = [b.model_copy(update={"taker_buy_volume": None}) for b in absorption_bars()]
    scores, _ = deep_scores(snapshot({"15m": data}), DiscoverySettings())
    sell = next(c for c in scores["absorption"].components if c.name == "sell_volume_decay")
    assert sell.normalized is None
    assert scores["absorption"].coverage < 1


def test_nested_missing_weights_propagate_and_contributions_sum_to_score():
    settings = DiscoverySettings()
    partial = _score([("observed", .7, .8, "known", {}), ("missing", .3, None, "unknown", {})], settings, "partial")
    nested = _score([("child", .8, partial, "nested", {}), ("known", .2, .5, "known", {})], settings, "nested")
    assert nested.coverage == pytest.approx(.76)
    assert nested.lower_bound == pytest.approx(54.8)
    assert nested.upper_bound == pytest.approx(78.8)
    assert nested.value == pytest.approx(54.8 / .76)
    assert sum(c.contribution or 0 for c in nested.components) == pytest.approx(nested.value)
    assert nested.calibration == "UNCALIBRATED"


def test_weight_overrides_change_explainable_formula_and_unknown_keys_are_rejected():
    settings = DiscoverySettings(score_weights={"example": {"A": 3}})
    result = _score([("A", 1, 1, "known", {}), ("B", 1, 0, "known", {})], settings, "example")
    assert result.value == 75
    with pytest.raises(ValueError, match="unknown example score components"):
        _score([("B", 1, 0, "known", {})], settings, "example")


def test_volume_awakening_detects_activity_before_large_price_move():
    settings = DiscoverySettings()
    quiet = bars(prices=[100] * 128, volumes=[100] * 100 + [20] * 28)
    increasing = bars(prices=[100] * 128, volumes=[100] * 100 + [20] * 25 + [80, 130, 180])
    abnormal = bars(prices=[100] * 128, volumes=[100] * 100 + [20] * 25 + [150, 300, 600])
    quiet_score = volume_awakening(snapshot({"15m": quiet}), settings)
    rising_score = volume_awakening(snapshot({"15m": increasing}), settings)
    abnormal_score = volume_awakening(snapshot({"15m": abnormal}), settings)
    assert quiet_score.value < rising_score.value <= abnormal_score.value
    assert abnormal_score.value > 65
    assert abnormal_score.coverage == .25
    assert abnormal[-1].close == abnormal[0].close


def test_deep_scores_have_documented_unknowns_and_finite_bounds():
    data = absorption_bars()
    market = {"spread_bps": metric(3, "bps", "binance/spot"),
              "quote_volume_24h": metric(10_000_000, "USDT", "binance/spot"),
              "depth_usd": metric(100_000, "USDT in 1% band", "binance/spot")}
    scores, levels = deep_scores(snapshot({"15m": data}, market), DiscoverySettings(), snapshot())
    for name in ("stored_energy", "absorption", "trigger_sensitivity", "expansion_readiness",
                 "volume_awakening", "relative_strength", "volume_structure", "vwap_structure", "liquidity_quality"):
        score = scores[name]
        assert score.calibration == "UNCALIBRATED"
        assert 0 <= score.lower_bound <= score.upper_bound <= 100
        assert 0 <= score.coverage <= 1
        if score.value is not None:
            assert sum(c.contribution or 0 for c in score.components) == pytest.approx(score.value)
    unknown = next(c for c in scores["trigger_sensitivity"].components if c.name == "observed_book_mobility")
    assert unknown.normalized is None
    assert levels["profile_approximate"] == 1
    assert levels["poc"] is not None
    assert scores["stored_energy"].value is not None
    assert scores["expansion_readiness"].coverage < 1
