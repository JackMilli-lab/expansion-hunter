"""Synthetic deterministic software tests; not evidence of a market-cycle edge."""

import math
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from expansion_hunter.cycle import (
    BreadthReport,
    CyclePhase,
    CycleSettings,
    MarketCycleReport,
    classify_market_cycle,
    market_congruence,
    structure_events,
)
from expansion_hunter.models import Asset, AssetSnapshot, Candle, Metric, Regime, RegimeReport, Status

AS_OF = datetime(2026, 10, 5, 0, 1, tzinfo=UTC)


def sample(prices, *, asset_id="benchmark", as_of=AS_OF, volumes=None, quote="USDT"):
    end = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    bars = []
    volumes = volumes if volumes is not None else [100.0] * len(prices)
    for index, (price, volume) in enumerate(zip(prices, volumes, strict=True)):
        close_time = end - timedelta(days=len(prices) - index - 1)
        bars.append(
            Candle(
                open_time=close_time - timedelta(days=1),
                close_time=close_time,
                available_at=close_time + timedelta(seconds=1),
                open=price,
                high=price * 1.01,
                low=price * 0.99,
                close=price,
                volume=volume,
                source="synthetic:spot",
            )
        )
    return AssetSnapshot(
        asset=Asset(asset_id=asset_id, symbol=asset_id.upper(), name=asset_id, quote_asset=quote),
        as_of=as_of,
        candles={"1D": bars},
    )


def trend(rate, asset_id="benchmark"):
    return sample([100 * math.exp(rate * index) for index in range(240)], asset_id=asset_id)


def metric(value, unit="fraction", *, observed_at=AS_OF, available_at=AS_OF):
    return Metric(
        value=value,
        observed_at=observed_at,
        available_at=available_at,
        source="synthetic:global",
        unit=unit,
        status=Status.AVAILABLE,
    )


def report(btc=None, eth=None, candidates=None, **kwargs):
    return classify_market_cycle(btc, eth, candidates or [], {}, AS_OF, **kwargs)


def test_sma50_sma200_and_slopes_are_actual_daily_averages():
    result = report(sample(list(range(1, 206))))
    assert result.metrics["btc_sma50"].value == pytest.approx(sum(range(156, 206)) / 50)
    assert result.metrics["btc_sma200"].value == pytest.approx(sum(range(6, 206)) / 200)
    assert result.metrics["btc_sma50_slope"].value == pytest.approx(180.5 / 175.5 - 1)
    assert result.metrics["btc_sma200_slope"].value == pytest.approx(105.5 / 100.5 - 1)
    assert result.metrics["btc_sma50"].observed_at == AS_OF.replace(minute=0)
    assert result.calibration == "UNVALIDATED"


def test_sma_does_not_appear_before_its_real_warmup():
    result = report(sample(list(range(1, 50))))
    assert result.metrics["btc_sma50"].value is None
    assert result.metrics["btc_sma200"].value is None
    assert result.phase == CyclePhase.NEUTRAL_UNCERTAIN


def test_observed_high_is_not_misrepresented_as_all_time_high():
    btc = sample([200.0] * 220 + [100.0] * 20)
    result = report(btc)
    assert result.metrics["btc_observed_high"].value == 202
    assert result.metrics["btc_drawdown_observed_high"].value == pytest.approx(100 / 202 - 1)
    assert result.metrics["btc_drawdown_ath"].value is None
    assert "btc_drawdown_ath" in result.missing


def test_reliable_dated_same_currency_ath_is_used_separately():
    btc = sample(list(range(1, 241)))
    btc.market["all_time_high"] = metric(400, "USDT")
    result = report(btc)
    assert result.metrics["btc_drawdown_ath"].value == pytest.approx(240 / 400 - 1)
    assert result.metrics["btc_drawdown_observed_high"].value != result.metrics["btc_drawdown_ath"].value


@pytest.mark.parametrize("kind", ["future", "unreliable", "currency"])
def test_future_or_unreliable_or_different_currency_ath_is_not_used(kind):
    btc = sample(list(range(1, 241)))
    value = metric(400, "USDT")
    if kind == "future":
        value = metric(
            400, "USDT", observed_at=AS_OF + timedelta(days=1), available_at=AS_OF + timedelta(days=1)
        )
    elif kind == "unreliable":
        value.reliable = False
    else:
        value.unit = "USD"
    btc.market["all_time_high"] = value
    assert report(btc).metrics["btc_drawdown_ath"].value is None


def test_no_data_means_unknown_not_a_fabricated_accumulation_state():
    result = report()
    assert result.phase == CyclePhase.NEUTRAL_UNCERTAIN
    assert result.evidence_score == 0
    assert result.metrics["btc_close"].value is None
    assert result.metrics["btc_funding_rate"].value is None
    assert result.breadth.above_sma50 is None


def test_future_bar_values_cannot_change_the_current_cycle():
    btc = trend(0.004)
    baseline = report(btc)
    future = sample([9999.0], as_of=AS_OF + timedelta(days=1)).candles["1D"][0]
    btc.candles["1D"].append(future)
    assert report(btc).model_dump() == baseline.model_dump()


def test_future_snapshot_and_future_global_values_are_not_consumed():
    btc = trend(0.004)
    btc.as_of = AS_OF + timedelta(seconds=1)
    future = metric(1, observed_at=AS_OF + timedelta(seconds=1), available_at=AS_OF + timedelta(seconds=1))
    result = classify_market_cycle(btc, None, [], {"total3_return_7d": future}, AS_OF)
    assert result.phase == CyclePhase.NEUTRAL_UNCERTAIN
    assert result.metrics["total3_return_7d"].value is None


@pytest.mark.parametrize("bad", ["gap", "duplicate", "mixed_source", "late", "stale"])
def test_bad_daily_history_never_becomes_complete_trend_evidence(bad):
    btc = trend(0.004)
    if bad == "gap":
        del btc.candles["1D"][100]
    elif bad == "duplicate":
        btc.candles["1D"].insert(100, btc.candles["1D"][100])
    elif bad == "mixed_source":
        btc.candles["1D"][100].source = "synthetic:second"
    elif bad == "late":
        btc.candles["1D"][100].available_at = AS_OF + timedelta(days=1)
    else:
        btc = sample([100.0] * 240, as_of=AS_OF - timedelta(days=2))
    result = report(btc)
    assert result.phase == CyclePhase.NEUTRAL_UNCERTAIN
    assert result.metrics["btc_sma200"].value is None


def test_eth_btc_requires_matched_endpoints_and_same_quote_currency():
    btc, eth = trend(0.001, "btc"), trend(0.002, "eth")
    result = report(btc, eth)
    assert result.metrics["eth_btc_return_7d"].value == pytest.approx(math.exp(0.001 * 7) - 1)
    eth.asset.quote_asset = "USD"
    assert report(btc, eth).metrics["eth_btc_return_7d"].value is None
    eth = sample([100.0] * 240, as_of=AS_OF - timedelta(days=1), asset_id="eth")
    assert report(btc, eth).metrics["eth_btc_return_7d"].value is None


def test_breadth_is_equal_weight_with_dated_endpoint_and_duplicate_rejection():
    settings = CycleSettings(minimum_breadth_assets=2)
    candidates = [trend(0.004, "a"), trend(0.005, "b"), trend(-0.006, "c")]
    result = report(trend(0.001), candidates=candidates, settings=settings)
    assert result.breadth.above_sma50 == pytest.approx(2 / 3)
    assert result.breadth.evaluated_assets == 3
    duplicate = report(trend(0.001), candidates=candidates + [candidates[0]], settings=settings)
    assert duplicate.breadth.evaluated_assets == 2
    assert duplicate.breadth.above_sma50 == 0.5
    assert "duplicate" in duplicate.breadth.excluded["a"]


def test_insufficient_breadth_and_unaligned_candidates_are_visible():
    old = sample([100.0] * 240, as_of=AS_OF - timedelta(days=1), asset_id="old")
    result = report(trend(0.004), candidates=[old, trend(0.003, "fresh")])
    assert result.breadth.above_sma50 is None
    assert "endpoint" in result.breadth.excluded["old"]
    assert result.breadth.evaluated_assets == 1
    assert result.breadth.reasons


def test_alt_breadth_excludes_benchmark_roles_and_measures_new_highs_lows():
    btc, eth = trend(0.001, "btc"), trend(0.002, "eth")
    candidates = [btc, eth, trend(0.004, "alt-up"), trend(-0.006, "alt-down")]
    result = report(btc, eth, candidates=candidates, settings=CycleSettings(minimum_breadth_assets=2))
    assert result.breadth.evaluated_assets == 2
    assert result.breadth.new_highs_30d == 0.5
    assert result.breadth.new_lows_30d == 0.5
    assert "benchmark" in result.breadth.excluded["btc"]
    assert "benchmark" in result.breadth.excluded["eth"]


def structural_sample():
    highs = [10, 11, 12, 11, 10, 11, 13, 12, 11, 10, 9, 10]
    value = sample([high - 0.4 for high in highs])
    for bar, high in zip(value.candles["1D"], highs, strict=True):
        bar.high, bar.low = high, high - 1
    return value


def test_bos_and_choch_pivots_have_separate_causal_discovery_timestamps():
    btc = structural_sample()
    events = structure_events(btc, AS_OF, CycleSettings(pivot_left=1, pivot_right=1))
    assert events[0].kind == "BOS_UP"
    assert events[0].level == 12
    assert events[0].level_timestamp == btc.candles["1D"][2].close_time
    assert events[0].level_discovery_timestamp == btc.candles["1D"][3].available_at
    assert events[0].event_timestamp == btc.candles["1D"][6].close_time
    assert any(event.kind == "CHOCH_DOWN" for event in events)
    assert all(event.level_discovery_timestamp <= event.discovery_timestamp <= AS_OF for event in events)


def test_pivot_cannot_be_used_before_confirming_candle_exists():
    btc = structural_sample()
    cutoff = btc.candles["1D"][2].available_at
    btc.as_of = cutoff
    assert structure_events(btc, cutoff, CycleSettings(pivot_left=1, pivot_right=1)) == []
    cutoff = btc.candles["1D"][5].available_at
    btc.as_of = cutoff
    assert structure_events(btc, cutoff, CycleSettings(pivot_left=1, pivot_right=1)) == []


def test_cycle_transitions_are_reproducible_and_serializable():
    bull = report(trend(0.004))
    assert bull.phase == CyclePhase.BULL_EXPANSION
    assert MarketCycleReport.model_validate_json(bull.model_dump_json()) == bull
    earlier_time = AS_OF - timedelta(days=1)
    earlier_btc = sample([100 * math.exp(0.004 * index) for index in range(240)], as_of=earlier_time)
    earlier = classify_market_cycle(earlier_btc, None, [], {}, earlier_time)
    bear = report(trend(-0.006), previous=earlier)
    assert bear.phase == CyclePhase.BEAR_EXPANSION
    assert bear.changed is True
    assert bear.previous_phase == CyclePhase.BULL_EXPANSION


def test_future_previous_cycle_or_regime_cannot_leak_into_new_phase():
    previous = report(trend(0.004))
    with pytest.raises(ValueError, match="previous cycle"):
        report(trend(0.004), previous=previous)
    regime = RegimeReport(as_of=AS_OF + timedelta(seconds=1), available_at=AS_OF + timedelta(seconds=1))
    with pytest.raises(ValueError, match="regime context"):
        report(trend(0.004), regime=regime)


def test_congruence_shows_conflict_and_does_not_modify_coin_or_gate_signals():
    candidates = [trend(-0.006, f"alt-{index}") for index in range(10)]
    globals_ = {
        "total3_return_7d": metric(-0.05),
        "btc_dominance_change_7d": metric(0.5, "percentage_points"),
    }
    regime = RegimeReport(as_of=AS_OF, available_at=AS_OF, regime=Regime.RISK_OFF)
    cycle = classify_market_cycle(
        trend(-0.006), trend(-0.005, "eth"), candidates, globals_, AS_OF, regime=regime
    )
    coin = trend(0.004, "unknown-future-asset")
    original = coin.model_dump()
    result = market_congruence(coin, cycle, AS_OF)
    assert result.market_conflict
    assert result.score.value is not None and result.score.value < 40
    assert result.score.calibration == "UNCALIBRATED"
    assert result.interpretation == "CONTEXT_ONLY"
    assert coin.model_dump() == original
    assert "not automatically rejected" in " ".join(result.reasons)
    assert sum(
        component.contribution for component in result.score.components if component.contribution is not None
    ) == pytest.approx(result.score.value)


def test_missing_congruence_is_unknown_with_explicit_missing_components():
    result = market_congruence(trend(0.004), report(), AS_OF)
    assert result.score.value is None
    assert result.score.coverage == 0
    assert "derivatives_context" in result.missing


def test_future_cycle_is_rejected_and_stale_context_cannot_score():
    cycle = report(trend(0.004))
    future = cycle.model_copy(
        update={"as_of": AS_OF + timedelta(seconds=1), "available_at": AS_OF + timedelta(seconds=1)}
    )
    with pytest.raises(ValueError, match="future cycle"):
        market_congruence(trend(0.004), future, AS_OF)
    earlier_time = AS_OF - timedelta(days=2)
    earlier_btc = sample([100 * math.exp(0.004 * index) for index in range(240)], as_of=earlier_time)
    old = classify_market_cycle(earlier_btc, None, [], {}, earlier_time)
    result = market_congruence(trend(0.004), old, AS_OF)
    assert result.score.value is None
    assert result.score.coverage == 0
    assert "stale" in " ".join(result.reasons)


def test_negative_or_future_derivatives_are_not_invented_as_neutral():
    btc = trend(0.004)
    btc.derivatives["open_interest"] = metric(
        123, observed_at=AS_OF + timedelta(seconds=1), available_at=AS_OF + timedelta(seconds=1)
    )
    result = report(btc)
    assert result.metrics["btc_open_interest"].value is None
    assert result.metrics["btc_funding_rate"].value is None
    assert "btc_open_interest" in result.missing


def test_stale_macro_market_observation_preserves_age_but_does_not_supply_evidence():
    observed = AS_OF - timedelta(days=2)
    stale = metric(0.10, observed_at=observed, available_at=observed)
    result = classify_market_cycle(trend(0.004), None, [], {"total3_return_7d": stale}, AS_OF)
    value = result.metrics["total3_return_7d"]
    assert value.status == Status.STALE
    assert value.value == 0.10
    assert value.observed_at == observed
    assert "total3_return_7d" in result.missing


def test_negative_open_interest_is_invalid_but_negative_funding_is_real_evidence():
    btc = trend(0.004)
    btc.derivatives["open_interest"] = metric(-1, "contracts")
    btc.derivatives["funding_rate"] = metric(-0.001)
    result = report(btc)
    assert result.metrics["btc_open_interest"].value is None
    assert result.metrics["btc_funding_rate"].value == -0.001


def test_mutated_previous_report_cannot_backdate_future_inputs():
    previous = report(trend(0.004))
    forged = previous.model_copy(
        update={"as_of": AS_OF - timedelta(days=1), "available_at": AS_OF - timedelta(days=1)}
    )
    with pytest.raises(ValidationError, match="future input"):
        report(trend(-0.006), previous=forged)


def test_report_validator_rejects_future_input_and_phase_thresholds_are_explicit():
    with pytest.raises(ValidationError, match="future input"):
        MarketCycleReport(
            as_of=AS_OF,
            available_at=AS_OF,
            breadth=BreadthReport(supplied_assets=0, evaluated_assets=0),
            metrics={
                "future": metric(
                    10, observed_at=AS_OF + timedelta(seconds=1), available_at=AS_OF + timedelta(seconds=1)
                )
            },
        )
    with pytest.raises(ValidationError, match="late-bull"):
        CycleSettings(late_bull_distance_sma50=0.5)
    assert report(trend(0.004)).settings["expansion_return_30d"] == 0.12
