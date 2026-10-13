"""Seeded offline load/replay validation of the production native feed path.

This is TEST_ONLY_SYNTHETIC data, not exchange connectivity or profitability
evidence, and one million processed events are not one million unit tests.
Run: PYTHONPATH=src python benchmarks/million_events.py --events 1000000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import random
import resource
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from expansion_hunter.fast import FastIgnitionMonitor, detect_resistance
from expansion_hunter.fast_models import FastResistance, FastSettings, FastStage, FastState
from expansion_hunter.live_feed import LiveFeedState
from expansion_hunter.live_models import LiveFeedSettings
from expansion_hunter.models import Asset, AssetSnapshot, Candle
from expansion_hunter.providers.binance_ws import decode_message

SOURCE = "TEST_ONLY_SYNTHETIC:million-event-replay-v1"
START = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _rss_bytes() -> int:
    """OS peak RSS, not Python heap usage; Linux reports KiB, macOS bytes."""
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    left = math.floor(index)
    right = math.ceil(index)
    return ordered[left] + (ordered[right] - ordered[left]) * (index - left)


def _raw_message(
    symbol: str, kind: str, timestamp: datetime, sequence: int, randomizer: random.Random
) -> dict[str, Any]:
    milliseconds = int(timestamp.timestamp() * 1000)
    price = 100 + randomizer.randrange(-8, 9) / 100
    if kind == "TRADE":
        data = {
            "e": "aggTrade",
            "s": symbol,
            "T": milliseconds,
            "a": sequence,
            "p": str(price),
            "q": "1.25",
            "m": bool(sequence % 2),
        }
    elif kind == "BOOK_TICKER":
        data = {
            "e": "bookTicker",
            "s": symbol,
            "u": sequence,
            "b": "99.95",
            "B": "40",
            "a": "100.05",
            "A": "35",
        }
    elif kind == "DEPTH":
        data = {
            "lastUpdateId": sequence,
            "bids": [["99.95", "40"], ["99.90", "90"], ["98.50", "900"]],
            "asks": [["100.05", "35"], ["101.00", "700"], ["102.50", "850"]],
        }
    else:
        frame = randomizer.choice(("1m", "5m", "15m"))
        seconds = {"1m": 60, "5m": 300, "15m": 900}[frame]
        complete = randomizer.randrange(4) == 0
        boundary = math.floor(timestamp.timestamp() / seconds) * seconds
        opened = boundary - seconds if complete else boundary
        data = {
            "e": "kline",
            "s": symbol,
            "E": milliseconds,
            "k": {
                "i": frame,
                "t": opened * 1000,
                "T": (opened + seconds) * 1000 - 1,
                "o": "99.98",
                "h": "100.10",
                "l": "99.90",
                "c": str(price),
                "v": "100",
                "q": "10000",
                "V": "55",
                "Q": "5500",
                "n": 80,
                "x": complete,
            },
        }
    return {"stream": symbol.lower() + "@" + kind.lower(), "data": data}


def _bar(
    at: datetime, frame: str = "1m", *, close: float = 99.9, high: float | None = None, volume: float = 100
) -> Candle:
    seconds = {"1m": 60, "5m": 300, "15m": 900}[frame]
    return Candle(
        open_time=at - timedelta(seconds=seconds),
        close_time=at,
        available_at=at,
        open=99.8,
        high=high or max(100, close + 0.02),
        low=min(99.5, close),
        close=close,
        volume=volume,
        source=SOURCE,
    )


def _known_monitor() -> tuple[Asset, FastIgnitionMonitor]:
    asset = Asset(asset_id="synthetic-fast-fixture", symbol="SYNTEST", name=SOURCE)
    state = FastState(
        asset=asset,
        stage=FastStage.BREAKOUT_PRESSURE,
        resistance=FastResistance(
            price=100,
            lower=99.95,
            upper=100.05,
            event_timestamp=START - timedelta(hours=1),
            discovered_at=START,
            atr=0.5,
            kind="PREDEFINED_TEST_ONLY",
            tests=4,
            reason=SOURCE,
        ),
        candles={"1m": [_bar(START - timedelta(minutes=20 - index)) for index in range(21)]},
        last_processed={"1m": START},
        stale=False,
    )
    return asset, FastIgnitionMonitor(states=[state])


def run_fast_invariants(cases: int, seed: int) -> dict[str, Any]:
    """Actual fast engine replays; parameters vary without tuning production rules."""
    randomizer = random.Random(seed)
    checks = Counter()
    failures: list[str] = []
    started = time.perf_counter()

    def check(condition: bool, name: str) -> None:
        checks[name] += 1
        if not condition and len(failures) < 50:
            failures.append(name)

    for index in range(cases):
        asset, monitor = _known_monitor()
        at = START + timedelta(minutes=1)
        wick_only = index % 2 == 0
        close = 99.90 + randomizer.random() * 0.09 if wick_only else 100.03 + randomizer.random() * 0.3
        candle = _bar(at, close=close, high=max(100.12, close + 0.02), volume=10 if index % 3 else 500)
        result = monitor.evaluate(asset, AssetSnapshot(asset=asset, as_of=at, candles={"1m": [candle]}))
        events = [event.event_type for event in result.events]
        expected = "LIQUIDITY_SWEEP" if wick_only else "1M_BREAKOUT_CONFIRMED"
        check(expected in events, "wick_is_sweep" if wick_only else "close_confirms_without_5m")
        check(("1M_BREAKOUT_CONFIRMED" in events) == (not wick_only), "objective_close_condition")
        duplicate = monitor.evaluate(asset, AssetSnapshot(asset=asset, as_of=at, candles={"1m": [candle]}))
        check(not duplicate.events, "duplicate_suppression")
        restored = FastIgnitionMonitor(states=monitor.export_states())
        check(restored.states[asset.asset_id].stale, "restored_state_stale")
        replay = restored.evaluate(asset, AssetSnapshot(asset=asset, as_of=at, candles={"1m": [candle]}))
        check(not replay.events, "restart_deduplication")
        for event in result.events:
            check(event.resistance.discovered_at <= candle.open_time, "predefined_resistance")
            check(
                event.market_event_timestamp <= event.data_received_timestamp <= event.evaluation_timestamp,
                "timestamp_order",
            )
        asset, future_monitor = _known_monitor()
        future = future_monitor.evaluate(
            asset, AssetSnapshot(asset=asset, as_of=START, candles={"1m": [candle]})
        )
        check(
            not any(event.event_type == "1M_BREAKOUT_CONFIRMED" for event in future.events),
            "future_close_rejected",
        )
        asset, stale_monitor = _known_monitor()
        stale = stale_monitor.evaluate(
            asset, AssetSnapshot(asset=asset, as_of=at + timedelta(minutes=5), candles={"1m": [candle]})
        )
        check(
            not any(event.event_type == "1M_BREAKOUT_CONFIRMED" for event in stale.events),
            "stale_close_rejected",
        )
    history = [_bar(START - timedelta(minutes=5 * (35 - index)), "5m") for index in range(36)]
    before = detect_resistance(history, FastSettings(), START)
    history.append(_bar(START + timedelta(minutes=5), "5m", close=120, high=121))
    check(before == detect_resistance(history, FastSettings(), START), "future_pivot_does_not_reanchor")
    return {
        "cases": cases,
        "checks": dict(checks),
        "total_checks": sum(checks.values()),
        "failures": failures,
        "elapsed_seconds": time.perf_counter() - started,
    }


def run_benchmark(
    event_count: int = 1_000_000,
    symbols: int = 25,
    seed: int = 73021,
    fast_cases: int = 1000,
    latency_reservoir_size: int = 8192,
) -> dict[str, Any]:
    if event_count < 1 or not 1 <= symbols <= 50 or fast_cases < 1 or latency_reservoir_size < 1:
        raise ValueError("positive event/case/sample counts and 1–50 symbols are required")
    randomizer = random.Random(seed)
    sampler = random.Random(seed + 1)
    names = [f"SYN{index:03d}USDT" for index in range(symbols)]
    state = LiveFeedState(LiveFeedSettings(max_symbols=50, candle_capacity=64))
    sequences: Counter = Counter()
    kinds: Counter = Counter()
    perturbations: Counter = Counter()
    last_raw: dict[tuple[str, str], dict[str, Any]] = {}
    samples: list[float] = []
    failures: list[str] = []
    decoded = malformed = 0
    starting_rss = _rss_bytes()
    cpu_started = time.process_time()
    started = time.perf_counter()
    last_received = START
    for index in range(event_count):
        symbol = names[index % symbols]
        choice = randomizer.randrange(100)
        kind = (
            "TRADE" if choice < 78 else "BOOK_TICKER" if choice < 88 else "DEPTH" if choice < 94 else "CANDLE"
        )
        kinds[kind] += 1
        key = symbol, kind
        source_time = START + timedelta(milliseconds=index * 100)
        received = source_time + timedelta(milliseconds=3)
        processed = received + timedelta(microseconds=50)
        epoch = 1 if index < event_count // 2 else 2
        if index and index % 101 == 0 and key in last_raw:
            raw = last_raw[key]
            perturbations["duplicate_native_sequence_or_candle"] += 1
        else:
            increment = 3 if kind == "TRADE" and index % 211 == 0 else 1
            if increment > 1:
                perturbations["aggregate_trade_sequence_gap"] += 1
            sequences[key] += increment
            raw = _raw_message(symbol, kind, source_time, sequences[key], randomizer)
            last_raw[key] = raw
        if index and index % 127 == 0:
            received -= timedelta(seconds=10)
            processed = received + timedelta(microseconds=50)
            # Candle receipt cannot precede its open/close: malformed rejection is
            # also a valid outcome; no model_construct bypass is used.
            perturbations["out_of_order_receipt"] += 1
        if kind == "TRADE" and index % 997 == 0:
            raw = {**raw, "data": {**raw["data"]}}
            del raw["data"]["q"]
            perturbations["missing_native_trade_quantity"] += 1
        event_started = time.perf_counter_ns()
        try:
            event = decode_message(raw, received, processed, stream_epoch=epoch)
        except (ValueError, KeyError, TypeError):
            malformed += 1
        else:
            if event is None:
                failures.append("supported fixture unexpectedly decoded to None")
            else:
                event.source = SOURCE
                state.apply(event)
                decoded += 1
                last_received = max(last_received, received)
        elapsed_us = (time.perf_counter_ns() - event_started) / 1000
        if len(samples) < latency_reservoir_size:
            samples.append(elapsed_us)
        else:
            slot = sampler.randrange(index + 1)
            if slot < latency_reservoir_size:
                samples[slot] = elapsed_us
    duration = time.perf_counter() - started
    cpu_seconds = time.process_time() - cpu_started
    peak_rss = _rss_bytes()
    health = state.health(last_received)
    cache = {
        "symbols": len(state._symbols),
        "maximum_closed_candles_per_frame": max(
            (len(bars) for row in state._symbols.values() for bars in row.closed.values()), default=0
        ),
        "maximum_flow_buckets_per_symbol": max(
            (len(row.flow_buckets) for row in state._symbols.values()), default=0
        ),
        "maximum_latest_channels_per_symbol": max(
            (len(row.latest) for row in state._symbols.values()), default=0
        ),
        "maximum_candle_gap_ranges_per_frame": max(
            (len(gaps) for row in state._symbols.values() for gaps in row.candle_gaps.values()), default=0
        ),
        "maximum_open_candles_per_symbol": max(
            (len(row.opened) for row in state._symbols.values()), default=0
        ),
    }
    if state.events_accepted + state.events_rejected != decoded:
        failures.append("decoded events differ from accepted plus rejected")
    if decoded + malformed != event_count:
        failures.append("attempted events were lost")
    if cache["symbols"] != symbols or cache["maximum_closed_candles_per_frame"] > 64:
        failures.append("symbol/candle cache capacity invariant failed")
    if cache["maximum_flow_buckets_per_symbol"] > 1440 or cache["maximum_latest_channels_per_symbol"] > 6:
        failures.append("flow/latest cache capacity invariant failed")
    if cache["maximum_candle_gap_ranges_per_frame"] > 64 or cache["maximum_open_candles_per_symbol"] > 3:
        failures.append("gap/open candle cache capacity invariant failed")
    closed_count = 0
    for symbol in names:
        for frame in ("1m", "5m", "15m"):
            bars = state.candles(symbol, frame, last_received)
            closed_count += len(bars)
            if any(bar.available_at > last_received or bar.close_time > last_received for bar in bars):
                failures.append("future/unavailable candle escaped snapshot filter")
    stale_health = state.health(last_received + timedelta(seconds=60))
    stale_values = [
        value for row in stale_health["per_symbol_data_quality"].values() for value in row.values()
    ]
    if "AVAILABLE" in stale_values:
        failures.append("stale feed still reports available evidence")
    fast = run_fast_invariants(fast_cases, seed + 2)
    failures.extend(fast["failures"])
    repository = Path(__file__).resolve().parents[1]
    files = ["live_feed.py", "live_models.py", "fast.py", "providers/binance_ws.py"]
    hashes = {
        file: hashlib.sha256((repository / "src" / "expansion_hunter" / file).read_bytes()).hexdigest()
        for file in files
    }
    return {
        "schema_version": 1,
        "fixture": SOURCE,
        "seed": seed,
        "generated_at": datetime.now(UTC).isoformat(),
        "verdict": "PASS" if not failures else "FAIL",
        "failures": failures,
        "scope": "Native message decoder + production incremental LiveFeedState.apply; separate FastIgnitionMonitor replays",
        "limitations": [
            "Synthetic offline stress test; does not establish public connectivity or 24/7 reliability.",
            "One million event attempts are not one million independent unit tests.",
            "No network, SQLite writer, dashboard fanout or notifications in event throughput measurement.",
            "Receipt/processing timestamps in fixture are artificial; reported compute timings use perf_counter.",
            "Peak RSS is the OS process high-water mark, including imports; not an isolated Python heap measurement.",
        ],
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "production_module_sha256": hashes,
        "event_attempts": event_count,
        "decoded_and_applied": decoded,
        "malformed_rejected_at_decoder": malformed,
        "accepted": state.events_accepted,
        "rejected_at_state": state.events_rejected,
        "unaccounted_event_attempts": event_count - decoded - malformed,
        "kind_attempts": dict(kinds),
        "injected_perturbations": dict(perturbations),
        "elapsed_seconds": duration,
        "events_per_second": event_count / duration,
        "cpu_seconds": cpu_seconds,
        "cpu_core_equivalent": cpu_seconds / duration,
        "peak_rss_bytes": peak_rss,
        "peak_rss_growth_bytes": max(0, peak_rss - starting_rss),
        "latency_microseconds": {
            "method": "Seeded uniform reservoir of native decode + apply timings",
            "sample_count": len(samples),
            "p50": _percentile(samples, 0.5),
            "p95": _percentile(samples, 0.95),
            "p99": _percentile(samples, 0.99),
        },
        "sequence_gaps_detected": state.sequence_gaps,
        "cache_bounds": cache,
        "archived_closed_candles_at_completion": closed_count,
        "stale_feed_after_60_seconds": "AVAILABLE" not in stale_values,
        "latest_feed_health": health,
        "fast_ignition_invariants": fast,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=1_000_000)
    parser.add_argument("--symbols", type=int, default=25)
    parser.add_argument("--seed", type=int, default=73021)
    parser.add_argument("--fast-cases", type=int, default=1000)
    parser.add_argument("--output", default="reports/million-events.json")
    arguments = parser.parse_args()
    result = run_benchmark(arguments.events, arguments.symbols, arguments.seed, arguments.fast_cases)
    path = Path(arguments.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "verdict",
                    "event_attempts",
                    "decoded_and_applied",
                    "accepted",
                    "rejected_at_state",
                    "malformed_rejected_at_decoder",
                    "elapsed_seconds",
                    "events_per_second",
                    "cpu_seconds",
                    "peak_rss_bytes",
                    "latency_microseconds",
                    "failures",
                )
            },
            indent=2,
        )
    )
    print(f"Evidence written to {path}")
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
