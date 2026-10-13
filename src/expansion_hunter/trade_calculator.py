"""Cost-aware manual decision support. No exchange client or order execution.

Fee rates are fractions (0.001 = 0.1%). Funding is a signed, user-supplied
cash estimate: positive means a cost and negative means a credit. Zero is
explicitly a USER_ASSUMPTION, never a report of exchange funding. Quantities
assume linear quote-currency settlement; inverse contracts are unsupported.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import Model


class CalculationInputs(Model):
    side: Literal["LONG", "SHORT"] = "LONG"
    entry: float = Field(gt=0)
    leverage: float = Field(default=1, ge=1, le=125)
    margin_mode: Literal["ISOLATED", "CROSS"] = "ISOLATED"
    entry_fee_rate: float = Field(default=0.0004, ge=0, lt=1)
    exit_fee_rate: float = Field(default=0.0004, ge=0, lt=1)
    entry_fee_type: Literal["MAKER", "TAKER"] = "TAKER"
    exit_fee_type: Literal["MAKER", "TAKER"] = "TAKER"
    funding_cost: float | None = 0
    funding_source: Literal["USER_ASSUMPTION", "USER_INPUT"] = "USER_ASSUMPTION"
    maintenance_margin_rate: float | None = Field(default=None, ge=0, lt=1)

    def validate_levels(self, target: float | None, stop: float | None) -> None:
        direction = 1 if self.side == "LONG" else -1
        if target is not None and direction * (target - self.entry) <= 0:
            raise ValueError("target must be beyond entry in the selected trade direction")
        if stop is not None and direction * (stop - self.entry) >= 0:
            raise ValueError("stop must be on the losing side of entry")
        if (
            self.maintenance_margin_rate is not None
            and self.maintenance_margin_rate >= 1 / self.leverage
        ):
            raise ValueError("maintenance margin must be below the initial margin fraction")


class TradeInput(CalculationInputs):
    mode: Literal["TRADE"] = "TRADE"
    target: float = Field(gt=0)
    stop: float = Field(gt=0)
    margin: float = Field(gt=0)

    @model_validator(mode="after")
    def levels(self):
        self.validate_levels(self.target, self.stop)
        return self


class MaxRiskInput(CalculationInputs):
    mode: Literal["MAX_RISK"] = "MAX_RISK"
    account_size: float = Field(gt=0)
    max_risk_amount: float | None = Field(default=None, gt=0)
    max_risk_percent: float | None = Field(default=None, gt=0, le=100)
    stop: float = Field(gt=0)
    target: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def levels(self):
        self.validate_levels(self.target, self.stop)
        if (self.max_risk_amount is None) == (self.max_risk_percent is None):
            raise ValueError("supply exactly one risk budget: amount or percent")
        if self.max_risk_amount is not None and self.max_risk_amount > self.account_size:
            raise ValueError("risk budget cannot exceed account size")
        return self


class TargetProfitInput(CalculationInputs):
    mode: Literal["TARGET_PROFIT"] = "TARGET_PROFIT"
    target: float = Field(gt=0)
    desired_profit: float = Field(gt=0)
    stop: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def levels(self):
        self.validate_levels(self.target, self.stop)
        return self


CalculatorRequest = Annotated[TradeInput | MaxRiskInput | TargetProfitInput, Field(discriminator="mode")]


class CalculatorResult(Model):
    mode: str
    side: str
    status: Literal["CALCULATED", "INSUFFICIENT_DATA"] = "CALCULATED"
    settlement: str = "LINEAR_QUOTE_CURRENCY"
    currency: str = "INPUT_QUOTE_CURRENCY"
    position_size: float | None = None
    notional: float | None = None
    required_margin: float | None = None
    price_move_percent: float | None = None
    gross_pnl: float | None = None
    entry_fee: float | None = None
    exit_fee: float | None = None
    estimated_fees: float | None = None
    estimated_funding: float | None = None
    net_pnl: float | None = None
    net_status: Literal["USER_ASSUMPTION", "USER_INPUT", "UNAVAILABLE"] = "UNAVAILABLE"
    roi_on_margin_percent: float | None = None
    reward_risk: float | None = None
    gross_loss_at_stop: float | None = None
    loss_at_stop: float | None = None
    profit_at_target: float | None = None
    break_even_price: float | None = None
    estimated_liquidation_price: float | None = None
    liquidation_status: str = "UNAVAILABLE"
    actual_risk_amount: float | None = None
    actual_risk_percent: float | None = None
    risk_budget: float | None = None
    required_net_move_percent: float | None = None
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _empty(request: CalculationInputs, mode: str, reason: str) -> CalculatorResult:
    return CalculatorResult(
        mode=mode,
        side=request.side,
        status="INSUFFICIENT_DATA",
        warnings=[reason],
        assumptions=["User-supplied inputs; no exchange execution or guaranteed outcome."],
    )


def _liquidation(request: CalculationInputs, quantity: float, margin: float) -> tuple[float | None, str]:
    if request.margin_mode == "CROSS":
        return None, "UNAVAILABLE_CROSS_ACCOUNT_STATE_REQUIRED"
    maintenance = request.maintenance_margin_rate
    if maintenance is None:
        return None, "UNAVAILABLE_MAINTENANCE_RATE_REQUIRED"
    direction = 1 if request.side == "LONG" else -1
    price = (quantity * request.entry - direction * margin) / (quantity * (1 - direction * maintenance))
    if price <= 0:
        return None, "NO_POSITIVE_LIQUIDATION_IN_SIMPLIFIED_MODEL"
    return price, "ESTIMATED_LIQUIDATION"


def _result(
    request: CalculationInputs,
    mode: str,
    quantity: float,
    target: float | None,
    stop: float | None,
) -> CalculatorResult:
    direction = 1 if request.side == "LONG" else -1
    notional = quantity * request.entry
    margin = notional / request.leverage
    entry_fee = notional * request.entry_fee_rate
    exit_fee = quantity * target * request.exit_fee_rate if target is not None else None
    fees = entry_fee + exit_fee if exit_fee is not None else None
    gross = quantity * direction * (target - request.entry) if target is not None else None
    funding = request.funding_cost
    net = gross - fees - funding if gross is not None and fees is not None and funding is not None else None
    stop_gross = quantity * abs(stop - request.entry) if stop is not None else None
    loss = (
        stop_gross + entry_fee + quantity * stop * request.exit_fee_rate + funding
        if stop_gross is not None and stop is not None and funding is not None
        else None
    )
    break_even = None
    if funding is not None:
        break_even = (
            request.entry * (direction + request.entry_fee_rate) + funding / quantity
        ) / (direction - request.exit_fee_rate)
        if break_even <= 0:
            break_even = None
    liquidation, liquidation_status = _liquidation(request, quantity, margin)
    warnings = []
    if funding is None:
        warnings.append("Funding is UNAVAILABLE; net PnL, net risk and break-even cannot be calculated.")
    if net is not None and net <= 0:
        warnings.append("Target move does not cover supplied fees and funding.")
    if liquidation is not None and stop is not None and direction * (stop - liquidation) <= 0:
        warnings.append("Estimated liquidation is reached before the specified stop.")
    if loss is not None and loss <= 0:
        warnings.append("Funding-credit assumption eliminates the modeled stop loss; reward/risk is unavailable.")
    return CalculatorResult(
        mode=mode,
        side=request.side,
        position_size=quantity,
        notional=notional,
        required_margin=margin,
        price_move_percent=(target / request.entry - 1) * 100 if target is not None else None,
        gross_pnl=gross,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        estimated_fees=fees,
        estimated_funding=funding,
        net_pnl=net,
        net_status=request.funding_source if funding is not None else "UNAVAILABLE",
        roi_on_margin_percent=net / margin * 100 if net is not None else None,
        reward_risk=net / loss if net is not None and loss is not None and loss > 0 else None,
        gross_loss_at_stop=stop_gross,
        loss_at_stop=loss,
        profit_at_target=net,
        break_even_price=break_even,
        estimated_liquidation_price=liquidation,
        liquidation_status=liquidation_status,
        assumptions=[
            "Linear contract / quote-currency settlement; not an inverse-contract calculation.",
            "Prices, account capital, margin, funding and desired profit must use the same quote currency; no EUR/USD conversion is performed.",
            "Fee rates and maker/taker labels are user inputs, not queried exchange tariffs.",
            "Funding is a fixed signed cash assumption applied once to both target and stop scenarios.",
            "No slippage, mark-price divergence, fee tiers or additional collateral modeled.",
            "Liquidation, when available, uses a simplified isolated maintenance-equity equation; exchange rules differ.",
        ],
        warnings=warnings,
    )


def calculate_trade(request: TradeInput) -> CalculatorResult:
    return _result(request, request.mode, request.margin * request.leverage / request.entry, request.target, request.stop)


def calculate_max_risk(request: MaxRiskInput) -> CalculatorResult:
    if request.funding_cost is None:
        return _empty(request, request.mode, "Funding is UNAVAILABLE; exact net-risk sizing requires a funding assumption.")
    budget = request.max_risk_amount
    if budget is None:
        assert request.max_risk_percent is not None
        budget = request.account_size * request.max_risk_percent / 100
    unit_loss = abs(request.entry - request.stop) + request.entry * request.entry_fee_rate + request.stop * request.exit_fee_rate
    if budget <= request.funding_cost:
        return _empty(request, request.mode, "Funding assumption consumes the entire risk budget.")
    quantity = (budget - request.funding_cost) / unit_loss
    capital = request.account_size - max(0, request.funding_cost)
    if capital <= 0:
        return _empty(request, request.mode, "Funding reserve exceeds account capital.")
    affordable = capital / (request.entry / request.leverage + request.entry * request.entry_fee_rate)
    capped = quantity > affordable
    quantity = min(quantity, affordable)
    result = _result(request, request.mode, quantity, request.target, request.stop)
    result.actual_risk_amount = result.loss_at_stop
    result.actual_risk_percent = result.loss_at_stop / request.account_size * 100 if result.loss_at_stop is not None else None
    result.risk_budget = budget
    if capped:
        result.warnings.append("Position capped by supplied account capital after margin, entry fee and positive funding reserve.")
    if request.funding_cost < 0:
        result.warnings.append("Sizing relies on a funding credit that may not be received.")
    return result


def calculate_target_profit(request: TargetProfitInput) -> CalculatorResult:
    if request.funding_cost is None:
        return _empty(request, request.mode, "Funding is UNAVAILABLE; net-profit sizing requires a funding assumption.")
    direction = 1 if request.side == "LONG" else -1
    unit_net = direction * (request.target - request.entry) - request.entry * request.entry_fee_rate - request.target * request.exit_fee_rate
    if unit_net <= 0:
        return _empty(request, request.mode, "Target move does not cover supplied proportional transaction costs.")
    if request.desired_profit + request.funding_cost <= 0:
        return _empty(request, request.mode, "Funding-credit assumption alone exceeds desired profit; no positive position solves the request.")
    quantity = (request.desired_profit + request.funding_cost) / unit_net
    result = _result(request, request.mode, quantity, request.target, request.stop)
    result.required_net_move_percent = request.desired_profit / result.notional * 100
    return result


def calculate(request: TradeInput | MaxRiskInput | TargetProfitInput) -> CalculatorResult:
    if isinstance(request, TradeInput):
        return calculate_trade(request)
    if isinstance(request, MaxRiskInput):
        return calculate_max_risk(request)
    if isinstance(request, TargetProfitInput):
        return calculate_target_profit(request)
    raise TypeError("unsupported calculator request")


class PortfolioPosition(Model):
    symbol: str = Field(min_length=1, max_length=64)
    side: Literal["LONG", "SHORT"]
    entry: float = Field(gt=0)
    quantity: float = Field(gt=0)
    stop: float | None = Field(default=None, gt=0)
    btc_beta: float | None = None
    sector: str | None = None
    entry_fee_rate: float = Field(default=0, ge=0, lt=1)
    exit_fee_rate: float = Field(default=0, ge=0, lt=1)
    funding_cost: float | None = None

    @model_validator(mode="after")
    def stop_side(self):
        direction = 1 if self.side == "LONG" else -1
        if self.stop is not None and direction * (self.stop - self.entry) >= 0:
            raise ValueError("portfolio stop must be on the losing side of entry")
        return self


class SuppliedCorrelation(Model):
    first_symbol: str
    second_symbol: str
    correlation: float = Field(ge=-1, le=1)

    @model_validator(mode="after")
    def distinct(self):
        if self.first_symbol == self.second_symbol:
            raise ValueError("correlation requires two distinct symbols")
        return self


class PortfolioInput(Model):
    positions: list[PortfolioPosition] = Field(default_factory=list, max_length=500)
    correlations: list[SuppliedCorrelation] = Field(default_factory=list, max_length=10000)
    correlation_threshold: float = Field(default=0.7, ge=0, le=1)

    @model_validator(mode="after")
    def valid_correlations(self):
        symbols = {position.symbol for position in self.positions}
        seen = set()
        for correlation in self.correlations:
            pair = frozenset((correlation.first_symbol, correlation.second_symbol))
            if not pair <= symbols:
                raise ValueError("correlation references a symbol absent from portfolio")
            if pair in seen:
                raise ValueError("duplicate correlation pair")
            seen.add(pair)
        return self


class PortfolioResult(Model):
    total_exposure: float
    long_exposure: float
    short_exposure: float
    net_exposure: float
    btc_adjusted_exposure: float | None
    known_btc_adjusted_exposure: float
    btc_beta_coverage: float
    total_risk_to_stop: float | None
    known_risk_to_stop: float
    risk_coverage: float
    sector_concentration: dict[str, float]
    correlated_exposure_warnings: list[str]
    data_quality: dict[str, str]
    assumptions: list[str]


def calculate_portfolio(request: PortfolioInput) -> PortfolioResult:
    """Manual exposure arithmetic; absent betas/correlations remain unavailable."""
    long = short = beta_known = risk_known = beta_notional = risk_notional = 0.0
    sectors: dict[str, float] = {}
    exposures: dict[str, float] = {}
    for position in request.positions:
        notional = position.entry * position.quantity
        direction = 1 if position.side == "LONG" else -1
        if direction == 1:
            long += notional
        else:
            short += notional
        exposures[position.symbol] = exposures.get(position.symbol, 0) + direction * notional
        sector = position.sector or "UNAVAILABLE"
        sectors[sector] = sectors.get(sector, 0) + notional
        if position.btc_beta is not None:
            beta_known += direction * notional * position.btc_beta
            beta_notional += notional
        if position.stop is not None and position.funding_cost is not None:
            risk_known += max(0, position.quantity * abs(position.entry - position.stop) + notional * position.entry_fee_rate + position.quantity * position.stop * position.exit_fee_rate + position.funding_cost)
            risk_notional += notional
    total = long + short
    warnings = []
    for correlation in request.correlations:
        aligned = exposures[correlation.first_symbol] * exposures[correlation.second_symbol] * correlation.correlation > 0
        if aligned and abs(correlation.correlation) >= request.correlation_threshold:
            warnings.append(f"CORRELATED EXPOSURE: {correlation.first_symbol} / {correlation.second_symbol}; supplied correlation {correlation.correlation:.3f}.")
    if len(request.positions) > 1 and not request.correlations:
        warnings.append("Correlation concentration is UNAVAILABLE; positions cannot be assumed independent.")
    all_beta = all(position.btc_beta is not None for position in request.positions)
    all_risk = all(position.stop is not None and position.funding_cost is not None for position in request.positions)
    return PortfolioResult(
        total_exposure=total,
        long_exposure=long,
        short_exposure=short,
        net_exposure=long - short,
        btc_adjusted_exposure=beta_known if all_beta else None,
        known_btc_adjusted_exposure=beta_known,
        btc_beta_coverage=beta_notional / total if total else 1,
        total_risk_to_stop=risk_known if all_risk else None,
        known_risk_to_stop=risk_known,
        risk_coverage=risk_notional / total if total else 1,
        sector_concentration={sector: notional / total for sector, notional in sectors.items()} if total else {},
        correlated_exposure_warnings=warnings,
        data_quality={"btc_beta": "USER_INPUT" if all_beta else "PARTIAL_OR_UNAVAILABLE", "stop_risk": "USER_INPUT" if all_risk else "PARTIAL_OR_UNAVAILABLE", "correlations": "USER_INPUT" if request.correlations else "UNAVAILABLE"},
        assumptions=["Positions, betas, sectors and correlations are manually supplied; not observed exchange holdings.", "Exposures use supplied entry prices, not live mark-to-market prices.", "No diversification or portfolio loss guarantee is inferred from these arithmetic totals."],
    )
