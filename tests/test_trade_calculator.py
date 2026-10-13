"""Deterministic arithmetic checks; no execution or hypothetical win-rate tests."""

import math

import pytest
from pydantic import TypeAdapter, ValidationError

from expansion_hunter.trade_calculator import (
    CalculatorRequest,
    MaxRiskInput,
    PortfolioInput,
    PortfolioPosition,
    SuppliedCorrelation,
    TargetProfitInput,
    TradeInput,
    calculate,
    calculate_max_risk,
    calculate_portfolio,
    calculate_target_profit,
    calculate_trade,
)


def trade(**overrides):
    values = {"entry": 100, "target": 110, "stop": 95, "margin": 1000, "leverage": 2, "entry_fee_rate": 0.001, "exit_fee_rate": 0.002, "funding_cost": 3}
    values.update(overrides)
    return TradeInput(**values)


def test_long_costs_use_entry_and_exit_notional_not_flat_roundtrip():
    result = calculate_trade(trade())
    assert result.position_size == 20
    assert result.notional == 2000
    assert result.gross_pnl == 200
    assert result.entry_fee == 2
    assert result.exit_fee == 4.4
    assert result.estimated_fees == pytest.approx(6.4)
    assert result.net_pnl == pytest.approx(190.6)
    assert result.loss_at_stop == pytest.approx(108.8)
    assert result.reward_risk == pytest.approx(190.6 / 108.8)
    assert result.roi_on_margin_percent == pytest.approx(19.06)


def test_short_sign_and_costs():
    result = calculate_trade(trade(side="SHORT", target=90, stop=105))
    assert result.gross_pnl == 200
    assert result.price_move_percent == pytest.approx(-10)
    assert result.estimated_fees == pytest.approx(5.6)
    assert result.net_pnl == pytest.approx(191.4)
    assert result.loss_at_stop == pytest.approx(109.2)


@pytest.mark.parametrize("side,target,stop", [("LONG", 110, 95), ("SHORT", 90, 105)])
def test_break_even_solves_net_pnl_equation(side, target, stop):
    request = trade(side=side, target=target, stop=stop)
    result = calculate_trade(request)
    quantity = result.position_size
    close = result.break_even_price
    direction = 1 if side == "LONG" else -1
    net = quantity * direction * (close - request.entry) - quantity * request.entry * request.entry_fee_rate - quantity * close * request.exit_fee_rate - request.funding_cost
    assert net == pytest.approx(0, abs=1e-10)


def test_leverage_changes_margin_not_pnl_for_fixed_notional():
    unleveraged = calculate_trade(trade(margin=2000, leverage=1))
    leveraged = calculate_trade(trade(margin=200, leverage=10))
    assert unleveraged.net_pnl == leveraged.net_pnl
    assert unleveraged.notional == leveraged.notional
    assert leveraged.required_margin == 200


def test_missing_funding_is_unknown_net_not_zero():
    result = calculate_trade(trade(funding_cost=None))
    assert result.gross_pnl == 200
    assert result.net_pnl is None
    assert result.loss_at_stop is None
    assert result.break_even_price is None
    assert result.reward_risk is None
    assert result.net_status == "UNAVAILABLE"


def test_zero_funding_is_explicit_user_assumption():
    result = calculate_trade(trade(funding_cost=0))
    assert result.estimated_funding == 0
    assert result.net_status == "USER_ASSUMPTION"


def test_negative_funding_is_a_signed_credit():
    cost = calculate_trade(trade(funding_cost=3))
    credit = calculate_trade(trade(funding_cost=-3, funding_source="USER_INPUT"))
    assert credit.net_pnl == pytest.approx(cost.net_pnl + 6)
    assert credit.loss_at_stop == pytest.approx(cost.loss_at_stop - 6)
    assert credit.net_status == "USER_INPUT"


@pytest.mark.parametrize("side,expected", [("LONG", 80 / 0.995), ("SHORT", 120 / 1.005)])
def test_isolated_liquidation_explicitly_estimated(side, expected):
    request = trade(side=side, target=110 if side == "LONG" else 90, stop=95 if side == "LONG" else 105, leverage=5, maintenance_margin_rate=0.005)
    result = calculate_trade(request)
    assert result.estimated_liquidation_price == pytest.approx(expected)
    assert result.liquidation_status == "ESTIMATED_LIQUIDATION"


def test_cross_liquidation_requires_missing_account_state():
    result = calculate_trade(trade(margin_mode="CROSS", maintenance_margin_rate=0.005))
    assert result.estimated_liquidation_price is None
    assert result.liquidation_status == "UNAVAILABLE_CROSS_ACCOUNT_STATE_REQUIRED"


def test_missing_maintenance_rate_does_not_invent_exchange_liquidation():
    result = calculate_trade(trade())
    assert result.estimated_liquidation_price is None
    assert result.liquidation_status == "UNAVAILABLE_MAINTENANCE_RATE_REQUIRED"


def test_stop_after_estimated_liquidation_is_flagged():
    result = calculate_trade(trade(leverage=20, stop=90, maintenance_margin_rate=0.005))
    assert any("before the specified stop" in warning for warning in result.warnings)


@pytest.mark.parametrize("overrides", [
    {"entry": math.inf}, {"entry": math.nan}, {"margin": math.inf},
    {"margin": 0}, {"leverage": 0}, {"target": 99}, {"stop": 101},
    {"entry_fee_rate": -0.01}, {"exit_fee_rate": 1}, {"funding_cost": math.inf},
    {"leverage": 10, "maintenance_margin_rate": 0.2},
    {"side": "SHORT", "target": 110, "stop": 105},
])
def test_invalid_values_rejected(overrides):
    with pytest.raises(ValidationError):
        trade(**overrides)


def test_max_risk_budget_includes_both_fees_at_stop_and_fixed_funding():
    request = MaxRiskInput(entry=100, stop=95, target=110, account_size=10000, max_risk_amount=100, leverage=3, funding_cost=5, entry_fee_rate=0.001, exit_fee_rate=0.002)
    result = calculate_max_risk(request)
    assert result.position_size == pytest.approx(95 / 5.29)
    assert result.actual_risk_amount == pytest.approx(100)
    assert result.actual_risk_percent == pytest.approx(1)
    assert result.required_margin == pytest.approx(result.notional / 3)


def test_max_risk_percent_and_short_sizing():
    request = MaxRiskInput(side="SHORT", entry=100, stop=105, account_size=5000, max_risk_percent=2, entry_fee_rate=0, exit_fee_rate=0)
    result = calculate_max_risk(request)
    assert result.position_size == 20
    assert result.risk_budget == 100
    assert result.actual_risk_percent == 2
    assert result.profit_at_target is None


def test_max_risk_capped_by_available_account_after_upfront_cost():
    request = MaxRiskInput(entry=100, stop=99.99, account_size=1000, max_risk_percent=2, entry_fee_rate=0.01, exit_fee_rate=0, funding_cost=5)
    result = calculate_max_risk(request)
    assert result.required_margin + result.entry_fee + 5 == pytest.approx(1000)
    assert result.actual_risk_amount < result.risk_budget
    assert any("capped" in warning for warning in result.warnings)


@pytest.mark.parametrize("budgets", [{}, {"max_risk_amount": 10, "max_risk_percent": 1}, {"max_risk_amount": 1001}])
def test_risk_mode_requires_one_valid_budget(budgets):
    with pytest.raises(ValidationError):
        MaxRiskInput(entry=100, stop=95, account_size=1000, **budgets)


def test_unknown_funding_cannot_promise_exact_risk_or_profit():
    risk = calculate_max_risk(MaxRiskInput(entry=100, stop=95, account_size=1000, max_risk_amount=10, funding_cost=None))
    profit = calculate_target_profit(TargetProfitInput(entry=100, target=110, desired_profit=100, funding_cost=None))
    assert risk.status == profit.status == "INSUFFICIENT_DATA"
    assert risk.notional is profit.notional is None


@pytest.mark.parametrize("side,target", [("LONG", 110), ("SHORT", 90)])
def test_target_profit_sizing_backsolves_net_after_costs(side, target):
    result = calculate_target_profit(TargetProfitInput(side=side, entry=100, target=target, desired_profit=123, funding_cost=7, entry_fee_rate=0.001, exit_fee_rate=0.002, leverage=4))
    assert result.net_pnl == pytest.approx(123)
    assert result.required_margin == pytest.approx(result.notional / 4)
    assert result.required_net_move_percent == pytest.approx(123 / result.notional * 100)


def test_target_profit_does_not_manufacture_edge_by_increasing_leverage():
    low = calculate_target_profit(TargetProfitInput(entry=100, target=110, desired_profit=100, leverage=1))
    high = calculate_target_profit(TargetProfitInput(entry=100, target=110, desired_profit=100, leverage=20))
    assert low.notional == high.notional
    assert low.net_pnl == high.net_pnl
    assert high.required_margin == low.required_margin / 20


def test_target_smaller_than_costs_is_not_a_viable_profit_size():
    result = calculate_target_profit(TargetProfitInput(entry=100, target=100.01, desired_profit=10, entry_fee_rate=0.001, exit_fee_rate=0.001))
    assert result.status == "INSUFFICIENT_DATA"
    assert result.notional is None


def test_request_discriminator_and_dispatch():
    request = TypeAdapter(CalculatorRequest).validate_python({"mode": "TRADE", "entry": 100, "target": 110, "stop": 95, "margin": 100})
    assert isinstance(request, TradeInput)
    assert calculate(request).mode == "TRADE"
    request = TypeAdapter(CalculatorRequest).validate_python({"mode": "MAX_RISK", "entry": 100, "stop": 95, "account_size": 1000, "max_risk_amount": 10})
    assert calculate(request).mode == "MAX_RISK"
    request = TypeAdapter(CalculatorRequest).validate_python({"mode": "TARGET_PROFIT", "entry": 100, "target": 110, "desired_profit": 10})
    assert calculate(request).mode == "TARGET_PROFIT"


def test_portfolio_signed_exposure_beta_risk_and_concentration():
    request = PortfolioInput(positions=[
        PortfolioPosition(symbol="AAA", side="LONG", entry=100, quantity=10, stop=95, btc_beta=1.5, sector="gaming", funding_cost=0),
        PortfolioPosition(symbol="BBB", side="SHORT", entry=200, quantity=2, stop=210, btc_beta=0.5, sector="gaming", funding_cost=0),
    ])
    result = calculate_portfolio(request)
    assert result.total_exposure == 1400
    assert result.long_exposure == 1000
    assert result.short_exposure == 400
    assert result.net_exposure == 600
    assert result.btc_adjusted_exposure == 1300
    assert result.total_risk_to_stop == 70
    assert result.sector_concentration == {"gaming": 1}
    assert result.data_quality["correlations"] == "UNAVAILABLE"


def test_portfolio_missing_beta_stop_and_funding_not_assumed_zero():
    request = PortfolioInput(positions=[
        PortfolioPosition(symbol="AAA", side="LONG", entry=100, quantity=10, stop=95, btc_beta=1, funding_cost=0),
        PortfolioPosition(symbol="BBB", side="LONG", entry=100, quantity=10),
    ])
    result = calculate_portfolio(request)
    assert result.btc_adjusted_exposure is None
    assert result.known_btc_adjusted_exposure == 1000
    assert result.btc_beta_coverage == 0.5
    assert result.total_risk_to_stop is None
    assert result.known_risk_to_stop == 50
    assert result.risk_coverage == 0.5
    assert result.correlated_exposure_warnings


@pytest.mark.parametrize("side,correlation,warning", [("LONG", 0.9, True), ("SHORT", 0.9, False), ("SHORT", -0.9, True)])
def test_correlated_exposure_respects_position_direction(side, correlation, warning):
    request = PortfolioInput(positions=[PortfolioPosition(symbol="AAA", side="LONG", entry=100, quantity=1), PortfolioPosition(symbol="BBB", side=side, entry=100, quantity=1)], correlations=[SuppliedCorrelation(first_symbol="AAA", second_symbol="BBB", correlation=correlation)])
    assert bool(calculate_portfolio(request).correlated_exposure_warnings) == warning


def test_portfolio_rejects_unmatched_and_duplicate_correlations():
    position = PortfolioPosition(symbol="AAA", side="LONG", entry=100, quantity=1)
    with pytest.raises(ValidationError):
        PortfolioInput(positions=[position], correlations=[SuppliedCorrelation(first_symbol="AAA", second_symbol="BBB", correlation=0.9)])
    second = PortfolioPosition(symbol="BBB", side="LONG", entry=100, quantity=1)
    with pytest.raises(ValidationError):
        PortfolioInput(positions=[position, second], correlations=[SuppliedCorrelation(first_symbol="AAA", second_symbol="BBB", correlation=0.9), SuppliedCorrelation(first_symbol="BBB", second_symbol="AAA", correlation=0.8)])


def test_empty_portfolio_is_explicit_zero_positions():
    result = calculate_portfolio(PortfolioInput())
    assert result.total_exposure == 0
    assert result.total_risk_to_stop == 0
    assert result.sector_concentration == {}
