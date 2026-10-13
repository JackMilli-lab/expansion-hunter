"""Synthetic mathematical/causal tests, not market-performance evidence."""

import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.models import TIMEFRAMES, Asset, AssetSnapshot, Candle
from expansion_hunter.relative_context import (
    BenchmarkReturn,
    RelativeContextSettings,
    analyze_relative_context,
)

END = datetime(2025, 1, 4, tzinfo=UTC)


def bars(prices, frame="1m", symbol="SYNUSDT", end=END, source=None):
    interval = timedelta(seconds=TIMEFRAMES[frame])
    result = []
    for index, price in enumerate(prices):
        closed = end - interval * (len(prices) - index - 1)
        result.append(Candle(open_time=closed - interval, close_time=closed, available_at=closed,
                             open=price, high=price * 1.01, low=price * 0.99, close=price,
                             volume=100, source=source or f"binance/spot/{symbol}/{frame}"))
    return result


def snapshot(prices=None, *, symbol="SYNUSDT", frame="1m", candles=None, point=END, quote="USDT"):
    return AssetSnapshot(asset=Asset(asset_id=symbol.lower(), symbol=symbol, name=symbol,
                                    exchange_symbol=symbol, quote_asset=quote),
                         as_of=point, candles=candles if candles is not None else {frame: bars(prices or [100] * 130, frame, symbol)})


def from_returns(returns, initial=100):
    prices = [initial]
    for change in returns:
        prices.append(prices[-1] * (1 + change))
    return prices


def beta_pair(beta=2.0, count=130, alpha=0.0001):
    btc_returns = [0.004 * math.sin(index * 0.61) + 0.001 * math.sin(index * 1.17) for index in range(count)]
    btc = snapshot(from_returns(btc_returns), symbol="BTCUSDT")
    asset = snapshot(from_returns([alpha + beta * change for change in btc_returns]))
    return asset, btc


def basket(*, start=END - timedelta(minutes=15), end=END, received=END,
           value=2, quality="AVAILABLE", **extra):
    return {"start_timestamp": start.isoformat(), "end_timestamp": end.isoformat(),
            "available_at": received.isoformat(), "return_pct": value,
            "quality": quality, "source": "synthetic_test_only_dated_basket", **extra}


def test_empty_context_is_explicitly_unavailable_and_json_safe():
    result = analyze_relative_context(snapshot(candles={}))
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["coverage"] == 0
    assert result["research_weight"] == 0
    assert result["btc_dependency"]["correlation"] is None
    for row in result["windows"].values():
        assert all(metric["value"] is None for metric in row.values())
    json.dumps(result, allow_nan=False)


def test_exact_relative_returns_are_percentage_point_differences():
    asset = snapshot([100 + index * 10 / 15 for index in range(16)])
    btc = snapshot([100 + index * 4 / 15 for index in range(16)], symbol="BTCUSDT")
    eth = snapshot([100 - index * 3 / 15 for index in range(16)], symbol="ETHUSDT")
    result = analyze_relative_context(asset, btc, eth)
    row = result["windows"]["15m"]
    assert row["asset_return"]["value"] == pytest.approx(10)
    assert row["vs_btc"]["value"] == pytest.approx(6)
    assert row["vs_eth"]["value"] == pytest.approx(13)
    assert row["vs_btc"]["unit"] == "percentage points"
    assert row["vs_btc"]["start_timestamp"] == (END - timedelta(minutes=15)).isoformat()
    assert row["vs_btc"]["source_timestamp"] == END.isoformat()
    assert row["vs_market"]["value"] is None
    assert row["vs_sector"]["status"] == "UNAVAILABLE"


def test_all_windows_choose_existing_frame_with_exact_endpoints():
    own = {frame: bars([100 + index * 0.03 for index in range(240)], frame) for frame in ("1m", "5m", "15m")}
    compared = {frame: bars([100 + index * 0.01 for index in range(240)], frame, "BTCUSDT") for frame in own}
    result = analyze_relative_context(snapshot(candles=own), snapshot(candles=compared, symbol="BTCUSDT"))
    assert {window: row["asset_return"]["timeframe"] for window, row in result["windows"].items()} == {
        "15m": "1m", "1h": "1m", "4h": "5m", "24h": "15m"}
    for row in result["windows"].values():
        assert row["vs_btc"]["status"] == "AVAILABLE"


def test_positive_ols_beta_and_side_betas_use_joint_return_subsets():
    own, btc = beta_pair()
    dependency = analyze_relative_context(own, btc)["btc_dependency"]
    assert dependency["status"] == "AVAILABLE"
    assert dependency["sample_count"] == 120
    assert dependency["correlation"] == pytest.approx(1)
    assert dependency["beta"] == pytest.approx(2)
    assert dependency["upside_beta"] == pytest.approx(2)
    assert dependency["downside_beta"] == pytest.approx(2)
    assert dependency["reaction_sensitivity"] == pytest.approx(2)
    assert dependency["dependency"] == "HIGH"
    assert dependency["relationship"] == "SAME_DIRECTION"
    assert dependency["window_start"] == (END - timedelta(minutes=120)).isoformat()


def test_inverse_beta_and_correlation_are_not_clipped_to_bullish_values():
    own, btc = beta_pair(beta=-1.5)
    dependency = analyze_relative_context(own, btc)["btc_dependency"]
    assert dependency["beta"] == pytest.approx(-1.5)
    assert dependency["correlation"] == pytest.approx(-1)
    assert dependency["upside_beta"] == pytest.approx(-1.5)
    assert dependency["downside_beta"] == pytest.approx(-1.5)
    assert dependency["reaction_sensitivity"] == pytest.approx(1.5)
    assert dependency["relationship"] == "INVERSE"
    assert dependency["dependency"] == "HIGH"


def test_side_beta_stays_unknown_with_no_negative_btc_returns():
    changes = [0.002 + 0.0001 * math.sin(index) for index in range(50)]
    btc = snapshot(from_returns(changes), symbol="BTCUSDT")
    own = snapshot(from_returns([2 * change for change in changes]))
    dependency = analyze_relative_context(own, btc)["btc_dependency"]
    assert dependency["beta"] == pytest.approx(2)
    assert dependency["upside_beta"] == pytest.approx(2)
    assert dependency["downside_beta"] is None
    assert dependency["downside_sample_count"] == 0
    assert dependency["status"] == "PARTIAL"
    assert dependency["coverage"] == 0.8


@pytest.mark.parametrize("constant", ["asset", "btc", "both"])
def test_constant_return_series_do_not_fabricate_zero_correlation_or_beta(constant):
    own, btc = beta_pair()
    if constant in {"asset", "both"}:
        own = snapshot([100] * 131)
    if constant in {"btc", "both"}:
        btc = snapshot([100] * 131, symbol="BTCUSDT")
    dependency = analyze_relative_context(own, btc)["btc_dependency"]
    assert dependency["correlation"] is None
    assert dependency["beta"] is None
    assert dependency["reaction_sensitivity"] is None
    assert dependency["dependency"] == "UNAVAILABLE"
    assert dependency["status"] == "INSUFFICIENT_DATA"


def test_minimum_joint_samples_is_enforced_at_exact_boundary():
    own, btc = beta_pair(count=31)
    assert analyze_relative_context(own, btc)["btc_dependency"]["beta"] is None
    own, btc = beta_pair(count=32)
    assert analyze_relative_context(own, btc)["btc_dependency"]["beta"] == pytest.approx(2)
    assert analyze_relative_context(own, btc)["btc_dependency"]["sample_count"] == 32


def test_equal_array_lengths_with_mismatched_time_endpoints_are_not_comparable():
    own, btc = beta_pair()
    btc.candles["1m"] = bars([bar.close for bar in btc.candles["1m"]], symbol="BTCUSDT", end=END - timedelta(minutes=1))
    result = analyze_relative_context(own, btc)
    assert result["windows"]["15m"]["vs_btc"]["value"] is None
    assert result["btc_dependency"]["beta"] is None
    assert "latest_completed_endpoints_differ" in " ".join(result["btc_dependency"]["issues"])


def test_future_bars_do_not_change_current_result():
    own, btc = beta_pair()
    expected = analyze_relative_context(own, btc)
    future = bars([500], end=END + timedelta(minutes=1))
    own = own.model_copy(deep=True)
    own.candles["1m"] += future
    btc = btc.model_copy(deep=True)
    btc.candles["1m"] += bars([20], symbol="BTCUSDT", end=END + timedelta(minutes=1))
    assert analyze_relative_context(own, btc) == expected


def test_future_receipt_of_final_benchmark_candle_is_not_usable():
    own, btc = beta_pair()
    btc = btc.model_copy(deep=True)
    btc.candles["1m"][-1].available_at = END + timedelta(seconds=1)
    result = analyze_relative_context(own, btc)
    assert result["windows"]["15m"]["vs_btc"]["value"] is None
    assert result["btc_dependency"]["beta"] is None


def test_benchmark_snapshot_decision_clock_is_honored():
    own, btc = beta_pair()
    btc = btc.model_copy(update={"as_of": END - timedelta(minutes=1)})
    result = analyze_relative_context(own, btc)
    assert result["windows"]["15m"]["vs_btc"]["value"] is None
    assert result["btc_dependency"]["beta"] is None


def test_delayed_intermediate_bar_breaks_causal_contiguous_history():
    own, btc = beta_pair()
    own = own.model_copy(deep=True)
    own.candles["1m"][-7].available_at = END + timedelta(days=1)
    result = analyze_relative_context(own, btc)
    assert result["windows"]["15m"]["asset_return"]["value"] is None
    assert result["btc_dependency"]["beta"] is None


@pytest.mark.parametrize("kind", ["gap", "duplicate", "mixed_sources"])
def test_invalid_causal_histories_fail_closed(kind):
    own, btc = beta_pair()
    own = own.model_copy(deep=True)
    if kind == "gap":
        del own.candles["1m"][10]
    elif kind == "duplicate":
        own.candles["1m"].append(own.candles["1m"][10])
    else:
        own.candles["1m"][10].source = "another_exchange"
    result = analyze_relative_context(own, btc)
    assert result["windows"]["15m"]["asset_return"]["value"] is None
    assert result["btc_dependency"]["beta"] is None


def test_stale_history_does_not_remain_an_available_relative_signal():
    own, btc = beta_pair()
    point = END + timedelta(minutes=2)
    result = analyze_relative_context(own.model_copy(update={"as_of": point}), btc.model_copy(update={"as_of": point}))
    assert result["windows"]["15m"]["asset_return"]["status"] == "STALE"
    assert result["windows"]["15m"]["vs_btc"]["value"] is None
    assert result["btc_dependency"]["beta"] is None


def test_stale_boundary_is_exact_and_configurable():
    own, btc = beta_pair()
    point = END + timedelta(seconds=90)
    result = analyze_relative_context(own.model_copy(update={"as_of": point}), btc.model_copy(update={"as_of": point}))
    assert result["windows"]["15m"]["vs_btc"]["status"] == "AVAILABLE"
    point += timedelta(microseconds=1)
    result = analyze_relative_context(own.model_copy(update={"as_of": point}), btc.model_copy(update={"as_of": point}))
    assert result["windows"]["15m"]["asset_return"]["status"] == "STALE"


def test_quote_currency_and_provider_disagreement_are_visible():
    own, btc = beta_pair()
    result = analyze_relative_context(own, btc.model_copy(update={"asset": btc.asset.model_copy(update={"quote_asset": "EUR"})}))
    assert result["windows"]["15m"]["vs_btc"]["value"] is None
    assert result["btc_dependency"]["beta"] is None
    btc = btc.model_copy(deep=True)
    for candle in btc.candles["1m"]:
        candle.source = "other_venue"
    assert analyze_relative_context(own, btc)["btc_dependency"]["beta"] is None


def test_wrong_instrument_cannot_masquerade_as_btc_benchmark():
    own, btc = beta_pair()
    wrong = btc.model_copy(update={"asset": btc.asset.model_copy(update={"symbol": "ETHUSDT", "exchange_symbol": "ETHUSDT"})})
    result = analyze_relative_context(own, wrong)
    assert result["btc_dependency"]["beta"] is None
    assert "not BTC" in result["btc_dependency"]["reason"]
    assert result["windows"]["15m"]["vs_btc"]["value"] is None


def test_extreme_valid_prices_fail_closed_without_nonfinite_statistics():
    own = snapshot([1e200 if index % 2 else 1e-200 for index in range(130)])
    _, btc = beta_pair()
    result = analyze_relative_context(own, btc)
    assert result["windows"]["15m"]["asset_return"]["value"] is None
    assert result["btc_dependency"]["beta"] is None
    json.dumps(result, allow_nan=False)


def test_recognized_binance_websocket_and_rest_transports_share_native_venue():
    own, btc = beta_pair()
    own = own.model_copy(deep=True)
    for candle in own.candles["1m"]:
        candle.source = "binance-public-ws"
    result = analyze_relative_context(own, btc)
    assert result["btc_dependency"]["beta"] == pytest.approx(2)
    assert "binance-public-ws" in result["btc_dependency"]["source"]


def test_point_in_time_market_and_sector_return_controls_are_used_when_supplied():
    own = snapshot([100 + index * 10 / 15 for index in range(16)])
    result = analyze_relative_context(own, market_returns={"15m": basket(value=2)},
                                      sector_returns={"15m": basket(value=4, quality="ESTIMATED", sector="known_test_sector")})
    assert result["windows"]["15m"]["vs_market"]["value"] == pytest.approx(8)
    assert result["windows"]["15m"]["vs_sector"]["value"] == pytest.approx(6)
    assert result["windows"]["15m"]["vs_sector"]["quality"] == "ESTIMATED"
    assert result["windows"]["15m"]["vs_sector"]["sector"] == "known_test_sector"
    assert result["windows"]["1h"]["vs_sector"]["value"] is None


@pytest.mark.parametrize("bad", ["future_receipt", "wrong_start", "wrong_end", "stale", "missing_value", "wrong_unit", "nan", "naive"])
def test_invalid_or_future_external_benchmark_does_not_enter_feature(bad):
    own = snapshot([100 + index for index in range(16)])
    observed = basket()
    if bad == "future_receipt":
        observed["available_at"] = (END + timedelta(seconds=1)).isoformat()
    elif bad == "wrong_start":
        observed["start_timestamp"] = (END - timedelta(minutes=16)).isoformat()
    elif bad == "wrong_end":
        observed["end_timestamp"] = (END - timedelta(minutes=1)).isoformat()
    elif bad == "stale":
        observed["quality"] = "STALE"
    elif bad == "missing_value":
        observed["return_pct"] = None
    elif bad == "wrong_unit":
        observed["unit"] = "fraction"
    elif bad == "nan":
        observed["return_pct"] = float("nan")
    else:
        observed["available_at"] = END.replace(tzinfo=None).isoformat()
    result = analyze_relative_context(own, market_returns={"15m": observed})
    assert result["windows"]["15m"]["vs_market"]["value"] is None


def test_received_timestamp_reflects_actual_latest_knowledge_not_candle_time():
    own, btc = beta_pair()
    delayed = END + timedelta(seconds=10)
    btc = btc.model_copy(deep=True)
    btc.candles["1m"][-1].available_at = delayed
    result = analyze_relative_context(own.model_copy(update={"as_of": delayed}), btc.model_copy(update={"as_of": delayed}))
    assert result["windows"]["15m"]["vs_btc"]["received_timestamp"] == delayed.isoformat()
    assert result["btc_dependency"]["received_timestamp"] == delayed.isoformat()


def test_dependence_can_fall_back_to_configured_actual_longer_frame():
    own, btc = beta_pair(count=20)
    a, b = beta_pair(count=40)
    own.candles["5m"] = bars([bar.close for bar in a.candles["1m"]], "5m")
    btc.candles["5m"] = bars([bar.close for bar in b.candles["1m"]], "5m", "BTCUSDT")
    dependency = analyze_relative_context(own, btc)["btc_dependency"]
    assert dependency["timeframe"] == "5m"
    assert dependency["sample_count"] == 40
    assert dependency["window_start"] == (END - timedelta(minutes=200)).isoformat()
    assert dependency["beta"] == pytest.approx(2)


def test_explicit_custom_minimum_window_and_threshold_validation():
    own, btc = beta_pair(count=20)
    config = RelativeContextSettings(minimum_joint_returns=16, rolling_window_returns=16, minimum_side_returns=3)
    assert analyze_relative_context(own, btc, settings=config)["btc_dependency"]["sample_count"] == 16
    with pytest.raises(ValueError, match="rolling window"):
        RelativeContextSettings(minimum_joint_returns=32, rolling_window_returns=16)
    with pytest.raises(ValueError, match="medium correlation"):
        RelativeContextSettings(medium_correlation_threshold=0.8, high_correlation_threshold=0.7)
    with pytest.raises(ValueError):
        BenchmarkReturn.model_validate(basket(received=END - timedelta(seconds=1)))


def test_full_operational_coverage_requires_real_baskets_not_default_zeros():
    own = {frame: bars([100 + index * 0.03 for index in range(240)], frame) for frame in ("1m", "5m", "15m")}
    context = analyze_relative_context(snapshot(candles=own))
    assert context["component_coverage"]["asset_return"] == 1
    assert context["component_coverage"]["vs_market"] == 0
    assert context["component_coverage"]["vs_sector"] == 0
    assert context["coverage"] == pytest.approx(1 / 6)
    assert context["status"] == "PARTIAL"
    assert "existing discovery_ranking" in context["ranking_source"]
