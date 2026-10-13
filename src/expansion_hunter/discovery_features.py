"""Causal, deterministic Phase 1 discovery features. No fitted probabilities.

Broad returns use four completed light candles; RVOL divides the last candle by
the preceding 20. Acceleration compares consecutive three-candle mean volumes.
Compression compares five-candle mean ranges with the preceding 20. Historical
percentiles are midranks against *earlier* valid values of the same formula.

Each score is 100 times a weighted observed-component mean. Coverage counts
observed leaf weights, including missing leaves inside nested scores. Bounds
assign missing leaves zero/one; neither the score nor bounds are probabilities.
Weights and deterministic clipping transforms are UNCALIBRATED hypotheses.

Resistance episodes share a level frozen from a preceding reference window.
Rejections settle only after a fixed reaction horizon, never using a future low.
OHLCV profiles remain approximations of historical traded volume, not order-book
liquidity. Missing aggressor activity, order books or macro data stay unknown.
"""

from __future__ import annotations

import math
from datetime import timedelta

import numpy as np

from .discovery_config import DiscoverySettings
from .discovery_models import (
    DiscoveryFeature,
    DiscoveryObservation,
    DiscoveryScore,
    Freshness,
    ScoreComponent,
)
from .models import TIMEFRAMES, AssetSnapshot, Candle, Metric, Regime, RegimeReport, Status
from .technical import _ema, _volume_profile, analyze, confirmed_swings


def _clip(value: float | None) -> float | None:
    return float(np.clip(value, 0, 1)) if value is not None and math.isfinite(value) else None


def _bars(snapshot: AssetSnapshot, frame: str) -> tuple[list[Candle], str | None]:
    bars = sorted(
        (b for b in snapshot.candles.get(frame, [])
         if b.close_time <= snapshot.as_of and b.available_at <= snapshot.as_of),
        key=lambda b: b.open_time,
    )
    seconds = TIMEFRAMES[frame]
    if not bars:
        return [], "no closed, available candles"
    if len({b.open_time for b in bars}) != len(bars):
        return [], "duplicate candle timestamps"
    if len({b.source for b in bars}) != 1:
        return [], "mixed candle providers"
    if any(abs((b.close_time - b.open_time).total_seconds() - seconds) > 1.01 for b in bars):
        return [], "candle duration does not match timeframe"
    if any((b.open_time - a.open_time).total_seconds() != seconds for a, b in zip(bars, bars[1:], strict=False)):
        return [], "noncontiguous candle history"
    return bars, None


def _freshness(age: float, frame: str, settings: DiscoverySettings) -> Freshness:
    if frame in TIMEFRAMES:
        fresh = TIMEFRAMES[frame] * getattr(settings, "feature_fresh_frame_multiple", 2) + 60
        degraded = TIMEFRAMES[frame] * getattr(settings, "feature_degraded_frame_multiple", 4) + 60
    else:
        fresh = getattr(settings, "market_fresh_seconds", 300)
        degraded = getattr(settings, "market_degraded_seconds", 900)
    return Freshness.FRESH if age <= fresh else Freshness.DEGRADED if age <= degraded else Freshness.STALE


def _historical_percentile(value: float | None, prior: list[float], minimum: int) -> float | None:
    clean = [v for v in prior if math.isfinite(v)]
    if value is None or len(clean) < minimum:
        return None
    return (sum(v < value for v in clean) + 0.5 * sum(v == value for v in clean)) / len(clean)


def _feature(
    snapshot: AssetSnapshot, settings: DiscoverySettings, frame: str, bars: list[Candle],
    value: float | None, unit: str, prior: list[float] | None = None,
    direction: int = 1, critical: bool = False, reason: str | None = None,
) -> DiscoveryFeature:
    if value is None or not math.isfinite(value) or not bars:
        return DiscoveryFeature(provider=bars[-1].source if bars else "unavailable", timeframe=frame,
                                unit=unit, direction=direction, critical=critical,
                                reason=reason or "insufficient causal history or nonpositive denominator")
    age = max(0.0, (snapshot.as_of - bars[-1].close_time).total_seconds())
    return DiscoveryFeature(
        value=value, unit=unit, source_timestamp=bars[-1].close_time,
        retrieved_at=max(b.available_at for b in bars), age_seconds=age,
        provider=bars[-1].source, timeframe=frame, freshness=_freshness(age, frame, settings),
        historical_percentile=_historical_percentile(value, prior or [], settings.minimum_history_samples),
        sample_count=len(prior or []), direction=direction, critical=critical, reason=reason,
    )


def _metric_feature(
    snapshot: AssetSnapshot, settings: DiscoverySettings, metric: Metric | None,
    unit: str = "", direction: int = 1, reason: str | None = None,
) -> DiscoveryFeature:
    if (metric is None or metric.status not in {Status.AVAILABLE, Status.STALE} or metric.value is None or not metric.reliable
            or metric.available_at is None or metric.observed_at is None
            or metric.available_at > snapshot.as_of or metric.observed_at > snapshot.as_of):
        return DiscoveryFeature(provider=metric.source if metric else "unavailable", timeframe="snapshot",
                                unit=unit, direction=direction,
                                reason=reason or (metric.reason if metric else "metric unavailable"))
    age = (snapshot.as_of - metric.observed_at).total_seconds()
    freshness = _freshness(age, "snapshot", settings)
    if metric.status == Status.STALE:
        freshness = Freshness.STALE
    return DiscoveryFeature(value=metric.value, unit=unit or metric.unit, provider=metric.source,
                            timeframe="snapshot", source_timestamp=metric.observed_at,
                            retrieved_at=metric.available_at, age_seconds=age,
                            freshness=freshness, direction=direction)


def _light_series(bars: list[Candle]) -> dict[str, list[float | None]]:
    """Small-window broad features: no daily listing-age or EMA200 requirement."""
    close = np.array([b.close for b in bars])
    high = np.array([b.high for b in bars])
    low = np.array([b.low for b in bars])
    volume = np.array([b.volume for b in bars])
    ranges = (high - low) / close
    emas = [_ema(close, p) for p in (10, 20, 50)]
    names = ("price_change", "rvol", "volume_acceleration", "volatility", "range_compression",
             "distance_recent_high", "distance_recent_low", "volume_contraction", "range_position", "ema_convergence",
             "resistance_pressure", "volume_slope", "bb_width")
    output: dict[str, list[float | None]] = {name: [] for name in names}
    for i in range(len(bars)):
        previous20 = float(np.mean(volume[i - 20:i])) if i >= 20 else 0
        previous3 = float(np.mean(volume[i - 5:i - 2])) if i >= 5 else 0
        range_baseline = float(np.mean(ranges[i - 24:i - 4])) if i >= 24 else 0
        high32 = float(np.max(high[i - 32:i])) if i >= 32 else None
        low32 = float(np.min(low[i - 32:i])) if i >= 32 else None
        width = high32 - low32 if high32 is not None else 0
        contraction_base = float(np.mean(volume[i - 24:i - 4])) if i >= 24 else 0
        mean6 = float(np.mean(volume[i - 5:i + 1])) if i >= 5 else 0
        emavalues = [float(e[i]) for e in emas]
        values = {
            "price_change": 100 * (close[i] / close[i - 4] - 1) if i >= 4 else None,
            "rvol": float(volume[i] / previous20) if previous20 > 0 else None,
            "volume_acceleration": float(np.mean(volume[i - 2:i + 1]) / previous3 - 1)
            if previous3 > 0 else None,
            "volatility": float(np.std(np.diff(np.log(close[i - 20:i + 1])), ddof=1)) if i >= 20 else None,
            "range_compression": float(np.mean(ranges[i - 4:i + 1]) / range_baseline)
            if range_baseline > 0 else None,
            "distance_recent_high": 100 * (high32 / close[i] - 1) if high32 else None,
            "distance_recent_low": 100 * (close[i] / low32 - 1) if low32 else None,
            "volume_contraction": float(np.mean(volume[i - 4:i + 1]) / contraction_base)
            if contraction_base > 0 else None,
            "range_position": _clip(float((close[i] - low32) / width)) if width > 0 else None,
            "ema_convergence": (max(emavalues) - min(emavalues)) / close[i]
            if all(math.isfinite(e) for e in emavalues) else None,
            "resistance_pressure": float(np.mean(close[i - 7:i + 1] >= low32 + 0.75 * width))
            if i >= 32 and width > 0 else None,
            "volume_slope": float(np.polyfit(np.arange(6), volume[i - 5:i + 1], 1)[0] / mean6)
            if mean6 > 0 else None,
            "bb_width": float(4 * np.std(close[i - 19:i + 1]) / np.mean(close[i - 19:i + 1]))
            if i >= 19 else None,
        }
        for name, value in values.items():
            output[name].append(value)
    return output


def _usable(feature: DiscoveryFeature | None) -> float | None:
    return feature.value if feature and feature.freshness in {Freshness.FRESH, Freshness.DEGRADED} else None


def light_features(
    snapshot: AssetSnapshot, settings: DiscoverySettings, benchmark: AssetSnapshot | None = None,
    historical: list[DiscoveryObservation] | None = None,
) -> dict[str, DiscoveryFeature]:
    frame = settings.light_timeframe
    bars, issue = _bars(snapshot, frame)
    series = _light_series(bars)
    units = {"price_change": "percentage points over 4 bars", "rvol": "multiple of preceding 20 bars",
             "distance_recent_high": "percentage points to prior 32-bar high",
             "distance_recent_low": "percentage points from prior 32-bar low",
             "volatility": "log-return standard deviation per bar", "volume_slope": "fraction per bar"}
    lower_better = {"volatility", "range_compression", "volume_contraction", "ema_convergence",
                    "distance_recent_high", "distance_recent_low", "bb_width"}
    result = {
        name: _feature(snapshot, settings, frame, bars, values[-1] if values else None,
                       units.get(name, "fraction"), [float(v) for v in values[:-1] if v is not None],
                       -1 if name in lower_better else 1, name in {"price_change", "rvol"}, issue)
        for name, values in series.items()
    }
    result["relative_strength_market"] = DiscoveryFeature(
        provider="universe matched endpoints", timeframe=frame,
        unit="percentage points excess over 4 bars", reason="requires aligned cross-sectional market basket")
    matched = []
    if benchmark is not None and bars:
        benchmark_bars, _ = _bars(benchmark.model_copy(update={"as_of": min(snapshot.as_of, benchmark.as_of)}), frame)
        left = {b.close_time: b for b in bars}
        right = {b.close_time: b for b in benchmark_bars}
        for endpoint in sorted(left.keys() & right.keys()):
            start = endpoint - timedelta(seconds=TIMEFRAMES[frame] * 4)
            if start in left and start in right:
                matched.append((endpoint, 100 * (left[endpoint].close / left[start].close
                                                 - right[endpoint].close / right[start].close)))
        # Do not quietly use yesterday's matching benchmark for today's price.
        if matched and matched[-1][0] != bars[-1].close_time:
            matched = []
    result["relative_strength_btc"] = _feature(
        snapshot, settings, frame, bars, matched[-1][1] if matched else None,
        "percentage points excess over 4 bars", [v for _, v in matched[:-1]],
        reason=None if matched else "BTC needs identical current endpoint and four-bar start")
    if matched and benchmark is not None:
        result["relative_strength_btc"].retrieved_at = max(
            result["relative_strength_btc"].retrieved_at,
            max(b.available_at for b in benchmark_bars),
        )
        result["relative_strength_btc"].provider += "; BTC=" + benchmark_bars[-1].source
    cap = snapshot.market.get("market_cap")
    volume = snapshot.market.get("volume_24h")
    cap_feature = _metric_feature(snapshot, settings, cap)
    volume_feature = _metric_feature(snapshot, settings, volume)
    valid_turnover = (cap and volume and "USD" in cap.unit and volume.unit.startswith("USD")
                      and cap.source.startswith("coingecko") and volume.source.startswith("coingecko")
                      and cap.observed_at == volume.observed_at
                      and cap_feature.value is not None and volume_feature.value is not None
                      and cap.value > 0)
    turnover_metric = None
    if valid_turnover:
        turnover_metric = Metric(value=volume.value / cap.value, observed_at=cap.observed_at,
                                 available_at=max(cap.available_at, volume.available_at),
                                 source="coingecko/aggregated_USD_volume/dated_USD_market_cap",
                                 unit="fraction per 24h; reported aggregate volume", status=Status.AVAILABLE)
    result["turnover"] = _metric_feature(snapshot, settings, turnover_metric,
                                        reason="requires aligned dated CoinGecko USD cap and aggregate volume")
    spread = _metric_feature(snapshot, settings, snapshot.market.get("spread_bps"), direction=-1)
    result["spread_bps"] = spread
    quote = snapshot.market.get("quote_volume_24h")
    quote_feature = _metric_feature(snapshot, settings, quote)
    result["quote_volume_24h"] = quote_feature
    # Volume is contextual: a USD reported aggregate is not a native USDT book.
    available_liquidity = []
    if _usable(spread) is not None:
        available_liquidity.append(_clip(1 - spread.value / settings.max_spread_bps))
    if _usable(quote_feature) is not None and quote.value >= 0:
        available_liquidity.append(_clip(quote.value / (2 * settings.min_quote_volume_24h))
                                   if settings.min_quote_volume_24h > 0 else 1)
    if available_liquidity:
        source_feature = spread if _usable(spread) is not None else quote_feature
        contributing = [f for f in (spread, quote_feature) if _usable(f) is not None]
        result["liquidity_quality"] = source_feature.model_copy(update={
            "value": 100 * sum(available_liquidity) / 2, "direction": 1,
            "unit": "uncalibrated 0-100 conservative observed spread/native volume proxy",
            "provider": "; ".join(f.provider for f in contributing),
            "source_timestamp": min(f.source_timestamp for f in contributing),
            "retrieved_at": max(f.retrieved_at for f in contributing),
            "age_seconds": max(f.age_seconds for f in contributing),
            "freshness": Freshness.DEGRADED if any(f.freshness == Freshness.DEGRADED for f in contributing) else Freshness.FRESH,
            "reason": f"{len(available_liquidity)}/2 observed broad inputs; conservative lower bound, not imputed spread/depth",
        })
    else:
        result["liquidity_quality"] = DiscoveryFeature(
            provider="unavailable", timeframe="snapshot", unit="uncalibrated 0-100",
            reason="requires dated native quote activity or spread; no assumed depth")
    for name, feature in result.items():
        if feature.historical_percentile is not None or feature.value is None:
            continue
        versions = {}
        for observation in sorted(historical or [], key=lambda o: o.as_of):
            old = observation.features.get(name)
            if (observation.as_of < snapshot.as_of and observation.asset.asset_id == snapshot.asset.asset_id
                    and old and _usable(old) is not None and old.source_timestamp and old.retrieved_at
                    and old.source_timestamp <= observation.as_of and old.retrieved_at <= observation.as_of
                    and old.source_timestamp < feature.source_timestamp
                    and old.provider == feature.provider and old.timeframe == feature.timeframe and old.unit == feature.unit):
                # Repeated polling is one source event, not multiple baseline samples.
                versions[old.source_timestamp] = old.value
        prior = list(versions.values())
        feature.historical_percentile = _historical_percentile(feature.value, prior, settings.minimum_history_samples)
        feature.sample_count = len(prior)
    result["relative_volume"] = result["rvol"].model_copy()
    return result


def _score(entries: list[tuple], settings: DiscoverySettings, name: str,
           minimum: float | None = None) -> DiscoveryScore:
    """Entry: name, nominal weight, normalized/child score, reason, inputs."""
    configured = getattr(settings, "score_weights", {}).get(name, {})
    unknown = set(configured) - {entry[0] for entry in entries}
    if unknown:
        raise ValueError(f"unknown {name} score components: {sorted(unknown)}")
    components = []
    total = known = numerator = 0.0
    for component_name, weight, value, reason, inputs in entries:
        weight = configured.get(component_name, weight)
        if weight <= 0:
            continue
        coverage = value.coverage if isinstance(value, DiscoveryScore) and value.value is not None else 1
        normalized = value.value / 100 if isinstance(value, DiscoveryScore) and value.value is not None else (
            None if isinstance(value, DiscoveryScore) else _clip(value))
        total += weight
        effective_weight = weight * coverage if normalized is not None else 0
        known += effective_weight
        numerator += effective_weight * (normalized or 0)
        components.append(ScoreComponent(name=component_name, weight=weight, normalized=normalized,
                                        reason=reason, inputs={**inputs, "observed_leaf_coverage":
                                                              coverage if normalized is not None else 0}))
    coverage = known / total if total else 0
    threshold = settings.minimum_score_coverage if minimum is None else minimum
    value = 100 * numerator / known if known and coverage >= threshold else None
    for component in components:
        if component.normalized is not None and known:
            component.contribution = 100 * component.weight * component.inputs["observed_leaf_coverage"] * component.normalized / known
    lower = 100 * numerator / total if total else 0
    return DiscoveryScore(value=value, coverage=coverage, lower_bound=lower,
                          upper_bound=min(100, lower + 100 * (1 - coverage)), components=components)


def _entry(name: str, weight: float, value, reason: str, **inputs) -> tuple:
    return name, weight, value, reason, inputs


def _structures(bars: list[Candle], settings: DiscoverySettings) -> dict[str, float | None]:
    output = dict.fromkeys(("resistance", "support", "tests", "rejection_decay", "higher_lows",
                            "shallowness", "recovery_improvement", "sell_volume_decay",
                            "activity_efficiency", "latest_rejection"))
    if len(bars) < 48:
        return output
    window = bars[-128:]
    reference_end = min(32, len(window) // 2)
    reference = window[:reference_end]
    resistance, support = max(b.high for b in reference), min(b.low for b in reference)
    tolerance = max(resistance * 0.003, 0.5 * float(np.mean([b.high - b.low for b in reference])))
    reaction = settings.resistance_reaction_bars
    episodes = []
    previous_touch = -reaction - 1
    previously_near = False
    for i in range(reference_end, len(window)):
        near = window[i].high >= resistance - tolerance
        if near and not previously_near and i - previous_touch > reaction:
            previous_touch = i
            if i + reaction < len(window):
                post = window[i + 1:i + reaction + 1]
                low = min(b.low for b in post)
                rejected = max(0, (resistance - low) / resistance)
                displaced = next((j for j, b in enumerate(post) if b.close < resistance - tolerance), None)
                recovery = next((j - displaced for j, b in enumerate(post)
                                 if displaced is not None and j > displaced and b.close >= resistance - tolerance), None)
                sell = sum(b.volume - b.taker_buy_volume for b in post) if all(
                    b.taker_buy_volume is not None for b in post) else None
                activity = sum(b.volume for b in post)
                episodes.append({"rejection": rejected, "low": low, "recovery": recovery,
                                 "sell": sell, "activity": activity})
        previously_near = near
    output.update(resistance=resistance, support=support, tests=float(len(episodes)))
    pivots = [p["price"] for p in confirmed_swings(window) if p["kind"] == "low"][-3:]
    if len(pivots) == 3:
        output["higher_lows"] = float(sum(b > a for a, b in zip(pivots, pivots[1:], strict=False)) / 2)
    if len(episodes) >= 2:
        output["shallowness"] = _clip(1 - np.mean([e["rejection"] for e in episodes[-2:]]) / 0.1)
        output["latest_rejection"] = episodes[-1]["rejection"]
    if len(episodes) < 3:
        return output
    first, last = episodes[-3], episodes[-1]
    output["rejection_decay"] = _clip(1 - last["rejection"] / first["rejection"]) if first["rejection"] > 0 else 0
    lows = [e["low"] for e in episodes[-3:]]
    output["higher_lows"] = sum(b > a for a, b in zip(lows, lows[1:], strict=False)) / 2
    if first["recovery"] is not None and last["recovery"] is not None:
        output["recovery_improvement"] = _clip(1 - last["recovery"] / max(1, first["recovery"]))
    if first["sell"] is not None and last["sell"] is not None and first["sell"] > 0:
        output["sell_volume_decay"] = _clip(1 - last["sell"] / first["sell"])
    if first["activity"] > 0 and last["activity"] > 0 and first["rejection"] > 0:
        old = first["rejection"] / first["activity"]
        new = last["rejection"] / last["activity"]
        output["activity_efficiency"] = _clip(1 - new / old)
    return output


def volume_awakening(snapshot: AssetSnapshot, settings: DiscoverySettings) -> DiscoveryScore:
    entries = []
    for frame in ("1m", "5m", "15m", "1h"):
        bars, issue = _bars(snapshot, frame)
        valid = bars and _freshness((snapshot.as_of - bars[-1].close_time).total_seconds(), frame, settings) != Freshness.STALE
        series = _light_series(bars) if valid else {}
        rvol = series.get("rvol", [None])[-1]
        acceleration = series.get("volume_acceleration", [None])[-1]
        slope = series.get("volume_slope", [None])[-1]
        quiet = None
        if valid and len(bars) >= 40:
            baseline = np.mean([b.volume for b in bars[-40:-10]])
            if baseline > 0:
                quiet = _clip(1 - np.mean([b.volume for b in bars[-10:-3]]) / baseline)
        child = _score([
            _entry("relative_volume", .4, _clip((rvol - 1) / 2) if rvol is not None else None,
                   "RVOL 1→3 maps 0→1", rvol=rvol),
            _entry("acceleration", .3, _clip(acceleration) if acceleration is not None else None,
                   "three-bar mean volume increase 0→100%", acceleration=acceleration),
            _entry("slope", .2, _clip(slope * 3) if slope is not None else None,
                   "positive six-bar normalized slope", slope=slope),
            _entry("prior_quiet", .1, quiet, "pre-awakening dry-up, excluding latest three bars"),
        ], settings, "volume_awakening_timeframe", minimum=.5)
        entries.append(_entry(frame, .25, child, issue or "independent closed spot timeframe; no huge price candle required"))
    return _score(entries, settings, "volume_awakening", minimum=.25)


def deep_scores(
    snapshot: AssetSnapshot, settings: DiscoverySettings, benchmark: AssetSnapshot | None = None,
    regime: RegimeReport | None = None,
    *, bias: str = "LONG",
) -> tuple[dict[str, DiscoveryScore], dict[str, float | None]]:
    """Legacy LONG scores; unsupported directional formulas stay unavailable.

    TWO-sided broad ranking does not make the LONG absorption/VWAP/overhead
    formulas valid for a SHORT. Direction-neutral activity and actual chart
    levels remain usable. The independent fast engine can evaluate native
    short pressure while these deeper directional hypotheses are unavailable.
    """
    if bias not in {"LONG", "SHORT", "NEUTRAL", "UNAVAILABLE"}:
        raise ValueError("unsupported discovery score bias")
    chosen, bars = "4h", []
    for frame in ("4h", "1h", "15m", "5m"):
        candidate, _ = _bars(snapshot, frame)
        if len(candidate) >= 32:
            chosen, bars = frame, candidate
            break
    local_settings = settings.model_copy(update={"light_timeframe": chosen})
    features = light_features(snapshot, local_settings, benchmark)
    fresh = bool(bars and _freshness((snapshot.as_of - bars[-1].close_time).total_seconds(), chosen, settings) != Freshness.STALE)
    structure = _structures(bars, settings) if fresh else _structures([], settings)
    technical = analyze(bars, chosen, snapshot.as_of) if fresh else None
    valid_anchors = [a for a in technical.anchors if a.value is not None] if technical else []
    price = bars[-1].close if fresh else None
    vwap = technical.metrics.get("vwap") if technical else None
    vwap_score = _score([
        _entry("session_acceptance", .4, float(price > vwap) if vwap and price else None,
               "complete UTC session VWAP, native quote/base or labelled typical-price estimate"),
        _entry("avwap_support", .35, float(any(a.support or a.reclaim for a in valid_anchors)) if valid_anchors else None,
               "existing causal anchor support/reclaim; two accepted closes required"),
        _entry("avwap_position", .25, float(sum(price >= a.value for a in valid_anchors) / len(valid_anchors))
               if valid_anchors and price else None, "share of available AVWAPs below price"),
    ], settings, "vwap_structure")
    rs_feature = features["relative_strength_btc"]
    rs = _usable(rs_feature)
    rs_score = _score([
        _entry("BTC", .5, _clip(.5 + rs / 4) if rs is not None else None,
               "matched four-bar excess return; ±2 percentage points maps 0→1", excess_return=rs),
        _entry("market", .5, None, "deep market RS requires archived matched market benchmark"),
    ], settings, "relative_strength", minimum=.5)
    tests = structure["tests"]
    absorption = _score([
        _entry("repeated_tests", .15, _clip((tests - 1) / 4) if tests is not None and tests >= 3 else None,
               "three or more settled episodes at one previously known zone", settled_tests=tests),
        _entry("rejection_decay", .25, structure["rejection_decay"], "decreasing downside in fixed reaction windows"),
        _entry("higher_lows", .2, structure["higher_lows"] if tests is not None and tests >= 3 else None,
               "settled rejection lows rise"),
        _entry("recovery_speed", .15, structure["recovery_improvement"], "actual observed recovery; censored recovery remains unknown"),
        _entry("sell_volume_decay", .1, structure["sell_volume_decay"], "native taker sell volume; candle colour is not aggressor volume"),
        _entry("displacement_per_activity", .15, structure["activity_efficiency"], "less downside per base-volume unit at the same zone"),
    ], settings, "absorption")
    compression_parts = []
    for name in ("volatility", "range_compression", "bb_width"):
        feature = features[name]
        normalized = 1 - feature.historical_percentile if _usable(feature) is not None and feature.historical_percentile is not None else None
        compression_parts.append(_entry(name, 1 / 3, normalized, "inverse earlier-self midrank", historical_percentile=feature.historical_percentile))
    compression = _score(compression_parts, settings, "compression")
    contraction = _usable(features["volume_contraction"])
    acceptance = _usable(features["range_position"])
    pressure = _usable(features["resistance_pressure"])
    structural_score = _score([
        _entry("higher_lows", .4, structure["higher_lows"], "confirmed or settled rising lows"),
        _entry("shallow_pullbacks", .3, structure["shallowness"], "mean last settled rejections 0→10% maps 1→0"),
        _entry("resistance_pressure", .3, pressure, "fraction of eight closes in upper reference quartile"),
    ], settings, "structural_pressure")
    ema = features["ema_convergence"]
    ema_compression = 1 - ema.historical_percentile if _usable(ema) is not None and ema.historical_percentile is not None else None
    profile = _volume_profile(bars[-128:]) if fresh else {}
    poc, vah, val = profile.get("poc"), profile.get("vah"), profile.get("val")
    profile_score = _score([
        _entry("above_poc", .5, float(price >= poc) if price and poc else None, "OHLCV profile approximation; not observed resting depth"),
        _entry("vah_pressure", .5, _clip(1 - abs(vah / price - 1) / .05) if price and vah else None,
               "near historical value-area high; single-window approximation"),
    ], settings, "volume_profile")
    stored = _score([
        _entry("volatility_compression", .2, compression, "self-ranked realized/candle/Bollinger compression"),
        _entry("volume_dry_up", .1, _clip(1 - contraction / 1.5) if contraction is not None else None,
               "recent baseline volume contraction; does not infer selling", ratio=contraction),
        _entry("structure", .2, structural_score, "confirmed higher lows, settled pullbacks and pressure"),
        _entry("range_acceptance", .1, acceptance, "current close within prior range"),
        _entry("trend_compression", .1, ema_compression, "EMA10/20/50 inverse self-history midrank"),
        _entry("relative_strength", .1, rs_score, "observed BTC benchmark; missing broad benchmark reserved"),
        _entry("vwap_structure", .1, vwap_score, "causal acceptance and anchors"),
        _entry("profile_structure", .1, profile_score, "historical OHLCV approximation; no POC migration claim"),
    ], settings, "stored_energy")
    liquidity = _score([
        _entry("spread", .4, _clip(1 - _usable(features["spread_bps"]) / settings.max_spread_bps)
               if _usable(features["spread_bps"]) is not None else None, "fresh observed spread"),
        _entry("native_activity", .3, _clip(_usable(features["quote_volume_24h"]) / (2 * settings.min_quote_volume_24h))
               if _usable(features["quote_volume_24h"]) is not None and settings.min_quote_volume_24h > 0 else None,
               "native quote 24h activity, never USD/USDT-converted market cap"),
        _entry("two_sided_depth", .3, _clip(_usable(_metric_feature(snapshot, settings, snapshot.market.get("depth_usd"))) / 100_000)
               if _usable(_metric_feature(snapshot, settings, snapshot.market.get("depth_usd"))) is not None else None,
               "existing observed ±1% minimum bid/ask depth; native quote despite legacy field name"),
    ], settings, "liquidity_quality")
    resistance = structure["resistance"]
    distance = (resistance / price - 1) * 100 if resistance and price else None
    lvn = min((v for v in profile.get("lvn", []) if price and v > price), default=None)
    hvn = min((v for v in profile.get("hvn", []) if price and v > price), default=None)
    overhead = None
    if resistance and bars and resistance < max(b.high for b in bars[-128:]):
        overhead = float(lvn is not None and lvn > resistance and (hvn is None or lvn < hvn))
    rejection = structure["latest_rejection"]
    trigger = _score([
        _entry("distance_resistance", .2, _clip(1 - distance / 5) if distance is not None and distance >= 0 else None,
               "positive distance to a previously frozen resistance zone", distance_pct=distance),
        _entry("settled_tests", .15, _clip(tests / 5) if tests is not None else None, "repeated settled zone tests", tests=tests),
        _entry("recent_rejection", .15, _clip(1 - rejection / .1) if rejection is not None else None,
               "latest fully settled downside 0→10% maps 1→0"),
        _entry("overhead_profile", .15, overhead, "LVN before HVN inside observed support; OHLCV proxy only", lvn_above=lvn, hvn_above=hvn),
        _entry("compression", .15, compression, "earlier-self compressed behavior"),
        _entry("execution_liquidity", .1, liquidity, "adequate liquidity; thin manipulated books earn no bonus"),
        _entry("observed_book_mobility", .1, None,
               "not inferred from historical profile or OI; native demand-scaled mobility not yet available"),
    ], settings, "trigger_sensitivity")
    rvol = _usable(features["rvol"])
    acceleration = _usable(features["volume_acceleration"])
    volume_structure = _score([
        _entry("gradual_demand", .5, _clip((rvol - .5) / 2) if rvol is not None else None,
               "bounded activity evidence; high volume alone does not establish demand", rvol=rvol),
        _entry("volume_acceleration", .5, _clip(.5 + acceleration / 2) if acceleration is not None else None,
               "three-bar activity growth, price interaction explained separately", acceleration=acceleration),
    ], settings, "volume_structure")
    regime_value = None
    if regime and regime.as_of and regime.available_at and regime.available_at <= snapshot.as_of and regime.as_of <= snapshot.as_of:
        if (snapshot.as_of - regime.as_of).total_seconds() <= getattr(settings, "market_degraded_seconds", 900):
            regime_value = {Regime.RISK_ON: 1, Regime.ALT_EXPANSION: 1, Regime.ALT_ROTATION: .9,
                            Regime.BTC_EXPANSION: .6, Regime.NEUTRAL: .5,
                            Regime.RISK_OFF: .1, Regime.SHAKEOUT: 0}[regime.regime] if regime.confidence > 0 else None
    readiness = _score([
        _entry("stored_energy", .25, stored, "deterministic structural score"),
        _entry("absorption", .15, absorption, "insufficient settled episodes remain unknown"),
        _entry("trigger_sensitivity", .15, trigger, "structural expansion mobility proxy"),
        _entry("relative_strength", .1, rs_score, "matched benchmark excess return"),
        _entry("volume_behavior", .1, volume_structure, "observed bounded activity"),
        _entry("vwap_structure", .075, vwap_score, "causal VWAP/AVWAP evidence"),
        _entry("volume_profile", .075, profile_score, "explicit OHLCV approximation"),
        _entry("liquidity_quality", .05, liquidity, "actual dated native quote liquidity evidence"),
        _entry("market_regime", .05, regime_value, "known dated regime; absence does not imply safety"),
    ], settings, "expansion_readiness")
    awakening = volume_awakening(snapshot, settings)
    scores = {"stored_energy": stored, "absorption": absorption, "trigger_sensitivity": trigger,
              "expansion_readiness": readiness, "volume_awakening": awakening,
              "relative_strength": rs_score, "volume_structure": volume_structure,
              "vwap_structure": vwap_score, "liquidity_quality": liquidity}
    levels = {"price": price, "resistance": resistance, "support": structure["support"],
              "vwap": vwap, "avwap": valid_anchors[-1].value if valid_anchors else None,
              "poc": poc, "vah": vah, "val": val, "lvn_above": lvn, "hvn_above": hvn,
              "distance_resistance_pct": distance, "resistance_tests": tests,
              "rvol": rvol, "profile_approximate": 1.0,
              "atr": technical.metrics.get("atr14") if technical else None,
              "ema20": technical.metrics.get("ema20") if technical else None,
              "extension_atr": technical.metrics.get("extension_atr") if technical else None}
    for frame in ("1m", "5m", "15m", "1h"):
        frame_bars, _ = _bars(snapshot, frame)
        frame_fresh = bool(frame_bars and _freshness((snapshot.as_of - frame_bars[-1].close_time).total_seconds(), frame, settings) != Freshness.STALE)
        frame_series = _light_series(frame_bars) if frame_fresh else {}
        levels[f"rvol_{frame}"] = frame_series["rvol"][-1] if frame_fresh else None
        levels[f"micro_rvol_{frame}"] = levels[f"rvol_{frame}"]
    if bias != "LONG":
        for name in ("stored_energy", "absorption", "trigger_sensitivity", "expansion_readiness",
                     "relative_strength", "vwap_structure"):
            previous = scores[name]
            reason = f"{bias} directional deep formula unavailable; legacy LONG evidence is not mirrored or reused"
            components = [component.model_copy(update={"normalized": None, "contribution": None,
                                                       "reason": reason, "inputs": {"bias": bias}}, deep=True)
                          for component in previous.components]
            scores[name] = DiscoveryScore(formula_version="phase1-v1-directional-unavailable",
                                          components=components)
    return scores, levels
