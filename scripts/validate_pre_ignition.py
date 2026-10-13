"""Bounded real-data counterfactual and explicitly synthetic throughput audit.

This is offline research: cached historical pressure transitions are not a
complete native episode replay, and no market fixtures enter the application.
"""
from __future__ import annotations

import argparse
import bisect
import copy
import hashlib
import json
import resource
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean

from expansion_hunter import missed_move_forensics as forensics
from expansion_hunter import pre_ignition as core
from expansion_hunter.pre_ignition import PreIgnitionEngine, causal_fast_features
from expansion_hunter.pre_ignition_research import evaluate_daily
from expansion_hunter.top_mover_benchmark import ExpansionDefinition, label_expansions
from expansion_hunter.trade_alert import btc_context

INDEPENDENT = {"ETHUSDT", "XRPUSDT", "ADAUSDT", "OPUSDT", "ARBUSDT", "NEARUSDT"}


def stamp(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def prefix(history, at, limit=240):
    """Binary search only native close chronology, then gate actual availability."""
    closes, bars = history
    end = bisect.bisect_right(closes, at)
    return [bar for bar in bars[max(0, end-limit):end] if bar.available_at <= at]


def pressure_direction(row, at, five_close, price):
    """A previous state transition cannot refresh an older pressure calculation."""
    observed = stamp(row.get("observation_timestamp", row["as_of"]))
    pressure_close = observed.replace(minute=observed.minute//5*5, second=0, microsecond=0)
    discovered = row.get("trigger_discovered_at")
    if pressure_close != five_close or observed > at or not discovered or stamp(discovered) > at:
        return None
    level, distance = row.get("trigger"), row.get("distance_atr")
    metrics = copy.deepcopy(row.get("metrics", {}))
    atr = metrics.get("atr")
    if atr is None and level and distance and row.get("price"):
        atr = abs(level-row["price"])/abs(distance)
    if not atr or not level:
        return None
    sign = 1 if row["bias"] == "LONG" else -1
    return {"level": level, "level_discovered_at": discovered,
            "distance_atr": sign*(level-price)/atr, "metrics": metrics,
            "pressure_source_timestamp": pressure_close.isoformat(),
            "pressure_snapshot_timestamp": observed.isoformat()}


def causal_observations(index, cache, at):
    """Recalculate native fast inputs, but do not invent missing pressure vintages."""
    frames = {symbol: {frame: prefix(bars, at) for frame, bars in history.items() if frame in {"1m", "5m"}}
              for symbol, history in index.items()}
    fast = {symbol: causal_fast_features(history, at) for symbol, history in frames.items()}
    returns = [row["price_return_3m"] for row in fast.values() if row.get("price_return_3m") is not None]
    market_return = mean(returns) if returns else None
    observations, known_directions = {}, 0
    for symbol, bars in frames.items():
        one, five = bars.get("1m", []), bars.get("5m", [])
        if not one or not five:
            continue
        directions = {}
        for bias in ("LONG", "SHORT"):
            cached = cache.get((symbol, bias))
            if cached:
                direction = pressure_direction(cached, at, five[-1].close_time, one[-1].close)
                if direction:
                    directions[bias] = direction
        known_directions += len(directions)
        observations[symbol] = {"symbol": symbol, "as_of": at.isoformat(), "price": one[-1].close,
            "status": "EVALUATED", "directions": directions, "fast_features": fast[symbol],
            "btc_context": btc_context(five, frames.get("BTCUSDT", {}).get("5m", []), at),
            "relative_market_return": fast[symbol].get("price_return_3m")-market_return
            if fast[symbol].get("price_return_3m") is not None and market_return is not None else None,
            "data_quality": {"1m": fast[symbol]["quality"], "5m": "AVAILABLE" if directions else "UNAVAILABLE",
                "order_book": "UNAVAILABLE", "CVD": "UNAVAILABLE", "futures": "UNAVAILABLE"}}
    return observations, known_directions


def replay(evidence, transitions, start, end):
    """Minute-close counterfactual with immutable historical pressure timestamps."""
    index = {symbol: {frame: ([bar.close_time for bar in bars], bars) for frame, bars in history.items()}
             for symbol, history in evidence["histories"].items()}
    ordered = sorted(transitions, key=lambda row: (stamp(row["as_of"]), row["asset_id"], row["bias"]))
    cursor, cache, engine = 0, {}, PreIgnitionEngine()
    alerts, state_counts, available_pressure, elapsed = [], Counter(), 0, []
    at = start
    iterations = 0
    maximum_score = 0.
    while at <= end:
        while cursor < len(ordered) and stamp(ordered[cursor]["as_of"]) <= at:
            row = ordered[cursor]
            cache[(row["asset_id"], row["bias"])] = row
            cursor += 1
        before = time.perf_counter()
        observations, known = causal_observations(index, cache, at)
        rows = engine.update(observations, {}, at)  # Native confirmations deliberately not reconstructed.
        elapsed.append((time.perf_counter()-before)*1000)
        available_pressure += known
        for row in rows:
            state_counts[row["state"]] += 1
            maximum_score = max(maximum_score, row["score"])
            if row["state"] == "READY" and row["state_changed"]:
                alerts.append(dict(row, phase="TRADE_ALERT", emitted=True,
                    detected_at=at.isoformat(), event_timestamp=at.isoformat(),
                    event_id=hashlib.sha256(f"{row['asset_id']}|{row['bias']}|{at.isoformat()}".encode()).hexdigest()))
        iterations += 1
        at += timedelta(minutes=1)
    return alerts, {"minutes": iterations, "symbols": len(index), "frozen_settings": engine.settings.model_dump(mode="json"),
        "directional_states": sum(state_counts.values()),
        "pressure_vintages_available": available_pressure,
        "pressure_vintage_coverage": available_pressure/sum(state_counts.values()) if state_counts else None,
        "state_counts": dict(state_counts), "maximum_pre_ignition_score": maximum_score,
        "processing_ms": percentiles(elapsed)}


def percentiles(values):
    ordered = sorted(values)
    return {name: ordered[min(len(ordered)-1, int((len(ordered)-1)*q))] if ordered else None
            for name, q in (("p50", .5), ("p95", .95), ("p99", .99))}


def load_benchmark(symbols=500, iterations=100):
    """SYNTHETIC throughput fixtures, not exchange market-data or signals."""
    now = datetime(2026, 10, 1, tzinfo=UTC)
    observations = {}
    for index in range(symbols):
        directions = {bias: {"level": 100.2 if bias == "LONG" else 99.8,
            "level_discovered_at": now.isoformat(), "distance_atr": .2,
            "metrics": {"higher_low_strength": .2, "lower_high_strength": .2,
                "resistance_test_count": 3, "support_test_count": 3, "rejection_weakening": .5,
                "taker_buy_ratio": .5}} for bias in ("LONG", "SHORT")}
        observations[f"PERF{index:04d}"] = {"symbol": f"PERF{index:04d}", "as_of": now.isoformat(),
            "price": 100., "directions": directions, "data_quality": {"1m": "AVAILABLE", "5m": "AVAILABLE"},
            "btc_context": {"status": "AVAILABLE", "relative_return": .001, "btc_return": 0.},
            "fast_features": {"quality": "AVAILABLE", "evidence_timestamp": now.isoformat(),
                "available_at": now.isoformat(), "fingerprint": "initial", "atr": 1.,
                "range_compression": .5, "atr_compression": .5, "volatility_compression": .5,
                "relative_volume": 2., "volume_zscore": 1., "volume_acceleration": .5,
                "volume_acceleration_zscore": 1., "trade_count_acceleration": .2,
                "trade_count_acceleration_zscore": 1., "price_return_3m": .001, "vwap_distance_atr": .1,
                "relative_return_scale": .001, "taker_buy_ratio": .5}}
    engine, latencies = PreIgnitionEngine(), []
    cpu_start, wall_start = time.process_time(), time.perf_counter()
    for iteration in range(iterations):
        at = now+timedelta(minutes=iteration)
        for row in observations.values():
            row["as_of"] = at.isoformat()
            row["fast_features"].update(evidence_timestamp=at.isoformat(), available_at=at.isoformat(), fingerprint=str(iteration))
        begin = time.perf_counter()
        engine.update(observations, {}, at)
        latencies.append((time.perf_counter()-begin)*1000)
    wall = time.perf_counter()-wall_start
    return {"data_type": "EXPLICIT_SYNTHETIC_PERFORMANCE_FIXTURES_ONLY", "symbols": symbols,
        "directional_rows_per_update": 2*symbols, "updates": iterations, "wall_seconds": wall,
        "cpu_seconds": time.process_time()-cpu_start, "peak_process_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "whole_universe_update_ms": percentiles(latencies), "directional_rows_per_second": 2*symbols*iterations/wall,
        "dropped_events": None, "reconnects": None,
        "limitations": "Core scoring only; no network, SQLite, candle feature derivation, 500-market live coverage or event queue load"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=Path("reports/top_mover_2026-10-09/raw"))
    parser.add_argument("--cached-signals", type=Path, default=Path("reports/top_mover_2026-10-09/replay_signals.json"))
    parser.add_argument("--output", type=Path, default=Path("reports/pre_ignition_upgrade/validation.json"))
    parser.add_argument("--load-only", action="store_true")
    parser.add_argument("--iterations", type=int, default=100)
    args = parser.parse_args(argv)
    report = {"validation_type": "COUNTERFACTUAL_CACHED_PRESSURE_RESEARCH_NOT_FULL_RUNTIME_REPLAY",
        "core_source_sha256": hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest(),
        "fixed_weights": True, "threshold_tuning": False, "execution_enabled": False,
        "blind_holdout": False, "live_500_market_verification": False,
        "limitations": ["Oct8 and Oct9 evidence already examined; chronological split is not a blind holdout.",
            "Only cached pressure state transitions present; missing current5m pressure vintages stay unavailable.",
            "Native episode confirmations are excluded, so READY-only counterfactual does not replace full production replay.",
            "Instant historical close availability is an optimistic declared receipt model, not archived live latency.",
            "15 fixed supported Binance assets, not reconstructed top500 market membership.",
            "No trade fills, true order book, CVD, funding/OI contribution or profitability assertion."]}
    if not args.load_only:
        evidence = forensics.load_evidence(args.raw_dir)
        cached = json.loads(args.cached_signals.read_text())
        start = datetime(2026, 10, 7, 23, tzinfo=UTC)
        alerts, replay_measurement = replay(evidence, cached["state_transitions"], start, evidence["as_of"])
        definition = ExpansionDefinition()
        labels = [row for symbol, frames in evidence["histories"].items()
                  for row in label_expansions(symbol, frames["1m"], evidence["as_of"], definition)
                  if stamp(row["expansion_start"]) >= datetime(2026, 10, 8, tzinfo=UTC)]
        controls = forensics.make_controls(evidence, datetime(2026, 10, 8, tzinfo=UTC), evidence["as_of"], definition)
        coverage = {symbol: {"status": "REPLAY_ASSUMED_MONITORED", "scope": report["validation_type"]}
                    for symbol in evidence["histories"]}
        evaluations = {}
        for name, wanted in (("previous_UTC_day", lambda row: stamp(row["expansion_start"]).day == 8),
                ("later_UTC_day", lambda row: stamp(row["expansion_start"]).day == 9),
                ("fixed_independent_asset_cohort", lambda row: row["symbol"] in INDEPENDENT)):
            chosen = [row for row in labels if wanted(row)]
            chosen_controls = [row for row in controls if (name != "fixed_independent_asset_cohort" and
                stamp(row["start"]).day == (8 if name == "previous_UTC_day" else 9)) or
                (name == "fixed_independent_asset_cohort" and row["symbol"] in INDEPENDENT)]
            evaluations[name] = {"new_ready": evaluate_daily(chosen, alerts, controls=chosen_controls, coverage=coverage, as_of=evidence["as_of"]),
                "existing_trade_alert": evaluate_daily(chosen, cached["alerts"], controls=chosen_controls, coverage=coverage, as_of=evidence["as_of"])}
        report.update(as_of=evidence["as_of"].isoformat(), source_sha256=hashlib.sha256(args.cached_signals.read_bytes()).hexdigest(),
            replay=replay_measurement, ready_alerts=alerts, evaluations=evaluations)
    report["synthetic_performance"] = load_benchmark(iterations=args.iterations)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str)+"\n")
    print(json.dumps({"report": str(args.output), "ready_alert_count": len(report.get("ready_alerts", [])),
        "replay": report.get("replay"), "synthetic_performance": report["synthetic_performance"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
