"""Long-only, cost-aware paper decision plans. This module never submits orders."""

from __future__ import annotations

import math
from datetime import datetime

from pydantic import Field, field_validator

from .config import Settings
from .models import (
    TIMEFRAMES,
    AssetSnapshot,
    Candle,
    Model,
    Regime,
    RegimeReport,
    Stage,
    TechnicalSnapshot,
    TradePlan,
    aware,
)


class PortfolioState(Model):
    """Observed account state; loss/risk fractions use account equity as denominator."""

    as_of: datetime
    daily_realized_loss_fraction: float = Field(default=0, ge=0)
    open_risk_fraction: float = Field(default=0, ge=0)
    kill_switch: bool = False

    @field_validator("as_of")
    @classmethod
    def timestamp(cls, value: datetime) -> datetime:
        return aware(value)


def causal_bars(snapshot: AssetSnapshot, frame: str) -> list[Candle]:
    """Do not repair gaps or duplicate candles into apparent complete history."""
    bars = [
        b
        for b in snapshot.candles.get(frame, [])
        if b.close_time <= snapshot.as_of and b.available_at <= snapshot.as_of
    ]
    bars.sort(key=lambda b: b.close_time)
    if len({b.source for b in bars}) > 1:
        return []
    seconds = TIMEFRAMES[frame]
    if any((b.close_time - b.open_time).total_seconds() != seconds for b in bars):
        return []
    if any(
        (b.close_time - a.close_time).total_seconds() != seconds for a, b in zip(bars, bars[1:], strict=False)
    ):
        return []
    return bars


def bounded_base(t: TechnicalSnapshot) -> bool:
    """Ignition needs an observed multiweek base beneath nearby resistance.

    A high-volume uptick in an unrelated trend is demand evidence, but is not
    sufficient to establish the pre-expansion sequence this scanner seeks.
    """
    age = _number(t.metrics.get("range_age_days"))
    width = _number(t.metrics.get("range_width_pct"))
    distance = _number(t.metrics.get("distance_resistance_pct"))
    return bool(
        age is not None
        and age >= 21
        and width is not None
        and 0 <= width <= 35
        and distance is not None
        and -2 <= distance <= 5
    )


def technical_usable(snapshot: AssetSnapshot, technical: dict[str, TechnicalSnapshot], frame: str) -> bool:
    t = technical.get(frame)
    bars = causal_bars(snapshot, frame)
    if (
        not t
        or t.timeframe != frame
        or t.as_of != snapshot.as_of
        or t.bars < 220
        or len(bars) < 220
        or t.bars != len(bars)
    ):
        return False
    epoch = t.metrics.get("last_close_epoch")
    if epoch is not None and epoch != bars[-1].close_time.timestamp():
        return False
    age = (snapshot.as_of - bars[-1].close_time).total_seconds()
    return 0 <= age <= TIMEFRAMES[frame] + 3600 and t.metrics.get("close") == bars[-1].close


def metric_value(
    snapshot: AssetSnapshot, name: str, settings: Settings, derivatives: bool = False
) -> float | None:
    metric = (snapshot.derivatives if derivatives else snapshot.market).get(name)
    max_age = settings.metric_max_age_seconds
    if derivatives and name == "funding_rate":
        interval = snapshot.derivatives.get("funding_interval_hours")
        if (
            interval
            and interval.usable(snapshot.as_of, 24 * 3600 + 300)
            and interval.value is not None
            and 0 < interval.value <= 24
            and metric
            and metric.observed_at
            and interval.observed_at
            and abs((metric.observed_at - interval.observed_at).total_seconds()) <= 60
        ):
            # Actual observed settlement cadence, not an assumed universal 8h.
            max_age = interval.value * 3600 + 300
    return metric.value if metric and metric.usable(snapshot.as_of, max_age) else None


def funding_8h_equivalent(snapshot: AssetSnapshot, settings: Settings) -> float | None:
    """Normalize observed settled funding only with a matched observed cadence.

    Eight-hour equivalent is a comparison convention, not a forecast of future
    settlement rates. A one-hour rate cannot be compared directly with an
    eight-hour rate to infer derivative crowding.
    """
    rate = metric_value(snapshot, "funding_rate", settings, True)
    funding = snapshot.derivatives.get("funding_rate")
    interval = snapshot.derivatives.get("funding_interval_hours")
    if (
        rate is None
        or funding is None
        or not funding.observed_at
        or not interval
        or not interval.usable(snapshot.as_of, 24 * 3600 + 300)
        or interval.value is None
        or not 0 < interval.value <= 24
        or not interval.observed_at
        or abs((funding.observed_at - interval.observed_at).total_seconds()) > 60
    ):
        return None
    return rate * 8 / interval.value


def _number(value: object) -> float | None:
    return (
        float(value)
        if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)
        else None
    )


def trade_plan(
    snapshot: AssetSnapshot,
    technical: dict[str, TechnicalSnapshot],
    stage: Stage,
    score: float,
    confidence: float,
    regime: RegimeReport,
    settings: Settings,
    portfolio: PortfolioState | None = None,
) -> TradePlan:
    """Fail closed on uncertain execution inputs, portfolio state, and observed targets.

    ENTRY means a reviewable paper plan, not an order authorization. A resistance
    target is an observed level, not a forecast or promised outcome.
    """
    if stage == Stage.EXPANSION:
        return TradePlan(action="AVOID", reasons=["Price is extended; chasing expansion is prohibited."])
    if stage in {Stage.NONE, Stage.INSUFFICIENT_DATA}:
        return TradePlan(reasons=["No actionable pre-expansion structure."])
    regime_time = getattr(regime, "as_of", None)
    regime_available = getattr(regime, "available_at", None)
    if regime_time != snapshot.as_of or regime_available is None or regime_available > snapshot.as_of:
        return TradePlan(
            action="WATCH", reasons=["Point-in-time regime provenance is unavailable or mismatched."]
        )
    if not all(technical_usable(snapshot, technical, frame) for frame in ("4h", "1D")):
        return TradePlan(reasons=["Fresh, complete 4h and daily closed-candle history is required."])
    t = technical["4h"]
    close = _number(t.metrics.get("close"))
    atr = _number(t.metrics.get("atr14"))
    extension = _number(t.metrics.get("extension_atr"))
    if extension is None:
        return TradePlan(action="WATCH", reasons=["Extension cannot be assessed."])
    if extension >= 3 or t.patterns.get("overextended") is True:
        return TradePlan(action="AVOID", reasons=["Price is too far above its observed base/ATR structure."])
    gates: list[str] = []
    if score < settings.min_score:
        gates.append("Expansion score is below the configured threshold.")
    if confidence < settings.min_confidence:
        gates.append("Evidence coverage is below the configured threshold.")
    if regime.confidence < 0.5 or regime.risk_multiplier <= 0:
        gates.append("BTC/market regime evidence is insufficient or risk is disabled.")
    if regime.regime in {Regime.SHAKEOUT, Regime.RISK_OFF}:
        gates.append("Market regime blocks new long exposure.")
    for name, limit, minimum in (
        ("quote_volume_24h", settings.min_quote_volume_24h, True),
        ("depth_usd", settings.min_depth_usd, True),
        ("spread_bps", settings.max_spread_bps, False),
    ):
        liquidity = snapshot.market.get(name)
        max_age = (
            min(settings.metric_max_age_seconds, 60)
            if name in {"depth_usd", "spread_bps"}
            else settings.metric_max_age_seconds
        )
        value = liquidity.value if liquidity and liquidity.usable(snapshot.as_of, max_age) else None
        if value is None or value < 0 or not liquidity or "spot" not in liquidity.source.lower():
            gates.append(f"Fresh exchange liquidity input {name} is unavailable.")
        elif (minimum and value < limit) or (not minimum and value > limit):
            gates.append(f"Exchange liquidity input {name} fails the configured limit.")
    if portfolio is None:
        gates.append("Observed portfolio risk state is required for ENTRY.")
    elif not 0 <= (snapshot.as_of - portfolio.as_of).total_seconds() <= settings.metric_max_age_seconds:
        gates.append("Portfolio state is stale or not yet available.")
    elif portfolio.kill_switch:
        gates.append("Portfolio kill switch is active.")
    elif portfolio.daily_realized_loss_fraction >= settings.max_daily_loss_fraction:
        gates.append("Daily realized-loss limit has been reached.")
    elif portfolio.open_risk_fraction >= settings.max_portfolio_risk_fraction:
        gates.append("Portfolio risk limit has been reached.")
    early = stage in {Stage.COMPRESSION, Stage.ACCUMULATION}
    if gates:
        return TradePlan(action="WATCH", reasons=gates)
    if early:
        return TradePlan(
            action="ARMED",
            reasons=["Structure qualifies; wait for accepted breakout/retest or spot-confirmed ignition."],
        )
    rv = _number(t.metrics.get("relative_volume"))
    spot_bars = causal_bars(snapshot, "4h")
    native_spot = bool(spot_bars and all("spot" in b.source.lower() for b in spot_bars))
    if not native_spot or rv is None or rv < 1.5:
        return TradePlan(
            action="ARMED",
            reasons=["Entry requires abnormal exchange-native spot volume on completed candles."],
        )
    if stage not in {Stage.IGNITION, Stage.BREAKOUT, Stage.RETEST} or extension > 2:
        return TradePlan(action="ARMED", reasons=["Wait for an accepted, unextended entry structure."])
    breakout_level = _number(t.levels.get("breakout_level", t.metrics.get("breakout_level")))
    if stage in {Stage.BREAKOUT, Stage.RETEST}:
        accepted = (
            breakout_level is not None
            and atr is not None
            and atr > 0
            and t.patterns.get("accepted_breakout") is True
            and all(bar.close > breakout_level + 0.1 * atr for bar in spot_bars[-2:])
        )
        if not accepted or (stage == Stage.RETEST and t.patterns.get("retest") is not True):
            return TradePlan(
                action="ARMED",
                reasons=["Breakout entry needs two accepted closes and a confirmed retest when selected."],
            )
    elif t.patterns.get("demand_expansion") is not True or not bounded_base(t):
        return TradePlan(
            action="ARMED",
            reasons=["Ignition entry requires spot demand at a nearby, observed multiweek base resistance."],
        )
    if close is None or atr is None or close <= 0 or atr <= 0:
        return TradePlan(action="WATCH", reasons=["Valid closed price and ATR are required."])
    quotes = [snapshot.market.get(name) for name in ("best_bid", "best_ask")]
    if not all(
        q
        and q.usable(snapshot.as_of, min(settings.metric_max_age_seconds, 60))
        and "spot" in q.source.lower()
        and q.value is not None
        and q.value > 0
        for q in quotes
    ):
        return TradePlan(
            action="ARMED", reasons=["Fresh exchange-native executable bid/ask reference is required."]
        )
    bid, ask = (q.value for q in quotes)
    assert bid is not None and ask is not None
    if ask <= bid or (ask - bid) / ((ask + bid) / 2) * 10_000 > settings.max_spread_bps:
        return TradePlan(action="ARMED", reasons=["Current bid/ask is crossed or exceeds the spread limit."])
    if abs(ask - close) > 0.5 * atr:
        return TradePlan(
            action="AVOID" if ask > close else "ARMED",
            reasons=[
                "Current executable price differs by over 0.5 ATR from the closed-candle setup; refresh structure."
            ],
        )
    if extension + (ask - close) / atr > 2:
        return TradePlan(action="AVOID", reasons=["Current executable price would chase an extended move."])
    if stage in {Stage.BREAKOUT, Stage.RETEST} and breakout_level is not None and bid <= breakout_level:
        return TradePlan(action="ARMED", reasons=["Current bid has lost the accepted breakout level."])
    close = ask  # Structure stays closed-candle based; the plan uses an observed executable ask.
    support_candidates = [_number(t.metrics.get("range_low"))]
    supports = t.levels.get("supports", [])
    if isinstance(supports, list):
        support_candidates.extend(_number(v) for v in supports)
    if stage in {Stage.BREAKOUT, Stage.RETEST}:
        support_candidates.append(_number(t.levels.get("breakout_level", t.metrics.get("breakout_level"))))
    valid_supports = [x for x in support_candidates if x is not None and 0 < x < close]
    if not valid_supports:
        return TradePlan(action="ARMED", reasons=["No observed structure supports an invalidation level."])
    stop = max(valid_supports) - 0.25 * atr
    if stop <= 0 or stop >= close:
        return TradePlan(
            action="ARMED", reasons=["Observed structure does not yield a valid ATR-buffered stop."]
        )
    resistances = t.levels.get("resistances", [])
    targets = [_number(v) for v in resistances] if isinstance(resistances, list) else []
    targets = [x for x in targets if x is not None and x > close]
    if not targets:
        return TradePlan(
            action="ARMED",
            entry=close,
            invalidation=stop,
            reasons=["No observed overhead resistance target; reward/risk cannot be verified."],
        )
    target = min(targets)
    round_trip_cost = close * 2 * (settings.fee_bps + settings.slippage_bps) / 10_000
    loss_per_unit = close - stop + round_trip_cost
    net_reward = target - close - round_trip_cost
    rr = net_reward / loss_per_unit
    if rr < settings.min_reward_risk:
        return TradePlan(
            action="ARMED",
            entry=close,
            invalidation=stop,
            target=target,
            reward_risk=rr,
            reasons=["Nearest observed resistance fails cost-adjusted reward/risk."],
        )
    assert portfolio is not None
    remaining_risk = max(0, settings.max_portfolio_risk_fraction - portfolio.open_risk_fraction)
    risk_fraction = min(settings.risk_per_trade * regime.risk_multiplier, remaining_risk)
    volume = metric_value(snapshot, "quote_volume_24h", settings)
    depth = metric_value(snapshot, "depth_usd", settings)
    assert volume is not None and depth is not None
    notional_cap = min(
        settings.account_equity * settings.max_position_fraction,
        volume * settings.max_volume_participation,
        depth * 0.1,
    )
    quantity = min(settings.account_equity * risk_fraction / loss_per_unit, notional_cap / close)
    actual_risk = quantity * loss_per_unit / settings.account_equity
    if quantity <= 0 or actual_risk <= 0:
        return TradePlan(
            action="ARMED", reasons=["Portfolio and liquidity limits leave no permitted position size."]
        )
    return TradePlan(
        action="ENTRY",
        entry=close,
        invalidation=stop,
        target=target,
        reward_risk=rr,
        quantity=quantity,
        notional=quantity * close,
        risk_fraction=actual_risk,
        reasons=[
            "Paper plan: confirmed spot structure, observed resistance, costs, liquidity and portfolio gates passed."
        ],
    )
