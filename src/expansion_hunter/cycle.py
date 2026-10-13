"""Point-in-time market-cycle context and market congruence.

Cycle names are unvalidated research labels. The classifier uses explicit,
configurable hypotheses; neither its evidence coverage nor congruence score is
a calibrated probability. This module has no trading or notification side effects.

All moving averages use closed, contiguous UTC-duration daily candles. Drawdown
from the high in supplied history is distinguished from a verified all-time high.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from enum import StrEnum
from statistics import median
from typing import Literal

import numpy as np
from pydantic import Field, model_validator

from .discovery_models import DiscoveryScore, ScoreComponent
from .models import AssetSnapshot, Candle, Metric, Model, Regime, RegimeReport, Status, aware
from .risk import causal_bars
from .technical import _ema, _wilder, confirmed_swings


class CyclePhase(StrEnum):
    ACCUMULATION = "ACCUMULATION"
    EARLY_RECOVERY = "EARLY_RECOVERY"
    REVIVAL = "REVIVAL"
    EARLY_BULL = "EARLY_BULL"
    BULL_EXPANSION = "BULL_EXPANSION"
    LATE_BULL = "LATE_BULL"
    EUPHORIA = "EUPHORIA"
    DISTRIBUTION = "DISTRIBUTION"
    ARMAGEDDON_TRANSITION = "ARMAGEDDON_TRANSITION"
    EARLY_BEAR = "EARLY_BEAR"
    BEAR_EXPANSION = "BEAR_EXPANSION"
    CAPITULATION = "CAPITULATION"
    KNOCKOUT = "KNOCKOUT"
    RECOVERY = "RECOVERY"
    NEUTRAL_UNCERTAIN = "NEUTRAL_UNCERTAIN"


class CycleSettings(Model):
    """Versioned hypothesis thresholds, never fitted to named historical winners."""

    formula_version: str = "cycle-research-v1"
    daily_max_age_seconds: float = Field(default=90_000, gt=0)
    metric_max_age_seconds: float = Field(default=86_400, gt=0)
    context_max_age_seconds: float = Field(default=86_400, gt=0)
    slope_days: int = Field(default=5, ge=1, le=30)
    pivot_left: int = Field(default=3, ge=1, le=10)
    pivot_right: int = Field(default=3, ge=1, le=10)
    structure_buffer_atr: float = Field(default=0, ge=0, le=1)
    recent_structure_days: int = Field(default=7, ge=1, le=90)
    minimum_breadth_assets: int = Field(default=10, ge=2)
    minimum_congruence_coverage: float = Field(default=0.60, ge=0, le=1)
    neutral_return: float = Field(default=0.005, ge=0, le=0.1)
    expansion_return_30d: float = Field(default=0.12, gt=0)
    recovery_return_30d: float = Field(default=0.08, gt=0)
    capitulation_return_7d: float = Field(default=-0.20, lt=0, gt=-1)
    capitulation_rsi: float = Field(default=25, gt=0, lt=50)
    abnormal_volume_ratio: float = Field(default=2, gt=1)
    knockout_drawdown: float = Field(default=-0.65, gt=-1, lt=0)
    knockout_lower_wick_fraction: float = Field(default=0.60, gt=0, le=1)
    euphoria_distance_sma50: float = Field(default=0.30, gt=0)
    euphoria_rsi: float = Field(default=80, gt=50, lt=100)
    late_bull_distance_sma50: float = Field(default=0.15, gt=0)
    late_bull_rsi: float = Field(default=70, gt=50, lt=100)
    bear_return_30d: float = Field(default=-0.12, gt=-1, lt=0)
    early_bear_return_30d: float = Field(default=-0.05, gt=-1, lt=0)
    accumulation_range_30d: float = Field(default=0.18, gt=0)
    accumulation_return_30d: float = Field(default=0.04, gt=0)
    accumulation_volume_ratio: float = Field(default=1.20, gt=0)
    revival_range_30d: float = Field(default=0.25, gt=0)
    distribution_max_drawdown: float = Field(default=-0.10, gt=-1, lt=0)
    early_recovery_rsi: float = Field(default=45, gt=0, lt=100)

    @model_validator(mode="after")
    def ordered_thresholds(self):
        if self.late_bull_distance_sma50 >= self.euphoria_distance_sma50:
            raise ValueError("late-bull extension must be below euphoria extension")
        if self.late_bull_rsi >= self.euphoria_rsi:
            raise ValueError("late-bull RSI must be below euphoria RSI")
        if self.bear_return_30d >= self.early_bear_return_30d:
            raise ValueError("bear expansion must require a larger decline than early bear")
        return self


class StructureEvent(Model):
    asset_id: str
    timeframe: Literal["1D"] = "1D"
    kind: Literal["BOS_UP", "BOS_DOWN", "CHOCH_UP", "CHOCH_DOWN"]
    direction: Literal[-1, 1]
    event_timestamp: datetime
    discovery_timestamp: datetime
    close: float = Field(gt=0)
    level: float = Field(gt=0)
    level_timestamp: datetime
    level_discovery_timestamp: datetime
    buffer: float = Field(ge=0)
    source: str
    method: str = "confirmed daily pivot, followed by a closed-candle cross"

    @model_validator(mode="after")
    def causal(self):
        if self.discovery_timestamp < self.event_timestamp:
            raise ValueError("structure discovery precedes event")
        if self.level_discovery_timestamp < self.level_timestamp:
            raise ValueError("pivot discovery precedes pivot")
        if self.level_discovery_timestamp > self.discovery_timestamp:
            raise ValueError("structure uses an undiscovered pivot")
        if self.level_timestamp >= self.event_timestamp:
            raise ValueError("structure pivot must predate its break")
        return self


class BreadthReport(Model):
    supplied_assets: int = Field(ge=0)
    evaluated_assets: int = Field(ge=0)
    sma50_assets: int = Field(default=0, ge=0)
    sma200_assets: int = Field(default=0, ge=0)
    return_assets: int = Field(default=0, ge=0)
    volume_assets: int = Field(default=0, ge=0)
    volatility_assets: int = Field(default=0, ge=0)
    endpoint: datetime | None = None
    available_at: datetime | None = None
    above_sma50: float | None = Field(default=None, ge=0, le=1)
    above_sma200: float | None = Field(default=None, ge=0, le=1)
    median_return_7d: float | None = None
    volume_breadth: float | None = Field(default=None, ge=0, le=1)
    volatility_breadth: float | None = Field(default=None, ge=0, le=1)
    new_highs_30d: float | None = Field(default=None, ge=0, le=1)
    new_lows_30d: float | None = Field(default=None, ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)
    excluded: dict[str, str] = Field(default_factory=dict)
    basis: str = "supplied dated candidate cohort; not guaranteed market-wide coverage"


class MarketCycleReport(Model):
    as_of: datetime
    available_at: datetime
    phase: CyclePhase = CyclePhase.NEUTRAL_UNCERTAIN
    previous_phase: CyclePhase | None = None
    changed: bool = False
    regime: Regime = Regime.NEUTRAL
    interpretation: Literal["RESEARCH_HYPOTHESIS"] = "RESEARCH_HYPOTHESIS"
    calibration: Literal["UNVALIDATED"] = "UNVALIDATED"
    formula_version: str = "cycle-research-v1"
    evidence_score: float = Field(default=0, ge=0, le=100)
    evidence_definition: str = "weighted input coverage, not phase probability or predictive confidence"
    metrics: dict[str, Metric] = Field(default_factory=dict)
    breadth: BreadthReport
    structure_events: list[StructureEvent] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    settings: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def causal(self):
        if self.available_at < self.as_of:
            raise ValueError("cycle availability precedes calculation")
        for metric in self.metrics.values():
            if metric.status == Status.AVAILABLE and (
                not metric.available_at or metric.available_at > self.as_of
            ):
                raise ValueError("cycle contains future input")
        if any(event.discovery_timestamp > self.as_of for event in self.structure_events):
            raise ValueError("cycle contains a future structural event")
        if self.breadth.available_at and self.breadth.available_at > self.as_of:
            raise ValueError("cycle contains future breadth")
        return self


class MarketCongruenceReport(Model):
    asset_id: str
    as_of: datetime
    available_at: datetime
    score: DiscoveryScore
    market_conflict: bool = False
    reasons: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    interpretation: Literal["CONTEXT_ONLY"] = "CONTEXT_ONLY"

    @model_validator(mode="after")
    def causal(self):
        if self.available_at < self.as_of:
            raise ValueError("congruence availability precedes calculation")
        return self


def _unknown(reason: str, source: str = "cycle", unit: str = "") -> Metric:
    return Metric(source=source, unit=unit, reason=reason)


def _input_metric(metric: Metric | None, as_of: datetime, maximum_age: float) -> Metric:
    """Preserve stale provenance while discarding unavailable or future values."""
    if metric is None:
        return _unknown("dated input unavailable")
    if (
        metric.available_at
        and metric.available_at > as_of
        or metric.observed_at
        and metric.observed_at > as_of
    ):
        return _unknown("input was not available at the decision timestamp", metric.source, metric.unit)
    if metric.usable(as_of, maximum_age):
        return metric.model_copy(deep=True)
    if (
        metric.status in {Status.AVAILABLE, Status.STALE}
        and metric.observed_at
        and metric.available_at
        and metric.reliable
    ):
        return metric.model_copy(
            update={"status": Status.STALE, "reason": "dated observation is stale"}, deep=True
        )
    return _unknown(metric.reason or "input is unavailable or unreliable", metric.source, metric.unit)


def _daily(
    snapshot: AssetSnapshot | None, as_of: datetime, settings: CycleSettings
) -> tuple[list[Candle], str | None]:
    if snapshot is None:
        return [], "asset snapshot unavailable"
    if snapshot.as_of > as_of:
        return [], "snapshot was evaluated after the decision timestamp"
    bars = causal_bars(snapshot, "1D")
    if not bars:
        return [], "closed available contiguous daily history unavailable"
    if (
        bars[-1].close_time > as_of
        or (as_of - bars[-1].close_time).total_seconds() > settings.daily_max_age_seconds
    ):
        return [], "daily history is stale"
    return bars, None


def _series(values: np.ndarray, period: int) -> np.ndarray:
    return np.array(
        [
            float(np.mean(values[i - period + 1 : i + 1])) if i >= period - 1 else math.nan
            for i in range(len(values))
        ]
    )


def _number(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


def _atr(bars: list[Candle]) -> np.ndarray:
    high = np.array([b.high for b in bars])
    low = np.array([b.low for b in bars])
    close = np.array([b.close for b in bars])
    prior = np.r_[math.nan, close[:-1]]
    ranges = np.maximum(high - low, np.maximum(np.abs(high - prior), np.abs(low - prior)))
    return _wilder(ranges, 14)


def _observed(value: float | None, bars: list[Candle], unit: str, reason: str) -> Metric:
    if value is None:
        return _unknown(reason, bars[-1].source if bars else "cycle", unit)
    return Metric(
        value=value,
        observed_at=bars[-1].close_time,
        available_at=max(b.available_at for b in bars),
        source=bars[-1].source,
        unit=unit,
        status=Status.AVAILABLE,
        reason=reason,
    )


def _asset_metrics(
    snapshot: AssetSnapshot | None, as_of: datetime, settings: CycleSettings
) -> tuple[dict[str, Metric], list[Candle], list[StructureEvent]]:
    bars, error = _daily(snapshot, as_of, settings)
    names = (
        "close",
        "sma50",
        "sma200",
        "sma50_slope",
        "sma200_slope",
        "distance_sma50",
        "distance_sma200",
        "ema20",
        "ema50",
        "return_7d",
        "return_30d",
        "return_90d",
        "observed_high",
        "drawdown_observed_high",
        "drawdown_ath",
        "atr14",
        "realized_volatility_30d",
        "volatility_ratio_7d_21d",
        "relative_volume",
        "volume_growth_7d",
        "range_width_30d",
        "rsi14",
        "macd",
        "macd_signal",
        "lower_wick_fraction",
        "higher_highs",
        "higher_lows",
        "new_high_30d",
        "new_low_30d",
    )
    if error:
        return {name: _unknown(error) for name in names}, [], []
    close = np.array([bar.close for bar in bars])
    volume = np.array([bar.volume for bar in bars])
    sma50, sma200 = _series(close, 50), _series(close, 200)
    ema20, ema50 = _ema(close, 20), _ema(close, 50)
    delta = np.r_[math.nan, np.diff(close)]
    gains, losses = _wilder(np.maximum(delta, 0), 14), _wilder(np.maximum(-delta, 0), 14)
    rsi = None
    if math.isfinite(gains[-1]) and math.isfinite(losses[-1]):
        rsi = (
            50.0
            if gains[-1] == losses[-1] == 0
            else (100.0 if losses[-1] == 0 else 100 - 100 / (1 + gains[-1] / losses[-1]))
        )
    macd = _ema(close, 12) - _ema(close, 26)
    logged_returns = np.diff(np.log(close))
    rvol = (
        float(volume[-1] / np.mean(volume[-21:-1]))
        if len(volume) >= 21 and np.mean(volume[-21:-1]) > 0
        else None
    )
    volatility = (
        float(np.std(logged_returns[-30:], ddof=1) * math.sqrt(365)) if len(logged_returns) >= 30 else None
    )
    volatility_ratio = None
    if len(logged_returns) >= 28:
        baseline = float(np.std(logged_returns[-28:-7], ddof=1))
        if baseline > 0:
            volatility_ratio = float(np.std(logged_returns[-7:], ddof=1) / baseline)
    high = max(b.high for b in bars)
    values: dict[str, float | None] = {
        "close": float(close[-1]),
        "sma50": _number(sma50[-1]),
        "sma200": _number(sma200[-1]),
        "sma50_slope": None,
        "sma200_slope": None,
        "distance_sma50": None,
        "distance_sma200": None,
        "ema20": _number(ema20[-1]),
        "ema50": _number(ema50[-1]),
        "return_7d": float(close[-1] / close[-8] - 1) if len(close) >= 8 else None,
        "return_30d": float(close[-1] / close[-31] - 1) if len(close) >= 31 else None,
        "return_90d": float(close[-1] / close[-91] - 1) if len(close) >= 91 else None,
        "observed_high": high,
        "drawdown_observed_high": float(close[-1] / high - 1),
        "drawdown_ath": None,
        "atr14": _number(_atr(bars)[-1]),
        "realized_volatility_30d": volatility,
        "volatility_ratio_7d_21d": volatility_ratio,
        "relative_volume": rvol,
        "volume_growth_7d": float(np.mean(volume[-7:]) / np.mean(volume[-14:-7]) - 1)
        if len(volume) >= 14 and np.mean(volume[-14:-7]) > 0
        else None,
        "range_width_30d": max(b.high for b in bars[-30:]) / min(b.low for b in bars[-30:]) - 1
        if len(bars) >= 30
        else None,
        "rsi14": rsi,
        "macd": _number(macd[-1]),
        "macd_signal": _number(_ema(macd, 9)[-1]),
        "lower_wick_fraction": (min(bars[-1].open, bars[-1].close) - bars[-1].low)
        / (bars[-1].high - bars[-1].low)
        if bars[-1].high > bars[-1].low
        else None,
        "higher_highs": None,
        "higher_lows": None,
        "new_high_30d": float(bars[-1].high > max(b.high for b in bars[-31:-1])) if len(bars) >= 31 else None,
        "new_low_30d": float(bars[-1].low < min(b.low for b in bars[-31:-1])) if len(bars) >= 31 else None,
    }
    for period, sma in ((50, sma50), (200, sma200)):
        if len(sma) >= period + settings.slope_days:
            values[f"sma{period}_slope"] = float(sma[-1] / sma[-settings.slope_days - 1] - 1)
        if math.isfinite(sma[-1]):
            values[f"distance_sma{period}"] = float(close[-1] / sma[-1] - 1)
    pivots = confirmed_swings(bars, settings.pivot_left, settings.pivot_right)
    for kind, name in (("high", "higher_highs"), ("low", "higher_lows")):
        selected = [pivot for pivot in pivots if pivot["kind"] == kind]
        if len(selected) >= 2:
            values[name] = float(selected[-1]["price"] > selected[-2]["price"])
    price_names = {
        "close",
        "sma50",
        "sma200",
        "ema20",
        "ema50",
        "observed_high",
        "atr14",
        "macd",
        "macd_signal",
    }
    ratio_names = set(names) - price_names - {"rsi14", "higher_highs", "higher_lows"}
    result = {
        name: _observed(
            value,
            bars,
            snapshot.asset.quote_asset
            if name in price_names and snapshot
            else ("fraction" if name in ratio_names else "index"),
            "calculated from closed available contiguous daily candles"
            if value is not None
            else "insufficient history or nonidentifiable denominator",
        )
        for name, value in values.items()
    }
    ath = next(
        (
            snapshot.market.get(name)
            for name in ("all_time_high", "ath")
            if snapshot and snapshot.market.get(name)
        ),
        None,
    )
    if (
        snapshot
        and ath
        and ath.usable(as_of, settings.metric_max_age_seconds)
        and ath.value
        and ath.unit == snapshot.asset.quote_asset
        and ath.value >= high
    ):
        result["drawdown_ath"] = Metric(
            value=float(close[-1] / ath.value - 1),
            observed_at=bars[-1].close_time,
            available_at=max(max(b.available_at for b in bars), ath.available_at),
            source=f"{bars[-1].source} + {ath.source}",
            status=Status.AVAILABLE,
            unit="fraction",
            reason="reliable dated ATH in the same quote currency; distinct from supplied-history high",
        )
    else:
        result["drawdown_ath"] = _unknown(
            "reliable contemporaneous ATH in the same quote currency unavailable", unit="fraction"
        )
    return result, bars, structure_events(snapshot, as_of, settings) if snapshot else []


def structure_events(
    snapshot: AssetSnapshot, as_of: datetime, settings: CycleSettings | None = None
) -> list[StructureEvent]:
    """Emit BOS/CHoCH only after pivot confirmation and a subsequent close cross."""
    as_of = aware(as_of)
    settings = settings or CycleSettings()
    bars, error = _daily(snapshot, as_of, settings)
    if error:
        return []
    pivots = confirmed_swings(bars, settings.pivot_left, settings.pivot_right)
    atr = _atr(bars)
    latest: dict[str, dict] = {}
    histories: dict[str, list[dict]] = {"high": [], "low": []}
    broken: set[tuple[str, int]] = set()
    trend = 0
    events = []
    confirmations: dict[int, list[dict]] = {}
    for pivot in pivots:
        confirmations.setdefault(pivot["confirmed_index"], []).append(pivot)
    for index, bar in enumerate(bars):
        for pivot in confirmations.get(index, []):
            latest[pivot["kind"]] = pivot
            histories[pivot["kind"]].append(pivot)
        if len(histories["high"]) >= 2 and len(histories["low"]) >= 2:
            hh = histories["high"][-1]["price"] > histories["high"][-2]["price"]
            hl = histories["low"][-1]["price"] > histories["low"][-2]["price"]
            lh = histories["high"][-1]["price"] < histories["high"][-2]["price"]
            ll = histories["low"][-1]["price"] < histories["low"][-2]["price"]
            if hh and hl:
                trend = 1
            elif lh and ll:
                trend = -1
        for kind, direction in (("high", 1), ("low", -1)):
            pivot = latest.get(kind)
            if not pivot or index == 0 or (kind, pivot["index"]) in broken:
                continue
            discovery = datetime.fromisoformat(pivot["confirmed_at"])
            if discovery > bar.available_at:
                continue
            if settings.structure_buffer_atr > 0 and not math.isfinite(atr[index]):
                continue
            buffer = float(atr[index] * settings.structure_buffer_atr) if math.isfinite(atr[index]) else 0.0
            level = pivot["price"] + direction * buffer
            crossed = direction * (bar.close - level) > 0 and direction * (bars[index - 1].close - level) <= 0
            if not crossed:
                continue
            event_kind = ("CHOCH" if trend and trend != direction else "BOS") + (
                "_UP" if direction == 1 else "_DOWN"
            )
            events.append(
                StructureEvent(
                    asset_id=snapshot.asset.asset_id,
                    kind=event_kind,
                    direction=direction,
                    event_timestamp=bar.close_time,
                    discovery_timestamp=max(bar.available_at, discovery),
                    close=bar.close,
                    level=pivot["price"],
                    level_timestamp=bars[pivot["index"]].close_time,
                    level_discovery_timestamp=discovery,
                    buffer=buffer,
                    source=bar.source,
                )
            )
            broken.add((kind, pivot["index"]))
            trend = direction
    return events


def _value(metrics: dict[str, Metric], name: str) -> float | None:
    metric = metrics.get(name)
    return metric.value if metric and metric.status == Status.AVAILABLE else None


def _breadth(
    candidates: list[AssetSnapshot],
    as_of: datetime,
    endpoint: datetime | None,
    settings: CycleSettings,
    benchmark_ids: set[str] | None = None,
) -> BreadthReport:
    result = BreadthReport(supplied_assets=len(candidates), evaluated_assets=0, endpoint=endpoint)
    rows: dict[str, tuple[dict[str, Metric], list[Candle]]] = {}
    for candidate in candidates:
        metrics, bars, _ = _asset_metrics(candidate, as_of, settings)
        asset_id = candidate.asset.asset_id
        if asset_id in (benchmark_ids or set()):
            result.excluded[asset_id] = "benchmark excluded from altcoin breadth"
            continue
        if asset_id in rows or asset_id in result.excluded:
            rows.pop(asset_id, None)
            result.excluded[asset_id] = "duplicate asset identity in candidate cohort"
        elif not bars:
            result.excluded[asset_id] = metrics["close"].reason or "daily data unavailable"
        elif endpoint and bars[-1].close_time != endpoint:
            result.excluded[asset_id] = "daily endpoint differs from BTC context"
        elif _value(metrics, "sma50") is None:
            result.excluded[asset_id] = "fewer than 50 closed daily bars"
        else:
            rows[asset_id] = metrics, bars
    if endpoint is None and rows:
        endpoints = [bars[-1].close_time for _, bars in rows.values()]
        endpoint = max(set(endpoints), key=lambda value: (endpoints.count(value), value))
        result.endpoint = endpoint
        for asset_id, (_, bars) in list(rows.items()):
            if bars[-1].close_time != endpoint:
                result.excluded[asset_id] = "daily endpoint differs from cohort endpoint"
                del rows[asset_id]
    result.evaluated_assets = len(rows)
    result.sma50_assets = len(rows)
    result.sma200_assets = sum(_value(metrics, "sma200") is not None for metrics, _ in rows.values())
    result.return_assets = sum(_value(metrics, "return_7d") is not None for metrics, _ in rows.values())
    result.volume_assets = sum(_value(metrics, "relative_volume") is not None for metrics, _ in rows.values())
    result.volatility_assets = sum(
        _value(metrics, "volatility_ratio_7d_21d") is not None for metrics, _ in rows.values()
    )
    if len(rows) < settings.minimum_breadth_assets:
        result.reasons.append(
            f"insufficient dated breadth: {len(rows)} assets; minimum {settings.minimum_breadth_assets}"
        )
        return result
    metrics_list = [metrics for metrics, _ in rows.values()]
    result.available_at = max(max(bar.available_at for bar in bars) for _, bars in rows.values())
    result.above_sma50 = sum(
        _value(metrics, "close") > _value(metrics, "sma50") for metrics in metrics_list
    ) / len(rows)
    sma200_rows = [metrics for metrics in metrics_list if _value(metrics, "sma200") is not None]
    if len(sma200_rows) >= settings.minimum_breadth_assets:
        result.above_sma200 = sum(
            _value(metrics, "close") > _value(metrics, "sma200") for metrics in sma200_rows
        ) / len(sma200_rows)
    for name, target, predicate in (
        ("relative_volume", "volume_breadth", lambda value: value > 1),
        ("volatility_ratio_7d_21d", "volatility_breadth", lambda value: value > 1),
        ("new_high_30d", "new_highs_30d", lambda value: value == 1),
        ("new_low_30d", "new_lows_30d", lambda value: value == 1),
    ):
        values = [_value(metrics, name) for metrics in metrics_list]
        known = [value for value in values if value is not None]
        if len(known) >= settings.minimum_breadth_assets:
            setattr(result, target, sum(predicate(value) for value in known) / len(known))
    returns = [_value(metrics, "return_7d") for metrics in metrics_list]
    known_returns = [value for value in returns if value is not None]
    if len(known_returns) >= settings.minimum_breadth_assets:
        result.median_return_7d = median(known_returns)
    result.reasons.append("breadth uses equal-weight unique assets with matched closed daily endpoints")
    return result


def _phase(
    metrics: dict[str, Metric],
    events: list[StructureEvent],
    as_of: datetime,
    settings: CycleSettings,
    previous: MarketCycleReport | None,
) -> tuple[CyclePhase, str]:
    def v(name: str) -> float | None:
        return _value(metrics, f"btc_{name}")

    close, sma50, sma200 = v("close"), v("sma50"), v("sma200")
    r7, r30, slope50, slope200 = v("return_7d"), v("return_30d"), v("sma50_slope"), v("sma200_slope")
    if any(value is None for value in (close, sma50, sma200, r7, r30, slope50, slope200)):
        return (
            CyclePhase.NEUTRAL_UNCERTAIN,
            "insufficient fresh BTC daily trend history; no cycle phase inferred",
        )
    rsi, rvol, drawdown, wick = (
        v("rsi14"),
        v("relative_volume"),
        v("drawdown_observed_high"),
        v("lower_wick_fraction"),
    )
    range_width, distance = v("range_width_30d"), v("distance_sma50")
    recent = [
        event
        for event in events
        if event.event_timestamp >= as_of - timedelta(days=settings.recent_structure_days)
    ]
    choch_down = any(event.kind == "CHOCH_DOWN" for event in recent)
    bos_up = any(event.direction == 1 for event in recent)
    was_bull = previous is not None and previous.phase in {
        CyclePhase.EARLY_BULL,
        CyclePhase.BULL_EXPANSION,
        CyclePhase.LATE_BULL,
        CyclePhase.EUPHORIA,
        CyclePhase.DISTRIBUTION,
    }
    was_bear = previous is not None and previous.phase in {
        CyclePhase.EARLY_BEAR,
        CyclePhase.BEAR_EXPANSION,
        CyclePhase.CAPITULATION,
        CyclePhase.KNOCKOUT,
    }
    up = close > sma50 > sma200 and slope50 > 0 and slope200 >= 0
    down = close < sma50 < sma200 and slope50 < 0 and slope200 <= 0
    checks = [
        (
            CyclePhase.KNOCKOUT,
            drawdown is not None
            and drawdown <= settings.knockout_drawdown
            and wick is not None
            and wick >= settings.knockout_lower_wick_fraction
            and rvol is not None
            and rvol >= settings.abnormal_volume_ratio,
            "extreme observed-history drawdown, lower-wick reclaim and abnormal daily volume",
        ),
        (
            CyclePhase.CAPITULATION,
            r7 <= settings.capitulation_return_7d
            and rsi is not None
            and rsi <= settings.capitulation_rsi
            and rvol is not None
            and rvol >= settings.abnormal_volume_ratio,
            "large seven-day decline, low RSI and abnormal daily volume",
        ),
        (
            CyclePhase.ARMAGEDDON_TRANSITION,
            was_bull and close < sma50 and choch_down,
            "prior bullish research phase, current SMA50 loss and confirmed bearish CHoCH",
        ),
        (
            CyclePhase.EUPHORIA,
            up
            and distance is not None
            and distance >= settings.euphoria_distance_sma50
            and rsi is not None
            and rsi >= settings.euphoria_rsi,
            "bullish SMA structure with extreme SMA50 extension and RSI",
        ),
        (
            CyclePhase.LATE_BULL,
            up
            and distance is not None
            and distance >= settings.late_bull_distance_sma50
            and rsi is not None
            and rsi >= settings.late_bull_rsi,
            "bullish SMA structure with elevated extension and RSI",
        ),
        (
            CyclePhase.BULL_EXPANSION,
            up and r30 >= settings.expansion_return_30d,
            "close above SMA50 above SMA200, improving slopes and positive 30-day expansion",
        ),
        (
            CyclePhase.DISTRIBUTION,
            was_bull
            and close < sma50
            and drawdown is not None
            and drawdown > settings.distribution_max_drawdown
            and r30 <= 0,
            "prior bullish phase, SMA50 loss near supplied-history high and nonpositive 30-day return",
        ),
        (
            CyclePhase.EARLY_BULL,
            close > sma200 and sma50 >= sma200 and slope50 > 0 and r30 > 0,
            "close above SMA200 with positive SMA50 slope and 30-day return",
        ),
        (
            CyclePhase.BEAR_EXPANSION,
            down and r30 <= settings.bear_return_30d,
            "close below SMA50 below SMA200, negative slopes and large 30-day decline",
        ),
        (
            CyclePhase.REVIVAL,
            close < sma200
            and range_width is not None
            and range_width <= settings.revival_range_30d
            and bos_up
            and v("higher_lows") == 1,
            "below SMA200 but bounded base, confirmed bullish structure break and higher pivot lows",
        ),
        (
            CyclePhase.RECOVERY,
            close < sma200 and slope50 > 0 and r30 >= settings.recovery_return_30d,
            "positive SMA50 slope and 30-day recovery while still below SMA200",
        ),
        (
            CyclePhase.EARLY_RECOVERY,
            was_bear and close < sma200 and r7 > 0 and rsi is not None and rsi >= settings.early_recovery_rsi,
            "prior bearish phase followed by positive seven-day return and RSI recovery",
        ),
        (
            CyclePhase.ACCUMULATION,
            close < sma200
            and range_width is not None
            and range_width <= settings.accumulation_range_30d
            and abs(r30) <= settings.accumulation_return_30d
            and rvol is not None
            and rvol <= settings.accumulation_volume_ratio,
            "bounded daily range below SMA200, limited 30-day displacement and normal/contracted volume",
        ),
        (
            CyclePhase.EARLY_BEAR,
            close < sma200 and slope50 < 0 and r30 <= settings.early_bear_return_30d,
            "close below SMA200, falling SMA50 and negative 30-day return",
        ),
    ]
    for phase, passed, reason in checks:
        if passed:
            return phase, reason + "; threshold hypothesis, not validated predictive value"
    return (
        CyclePhase.NEUTRAL_UNCERTAIN,
        "available evidence does not meet a defined research-phase hypothesis",
    )


def classify_market_cycle(
    btc: AssetSnapshot | None,
    eth: AssetSnapshot | None,
    candidates: list[AssetSnapshot],
    global_metrics: dict[str, Metric],
    as_of: datetime,
    settings: CycleSettings | None = None,
    previous: MarketCycleReport | None = None,
    regime: RegimeReport | None = None,
) -> MarketCycleReport:
    """Calculate contextual research labels without gating any fast breakout."""
    as_of = aware(as_of)
    settings = settings or CycleSettings()
    if previous:
        previous = MarketCycleReport.model_validate(previous.model_dump())
    if previous and (previous.as_of >= as_of or previous.available_at > as_of):
        raise ValueError("previous cycle state must have been available before this decision")
    if regime and (
        regime.as_of is None
        or regime.available_at is None
        or regime.as_of > as_of
        or regime.available_at > as_of
    ):
        raise ValueError("regime context must be dated and available by the decision")
    btc_metrics, btc_bars, btc_events = _asset_metrics(btc, as_of, settings)
    eth_metrics, eth_bars, eth_events = _asset_metrics(eth, as_of, settings)
    metrics = {
        f"{prefix}_{name}": metric
        for prefix, values in (("btc", btc_metrics), ("eth", eth_metrics))
        for name, metric in values.items()
    }
    breadth = _breadth(
        candidates,
        as_of,
        btc_bars[-1].close_time if btc_bars else None,
        settings,
        {snapshot.asset.asset_id for snapshot in (btc, eth) if snapshot},
    )
    if btc_bars and eth_bars:
        left = {bar.close_time: bar.close for bar in btc_bars}
        right = {bar.close_time: bar.close for bar in eth_bars}
        end = btc_bars[-1].close_time
        start = end - timedelta(days=7)
        if end == eth_bars[-1].close_time and start in left and start in right:
            metrics["eth_btc_return_7d"] = (
                Metric(
                    value=(right[end] / left[end]) / (right[start] / left[start]) - 1,
                    observed_at=end,
                    available_at=max(
                        max(b.available_at for b in btc_bars), max(b.available_at for b in eth_bars)
                    ),
                    source=f"{btc_bars[-1].source} + {eth_bars[-1].source}",
                    unit="fraction",
                    status=Status.AVAILABLE,
                    reason="matched seven-day endpoints and quote currencies",
                )
                if btc and eth and btc.asset.quote_asset == eth.asset.quote_asset
                else _unknown("BTC/ETH quote currencies differ")
            )
    metrics.setdefault("eth_btc_return_7d", _unknown("matched BTC and ETH daily endpoints unavailable"))
    global_names = (
        "btc_dominance",
        "btc_dominance_change_7d",
        "total_market_cap",
        "total2_market_cap",
        "total3_market_cap",
        "total_return_7d",
        "total2_return_7d",
        "total3_return_7d",
        "stablecoin_mcap_change_7d",
        "sector_relative_strength_7d",
        "derivatives_risk_score",
    )
    for name in global_names:
        metric = global_metrics.get(name)
        metrics[name] = _input_metric(metric, as_of, settings.metric_max_age_seconds)
        if name.endswith("market_cap") and metrics[name].value is not None and metrics[name].value < 0:
            metrics[name] = _unknown(
                "negative market capitalization is invalid", metrics[name].source, metrics[name].unit
            )
    for prefix, snapshot in (("btc", btc), ("eth", eth)):
        for name in (
            "funding_rate",
            "open_interest",
            "open_interest_change_pct",
            "basis_pct",
            "liquidations_long",
            "liquidations_short",
        ):
            metric = snapshot.derivatives.get(name) if snapshot and snapshot.as_of <= as_of else None
            value = _input_metric(metric, as_of, settings.metric_max_age_seconds)
            if (
                name in {"open_interest", "liquidations_long", "liquidations_short"}
                and value.value is not None
                and value.value < 0
            ):
                value = _unknown("negative notional/position size is invalid", value.source, value.unit)
            metrics[f"{prefix}_{name}"] = value
    for name, value in (
        ("alt_breadth", breadth.above_sma50),
        ("alt_breadth_sma200", breadth.above_sma200),
        ("alt_median_return_7d", breadth.median_return_7d),
        ("volume_breadth", breadth.volume_breadth),
        ("volatility_breadth", breadth.volatility_breadth),
        ("alt_new_highs_30d", breadth.new_highs_30d),
        ("alt_new_lows_30d", breadth.new_lows_30d),
    ):
        metrics[name] = (
            Metric(
                value=value,
                observed_at=breadth.endpoint,
                available_at=breadth.available_at,
                source="dated candidate cohort",
                unit="fraction",
                status=Status.AVAILABLE,
                reason=breadth.basis,
            )
            if value is not None
            else _unknown("insufficient contemporaneous breadth cohort")
        )
    phase, reason = _phase(metrics, btc_events, as_of, settings, previous)
    coverage_weights = {
        "btc_close": 3,
        "btc_sma50": 3,
        "btc_sma200": 3,
        "btc_sma50_slope": 2,
        "btc_sma200_slope": 2,
        "btc_return_30d": 2,
        "btc_drawdown_observed_high": 1,
        "btc_realized_volatility_30d": 1,
        "btc_relative_volume": 1,
        "eth_close": 2,
        "eth_sma50": 1,
        "eth_btc_return_7d": 2,
        "alt_breadth": 3,
        "alt_median_return_7d": 2,
        "volume_breadth": 1,
        "btc_dominance_change_7d": 2,
        "total3_return_7d": 2,
        "btc_funding_rate": 1,
        "btc_open_interest_change_pct": 1,
    }
    evidence = (
        100
        * sum(weight for name, weight in coverage_weights.items() if _value(metrics, name) is not None)
        / sum(coverage_weights.values())
    )
    reasons = [
        reason,
        "cycle labels are context-only research hypotheses; no predictive probability is supplied",
    ]
    if btc_bars:
        reasons.append(
            "drawdown_observed_high uses only the supplied past daily history; it is not an all-time-high claim"
        )
    if regime is None:
        reasons.append(
            "existing regime report not supplied; regime context remains NEUTRAL, not evidence of market safety"
        )
    return MarketCycleReport(
        as_of=as_of,
        available_at=as_of,
        phase=phase,
        previous_phase=previous.phase if previous else None,
        changed=bool(previous and previous.phase != phase),
        regime=regime.regime if regime else Regime.NEUTRAL,
        formula_version=settings.formula_version,
        evidence_score=evidence,
        metrics=metrics,
        breadth=breadth,
        structure_events=btc_events + eth_events,
        reasons=reasons,
        missing=[name for name, metric in metrics.items() if metric.status != Status.AVAILABLE],
        settings=settings.model_dump(mode="json"),
    )


def market_congruence(
    coin: AssetSnapshot,
    cycle: MarketCycleReport,
    as_of: datetime,
    settings: CycleSettings | None = None,
    sector_metrics: dict[str, Metric] | None = None,
) -> MarketCongruenceReport:
    """Explain market agreement/conflict separately from the coin's setup score."""
    as_of = aware(as_of)
    settings = settings or CycleSettings()
    cycle = MarketCycleReport.model_validate(cycle.model_dump())
    if cycle.as_of > as_of or cycle.available_at > as_of:
        raise ValueError("market congruence cannot consume a future cycle report")
    coin_metrics, bars, _ = _asset_metrics(coin, as_of, settings)
    stale_context = (as_of - cycle.as_of).total_seconds() > settings.context_max_age_seconds
    coin_return = _value(coin_metrics, "return_7d")
    endpoint = bars[-1].close_time if bars else None
    direction = (
        0
        if coin_return is None or abs(coin_return) <= settings.neutral_return
        else (1 if coin_return > 0 else -1)
    )
    components = []
    missing = []
    reasons = []
    weights = {
        "btc_direction": 3,
        "eth_direction": 2,
        "total3_direction": 2,
        "sector_strength": 1,
        "alt_breadth": 2,
        "dominance_rotation": 1,
        "volume_breadth": 1,
        "risk_regime": 2,
        "derivatives_context": 1,
    }

    def usable(name: str, align_daily: bool = False) -> float | None:
        metric = cycle.metrics.get(name)
        if stale_context or not metric or not metric.usable(as_of, settings.metric_max_age_seconds):
            return None
        if align_daily and metric.observed_at != endpoint:
            return None
        return metric.value

    def agreement(value: float | None) -> float | None:
        if value is None or coin_return is None:
            return None
        if not direction or abs(value) <= settings.neutral_return:
            return 0.5
        return float(direction * value > 0)

    btc_return, eth_return, total3 = (
        usable("btc_return_7d", True),
        usable("eth_return_7d", True),
        usable("total3_return_7d"),
    )
    breadth, dominance, vol_breadth = (
        usable("alt_breadth", True),
        usable("btc_dominance_change_7d"),
        usable("volume_breadth", True),
    )
    derivatives = usable("derivatives_risk_score")
    sector = (sector_metrics or {}).get("sector_relative_strength_7d")
    sector_return = (
        sector.value
        if sector
        and sector.usable(as_of, settings.metric_max_age_seconds)
        and sector.unit == "fraction"
        and not stale_context
        else None
    )
    normalized = {
        "btc_direction": agreement(btc_return),
        "eth_direction": agreement(eth_return),
        "total3_direction": agreement(total3),
        "sector_strength": agreement(sector_return),
        "alt_breadth": None
        if breadth is None or coin_return is None
        else (breadth if direction >= 0 else 1 - breadth),
        "dominance_rotation": None
        if dominance is None or coin_return is None
        else (0.5 if dominance == 0 or not direction else float(-direction * dominance > 0)),
        "volume_breadth": vol_breadth if coin_return is not None else None,
        "risk_regime": None,
        "derivatives_context": None,
    }
    if not stale_context and coin_return is not None:
        bullish = cycle.regime in {
            Regime.RISK_ON,
            Regime.ALT_ROTATION,
            Regime.ALT_EXPANSION,
            Regime.BTC_EXPANSION,
        }
        bearish = cycle.regime in {Regime.RISK_OFF, Regime.SHAKEOUT}
        if bullish or bearish:
            normalized["risk_regime"] = float((direction >= 0 and bullish) or (direction < 0 and bearish))
    if derivatives is not None and coin_return is not None and 0 <= derivatives <= 100:
        normalized["derivatives_context"] = 1 - derivatives / 100
    raw = {
        "btc_direction": btc_return,
        "eth_direction": eth_return,
        "total3_direction": total3,
        "sector_strength": sector_return,
        "alt_breadth": breadth,
        "dominance_rotation": dominance,
        "volume_breadth": vol_breadth,
        "risk_regime": cycle.regime.value,
        "derivatives_context": derivatives,
    }
    total_weight = sum(weights.values())
    for name, weight in weights.items():
        value = normalized[name]
        contribution = value * weight / total_weight * 100 if value is not None else None
        if value is None:
            missing.append(name)
        components.append(
            ScoreComponent(
                name=name,
                weight=weight,
                normalized=value,
                contribution=contribution,
                reason="dated market agreement hypothesis; not validated edge"
                if value is not None
                else "required dated market/coin evidence unavailable, stale or unaligned",
                inputs={"coin_return_7d": coin_return, "market_input": raw[name]},
            )
        )
    coverage = (
        sum(component.weight for component in components if component.normalized is not None) / total_weight
    )
    lower_bound = sum(
        component.contribution for component in components if component.contribution is not None
    )
    upper_bound = min(100.0, lower_bound + 100 * (1 - coverage))
    score = DiscoveryScore(
        value=lower_bound if coverage >= settings.minimum_congruence_coverage else None,
        coverage=coverage,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        formula_version="market-congruence-research-v1",
        components=components,
    )
    conflict = bool(
        direction > 0
        and (
            btc_return is not None
            and btc_return < -settings.neutral_return
            or total3 is not None
            and total3 < -settings.neutral_return
            or cycle.regime in {Regime.RISK_OFF, Regime.SHAKEOUT}
        )
    )
    if conflict:
        reasons.append(
            "positive coin return conflicts with a negative broad-market input; coin setup is not automatically rejected"
        )
    if stale_context:
        reasons.append("market-cycle context is stale; congruence cannot be evaluated")
    if score.value is None:
        reasons.append("insufficient evidence coverage for a numeric market-congruence score")
    reasons.append(
        "market agreement measures context; personal watchlist priority and coin setup scores are not changed"
    )
    return MarketCongruenceReport(
        asset_id=coin.asset.asset_id,
        as_of=as_of,
        available_at=as_of,
        score=score,
        market_conflict=conflict,
        reasons=reasons,
        missing=missing,
    )
