"""Deterministic chronological replay of the integrated decision-support engine.

Only closed candles and records available at a supplied boundary can be used.
The first boundary initializes from visible history; it does not retrospectively
claim alerts or structure sequences before the replay started. Sorted duplicate
boundaries are evaluated once. This is a feature/alert replay, not a strategy
simulation, profitability estimate or a measured real-time latency benchmark.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable
from datetime import datetime

from pydantic import Field

from .discovery_config import DiscoverySettings
from .discovery_features import deep_scores
from .fast import FastIgnitionMonitor
from .fast_models import FastSettings
from .live_analysis import LiveAnalysisSettings, _known_bars, analyze_live
from .models import TIMEFRAMES, AssetSnapshot, Metric, Model, aware
from .pattern_engine import PatternSettings
from .relative_context import (
    RelativeContextSettings,
    _benchmark_identity,
    _venue,
    analyze_relative_context,
)
from .replay import point_in_time
from .war_room import analyze_war_room


class WarRoomReplaySettings(Model):
    maximum_boundaries: int = Field(default=1000, ge=1, le=20000)
    maximum_analysis_bars: int = Field(default=240, ge=32, le=5000)
    fast: FastSettings = Field(default_factory=FastSettings)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    analysis: LiveAnalysisSettings = Field(default_factory=LiveAnalysisSettings)
    patterns: PatternSettings = Field(default_factory=PatternSettings)
    relative: RelativeContextSettings = Field(default_factory=RelativeContextSettings)


def _dated_metrics(metrics: dict[str, Metric], when: datetime) -> dict[str, Metric]:
    # Future record shells and undated current metadata are not historical facts.
    return {key: metric.model_copy(deep=True) for key, metric in sorted(metrics.items())
            if metric.observed_at is not None and metric.available_at is not None
            and metric.observed_at <= when and metric.available_at <= when}


def _visible(snapshot: AssetSnapshot, when: datetime, maximum: int) -> tuple[AssetSnapshot, list[str]]:
    cut = point_in_time(snapshot, when).model_copy(deep=True)
    cut.asset = cut.asset.model_copy(update={"rank": None, "name": cut.asset.symbol})
    cut.market = _dated_metrics(cut.market, when)
    cut.derivatives = _dated_metrics(cut.derivatives, when)
    # Scalar historical depth does not establish an available order-book snapshot.
    cut.market.pop("observed_depth_quote", None)
    cut.issues = []
    candles, issues = {}, []
    # Fixed supported channels: adding a future-only timeframe must not disclose
    # that future coverage or alter an earlier decision's unavailable context.
    for frame in sorted(TIMEFRAMES, key=lambda value: TIMEFRAMES[value]):
        visible, errors = _known_bars(cut, frame)
        instrument = cut.asset.exchange_symbol
        if not errors and instrument and any(bar.source.startswith("binance/spot/")
                                             and bar.source != f"binance/spot/{instrument}/{frame}" for bar in visible):
            errors = ["native_source_instrument_does_not_match_asset"]
        candles[frame] = visible[-maximum:] if not errors else []
        issues.extend(f"{frame}:{error}" for error in errors)
    cut.candles = candles
    cut.issues = list(issues)
    return cut, issues


def _score_benchmark(snapshot: AssetSnapshot, btc: AssetSnapshot | None) -> AssetSnapshot | None:
    if btc is None or snapshot.asset.quote_asset.upper() != btc.asset.quote_asset.upper():
        return None
    for frame in ("4h", "1h", "15m", "5m"):
        own, compared = snapshot.candles.get(frame, []), btc.candles.get(frame, [])
        if len(own) >= 32:
            return btc if compared and _venue(own[-1].source) == _venue(compared[-1].source) else None
    return None


def replay_war_room(
    snapshot: AssetSnapshot,
    boundaries: Iterable[datetime],
    *,
    btc: AssetSnapshot | None = None,
    eth: AssetSnapshot | None = None,
    settings: WarRoomReplaySettings | None = None,
) -> dict:
    """Replay existing fast/structure/scenario/score calculations without I/O.

    Dated public records supplied by the caller are the only data source.
    Benchmarks are cut independently at every decision; unavailable consensus,
    regimes, order-book snapshots, intraminute trades or flow cannot be invented.
    ``available_at`` retains actual source receipt time, including delayed data.
    """
    config = settings or WarRoomReplaySettings()
    for name, benchmark in (("BTC", btc), ("ETH", eth)):
        if benchmark is not None and not _benchmark_identity(benchmark, name):
            raise ValueError(f"provided {name} benchmark instrument does not match its declared identity")
    decisions = sorted({aware(boundary) for boundary in boundaries})
    if len(decisions) > config.maximum_boundaries:
        raise ValueError("requested replay boundaries exceed configured maximum; no silent truncation")
    monitor = FastIgnitionMonitor(config.fast)
    symbol = snapshot.asset.exchange_symbol or snapshot.asset.symbol
    previous_structure = None
    previous_levels = []
    evaluations = []
    for when in decisions:
        cut, issues = _visible(snapshot, when, config.maximum_analysis_bars)
        btc_cut, btc_issues = _visible(btc, when, config.maximum_analysis_bars) if btc else (None, [])
        eth_cut, eth_issues = _visible(eth, when, config.maximum_analysis_bars) if eth else (None, [])
        issues += [f"BTC:{issue}" for issue in btc_issues] + [f"ETH:{issue}" for issue in eth_issues]
        tick = cut.market.get("tick_size")
        tick_size = tick.value if tick and tick.usable(when) and tick.value > 0 else None
        fast = monitor.evaluate(cut.asset, cut, evaluation_at=when, tick_size=tick_size,
                                regime="UNAVAILABLE")
        analysis = analyze_live(cut, prior_levels=previous_levels, config=config.analysis)
        scores, levels = deep_scores(cut, config.discovery, _score_benchmark(cut, btc_cut), bias=fast.state.bias)
        serialized_scores = {name: score.model_dump(mode="json") for name, score in scores.items()}
        analysis["scores"] = serialized_scores
        analysis["score_levels"] = levels
        analysis["market_regime"] = "UNAVAILABLE"
        relative = analyze_relative_context(cut, btc_cut, eth_cut, settings=config.relative)
        analysis["relative_context"] = relative
        structure = analyze_war_room(symbol, cut.candles, when, analysis, fast.state,
                                     previous_structure, pattern_settings=config.patterns)
        # Fingerprint only visible used inputs: changing future suffixes cannot
        # change an earlier row's lineage identifier or reconstructed decisions.
        digest = hashlib.sha256(cut.model_dump_json().encode("utf-8")).hexdigest()
        inputs = {"asset": cut.model_dump(mode="json"), "btc": btc_cut.model_dump(mode="json") if btc_cut else None,
                  "eth": eth_cut.model_dump(mode="json") if eth_cut else None,
                  "settings": config.model_dump(mode="json")}
        inputs_digest = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        evaluations.append({
            "as_of": when.isoformat(),
            "visible_snapshot_sha256": digest,
            "visible_inputs_sha256": inputs_digest,
            "fast_state": fast.state.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in fast.events],
            "war_room": copy.deepcopy(structure),
            "scores": serialized_scores,
            "score_levels": levels,
            "relative_context": relative,
            "overlays": analysis["overlays"],
            "liquidity_map": analysis["liquidity_map"],
            "fibonacci": analysis["fibonacci"],
            "data_quality": {**analysis["data_quality"], "market_regime": "UNAVAILABLE",
                             "macro_context": "UNAVAILABLE", "order_book_snapshots": "UNAVAILABLE",
                             "tick_size": "AVAILABLE" if tick_size is not None else "UNAVAILABLE",
                             "intraminute_trades": "UNAVAILABLE", "spot_cvd": "UNAVAILABLE",
                             "market_basket": "UNAVAILABLE", "sector_basket": "UNAVAILABLE"},
            "issues": sorted(set(issues + fast.issues + analysis["issues"] + structure["issues"])),
        })
        previous_structure = copy.deepcopy(structure)
        previous_levels = copy.deepcopy(analysis["liquidity_map"])
    return {
        "mode": "HISTORICAL_REPLAY",
        "algorithm_version": "war-room-replay-v1",
        "symbol": symbol,
        "boundary_policy": "TIMEZONE_AWARE_SORTED_UNIQUE; duplicates evaluated once",
        "evaluation_count": len(evaluations),
        "evaluations": evaluations,
        "settings": config.model_dump(mode="json"),
        "research_weight": 0,
        "execution_enabled": False,
        "latency_interpretation": "Historical availability and supplied evaluation schedule; not measured network/runtime latency.",
        "limitations": [
            "Instrument identity is supplied by the caller; current market-cap rank and undated diagnostics are not historical features.",
            "Historical regime/macro labels and exact order-book snapshots are not supplied by AssetSnapshot and remain unavailable.",
            "Completed candles cannot reconstruct intraminute attempts or native trade-by-trade CVD.",
            "The first decision summarizes visible context; no prior alert lead time or earlier structure sequence is invented.",
            "Mixed, duplicate or gapped candle histories fail closed; data downloaded later retain their actual available_at.",
            "No return, probability, winrate, profitability or predictive-edge claim is produced by this replay.",
        ],
    }
