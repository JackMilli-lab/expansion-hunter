"""Causal market-regime and leverage-flush evidence; coverage is not probability."""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import Settings
from .models import AssetSnapshot, Metric, Regime, RegimeReport, TechnicalSnapshot
from .risk import causal_bars, funding_8h_equivalent, metric_value, technical_usable


def matched_returns(
    asset: AssetSnapshot, benchmark: AssetSnapshot, days: int = 7
) -> tuple[float, float] | None:
    """Compare identical candle endpoints and duration, never unequal lookbacks."""
    cutoff = min(asset.as_of, benchmark.as_of)
    left = {b.close_time: b.close for b in causal_bars(asset, "1D") if b.close_time <= cutoff}
    right = {b.close_time: b.close for b in causal_bars(benchmark, "1D") if b.close_time <= cutoff}
    shared = sorted(left.keys() & right.keys())
    if not shared:
        return None
    end = shared[-1]
    start = end - timedelta(days=days)
    if start not in left or start not in right or (asset.as_of - end).total_seconds() > 90_000:
        return None
    return left[end] / left[start] - 1, right[end] / right[start] - 1


def classify_regime(
    btc: AssetSnapshot | None,
    eth: AssetSnapshot | None,
    btc_technical: dict[str, TechnicalSnapshot],
    eth_technical: dict[str, TechnicalSnapshot],
    global_metrics: dict[str, Metric],
    breadth: float | None,
    as_of: datetime,
    settings: Settings,
) -> RegimeReport:
    missing: list[str] = []
    reasons: list[str] = []
    values: dict[str, float | None] = {}
    checks: list[tuple[float, bool]] = []

    def observe(name: str, value: float | None, weight: float = 1) -> float | None:
        values[name] = value
        checks.append((weight, value is not None))
        if value is None:
            missing.append(name)
        return value

    def global_value(name: str) -> float | None:
        m = global_metrics.get(name)
        return observe(name, m.value if m and m.usable(as_of, settings.metric_max_age_seconds) else None)

    valid_btc = bool(
        btc
        and btc.as_of == as_of
        and all(technical_usable(btc, btc_technical, frame) for frame in ("4h", "1D"))
    )
    valid_eth = bool(
        eth
        and eth.as_of == as_of
        and all(technical_usable(eth, eth_technical, frame) for frame in ("4h", "1D"))
    )
    checks.extend([(5, valid_btc), (2, valid_eth)])
    if not valid_btc:
        missing.append("fresh_btc_4h_and_daily_structure")
    if not valid_eth:
        missing.append("fresh_eth_4h_and_daily_structure")
    btc_d = btc_technical.get("1D") if valid_btc else None
    btc_h = btc_technical.get("4h") if valid_btc else None
    btc_spot_bars = causal_bars(btc, "4h") if valid_btc and btc else []
    native_btc_spot = bool(btc_spot_bars and all("spot" in b.source.lower() for b in btc_spot_bars))
    close = observe("btc_close", btc_d.metrics.get("close") if btc_d else None, 2)
    ema50 = observe("btc_ema50", btc_d.metrics.get("ema50") if btc_d else None, 2)
    ema200 = observe("btc_ema200", btc_d.metrics.get("ema200") if btc_d else None, 2)
    raw_btc_return = btc_d.metrics.get("return_7d") if btc_d else None
    # Technical *_pct and return fields are percentage points; provider returns
    # and matched_returns use fractions. Normalize at this boundary.
    btc_return = observe("btc_return_7d", raw_btc_return / 100 if raw_btc_return is not None else None, 2)
    btc_closes = {bar.close_time: bar.close for bar in btc_spot_bars}
    current_endpoint = btc_spot_bars[-1].close_time if btc_spot_bars else None
    prior_endpoint = current_endpoint - timedelta(days=1) if current_endpoint else None
    btc_day_return = observe(
        "btc_return_24h",
        btc_closes[current_endpoint] / btc_closes[prior_endpoint] - 1
        if current_endpoint in btc_closes and prior_endpoint in btc_closes
        else None,
    )
    rv = observe(
        "btc_relative_spot_volume",
        btc_h.metrics.get("relative_volume") if btc_h and native_btc_spot else None,
        2,
    )
    observe("btc_rv_percentile", btc_h.metrics.get("rv_percentile") if btc_h else None)
    oi = observe(
        "btc_oi_change_24h",
        metric_value(btc, "open_interest_change_pct", settings, True) if valid_btc and btc else None,
    )
    observe("btc_funding", metric_value(btc, "funding_rate", settings, True) if valid_btc and btc else None)
    normalized_funding = observe(
        "btc_funding_8h_equivalent", funding_8h_equivalent(btc, settings) if valid_btc and btc else None
    )
    liquidation = observe(
        "btc_liquidation_cluster_usd",
        metric_value(btc, "liquidation_cluster_usd", settings, True) if valid_btc and btc else None,
    )
    dominance = global_value("btc_dominance_change_7d")
    cross_returns = matched_returns(eth, btc) if valid_eth and valid_btc and eth and btc else None
    eth_btc = ((1 + cross_returns[0]) / (1 + cross_returns[1]) - 1) if cross_returns else None
    if eth_btc is None:
        m = global_metrics.get("eth_btc_return_7d")
        eth_btc = m.value if m and m.usable(as_of, settings.metric_max_age_seconds) else None
    eth_btc = observe("eth_btc_return_7d", eth_btc, 2)
    total = global_value("total_return_7d")
    total2 = global_value("total2_return_7d")
    total3 = global_value("total3_return_7d")
    global_value("stablecoin_mcap_change_7d")
    breadth = observe("alt_breadth", breadth if breadth is not None and 0 <= breadth <= 1 else None, 2)
    cap_resistance = global_value("total_distance_resistance_pct")

    avwap_values = (
        [
            a.value
            for a in btc_h.anchors
            if a.value is not None
            and a.confirmed_at <= as_of
            and a.kind in {"range_start", "major_swing_low", "breakout_origin", "current_range"}
        ]
        if btc_h
        else []
    )
    vwap = observe(
        "btc_structural_avwap",
        avwap_values[-1] if avwap_values else (btc_h.metrics.get("vwap") if btc_h else None),
        2,
    )
    four_h_close = btc_h.metrics.get("close") if btc_h else None
    weakening_spot = btc_h.metrics.get("volume_contraction") if btc_h and native_btc_spot else None
    observe("btc_volume_contraction", weakening_spot)
    divergence = btc_h.patterns.get("weak_advance") if btc_h else None
    checks.append((1, divergence is not None))
    if divergence is None:
        missing.append("btc_negative_price_volume_divergence")
    flush_checks = [
        ("OI rising at least 10% in the observed 24h window", 20, oi is not None and oi >= 0.10),
        (
            "Funding magnitude at least 5 bps on an observed 8h-equivalent basis",
            15,
            normalized_funding is not None and abs(normalized_funding) >= 0.0005,
        ),
        (
            "BTC fails to advance over an observed 24h closed-candle window",
            15,
            btc_day_return is not None and abs(btc_day_return) <= 0.02,
        ),
        ("Spot volume contracts", 15, weakening_spot is not None and weakening_spot <= 0.75),
        (
            "BTC closes below observed structural VWAP",
            15,
            vwap is not None and four_h_close is not None and four_h_close < vwap,
        ),
        (
            "Verified liquidation cluster exceeds $10m",
            10,
            liquidation is not None and liquidation >= 10_000_000,
        ),
        ("Negative price-volume divergence", 5, divergence is True),
        (
            "Total capitalization lies beneath nearby resistance",
            5,
            cap_resistance is not None and 0 <= cap_resistance <= 2,
        ),
    ]
    shakeout_score = float(sum(weight for _, weight, passed in flush_checks if passed))
    confluence = sum(passed for _, weight, passed in flush_checks if weight >= 15)
    shakeout = shakeout_score >= 60 and confluence >= 3 and oi is not None and oi >= 0.10
    reasons.extend(label for label, _, passed in flush_checks if passed)
    confidence = sum(weight for weight, known in checks if known) / sum(weight for weight, _ in checks)
    if not valid_btc:
        confidence = min(confidence, 0.25)
        return RegimeReport(
            as_of=as_of,
            available_at=as_of,
            confidence=confidence,
            risk_multiplier=0,
            shakeout_score=shakeout_score,
            metrics=values,
            reasons=reasons + ["BTC primary history is incomplete; long risk disabled."],
            missing=missing,
        )
    trend_up = close is not None and ema50 is not None and ema200 is not None and close > ema50 > ema200
    trend_down = close is not None and ema200 is not None and close < ema200
    regime = Regime.NEUTRAL
    if shakeout:
        regime = Regime.SHAKEOUT
    elif trend_down and btc_return is not None and btc_return <= -0.05:
        regime = Regime.RISK_OFF
    elif (
        trend_up
        and eth_btc is not None
        and eth_btc >= 0.03
        and dominance is not None
        and dominance <= -0.5
        and total3 is not None
        and total3 >= 0.05
        and breadth is not None
        and breadth >= 0.65
    ):
        regime = Regime.ALT_EXPANSION
    elif (
        not trend_down
        and eth_btc is not None
        and eth_btc > 0
        and dominance is not None
        and dominance < 0
        and breadth is not None
        and breadth >= 0.55
        and (total2 is not None and total2 > 0)
    ):
        regime = Regime.ALT_ROTATION
    elif (
        trend_up
        and btc_return is not None
        and btc_return >= 0.08
        and rv is not None
        and rv >= 1.5
        and breadth is not None
        and breadth < 0.6
    ):
        regime = Regime.BTC_EXPANSION
    elif trend_up and btc_return is not None and btc_return >= 0 and (total is None or total >= 0):
        regime = Regime.RISK_ON
    multiplier = {
        Regime.SHAKEOUT: 0,
        Regime.RISK_OFF: 0,
        Regime.NEUTRAL: 0.5,
        Regime.BTC_EXPANSION: 0.4,
        Regime.RISK_ON: 0.8,
        Regime.ALT_ROTATION: 0.9,
        Regime.ALT_EXPANSION: 1,
    }[regime] * confidence
    reasons.append(f"{regime.value} derives from current closed-candle and available point-in-time evidence.")
    if missing:
        reasons.append(
            "Missing derivatives/global series reduce coverage; their absence is not evidence of safety."
        )
    return RegimeReport(
        as_of=as_of,
        available_at=as_of,
        regime=regime,
        confidence=confidence,
        shakeout_score=shakeout_score,
        risk_multiplier=multiplier,
        metrics=values,
        reasons=reasons,
        missing=missing,
    )
