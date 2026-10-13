"""Causal OHLCV features and observable market structures.

Percent-suffixed metrics are percentage points. Band width is a fraction;
realized volatility is annualized using a 365-day crypto calendar. Structures
are heuristic evidence, not historically fitted predictions. The candle volume
profile is an explicitly labelled approximation of traded volume at price.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .avwap import analyze_anchors
from .models import TIMEFRAMES, Candle, TechnicalSnapshot, aware

PATTERNS = (
    "compression",
    "higher_lows",
    "higher_highs",
    "lower_lows",
    "lower_highs",
    "shallow_pullbacks",
    "absorption",
    "breakout",
    "accepted_breakout",
    "retest",
    "overextended",
    "weak_advance",
    "demand_expansion",
    "avwap_reclaim",
    "avwap_support",
    "avwap_confluence",
)


def _finite(value: float | np.floating | None) -> float | None:
    return float(value) if value is not None and math.isfinite(value) else None


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    """SMA-seeded EMA; never report a period before its warmup completes."""
    result = np.full(len(values), np.nan)
    valid = np.flatnonzero(np.isfinite(values))
    if not len(valid):
        return result
    first = int(valid[0])
    if len(values) - first < period:
        return result
    seed = first + period - 1
    result[seed] = float(np.mean(values[first : seed + 1]))
    alpha = 2 / (period + 1)
    for i in range(seed + 1, len(values)):
        if np.isfinite(values[i]):
            result[i] = alpha * values[i] + (1 - alpha) * result[i - 1]
    return result


def _wilder(values: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    valid = np.flatnonzero(np.isfinite(values))
    if not len(valid):
        return result
    first = int(valid[0])
    seed = first + period - 1
    if seed >= len(values):
        return result
    result[seed] = np.mean(values[first : seed + 1])
    for i in range(seed + 1, len(values)):
        result[i] = (result[i - 1] * (period - 1) + values[i]) / period
    return result


def _percentile(value: float, prior: np.ndarray) -> float | None:
    prior = prior[np.isfinite(prior)]
    if not math.isfinite(value) or len(prior) < 60:
        return None
    # Mid-rank prevents a flat constant-volatility history being called compressed.
    return float((np.sum(prior < value) + 0.5 * np.sum(prior == value)) / len(prior))


def confirmed_swings(candles: list[Candle], left: int = 3, right: int = 3) -> list[dict]:
    """A pivot becomes knowable only when its right-hand confirming bars close.

    Equality is deliberately excluded to avoid multiple pivots on flat plateaus.
    Callers must provide closed, available candles in chronological order.
    """
    if left < 1 or right < 1:
        raise ValueError("swing confirmation windows must be positive")
    swings: list[dict] = []
    for i in range(left, len(candles) - right):
        neighbours = candles[i - left : i] + candles[i + 1 : i + right + 1]
        confirm = i + right
        known = max(c.available_at for c in candles[i - left : confirm + 1])
        for kind, price, qualifies in (
            ("high", candles[i].high, all(candles[i].high > c.high for c in neighbours)),
            ("low", candles[i].low, all(candles[i].low < c.low for c in neighbours)),
        ):
            if qualifies:
                swings.append(
                    {
                        "index": i,
                        "kind": kind,
                        "price": price,
                        "confirmed_index": confirm,
                        "confirmed_at": known.isoformat(),
                    }
                )
    return swings


def _clusters(prices: list[float], tolerance: float) -> list[float]:
    groups: list[list[float]] = []
    for price in sorted(prices):
        if groups and abs(price - float(np.mean(groups[-1]))) <= tolerance:
            groups[-1].append(price)
        else:
            groups.append([price])
    return [float(np.mean(group)) for group in groups]


def _base(candles: list[Candle], seconds: int, end: int | None = None) -> tuple[int | None, int]:
    """Find the longest observable sideways box, fixed before two acceptance bars.

    A fetched time span alone is not a range age: qualifying boxes need >=21
    actual days, <=35% high/low width and limited endpoint displacement. The
    search spans 60 days, or 60 bars for slow frames such as weekly candles.
    """
    end = max(0, len(candles) - 2) if end is None else end
    if end < 32:
        return None, end
    start = max(0, end - max(60, math.ceil(60 * 86400 / seconds)))
    highs = np.array([c.high for c in candles[:end]])
    lows = np.array([c.low for c in candles[:end]])
    for candidate in range(start, end - 31):
        days = (candles[end - 1].close_time - candles[candidate].open_time).total_seconds() / 86400
        if days < 21:
            break
        high, low = float(highs[candidate:end].max()), float(lows[candidate:end].min())
        width = high - low
        displacement = abs(candles[end - 1].close - candles[candidate].close)
        if width / low <= 0.35 and (width == 0 or displacement <= 0.7 * width):
            return candidate, end
    return None, end


def _range_confirmation(candles: list[Candle], seconds: int, start: int, end: int) -> int | None:
    """First prefix that actually selected this range start, in O(n²) or less.

    Incremental extrema evaluate earlier competing starts without rerunning
    range fitting for every prefix. No candle beyond the fitted range is used.
    Availability delays are applied separately by the anchored VWAP engine.
    """
    horizon = max(60, math.ceil(60 * 86400 / seconds))
    highs = np.full(start + 1, -np.inf)
    lows = np.full(start + 1, np.inf)
    opening_closes = np.array([c.close for c in candles[: start + 1]])
    for i in range(end):
        count = min(i + 1, start + 1)
        highs[:count] = np.maximum(highs[:count], candles[i].high)
        lows[:count] = np.minimum(lows[:count], candles[i].low)
        if (
            i - start + 1 < 32
            or (candles[i].close_time - candles[start].open_time).total_seconds() < 21 * 86400
        ):
            continue
        first = max(0, i + 1 - horizon)
        width = highs[first:] - lows[first:]
        displacement = abs(candles[i].close - opening_closes[first:])
        qualifying = np.flatnonzero((width / lows[first:] <= 0.35) & (displacement <= 0.7 * width))
        if len(qualifying) and first + qualifying[0] == start:
            return i
    return None


def _breakout_episode(candles: list[Candle], seconds: int, atr: np.ndarray) -> dict | None:
    """Retain the resistance first crossed until two closes invalidate it.

    Every candidate's box is fitted only to its prior candles. Later candles
    cannot move the episode level, even after they enter a rolling range.
    """
    episode: dict | None = None
    failed = 0
    for i in range(219, len(candles)):
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        if episode is not None:
            threshold = episode["level"] + episode["acceptance_buffer"]
            episode["accepted_closes"] = episode["accepted_closes"] + 1 if candles[i].close > threshold else 0
            failed = failed + 1 if candles[i].close < episode["level"] - episode["acceptance_buffer"] else 0
            if failed >= 2:
                episode = None
                failed = 0
            continue
        start, end = _base(candles, seconds, end=i)
        if start is None:
            continue
        level = max(c.high for c in candles[start:end])
        buffer = float(atr[i]) * 0.1
        if candles[i - 1].close <= level + buffer < candles[i].close:
            episode = {
                "index": i,
                "level": level,
                "start": start,
                "end": end,
                "acceptance_buffer": buffer,
                "accepted_closes": 1,
            }
    return episode


def _volume_profile(candles: list[Candle], bins: int = 48) -> dict:
    """Uniformly apportion each candle's volume over its observed low/high range.

    This cannot reconstruct executed volume at price, aggressor distribution, or
    genuine liquidity gaps. HVN/LVN below are approximation nodes only.
    """
    metadata = {
        "method": "OHLCV uniform high-low approximation",
        "reliable_trade_distribution": False,
        "value_area_fraction": 0.7,
    }
    if not candles or sum(c.volume for c in candles) == 0:
        return {**metadata, "poc": None, "vah": None, "val": None, "hvn": [], "lvn": []}
    low, high = min(c.low for c in candles), max(c.high for c in candles)
    if low == high:
        return {**metadata, "poc": low, "vah": low, "val": low, "hvn": [low], "lvn": []}
    edges = np.linspace(low, high, bins + 1)
    distribution = np.zeros(bins)
    for candle in candles:
        if candle.high == candle.low:
            index = min(bins - 1, int(np.searchsorted(edges, candle.close, side="right") - 1))
            distribution[max(0, index)] += candle.volume
        else:
            overlap = np.maximum(0, np.minimum(edges[1:], candle.high) - np.maximum(edges[:-1], candle.low))
            distribution += candle.volume * overlap / (candle.high - candle.low)
    centers = (edges[:-1] + edges[1:]) / 2
    peak = int(distribution.argmax())
    left = right = peak
    included = distribution[peak]
    target = distribution.sum() * 0.7
    while included < target and (left > 0 or right < bins - 1):
        next_left = distribution[left - 1] if left else -1
        next_right = distribution[right + 1] if right < bins - 1 else -1
        if next_left >= next_right:
            left -= 1
            included += distribution[left]
        else:
            right += 1
            included += distribution[right]
    upper, lower = np.quantile(distribution, [0.8, 0.2])
    hvn = [
        float(centers[i])
        for i in range(1, bins - 1)
        if distribution[i] >= upper
        and distribution[i] > distribution[i - 1]
        and distribution[i] >= distribution[i + 1]
    ]
    lvn = [
        float(centers[i])
        for i in range(1, bins - 1)
        if distribution[i] <= lower
        and distribution[i] < distribution[i - 1]
        and distribution[i] <= distribution[i + 1]
    ]
    return {
        **metadata,
        "poc": float(centers[peak]),
        "vah": float(edges[right + 1]),
        "val": float(edges[left]),
        "hvn": hvn,
        "lvn": lvn,
    }


def analyze(candles: list[Candle], timeframe: str, as_of: datetime) -> TechnicalSnapshot:
    """Evaluate only closed candles whose availability is at or before as_of.

    Invalid chronology fails closed; missing history produces unknown patterns
    instead of valid-looking zero values. Future input never changes a snapshot.
    """
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    as_of = aware(as_of)
    seconds = TIMEFRAMES[timeframe]
    bars = sorted(
        (c for c in candles if c.close_time <= as_of and c.available_at <= as_of), key=lambda c: c.open_time
    )
    result = TechnicalSnapshot(
        timeframe=timeframe, as_of=as_of, bars=len(bars), patterns=dict.fromkeys(PATTERNS)
    )
    if not bars:
        result.issues.append("no_closed_available_candles")
        return result
    bad: list[str] = []
    if len({c.open_time for c in bars}) != len(bars):
        bad.append("duplicate_candle_timestamps")
    if any(abs((c.close_time - c.open_time).total_seconds() - seconds) > 1.01 for c in bars):
        bad.append("candle_duration_does_not_match_timeframe")
    if any(
        (b.open_time - a.open_time).total_seconds() != seconds for a, b in zip(bars, bars[1:], strict=False)
    ):
        bad.append("non_contiguous_candles")
    if len({c.source for c in bars}) > 1:
        bad.append("mixed_candle_sources")
    if bad:
        result.issues.extend(bad)
        return result
    if (as_of - bars[-1].close_time).total_seconds() > seconds * 1.5:
        result.issues.append("stale_latest_candle")

    close = np.array([c.close for c in bars])
    high = np.array([c.high for c in bars])
    low = np.array([c.low for c in bars])
    volume = np.array([c.volume for c in bars])
    n = len(bars)
    metrics: dict[str, float | None] = {
        "close": float(close[-1]),
        "last_close_epoch": bars[-1].close_time.timestamp(),
        "observed_history_days": (bars[-1].close_time - bars[0].open_time).total_seconds() / 86400,
    }
    emas = {period: _ema(close, period) for period in (10, 20, 50, 100, 200)}
    metrics.update({f"ema{period}": _finite(values[-1]) for period, values in emas.items()})
    difference = np.r_[np.nan, np.diff(close)]
    gain = _wilder(np.maximum(difference, 0), 14)
    loss = _wilder(np.maximum(-difference, 0), 14)
    if np.isfinite(gain[-1]) and np.isfinite(loss[-1]):
        metrics["rsi14"] = (
            50.0
            if gain[-1] == loss[-1] == 0
            else (100.0 if loss[-1] == 0 else float(100 - 100 / (1 + gain[-1] / loss[-1])))
        )
    else:
        metrics["rsi14"] = None
    macd = _ema(close, 12) - _ema(close, 26)
    signal = _ema(macd, 9)
    metrics.update(
        macd=_finite(macd[-1]), macd_signal=_finite(signal[-1]), macd_hist=_finite((macd - signal)[-1])
    )
    previous = np.r_[close[0], close[:-1]]
    tr = np.maximum(high - low, np.maximum(abs(high - previous), abs(low - previous)))
    atr = _wilder(tr, 14)
    metrics["atr14"] = _finite(atr[-1])
    metrics["atr_pct"] = _finite(atr[-1] / close[-1] * 100)
    close_series = pd.Series(close)
    middle = close_series.rolling(20, min_periods=20).mean().to_numpy()
    std = close_series.rolling(20, min_periods=20).std(ddof=0).to_numpy()
    upper, lower = middle + 2 * std, middle - 2 * std
    width = 4 * std / middle
    metrics.update(
        bb_middle=_finite(middle[-1]),
        bb_upper=_finite(upper[-1]),
        bb_lower=_finite(lower[-1]),
        bb_width=_finite(width[-1]),
        bb_width_percentile=_percentile(width[-1], width[max(0, n - 181) : n - 1]),
    )
    log_returns = np.r_[np.nan, np.diff(np.log(close))]
    annualization = math.sqrt(365 * 86400 / seconds)
    rv = pd.Series(log_returns).rolling(20, min_periods=20).std(ddof=1).to_numpy() * annualization
    metrics.update(
        realized_vol=_finite(rv[-1]), rv_percentile=_percentile(rv[-1], rv[max(0, n - 181) : n - 1])
    )
    for period in (10, 20, 50):
        metrics[f"volume_ma{period}"] = float(np.mean(volume[-period:])) if n >= period else None
    prior_mean = float(np.mean(volume[-21:-1])) if n >= 21 else None
    relative = float(volume[-1] / prior_mean) if prior_mean and prior_mean > 0 else None
    contraction_baseline = float(np.mean(volume[-60:-10])) if n >= 60 else None
    metrics.update(
        relative_volume=relative,
        spot_volume_expansion=relative,
        volume_contraction=(
            float(np.mean(volume[-10:]) / contraction_baseline)
            if contraction_baseline and contraction_baseline > 0
            else None
        ),
        obv=float(np.sum(np.sign(difference[1:]) * volume[1:])),
    )
    typical = (high + low + close) / 3
    traded_quote = np.array([c.quote_volume if c.quote_volume is not None else np.nan for c in bars])
    session = [i for i, candle in enumerate(bars) if candle.open_time.date() == bars[-1].open_time.date()]
    # A daily/weekly aggregate cannot reconstruct a daily intrabar VWAP.
    if seconds < 86400:
        session_volume = float(np.sum(volume[session]))
        session_start = bars[session[0]].open_time
        complete_session = (
            session_start.hour
            == session_start.minute
            == session_start.second
            == session_start.microsecond
            == 0
        )
        exact_session_notional = np.all(np.isfinite(traded_quote[session]))
        session_notional = (
            np.sum(traded_quote[session])
            if exact_session_notional
            else np.sum(typical[session] * volume[session])
        )
        metrics["window_vwap"] = float(session_notional / session_volume) if session_volume else None
        metrics["vwap"] = metrics["window_vwap"] if complete_session else None
        if not complete_session:
            result.issues.append("partial_session_vwap_unavailable")
    else:
        metrics["vwap"] = None
        result.issues.append("daily_session_vwap_requires_intraday_bars")
    valid_emas = [values[-1] for values in emas.values() if np.isfinite(values[-1])]
    metrics["ema_cluster_pct"] = (
        float((max(valid_emas) - min(valid_emas)) / close[-1] * 100) if len(valid_emas) == 5 else None
    )
    metrics["extension_atr"] = _finite((close[-1] - emas[20][-1]) / atr[-1]) if atr[-1] > 0 else None
    for days in (7, 30):
        cutoff = bars[-1].close_time - timedelta(days=days)
        previous_bars = [c for c in bars if c.close_time <= cutoff]
        metrics[f"return_{days}d"] = (
            (close[-1] / previous_bars[-1].close - 1) * 100 if previous_bars else None
        )

    swings = confirmed_swings(bars)
    tolerance = max(float(atr[-1]) * 0.75 if math.isfinite(atr[-1]) else 0, close[-1] * 0.003)
    support = _clusters(
        [s["price"] for s in swings if s["kind"] == "low" and s["price"] < close[-1]], tolerance
    )
    resistance = _clusters(
        [s["price"] for s in swings if s["kind"] == "high" and s["price"] > close[-1]], tolerance
    )
    metrics["distance_major_resistance_pct"] = (resistance[0] / close[-1] - 1) * 100 if resistance else None
    metrics["distance_support_pct"] = (close[-1] / support[-1] - 1) * 100 if support else None
    start, end = _base(bars, seconds)
    episode = _breakout_episode(bars, seconds, atr) if n >= 220 else None
    if episode is not None:
        start, end = episode["start"], episode["end"]
    fallback_start = max(0, end - 20)
    range_start = start if start is not None else fallback_start
    reference = bars[range_start:end]
    range_high = max(c.high for c in reference) if reference else None
    range_low = min(c.low for c in reference) if reference else None
    range_age = (
        (bars[end - 1].close_time - bars[start].open_time).total_seconds() / 86400
        if start is not None
        else None
    )
    metrics.update(
        range_high=range_high,
        range_low=range_low,
        range_width_pct=(range_high / range_low - 1) * 100 if range_high and range_low else None,
        range_age_days=range_age,
        breakout_level=range_high,
        distance_resistance_pct=(range_high / close[-1] - 1) * 100 if range_high else None,
    )
    touches: list[int] = []
    if start is not None and range_high is not None:
        touching = False
        for i in range(start, end):
            touch = bars[i].high >= range_high - tolerance
            if touch and not touching and (not touches or i - touches[-1] > 3):
                touches.append(i)
            touching = touch
    metrics["resistance_tests"] = float(len(touches)) if start is not None else None
    profile = _volume_profile(bars[range_start:end][-512:])
    metrics.update(profile_poc=profile["poc"], profile_vah=profile["vah"], profile_val=profile["val"])
    metrics.update(distance_52w_high_pct=None, distance_52w_low_pct=None, distance_ath_pct=None)
    window_start = bars[-1].close_time - timedelta(weeks=52)
    if bars[0].open_time <= window_start:
        year = [c for c in bars if c.close_time > window_start]
        year_high, year_low = max(c.high for c in year), min(c.low for c in year)
        metrics.update(
            distance_52w_high_pct=(year_high / close[-1] - 1) * 100,
            distance_52w_low_pct=(close[-1] / year_low - 1) * 100,
        )
    else:
        result.issues.append("52_week_history_unavailable")
    result.issues.append("all_time_high_requires_verified_inception_history")
    result.levels = {
        "supports": support,
        "resistances": resistance,
        "support": support,
        "resistance": resistance,
        "swings": swings,
        "range_start": bars[start].open_time.isoformat() if start is not None else None,
        "range_defined": start is not None,
        "breakout_level": range_high,
        "breakout_origin": bars[episode["index"]].open_time.isoformat() if episode else None,
        "vwap_method": (
            "exchange quote/base traded volume; UTC session"
            if seconds < 86400 and exact_session_notional
            else "OHLCV typical-price estimate; UTC session"
            if seconds < 86400
            else "unavailable without intraday candles"
        ),
        "volume_profile": profile,
    }
    result.metrics = metrics
    range_confirmation = _range_confirmation(bars, seconds, start, end) if start is not None else None
    result.anchors = analyze_anchors(
        bars,
        swings,
        start,
        range_high if start is not None else None,
        range_confirmed_index=range_confirmation,
    )
    structures_ready = (
        n >= 220 and metrics["observed_history_days"] >= 21 and "stale_latest_candle" not in result.issues
    )
    if n < 220:
        result.issues.append("insufficient_indicator_warmup_minimum_220_bars")
    if metrics["observed_history_days"] < 21:
        result.issues.append("insufficient_calendar_history_for_structures_minimum_21_days")
    if not structures_ready:
        return result

    for kind, up, down in (("low", "higher_lows", "lower_lows"), ("high", "higher_highs", "lower_highs")):
        sequence = [s["price"] for s in swings if s["kind"] == kind][-3:]
        if len(sequence) >= 3:
            result.patterns[up] = all(b > a * 1.0005 for a, b in zip(sequence, sequence[1:], strict=False))
            result.patterns[down] = all(b < a * 0.9995 for a, b in zip(sequence, sequence[1:], strict=False))
    pullbacks = []
    if range_high is not None:
        for a, b in zip(touches, touches[1:], strict=False):
            pullbacks.append((range_high - min(c.low for c in bars[a : b + 1])) / range_high)
    if len(pullbacks) >= 2:
        last_pullbacks = pullbacks[-3:]
        result.patterns["shallow_pullbacks"] = all(
            b < a * 0.95 for a, b in zip(last_pullbacks, last_pullbacks[1:], strict=False)
        )
    compression_inputs = (
        metrics["bb_width_percentile"],
        metrics["rv_percentile"],
        metrics["ema_cluster_pct"],
    )
    result.patterns["compression"] = bool(
        start is not None
        and all(v is not None for v in compression_inputs)
        and metrics["bb_width_percentile"] <= 0.3
        and metrics["rv_percentile"] <= 0.35
        and metrics["ema_cluster_pct"] <= 5
        and metrics["range_width_pct"] <= 30
    )
    result.patterns["overextended"] = (
        metrics["extension_atr"] >= 3 if metrics["extension_atr"] is not None else None
    )
    result.patterns["weak_advance"] = (
        bool(close[-1] > close[-2] and relative < 0.75) if relative is not None else None
    )
    result.patterns["demand_expansion"] = (
        bool(close[-1] > close[-2] and relative >= 1.5) if relative is not None else None
    )
    if start is not None and range_high is not None and atr[-1] > 0:
        result.patterns["breakout"] = bool(episode is not None and episode["index"] == n - 1)
        result.patterns["accepted_breakout"] = bool(episode is not None and episode["accepted_closes"] >= 2)
        result.patterns["retest"] = bool(
            episode is not None
            and episode["accepted_closes"] >= 2
            and bars[-1].low <= range_high + tolerance
            and bars[-1].low >= range_high - tolerance
        )
    else:
        result.issues.append("multiweek_sideways_base_not_detected")
    recent = bars[-20:]
    if all(c.taker_buy_volume is not None for c in recent) and sum(c.volume for c in recent) > 0:
        sell_share = sum(c.volume - c.taker_buy_volume for c in recent) / sum(c.volume for c in recent)
        metrics["aggressive_sell_share"] = sell_share
        five_bar_return = close[-1] / close[-6] - 1
        result.patterns["absorption"] = bool(
            start is not None
            and sell_share >= 0.55
            and -0.01 <= five_bar_return <= 0.03
            and len(touches) >= 3
            and (result.patterns["higher_lows"] or result.patterns["shallow_pullbacks"])
        )
    else:
        metrics["aggressive_sell_share"] = None
        result.issues.append("aggressor_volume_unavailable_absorption_unconfirmed")
    valid_anchors = [anchor for anchor in result.anchors if anchor.value is not None]
    result.patterns["avwap_reclaim"] = (
        any(anchor.reclaim for anchor in valid_anchors) if valid_anchors else None
    )
    result.patterns["avwap_support"] = (
        any(anchor.support for anchor in valid_anchors) if valid_anchors else None
    )
    near = [anchor for anchor in valid_anchors if abs(anchor.distance_pct or 0) <= 1]
    # Distinct origin times avoid duplicate kinds on one candle overstating confluence.
    result.patterns["avwap_confluence"] = len({a.anchor_time for a in near}) >= 2 if valid_anchors else None
    return result
