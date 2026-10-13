"""Synthetic, explicitly labeled fixtures test causal/risk invariants, not performance."""

from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.config import Settings
from expansion_hunter.models import (
    Asset,
    AssetSnapshot,
    Candle,
    Catalyst,
    Metric,
    Regime,
    RegimeReport,
    Stage,
    Status,
    TechnicalSnapshot,
)
from expansion_hunter.regime import classify_regime, matched_returns
from expansion_hunter.risk import PortfolioState, funding_8h_equivalent, metric_value, trade_plan
from expansion_hunter.rules import evaluate

NOW = datetime(2026, 10, 3, tzinfo=UTC)


def metric(value: float, **kwargs) -> Metric:
    return Metric(
        value=value,
        source="synthetic_test_only:spot",
        observed_at=NOW,
        available_at=NOW,
        status=Status.AVAILABLE,
        **kwargs,
    )


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def snapshot():
    candles = {}
    for frame, hours in (("4h", 4), ("1D", 24)):
        bars = []
        for i in range(230):
            end = NOW - timedelta(hours=hours * (229 - i))
            bars.append(
                Candle(
                    open_time=end - timedelta(hours=hours),
                    close_time=end,
                    available_at=end,
                    open=100,
                    high=102,
                    low=98,
                    close=100,
                    volume=1000,
                    source="synthetic_test_only:spot",
                )
            )
        candles[frame] = bars
    return AssetSnapshot(
        asset=Asset(asset_id="test", symbol="TEST", name="Synthetic test asset"),
        as_of=NOW,
        candles=candles,
        market={
            "market_cap": metric(1e9),
            "turnover_24h": metric(0.01),
            "quote_volume_24h": metric(20e6),
            "spread_bps": metric(10),
            "depth_usd": metric(100_000),
            "best_bid": metric(99.9),
            "best_ask": metric(100),
        },
        derivatives={
            "funding_rate": metric(0.0001),
            "funding_interval_hours": metric(8, unit="hours"),
            "open_interest_change_pct": metric(0.02),
            "futures_spot_volume_ratio": metric(2),
        },
    )


@pytest.fixture
def technical():
    metrics = {
        "close": 100,
        "atr14": 2,
        "extension_atr": 1,
        "range_high": 101,
        "range_low": 90,
        "range_age_days": 60,
        "range_width_pct": 20,
        "bb_width_percentile": 0.1,
        "rv_percentile": 0.1,
        "ema_cluster_pct": 2,
        "rsi14": 55,
        "resistance_tests": 4,
        "distance_resistance_pct": 1,
        "volume_contraction": 0.5,
        "relative_volume": 1,
        "return_7d": 1,
        "ema50": 95,
        "ema200": 90,
        "vwap": 95,
        "last_close_epoch": NOW.timestamp(),
    }
    patterns = {
        "compression": True,
        "higher_lows": True,
        "shallow_pullbacks": True,
        "absorption": True,
        "avwap_support": True,
        "avwap_reclaim": False,
        "demand_expansion": False,
        "weak_advance": False,
        "overextended": False,
        "accepted_breakout": False,
        "retest": False,
    }
    return {
        frame: TechnicalSnapshot(
            timeframe=frame,
            as_of=NOW,
            bars=230,
            metrics=dict(metrics),
            patterns=dict(patterns),
            levels={"supports": [95], "resistances": [120, 130], "breakout_level": 99},
        )
        for frame in ("4h", "1D")
    }


@pytest.fixture
def regime():
    return RegimeReport(
        as_of=NOW, available_at=NOW, regime=Regime.RISK_ON, confidence=0.8, risk_multiplier=0.8
    )


@pytest.fixture
def portfolio():
    return PortfolioState(as_of=NOW)


def breakout(technical):
    technical["4h"].patterns.update(accepted_breakout=True, demand_expansion=True)
    technical["4h"].metrics["relative_volume"] = 2


def test_early_scores_above_vertical(snapshot, technical, regime, settings, portfolio):
    early = evaluate(snapshot, technical, regime, settings, portfolio=portfolio)
    technical["4h"].metrics["extension_atr"] = 7
    technical["4h"].patterns["overextended"] = True
    vertical = evaluate(snapshot, technical, regime, settings, portfolio=portfolio)
    assert early.stage == Stage.ACCUMULATION
    assert early.trade.action == "ARMED"
    assert vertical.stage == Stage.EXPANSION
    assert vertical.trade.action == "AVOID"
    assert vertical.expansion_score <= 35 < early.expansion_score


def test_missing_liquidity_is_not_safe(snapshot, technical, regime, settings, portfolio):
    breakout(technical)
    del snapshot.market["depth_usd"]
    result = evaluate(snapshot, technical, regime, settings, portfolio=portfolio)
    assert result.trade.action != "ENTRY"
    assert result.trade.quantity is None
    assert any("depth_usd" in reason for reason in result.trade.reasons)


def test_stale_liquidity_blocks_entry(snapshot, technical, regime, settings, portfolio):
    breakout(technical)
    snapshot.market["spread_bps"].observed_at = NOW - timedelta(hours=2)
    result = evaluate(snapshot, technical, regime, settings, portfolio=portfolio)
    assert result.trade.action != "ENTRY"


def test_portfolio_required(snapshot, technical, regime, settings):
    breakout(technical)
    plan = trade_plan(snapshot, technical, Stage.BREAKOUT, 80, 0.9, regime, settings)
    assert plan.action != "ENTRY"
    assert any("portfolio" in reason.lower() for reason in plan.reasons)


def test_future_regime_discarded(snapshot, technical, regime, settings, portfolio):
    breakout(technical)
    regime.available_at = NOW + timedelta(seconds=1)
    result = evaluate(snapshot, technical, regime, settings, portfolio=portfolio)
    assert result.regime.confidence == 0 and result.regime.risk_multiplier == 0
    assert result.trade.action != "ENTRY"


def test_unstamped_regime_blocks_entry(snapshot, technical, settings, portfolio):
    breakout(technical)
    report = RegimeReport(regime=Regime.RISK_ON, confidence=1, risk_multiplier=1)
    assert (
        trade_plan(snapshot, technical, Stage.BREAKOUT, 80, 0.9, report, settings, portfolio).action
        != "ENTRY"
    )


@pytest.mark.parametrize(
    "field,value",
    [("kill_switch", True), ("daily_realized_loss_fraction", 0.02), ("open_risk_fraction", 0.03)],
)
def test_portfolio_controls(snapshot, technical, regime, settings, portfolio, field, value):
    breakout(technical)
    setattr(portfolio, field, value)
    assert (
        trade_plan(snapshot, technical, Stage.BREAKOUT, 80, 0.9, regime, settings, portfolio).action
        != "ENTRY"
    )


def test_no_observed_target_no_entry(snapshot, technical, regime, settings, portfolio):
    breakout(technical)
    technical["4h"].levels["resistances"] = []
    plan = trade_plan(snapshot, technical, Stage.BREAKOUT, 80, 0.9, regime, settings, portfolio)
    assert plan.action == "ARMED"
    assert plan.target is None and plan.reward_risk is None and plan.quantity is None


def test_target_costs_and_liquidity_cap(snapshot, technical, regime, settings, portfolio):
    breakout(technical)
    snapshot.market["quote_volume_24h"].value = 5e6
    plan = trade_plan(snapshot, technical, Stage.BREAKOUT, 80, 0.9, regime, settings, portfolio)
    assert plan.action == "ENTRY"
    assert plan.target == 120
    assert plan.invalidation < plan.entry
    assert plan.notional <= 5000
    assert plan.risk_fraction <= settings.risk_per_trade * regime.risk_multiplier
    assert plan.reward_risk < (plan.target - plan.entry) / (plan.entry - plan.invalidation)


def test_nearest_resistance_cannot_be_skipped(snapshot, technical, regime, settings, portfolio):
    breakout(technical)
    technical["4h"].levels["resistances"] = [101, 200]
    plan = trade_plan(snapshot, technical, Stage.BREAKOUT, 80, 0.9, regime, settings, portfolio)
    assert plan.target == 101
    assert plan.action != "ENTRY"


def test_wick_is_not_accepted_breakout(snapshot, technical, regime, settings, portfolio):
    breakout(technical)
    penultimate = snapshot.candles["4h"][-2]
    snapshot.candles["4h"][-2] = penultimate.model_copy(update={"close": 98.5, "open": 98.5})
    result = evaluate(snapshot, technical, regime, settings, portfolio=portfolio)
    assert result.stage != Stage.BREAKOUT
    # Explicit public risk invocation still verifies the acceptance evidence.
    assert (
        trade_plan(snapshot, technical, Stage.BREAKOUT, 80, 0.9, regime, settings, portfolio).action
        != "ENTRY"
    )


def test_futures_bars_cannot_confirm_spot_demand(snapshot, technical, regime, settings, portfolio):
    breakout(technical)
    snapshot.candles["4h"] = [
        b.model_copy(update={"source": "synthetic_test_only:futures"}) for b in snapshot.candles["4h"]
    ]
    assert (
        trade_plan(snapshot, technical, Stage.BREAKOUT, 80, 0.9, regime, settings, portfolio).action
        != "ENTRY"
    )


@pytest.mark.parametrize("defect", ["future_available", "gap", "duplicate", "stale", "short", "mixed_source"])
def test_primary_history_fail_closed(snapshot, technical, regime, settings, defect):
    if defect == "future_available":
        snapshot.candles["4h"][-1].available_at = NOW + timedelta(seconds=1)
    elif defect == "gap":
        del snapshot.candles["4h"][-3]
    elif defect == "duplicate":
        snapshot.candles["4h"].append(snapshot.candles["4h"][-1])
    elif defect == "stale":
        snapshot.as_of += timedelta(days=3)
    elif defect == "mixed_source":
        snapshot.candles["4h"][0].source = "another_venue:spot"
    else:
        snapshot.candles["1D"] = snapshot.candles["1D"][-20:]
    result = evaluate(snapshot, technical, regime, settings)
    assert result.stage == Stage.INSUFFICIENT_DATA
    assert result.trade.action != "ENTRY"
    assert result.confidence <= 0.2


def catalyst(category="adoption", **updates):
    values = dict(
        category=category,
        description="Synthetic quantitative test evidence",
        source_url="https://example.test/data",
        observed_at=NOW - timedelta(days=1),
        available_at=NOW - timedelta(days=1),
        verified=True,
        quantitative=True,
        strength=0.8,
    )
    values.update(updates)
    return Catalyst(**values)


@pytest.mark.parametrize(
    "item",
    [
        catalyst("social"),
        catalyst("marketing"),
        catalyst("partnership"),
        catalyst(quantitative=False),
        catalyst(verified=False),
        catalyst(available_at=NOW + timedelta(days=1)),
        catalyst(observed_at=NOW - timedelta(days=181)),
    ],
)
def test_narrative_rejections(snapshot, technical, regime, settings, item):
    snapshot.catalysts = [item]
    result = evaluate(snapshot, technical, regime, settings)
    narrative = [
        e
        for e in result.evidence
        if e.rule in {"quantitative_adoption", "supply_scarcity", "rwa_adoption", "institutional_inflows"}
    ]
    assert sum(e.points for e in narrative) == 0


def test_future_metrics_never_earn_points(snapshot, technical, regime, settings):
    snapshot.market["market_cap"].available_at = NOW + timedelta(seconds=1)
    result = evaluate(snapshot, technical, regime, settings)
    item = next(e for e in result.evidence if e.rule == "small_mid_cap")
    assert item.passed is None and item.points == 0


def test_verified_adoption_and_hybrid_unique_weights(snapshot, technical, regime, settings):
    snapshot.catalysts = [catalyst("adoption"), catalyst("scarcity"), catalyst("rwa"), catalyst("inflows")]
    result = evaluate(snapshot, technical, regime, settings, benchmark=snapshot)
    assert sum(e.weight for e in result.evidence) == 100
    assert sum(e.points for e in result.evidence if e.rule == "independent_hybrid") <= 5
    assert len({e.rule for e in result.evidence}) == len(result.evidence)
    assert result.archetypes["TAO"] > 0 and result.archetypes["ONDO"] > 0


def test_social_record_does_not_increase_adoption_coverage(snapshot, technical, regime, settings):
    before = evaluate(snapshot, technical, regime, settings)
    snapshot.catalysts = [catalyst("social")]
    after = evaluate(snapshot, technical, regime, settings)
    assert before.confidence == after.confidence


def test_funding_freshness_uses_observed_cadence(snapshot, settings):
    settled = NOW - timedelta(hours=7)
    snapshot.derivatives["funding_rate"].observed_at = settled
    assert metric_value(snapshot, "funding_rate", settings, True) is None
    snapshot.derivatives["funding_interval_hours"] = Metric(
        value=8,
        observed_at=settled,
        available_at=NOW,
        source="synthetic_test_only",
        status=Status.AVAILABLE,
        unit="hours",
    )
    assert metric_value(snapshot, "funding_rate", settings, True) == 0.0001
    snapshot.derivatives["funding_interval_hours"].value = 4
    assert metric_value(snapshot, "funding_rate", settings, True) is None


def test_book_age_stricter_than_general_metrics(snapshot, technical, regime, settings, portfolio):
    breakout(technical)
    snapshot.market["spread_bps"].observed_at = NOW - timedelta(seconds=61)
    assert (
        trade_plan(snapshot, technical, Stage.BREAKOUT, 80, 0.9, regime, settings, portfolio).action
        != "ENTRY"
    )


def test_intrabar_vertical_price_cannot_use_old_entry(snapshot, technical, regime, settings, portfolio):
    breakout(technical)
    snapshot.market["best_bid"].value = 149.9
    snapshot.market["best_ask"].value = 150
    plan = trade_plan(snapshot, technical, Stage.BREAKOUT, 80, 0.9, regime, settings, portfolio)
    assert plan.action == "AVOID"
    assert plan.entry is None and plan.quantity is None


def test_entry_uses_fresh_ask(snapshot, technical, regime, settings, portfolio):
    breakout(technical)
    snapshot.market["best_bid"].value = 100.4
    snapshot.market["best_ask"].value = 100.5
    plan = trade_plan(snapshot, technical, Stage.BREAKOUT, 80, 0.9, regime, settings, portfolio)
    assert plan.action == "ENTRY"
    assert plan.entry == 100.5


def test_matched_return_rejects_unequal_endpoints(snapshot):
    benchmark = snapshot.model_copy(deep=True)
    benchmark.candles["1D"] = [
        b.model_copy(
            update={
                "open_time": b.open_time + timedelta(minutes=1),
                "close_time": b.close_time + timedelta(minutes=1),
                "available_at": b.available_at + timedelta(minutes=1),
            }
        )
        for b in benchmark.candles["1D"]
    ]
    assert matched_returns(snapshot, benchmark) is None


def test_regime_unavailable_btc_disables_risk(settings):
    result = classify_regime(None, None, {}, {}, {}, None, NOW, settings)
    assert result.risk_multiplier == 0
    assert result.regime == Regime.NEUTRAL
    assert result.shakeout_score == 0
    assert result.confidence <= 0.25


def test_regime_missing_derivatives_reduce_coverage(snapshot, technical, settings):
    full = classify_regime(snapshot, snapshot, technical, technical, {}, 0.7, NOW, settings)
    snapshot.derivatives = {}
    missing = classify_regime(snapshot, snapshot, technical, technical, {}, 0.7, NOW, settings)
    assert missing.confidence < full.confidence
    assert "btc_oi_change_24h" in missing.missing
    assert missing.shakeout_score <= full.shakeout_score


def test_shakeout_requires_confluence(snapshot, technical, settings):
    snapshot.derivatives["open_interest_change_pct"].value = 0.15
    snapshot.derivatives["funding_rate"].value = 0.001
    technical["1D"].metrics["return_7d"] = 1  # 1%, stagnant under 2% threshold
    technical["4h"].metrics["vwap"] = 105
    report = classify_regime(snapshot, snapshot, technical, technical, {}, 0.5, NOW, settings)
    assert report.regime == Regime.SHAKEOUT
    assert report.risk_multiplier == 0
    snapshot.derivatives = {}
    report = classify_regime(snapshot, snapshot, technical, technical, {}, 0.5, NOW, settings)
    assert report.regime != Regime.SHAKEOUT


def test_shakeout_price_window_matches_oi_duration(snapshot, technical, settings):
    snapshot.derivatives["open_interest_change_pct"].value = 0.15
    snapshot.derivatives["funding_rate"].value = 0.001
    technical["1D"].metrics["return_7d"] = 1
    technical["4h"].metrics.update(vwap=105, volume_contraction=1)
    day_start = snapshot.candles["4h"][-7]
    snapshot.candles["4h"][-7] = day_start.model_copy(update={"open": 80, "high": 82, "low": 78, "close": 80})
    report = classify_regime(snapshot, snapshot, technical, technical, {}, 0.5, NOW, settings)
    assert report.metrics["btc_return_24h"] == pytest.approx(0.25)
    assert report.regime != Regime.SHAKEOUT


def test_funding_cadence_normalization(snapshot, technical, regime, settings):
    snapshot.derivatives["funding_rate"].value = 0.0001
    snapshot.derivatives["funding_interval_hours"].value = 1
    assert funding_8h_equivalent(snapshot, settings) == pytest.approx(0.0008)
    snapshot.derivatives["open_interest_change_pct"].value = 0.15
    hourly = evaluate(snapshot, technical, regime, settings)
    snapshot.derivatives["funding_interval_hours"].value = 8
    eight_hourly = evaluate(snapshot, technical, regime, settings)
    assert eight_hourly.expansion_score == hourly.expansion_score + 8
    del snapshot.derivatives["funding_interval_hours"]
    unknown = evaluate(snapshot, technical, regime, settings)
    assert funding_8h_equivalent(snapshot, settings) is None
    assert unknown.expansion_score == eight_hourly.expansion_score
    assert "funding_interval_hours_for_normalization" in unknown.missing
    assert unknown.confidence < eight_hourly.confidence


def test_ignition_requires_a_base_near_resistance(snapshot, technical, regime, settings, portfolio):
    t = technical["4h"]
    t.patterns.update(demand_expansion=True, absorption=False, compression=False)
    t.metrics["relative_volume"] = 2
    candidate = evaluate(snapshot, technical, regime, settings, portfolio=portfolio)
    assert candidate.stage == Stage.IGNITION
    t.metrics["range_age_days"] = None
    unrelated = evaluate(snapshot, technical, regime, settings, portfolio=portfolio)
    assert unrelated.stage == Stage.NONE
    assert next(e for e in unrelated.evidence if e.rule == "spot_ignition").points == 0
    assert (
        trade_plan(snapshot, technical, Stage.IGNITION, 90, 0.9, regime, settings, portfolio).action
        != "ENTRY"
    )
    t.metrics.update(range_age_days=60, distance_resistance_pct=20)
    assert evaluate(snapshot, technical, regime, settings).stage == Stage.NONE


def test_regime_percentage_point_boundary(snapshot, technical, settings):
    technical["4h"].metrics.update(relative_volume=2, volume_contraction=1)
    technical["1D"].metrics["return_7d"] = 0.1  # 0.1%, not a BTC expansion
    report = classify_regime(snapshot, snapshot, technical, technical, {}, 0.4, NOW, settings)
    assert report.regime != Regime.BTC_EXPANSION
    technical["1D"].metrics["return_7d"] = 8
    report = classify_regime(snapshot, snapshot, technical, technical, {}, 0.4, NOW, settings)
    assert report.regime == Regime.BTC_EXPANSION
    technical["1D"].metrics.update(ema200=110, return_7d=-0.1)
    report = classify_regime(snapshot, snapshot, technical, technical, {}, 0.4, NOW, settings)
    assert report.regime != Regime.RISK_OFF
    technical["1D"].metrics["return_7d"] = -5
    report = classify_regime(snapshot, snapshot, technical, technical, {}, 0.4, NOW, settings)
    assert report.regime == Regime.RISK_OFF


def test_alt_rotation_and_expansion(snapshot, technical, settings):
    eth = snapshot.model_copy(deep=True)
    eth.asset = Asset(asset_id="ethereum", symbol="ETH", name="Synthetic ETH")
    eth.candles["1D"][-1] = eth.candles["1D"][-1].model_copy(update={"close": 110, "high": 112})
    eth_technical = {frame: value.model_copy(deep=True) for frame, value in technical.items()}
    eth_technical["1D"].metrics["close"] = 110
    globals_ = {"btc_dominance_change_7d": metric(-0.2), "total2_return_7d": metric(0.04)}
    rotation = classify_regime(snapshot, eth, technical, eth_technical, globals_, 0.6, NOW, settings)
    assert rotation.regime == Regime.ALT_ROTATION
    globals_.update(btc_dominance_change_7d=metric(-1), total3_return_7d=metric(0.08))
    expansion = classify_regime(snapshot, eth, technical, eth_technical, globals_, 0.7, NOW, settings)
    assert expansion.regime == Regime.ALT_EXPANSION


def test_neutral_and_risk_on(snapshot, technical, settings):
    risk_on = classify_regime(snapshot, snapshot, technical, technical, {}, 0.6, NOW, settings)
    assert risk_on.regime == Regime.RISK_ON
    technical["1D"].metrics["ema50"] = 110
    neutral = classify_regime(snapshot, snapshot, technical, technical, {}, 0.6, NOW, settings)
    assert neutral.regime == Regime.NEUTRAL
