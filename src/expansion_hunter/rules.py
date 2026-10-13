"""Auditable, fixed-weight expansion fingerprints; scores are not probabilities.

Each observation contributes once even if it describes several archetypes.
Weights are research defaults, not claims of historically calibrated returns.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from urllib.parse import urlparse

from .config import Settings
from .models import (
    AssetSnapshot,
    Catalyst,
    Regime,
    RegimeReport,
    RuleEvidence,
    ScanResult,
    Stage,
    TechnicalSnapshot,
)
from .regime import matched_returns
from .risk import (
    PortfolioState,
    bounded_base,
    causal_bars,
    funding_8h_equivalent,
    metric_value,
    technical_usable,
    trade_plan,
)


def _eligible_catalyst(catalyst: Catalyst, snapshot: AssetSnapshot) -> bool:
    """Only sourced, already available quantitative evidence; announcements are insufficient."""
    parsed = urlparse(catalyst.source_url)
    return (
        catalyst.verified
        and catalyst.quantitative
        and catalyst.strength >= 0.5
        and parsed.scheme in {"https", "http"}
        and bool(parsed.hostname)
        and catalyst.category not in {"marketing", "social", "partnership"}
        and catalyst.available_at <= snapshot.as_of
        and snapshot.as_of - timedelta(days=180) <= catalyst.observed_at <= snapshot.as_of
    )


def _stage(snapshot: AssetSnapshot, technical: dict[str, TechnicalSnapshot]) -> Stage:
    if not all(technical_usable(snapshot, technical, frame) for frame in ("4h", "1D")):
        return Stage.INSUFFICIENT_DATA
    t = technical["4h"]
    extension = t.metrics.get("extension_atr")
    if t.patterns.get("overextended") is True or (extension is not None and extension >= 3):
        return Stage.EXPANSION
    bars = causal_bars(snapshot, "4h")
    level = t.levels.get("breakout_level", t.metrics.get("breakout_level"))
    atr = t.metrics.get("atr14")
    accepted = bool(
        isinstance(level, (int, float))
        and not isinstance(level, bool)
        and atr is not None
        and atr > 0
        and t.patterns.get("accepted_breakout") is True
        and all(bar.close > level + 0.1 * atr for bar in bars[-2:])
    )
    if accepted and t.patterns.get("retest") is True:
        return Stage.RETEST
    if accepted:
        return Stage.BREAKOUT
    if (
        t.patterns.get("demand_expansion") is True
        and t.metrics.get("relative_volume") is not None
        and t.metrics["relative_volume"] >= 1.5
        and extension is not None
        and extension <= 2
        and bounded_base(t)
        and all("spot" in bar.source.lower() for bar in bars)
    ):
        return Stage.IGNITION
    if t.patterns.get("absorption") is True:
        return Stage.ACCUMULATION
    if t.patterns.get("compression") is True:
        return Stage.COMPRESSION
    return Stage.NONE


def evaluate(
    snapshot: AssetSnapshot,
    technical: dict[str, TechnicalSnapshot],
    regime: RegimeReport,
    settings: Settings,
    benchmark: AssetSnapshot | None = None,
    portfolio: PortfolioState | None = None,
) -> ScanResult:
    """Evaluate one as-of snapshot with missing inputs earning zero points.

    Confidence is evidence coverage only. Current asset rank never enters scoring.
    Historical likeness labels describe structures, not proven token-specific models.
    """
    from .replay import point_in_time

    snapshot = point_in_time(snapshot, snapshot.as_of)
    ready = all(technical_usable(snapshot, technical, f) for f in ("4h", "1D"))
    t = technical.get("4h") if ready else None
    d = technical.get("1D") if ready else None
    evidence: list[RuleEvidence] = []
    missing: list[str] = []
    warnings = list(snapshot.issues)
    regime_time = getattr(regime, "as_of", None)
    regime_available = getattr(regime, "available_at", None)
    if regime_time != snapshot.as_of or regime_available is None or regime_available > snapshot.as_of:
        regime = RegimeReport(
            reasons=["Regime lacks matching causal timestamp provenance; new long risk is disabled."],
            missing=["point_in_time_regime_provenance"],
        )
        warnings.append("Unstamped or mismatched regime evidence was discarded.")
    memberships: dict[str, tuple[str, ...]] = {}
    spot_history = causal_bars(snapshot, "4h")
    native_spot = bool(spot_history and all("spot" in b.source.lower() for b in spot_history))

    def m(key: str, daily: bool = False) -> float | None:
        source = d if daily else t
        if key in {"volume_contraction", "relative_volume"} and not native_spot:
            return None
        return source.metrics.get(key) if source else None

    def p(key: str) -> bool | None:
        if key in {"absorption", "demand_expansion"} and not native_spot:
            return None
        return t.patterns.get(key) if t else None

    def predicate(value: float | None, fn: Callable[[float], bool]) -> bool | None:
        return fn(value) if value is not None else None

    def add(
        name: str,
        archetypes: tuple[str, ...],
        weight: float,
        passed: bool | None,
        detail: str,
        **inputs: float | bool | str | None,
    ) -> None:
        memberships[name] = archetypes
        evidence.append(
            RuleEvidence(
                rule=name,
                archetype="/".join(archetypes),
                weight=weight,
                passed=passed,
                points=weight if passed else 0,
                detail=detail,
                inputs=inputs,
            )
        )
        if passed is None:
            missing.append(name)

    market_cap = metric_value(snapshot, "market_cap", settings)
    turnover = metric_value(snapshot, "turnover_24h", settings)
    add(
        "small_mid_cap",
        ("QNT",),
        4,
        predicate(market_cap, lambda v: 30e6 <= v <= 8e9),
        "Observed small/mid-cap liquidity asymmetry; rank is not historical evidence.",
        market_cap=market_cap,
    )
    age = m("range_age_days", True)
    width = m("range_width_pct", True)
    if width is None:
        width = m("width_pct", True)
    base = None if age is None or width is None else age >= 35 and width <= 35
    add(
        "multiweek_base",
        ("QNT", "ZEC"),
        8,
        base,
        "At least 35 calendar days of bounded daily structure.",
        range_age_days=age,
        range_width_pct=width,
    )
    add(
        "low_turnover",
        ("QNT",),
        4,
        predicate(turnover, lambda v: 0 <= v <= 0.15),
        "Low observed 24h turnover is contextual, not an entry signal.",
        turnover_24h=turnover,
    )
    contraction = m("volume_contraction")
    add(
        "volume_contraction",
        ("QNT",),
        5,
        predicate(contraction, lambda v: 0 <= v <= 0.75),
        "Recent spot volume contracts relative to its preceding window.",
        volume_contraction=contraction,
    )
    bb, rv = m("bb_width_percentile"), m("rv_percentile")
    compression = (
        None
        if bb is None or rv is None or p("compression") is None
        else bb <= 0.2 and rv <= 0.2 and p("compression") is True
    )
    add(
        "volatility_compression",
        ("QNT", "ZEC"),
        8,
        compression,
        "Both band width and realized volatility sit in their trailing lowest quintile.",
        bb_percentile=bb,
        rv_percentile=rv,
    )
    cluster = m("ema_cluster_pct")
    add(
        "ema_cluster",
        ("QNT",),
        4,
        predicate(cluster, lambda v: 0 <= v <= 3),
        "Tight trailing EMA cluster.",
        ema_cluster_pct=cluster,
    )
    rsi = m("rsi14")
    add(
        "neutral_positive_rsi",
        ("QNT",),
        3,
        predicate(rsi, lambda v: 45 <= v <= 65),
        "Neutral-positive RSI avoids rewarding an already overheated oscillator.",
        rsi14=rsi,
    )
    tests = m("resistance_tests")
    add(
        "repeated_resistance_tests",
        ("QNT", "ZEC"),
        6,
        predicate(tests, lambda v: v >= 3),
        "At least three distinct resistance interactions; counted once across archetypes.",
        resistance_tests=tests,
    )
    add(
        "higher_lows",
        ("QNT", "ZEC"),
        5,
        p("higher_lows"),
        "Confirmed causal swing lows rise.",
        higher_lows=p("higher_lows"),
    )
    add(
        "shallow_pullbacks",
        ("QNT", "ZEC"),
        4,
        p("shallow_pullbacks"),
        "Observed pullback depth shrinks; this is a supply-absorption proxy.",
        shallow_pullbacks=p("shallow_pullbacks"),
    )
    distance = m("distance_resistance_pct")
    add(
        "near_resistance",
        ("QNT", "ZEC"),
        6,
        predicate(distance, lambda v: 0 <= v <= 5),
        "Price approaches observed overhead resistance before a vertical expansion.",
        distance_resistance_pct=distance,
    )
    returns = matched_returns(snapshot, benchmark) if benchmark else None
    relative_strength = None if returns is None else returns[0] - returns[1]
    add(
        "relative_strength",
        ("QNT", "TAO", "ONDO"),
        6,
        predicate(relative_strength, lambda v: v >= 0.02),
        "Asset and BTC returns use the same closed timestamps and seven-day duration.",
        relative_return_7d=relative_strength,
    )
    add(
        "supply_absorption",
        ("ZEC",),
        8,
        p("absorption"),
        "Price/volume and shrinking sell reactions imply potential absorption; OHLCV cannot prove inventory accumulation.",
        absorption_proxy=p("absorption"),
    )
    support = p("avwap_support")
    reclaim = p("avwap_reclaim")
    avwap = None if support is None and reclaim is None else support is True or reclaim is True
    add(
        "accepted_avwap_structure",
        ("ZEC",),
        4,
        avwap,
        "Confirmed support or accepted reclaim of an already available anchored VWAP.",
        avwap_support=support,
        avwap_reclaim=reclaim,
    )
    spot_rv = m("relative_volume")
    extension = m("extension_atr")
    ignition = (
        None
        if spot_rv is None or extension is None or p("demand_expansion") is None
        else (
            spot_rv >= 1.5
            and p("demand_expansion") is True
            and extension <= 2
            and t is not None
            and bounded_base(t)
        )
    )
    add(
        "spot_ignition",
        ("QNT", "ZEC"),
        5,
        ignition,
        "Abnormal completed-candle spot demand near multiweek base resistance before overextension.",
        relative_volume=spot_rv,
        extension_atr=extension,
    )
    eligible = [c for c in snapshot.catalysts if _eligible_catalyst(c, snapshot)]

    def catalyst_rule(name: str, archetype: str, weight: float, categories: set[str], text: str) -> None:
        found = [c for c in eligible if c.category in categories]
        reviewed = [
            c for c in snapshot.catalysts if c.available_at <= snapshot.as_of and c.category in categories
        ]
        add(
            name,
            (archetype,),
            weight,
            bool(found) if reviewed else None,
            text,
            eligible_sources=float(len(found)),
            categories=",".join(sorted(categories)),
        )

    catalyst_rule(
        "quantitative_adoption",
        "TAO",
        5,
        {"network_usage", "adoption"},
        "Verified sourced quantitative network/adoption evidence, dated within 180 days.",
    )
    catalyst_rule(
        "supply_scarcity",
        "TAO",
        3,
        {"scarcity"},
        "Verified quantitative supply/issuance evidence; social scarcity slogans earn no points.",
    )
    catalyst_rule(
        "rwa_adoption",
        "ONDO",
        4,
        {"rwa", "infrastructure"},
        "Verified quantified RWA/infrastructure usage; partnership announcements alone earn no points.",
    )
    catalyst_rule(
        "institutional_inflows",
        "ONDO",
        3,
        {"institutional", "inflows"},
        "Verified institutional adoption or realized capital inflow evidence.",
    )
    archetypes: dict[str, float] = {}
    for archetype in ("QNT", "ZEC", "TAO", "ONDO"):
        relevant = [e for e in evidence if archetype in memberships[e.rule]]
        archetypes[archetype] = round(
            100 * sum(e.points for e in relevant) / sum(e.weight for e in relevant), 2
        )
    # A hybrid earns five points only with distinct nonoverlapping evidence in two
    # archetypes; a shared compression/test/strength observation cannot earn it.
    independently_qualified = []
    for archetype in archetypes:
        exclusive = [e for e in evidence if memberships[e.rule] == (archetype,) and e.passed is True]
        if archetypes[archetype] >= 60 and len(exclusive) >= 2:
            independently_qualified.append(archetype)
    hybrid = 5 if len(independently_qualified) >= 2 else 0
    evidence.append(
        RuleEvidence(
            rule="independent_hybrid",
            archetype="HYBRID",
            passed=bool(hybrid),
            weight=5,
            points=hybrid,
            detail="Capped bonus requires two archetypes with independent exclusive observations.",
            inputs={"qualified": ",".join(independently_qualified)},
        )
    )
    score = sum(e.points for e in evidence)
    stage = _stage(snapshot, technical)
    if extension is not None and extension > 2:
        penalty = min(60, 15 * (extension - 2))
        score -= penalty
        warnings.append(
            f"Extension penalty {penalty:.1f}: the scanner favors structures before vertical moves."
        )
    if stage == Stage.EXPANSION:
        score = min(score, 35)
        warnings.append("Expansion has already occurred; this is not an early candidate.")
    if p("weak_advance") is True:
        score -= 8
        warnings.append("Price rises on weakening volume; demand confirmation is weak.")
    futures_ratio = metric_value(snapshot, "futures_spot_volume_ratio", settings, True)
    oi = metric_value(snapshot, "open_interest_change_pct", settings, True)
    funding = metric_value(snapshot, "funding_rate", settings, True)
    normalized_funding = funding_8h_equivalent(snapshot, settings)
    if futures_ratio is None:
        missing.append("matched_spot_futures_volume_ratio")
    elif futures_ratio > 5 and (spot_rv is None or spot_rv < 1.5):
        score -= 10
        warnings.append("Futures volume dominates without abnormal spot demand; leverage-driven warning.")
    if oi is None:
        missing.append("open_interest_change_pct")
    if funding is None:
        missing.append("funding_rate")
    if normalized_funding is None:
        missing.append("funding_interval_hours_for_normalization")
    if oi is not None and normalized_funding is not None and oi >= 0.1 and abs(normalized_funding) >= 0.0005:
        score -= 8
        warnings.append("Rising open interest and extreme funding raise liquidation risk.")
    if regime.regime in {Regime.SHAKEOUT, Regime.RISK_OFF}:
        warnings.append(
            "Market weakness restricts entry; matched relative strength remains separate evidence."
        )
    known_rule_weight = sum(
        e.weight for e in evidence if e.rule != "independent_hybrid" and e.passed is not None
    )
    derivative_coverage = sum(v is not None for v in (oi, normalized_funding, futures_ratio)) / 3
    confidence = 0.85 * known_rule_weight / 95 + 0.10 * derivative_coverage + 0.05 * regime.confidence
    if not ready:
        confidence = min(confidence, 0.2)
        missing.append("fresh_complete_primary_4h_daily_history")
    score = round(max(0, min(100, score)), 2)
    confidence = round(max(0, min(1, confidence)), 4)
    trade = trade_plan(snapshot, technical, stage, score, confidence, regime, settings, portfolio)
    return ScanResult(
        asset=snapshot.asset,
        as_of=snapshot.as_of,
        stage=stage,
        expansion_score=score,
        confidence=confidence,
        archetypes=archetypes,
        evidence=evidence,
        technical=technical,
        regime=regime,
        trade=trade,
        market=snapshot.market,
        derivatives=snapshot.derivatives,
        warnings=warnings,
        missing=sorted(set(missing)),
    )
