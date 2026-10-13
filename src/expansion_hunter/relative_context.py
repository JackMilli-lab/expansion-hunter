"""Point-in-time relative returns and descriptive BTC dependence.

Returns use exact completed-candle endpoints, never equal-length arrays from
different clocks. BTC beta is OLS with an intercept on joint arithmetic returns;
upside/downside beta fit the subsets where the BTC return is positive/negative.
The default dependence window is at most 120 returns, with at least 32 joint
returns and eight per directional subset. Constant/near-constant series cannot
produce a usable correlation or dependence estimate. These are descriptive
statistics, not causal impact estimates or calibrated predictions.

Market/sector returns require explicitly supplied, dated observations. No sector
membership, basket return, cross-sectional rank or score weight is invented here.
Existing discovery_ranking remains the source of ranks and rank migration.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from statistics import fmean
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from .live_analysis import _known_bars
from .models import TIMEFRAMES, AssetSnapshot, Candle, Model, aware

WINDOWS = {"15m": 900, "1h": 3600, "4h": 14400, "24h": 86400}
FRAMES = ("1m", "5m", "15m", "1h", "4h", "1D")


class RelativeContextSettings(Model):
    minimum_joint_returns: int = Field(default=32, ge=8, le=1000)
    rolling_window_returns: int = Field(default=120, ge=8, le=2000)
    minimum_side_returns: int = Field(default=8, ge=3, le=1000)
    fresh_frame_multiple: float = Field(default=1.5, ge=1, le=4)
    minimum_return_variance: float = Field(default=1e-16, gt=0)
    high_correlation_threshold: float = Field(default=0.75, gt=0, le=1)
    medium_correlation_threshold: float = Field(default=0.40, ge=0, lt=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.rolling_window_returns < self.minimum_joint_returns:
            raise ValueError("rolling window must accommodate minimum joint returns")
        if self.medium_correlation_threshold >= self.high_correlation_threshold:
            raise ValueError("medium correlation threshold must be below high threshold")
        return self


class BenchmarkReturn(Model):
    """An externally observed basket/index return; times and units are explicit."""

    start_timestamp: datetime
    end_timestamp: datetime
    available_at: datetime
    return_pct: float | None = Field(default=None, gt=-100)
    source: str = Field(min_length=1)
    quality: Literal["AVAILABLE", "DERIVED", "ESTIMATED", "STALE", "UNAVAILABLE"] = "AVAILABLE"
    unit: Literal["percent"] = "percent"
    constituent_count: int | None = Field(default=None, ge=1)
    sector: str | None = None

    @model_validator(mode="after")
    def causal(self):
        if self.start_timestamp >= self.end_timestamp or self.available_at < self.end_timestamp:
            raise ValueError("basket return requires ordered event and availability timestamps")
        if self.quality in {"AVAILABLE", "DERIVED", "ESTIMATED"} and self.return_pct is None:
            raise ValueError("available basket return requires an observed value")
        return self


def _iso(value: datetime | None) -> str | None:
    return aware(value).isoformat() if value else None


def _missing(reason: str, *, status: str = "UNAVAILABLE", **metadata: Any) -> dict:
    return {"value": None, "unit": "percentage points", "status": status,
            "quality": status, "source": None, "source_timestamp": None,
            "received_timestamp": None, "age_seconds": None, "reason": reason, **metadata}


def _venue(source: str) -> str:
    # These two transports are the same explicitly identified native spot venue.
    if source == "binance-public-ws" or re.fullmatch(r"binance/spot/[A-Z0-9]+/(?:1m|5m|15m|1h|4h|1D)", source):
        return "binance/spot"
    return source


def _benchmark_identity(snapshot: AssetSnapshot, name: str) -> bool:
    instrument = snapshot.asset.exchange_symbol
    expected = name + snapshot.asset.quote_asset.upper()
    if instrument:
        return instrument.upper() == expected
    return snapshot.asset.symbol.upper() in {name, expected}


def _history(snapshot: AssetSnapshot, frame: str, decision: datetime) -> tuple[list[Candle], list[str]]:
    cutoff = min(snapshot.as_of, decision)
    return _known_bars(snapshot.model_copy(update={"as_of": cutoff}), frame)


def _return_metric(bars: list[Candle], frame: str, seconds: int, decision: datetime) -> dict | None:
    end = bars[-1].close_time
    start = end - timedelta(seconds=seconds)
    by_close = {bar.close_time: bar for bar in bars}
    if start not in by_close:
        return None
    used = [bar for bar in bars if start <= bar.close_time <= end]
    value = 100 * (by_close[end].close / by_close[start].close - 1)
    if not math.isfinite(value):
        return None
    return {
        "value": value,
        "unit": "percent", "status": "AVAILABLE", "quality": "DERIVED",
        "source": bars[-1].source, "source_timestamp": _iso(end),
        "received_timestamp": _iso(max(bar.available_at for bar in used)),
        "start_timestamp": _iso(start), "end_timestamp": _iso(end),
        "timeframe": frame, "age_seconds": (decision - end).total_seconds(),
        "sample_count": len(used) - 1, "reason": "Exact completed close endpoints; arithmetic return.",
    }


def _asset_return(snapshot: AssetSnapshot, seconds: int, config: RelativeContextSettings) -> tuple[dict, list[Candle]]:
    failures = []
    stale = None
    for frame in FRAMES:
        if seconds % TIMEFRAMES[frame]:
            continue
        bars, errors = _history(snapshot, frame, snapshot.as_of)
        if errors:
            failures.extend(f"{frame}:{error}" for error in errors)
            continue
        metric = _return_metric(bars, frame, seconds, snapshot.as_of)
        if metric is None:
            failures.append(f"{frame}:insufficient_exact_window_endpoints")
            continue
        if metric["age_seconds"] > TIMEFRAMES[frame] * config.fresh_frame_multiple:
            stale = {**metric, "value": None, "status": "STALE", "quality": "STALE",
                     "reason": "Completed endpoint is too old for this timeframe."}
            continue
        return metric, bars
    if stale:
        return stale, []
    return _missing("No valid completed history for this window.", issues=failures, unit="percent"), []


def _relative_return(own: dict, snapshot: AssetSnapshot, benchmark: AssetSnapshot | None,
                     seconds: int, config: RelativeContextSettings, name: str) -> dict:
    if benchmark is None:
        return _missing(f"{name} completed-candle benchmark is unavailable.")
    if not _benchmark_identity(benchmark, name):
        return _missing(f"Provided benchmark instrument is not {name} in its declared quote asset.")
    if own["status"] != "AVAILABLE":
        return _missing("Asset return is unavailable or stale.", status=own["status"])
    if snapshot.asset.quote_asset.upper() != benchmark.asset.quote_asset.upper():
        return _missing("Quote assets differ; no unobserved currency conversion is permitted.")
    frame = own["timeframe"]
    bars, errors = _history(benchmark, frame, snapshot.as_of)
    if errors:
        return _missing("Invalid benchmark candle history.", issues=errors)
    if _iso(bars[-1].close_time) != own["end_timestamp"]:
        return _missing("Benchmark and asset do not have the identical latest completed endpoint.")
    if _venue(bars[-1].source) != _venue(own["source"]):
        return _missing("Benchmark venue/source is not comparable with the asset.")
    compared = _return_metric(bars, frame, seconds, snapshot.as_of)
    if compared is None:
        return _missing("Benchmark lacks the exact asset return start endpoint.")
    if compared["age_seconds"] > TIMEFRAMES[frame] * config.fresh_frame_multiple:
        return _missing("Benchmark endpoint is stale.", status="STALE")
    return {**own, "value": own["value"] - compared["value"], "unit": "percentage points",
            "source": f'{own["source"]}; {name}={compared["source"]}',
            "received_timestamp": max(own["received_timestamp"], compared["received_timestamp"]),
            "benchmark_return_pct": compared["value"], "benchmark": name,
            "reason": "Asset return minus benchmark return on identical close endpoints."}


def _basket_return(own: dict, observation: BenchmarkReturn | dict | None, decision: datetime, name: str) -> dict:
    if observation is None:
        return _missing(f"Dated {name} basket/index return and membership context are unavailable.")
    if own["status"] != "AVAILABLE":
        return _missing("Asset return is unavailable or stale.", status=own["status"])
    try:
        observed = observation if isinstance(observation, BenchmarkReturn) else BenchmarkReturn.model_validate(observation)
    except (ValidationError, TypeError, ValueError) as exc:
        return _missing("Invalid dated benchmark observation.", issues=[str(exc)])
    if observed.available_at > decision or observed.end_timestamp > decision:
        return _missing("Benchmark observation was not yet available at decision time.")
    if observed.quality in {"UNAVAILABLE", "STALE"}:
        return _missing("External benchmark is explicitly unavailable or stale.", status=observed.quality)
    if _iso(observed.start_timestamp) != own["start_timestamp"] or _iso(observed.end_timestamp) != own["end_timestamp"]:
        return _missing("External benchmark has different start/end timestamps.")
    return {**own, "value": own["value"] - observed.return_pct, "unit": "percentage points",
            "quality": "ESTIMATED" if observed.quality == "ESTIMATED" else "DERIVED",
            "source": f'{own["source"]}; {name}={observed.source}',
            "received_timestamp": max(own["received_timestamp"], _iso(observed.available_at)),
            "benchmark": name, "benchmark_return_pct": observed.return_pct,
            "constituent_count": observed.constituent_count, "sector": observed.sector,
            "reason": "Excess over explicitly supplied point-in-time benchmark; no inferred sector membership."}


def _moments(asset: list[float], btc: list[float], floor: float) -> tuple[float | None, float | None, float | None]:
    try:
        a_mean, b_mean = fmean(asset), fmean(btc)
        a_var = fmean((value - a_mean) * (value - a_mean) for value in asset)
        b_var = fmean((value - b_mean) * (value - b_mean) for value in btc)
        covariance = fmean((a - a_mean) * (b - b_mean) for a, b in zip(asset, btc, strict=True))
    except (OverflowError, ValueError):
        return None, None, None
    if not all(math.isfinite(value) for value in (a_var, b_var, covariance)) or min(a_var, b_var) <= floor:
        return None, None, None
    correlation = max(-1.0, min(1.0, covariance / math.sqrt(a_var) / math.sqrt(b_var)))
    beta, sensitivity = covariance / b_var, math.sqrt(a_var) / math.sqrt(b_var)
    return (correlation, beta, sensitivity) if all(math.isfinite(value) for value in (correlation, beta, sensitivity)) else (None, None, None)


def _dependency(snapshot: AssetSnapshot, btc: AssetSnapshot | None, config: RelativeContextSettings) -> dict:
    missing = {"status": "UNAVAILABLE", "quality": "UNAVAILABLE", "correlation": None,
               "beta": None, "upside_beta": None, "downside_beta": None,
               "reaction_sensitivity": None, "dependency": "UNAVAILABLE", "relationship": "UNAVAILABLE",
               "timeframe": None, "sample_count": 0, "upside_sample_count": 0, "downside_sample_count": 0,
               "coverage": 0.0, "source": None, "source_timestamp": None, "received_timestamp": None,
               "window_start": None, "window_end": None, "age_seconds": None}
    if btc is None:
        return {**missing, "reason": "BTC benchmark history is unavailable."}
    if not _benchmark_identity(btc, "BTC"):
        return {**missing, "reason": "Provided benchmark instrument is not BTC in its declared quote asset."}
    if snapshot.asset.quote_asset.upper() != btc.asset.quote_asset.upper():
        return {**missing, "reason": "Quote assets differ; no unobserved conversion is permitted."}
    failures = []
    for frame in FRAMES:
        left, left_errors = _history(snapshot, frame, snapshot.as_of)
        right, right_errors = _history(btc, frame, snapshot.as_of)
        if left_errors or right_errors:
            failures.extend(f"{frame}:{error}" for error in left_errors + right_errors)
            continue
        if left[-1].close_time != right[-1].close_time:
            failures.append(f"{frame}:latest_completed_endpoints_differ")
            continue
        age = (snapshot.as_of - left[-1].close_time).total_seconds()
        if age > TIMEFRAMES[frame] * config.fresh_frame_multiple:
            failures.append(f"{frame}:stale_current_endpoint")
            continue
        if _venue(left[-1].source) != _venue(right[-1].source):
            failures.append(f"{frame}:incomparable_benchmark_venue")
            continue
        a_by_close = {bar.close_time: bar for bar in left}
        b_by_close = {bar.close_time: bar for bar in right}
        joint = sorted(a_by_close.keys() & b_by_close.keys())[-config.rolling_window_returns - 1:]
        if len(joint) < config.minimum_joint_returns + 1:
            failures.append(f"{frame}:minimum_joint_returns_not_met")
            continue
        if any((end - start).total_seconds() != TIMEFRAMES[frame] for start, end in zip(joint, joint[1:], strict=False)):
            failures.append(f"{frame}:non_contiguous_joint_returns")
            continue
        asset_returns = [a_by_close[end].close / a_by_close[start].close - 1 for start, end in zip(joint, joint[1:], strict=False)]
        btc_returns = [b_by_close[end].close / b_by_close[start].close - 1 for start, end in zip(joint, joint[1:], strict=False)]
        if not all(math.isfinite(value) for value in asset_returns + btc_returns):
            failures.append(f"{frame}:non_finite_arithmetic_returns")
            continue
        correlation, beta, sensitivity = _moments(asset_returns, btc_returns, config.minimum_return_variance)
        if correlation is None:
            failures.append(f"{frame}:constant_or_insufficient_return_variation")
            continue
        directional = {}
        for label, sign in (("upside", 1), ("downside", -1)):
            pairs = [(a, b) for a, b in zip(asset_returns, btc_returns, strict=True) if b * sign > 0]
            side_beta = None
            if len(pairs) >= config.minimum_side_returns:
                _, side_beta, _ = _moments([pair[0] for pair in pairs], [pair[1] for pair in pairs], config.minimum_return_variance)
            directional[f"{label}_beta"] = side_beta
            directional[f"{label}_sample_count"] = len(pairs)
        sources = [left[-1].source, right[-1].source]
        received = max(max(a_by_close[stamp].available_at, b_by_close[stamp].available_at) for stamp in joint)
        absolute = abs(correlation)
        dependence = "HIGH" if absolute >= config.high_correlation_threshold else "MEDIUM" if absolute >= config.medium_correlation_threshold else "LOW"
        relationship = "INVERSE" if correlation < -config.medium_correlation_threshold else "SAME_DIRECTION" if correlation > config.medium_correlation_threshold else "WEAK_LINEAR_RELATIONSHIP"
        coverage = (3 + int(directional["upside_beta"] is not None) + int(directional["downside_beta"] is not None)) / 5
        return {**missing, **directional, "status": "AVAILABLE" if coverage == 1 else "PARTIAL",
                "quality": "DERIVED", "correlation": correlation, "beta": beta,
                "reaction_sensitivity": sensitivity, "reaction_sensitivity_definition": "asset/BTC joint-return standard-deviation ratio; not causal price impact",
                "dependency": dependence, "relationship": relationship,
                "timeframe": frame, "sample_count": len(asset_returns), "coverage": coverage,
                "source": "; BTC=".join(sources), "source_timestamp": _iso(joint[-1]),
                "received_timestamp": _iso(received), "age_seconds": age,
                "window_start": _iso(joint[0]), "window_end": _iso(joint[-1]),
                "reason": "Descriptive Pearson correlation and OLS slopes with intercept on exactly aligned completed returns.",
                "issues": failures}
    return {**missing, "status": "INSUFFICIENT_DATA", "reason": "No fresh comparable window with sufficient joint return variation.", "issues": failures}


def analyze_relative_context(
    asset_snapshot: AssetSnapshot,
    btc_snapshot: AssetSnapshot | None = None,
    eth_snapshot: AssetSnapshot | None = None,
    market_returns: dict[str, BenchmarkReturn | dict] | None = None,
    sector_returns: dict[str, BenchmarkReturn | dict] | None = None,
    *,
    settings: RelativeContextSettings | None = None,
) -> dict:
    """Compute observable context only; missing baskets never become zero returns."""
    config = settings or RelativeContextSettings()
    windows = {}
    for window, seconds in WINDOWS.items():
        own, _ = _asset_return(asset_snapshot, seconds, config)
        windows[window] = {
            "asset_return": own,
            "vs_btc": _relative_return(own, asset_snapshot, btc_snapshot, seconds, config, "BTC"),
            "vs_eth": _relative_return(own, asset_snapshot, eth_snapshot, seconds, config, "ETH"),
            "vs_market": _basket_return(own, (market_returns or {}).get(window), asset_snapshot.as_of, "market"),
            "vs_sector": _basket_return(own, (sector_returns or {}).get(window), asset_snapshot.as_of, "sector"),
        }
    dependency = _dependency(asset_snapshot, btc_snapshot, config)
    counts = {name: sum(result[name]["status"] == "AVAILABLE" for result in windows.values()) / len(WINDOWS)
              for name in ("asset_return", "vs_btc", "vs_eth", "vs_market", "vs_sector")}
    counts["btc_dependency"] = dependency["coverage"]
    coverage = fmean(counts.values())
    return {
        "symbol": asset_snapshot.asset.exchange_symbol or asset_snapshot.asset.symbol,
        "as_of": _iso(asset_snapshot.as_of),
        "status": "AVAILABLE" if coverage == 1 else "PARTIAL" if coverage > 0 else "INSUFFICIENT_DATA",
        "windows": windows, "btc_dependency": dependency, "coverage": coverage,
        "component_coverage": counts, "settings": config.model_dump(), "research_weight": 0,
        "ranking_source": "existing discovery_ranking; no rank or percentile is fabricated by this module",
        "limitations": ["Dependence is descriptive; correlation does not establish causation or independence.",
                        "Sector and market baskets require dated data supplied by the caller.",
                        "Returns compare matching quote assets and comparable sources, without currency conversion.",
                        "No production score weight, calibrated probability or predictive edge is asserted."],
    }
