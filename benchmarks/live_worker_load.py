"""Synthetic protocol load against the actual worker, writer and chart engine.

No provider connections are made. Every price is a labelled deterministic test
fixture in a temporary database, removed after the benchmark. This does NOT
demonstrate exchange coverage, network latency, clients, or 24/7 reliability.

Example:
 PYTHONPATH=src python benchmarks/live_worker_load.py --assets 20 50 --events-per-asset 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import resource
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from expansion_hunter.config import Settings
from expansion_hunter.fast_models import FastStage
from expansion_hunter.live_models import LiveFeedSettings
from expansion_hunter.live_runtime import LiveObservationWorker
from expansion_hunter.models import TIMEFRAMES, Asset, Candle
from expansion_hunter.providers.binance_ws import decode_message

START = datetime(2026, 1, 1, 12, tzinfo=UTC)
SOURCE = "TEST_ONLY_SYNTHETIC_PROTOCOL_LOAD"
FRAMES = ("1m", "5m", "15m", "1h", "4h", "1D")


def rss_bytes():
    if sys.platform.startswith("linux"):
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def peak_rss_bytes():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    point = (len(ordered) - 1) * fraction
    lower, upper = math.floor(point), math.ceil(point)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (point - lower)


def millis(when):
    return int(when.timestamp() * 1000)


def native_fixture(symbol, kind, when, sequence, *, breakout=False):
    """Use the real Binance decoder; tag decoded evidence as synthetic."""
    close = 100.12 if breakout else 99.9
    common = {"s": symbol, "E": millis(when)}
    if kind == "TRADE":
        data = dict(common, e="aggTrade", a=sequence, p=str(100.08 if breakout else 99.9),
                    q="2", m=False, T=millis(when))
        stream = f"{symbol.lower()}@aggTrade"
    elif kind == "BOOK_TICKER":
        data = dict(common, e="bookTicker", u=sequence, b="99.89", B="10", a="100.02", A="12")
        stream = f"{symbol.lower()}@bookTicker"
    elif kind == "DEPTH":
        data = {"lastUpdateId": sequence,
                "bids": [[str(round(99.89 - index * 0.01, 2)), str(10 + index)] for index in range(20)],
                "asks": [[str(round(100.02 + index * 0.01, 2)), str(12 + index)] for index in range(20)]}
        stream = f"{symbol.lower()}@depth20@100ms"
    else:
        closed = START + timedelta(minutes=1)
        volume = 200 if breakout else 100
        data = dict(common, e="kline", k={
            "t": millis(START), "T": millis(closed) - 1, "i": "1m", "o": "99.9",
            "h": "100.2" if breakout else "99.98", "l": "99.75", "c": str(close),
            "v": str(volume), "q": str(volume * close), "V": str(volume * 0.6),
            "Q": str(volume * close * 0.6), "n": 20, "x": breakout,
        })
        stream = f"{symbol.lower()}@kline_1m"
    received = when + timedelta(milliseconds=25)
    event = decode_message({"stream": stream, "data": data}, received,
                           received + timedelta(milliseconds=2))
    source = f"{SOURCE}:{symbol}:1m" if kind == "CANDLE" else SOURCE
    return event.model_copy(update={"source": source})


def seed_fixture(worker, count=240):
    """Six causal completed histories; last36 five-minute bars press100.00."""
    for symbol in worker.assets:
        for frame in FRAMES:
            seconds = TIMEFRAMES[frame]
            last = datetime.fromtimestamp(int(START.timestamp()) // seconds * seconds, UTC)
            bars = []
            for index in range(count):
                closed = last - timedelta(seconds=(count - index - 1) * seconds)
                close = 99.9 if frame == "1m" else 99.7 + 0.04 * math.sin(index * 0.3)
                high, low = max(close + 0.03, 99.8), close - 0.2
                if frame == "5m" and index >= count - 36:
                    local = index - (count - 36)
                    close = 99.9 if local == 35 else 99.7
                    high = 100 if local in (4, 12, 20, 28) else max(close + 0.02, 99.8)
                    low = 98.9 + local * 0.018
                bars.append(Candle(
                    open_time=closed - timedelta(seconds=seconds), close_time=closed,
                    available_at=closed, open=close - 0.04, high=high, low=low, close=close,
                    volume=100, quote_volume=100 * close, taker_buy_volume=60,
                    source=f"{SOURCE}:{symbol}:{frame}",
                ))
            worker.state.seed_candles(symbol, frame, bars, START)
        worker._evaluate(symbol, START)


def database_bytes(path):
    return sum(item.stat().st_size for item in path.parent.glob(path.name + "*") if item.is_file())


async def run_case(count, *, events_per_asset=100, bars=240, analysis=True):
    if not 1 <= count <= 150 or not 4 <= events_per_asset <= 10000 or not 36 <= bars <= 512:
        raise ValueError("assets1-150, events-per-asset4-10000, bars36-512 required")
    current = [START]
    with tempfile.TemporaryDirectory(prefix="eh-synthetic-load-") as directory:
        database = Path(directory) / "synthetic.sqlite3"
        feed = LiveFeedSettings(max_symbols=count, benchmark_symbols=[], analysis_concurrency=1)
        worker = LiveObservationWorker(Settings(db_path=str(database)), feed, clock=lambda: current[0])
        worker.assets = {
            f"TEST{index:04}USDT": Asset(asset_id=f"synthetic:{index}", symbol=f"TEST{index:04}",
                                      name="SYNTHETIC TEST ONLY", exchange_symbol=f"TEST{index:04}USDT")
            for index in range(count)
        }
        baseline_bytes = database_bytes(database)
        setup_start = time.perf_counter()
        seed_fixture(worker, bars)
        setup_seconds = time.perf_counter() - setup_start
        initial_states = Counter(state.stage.value for state in worker.monitor.states.values())
        worker._start_writer()
        if analysis:
            worker._analysis_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eh-load-analysis")
        else:
            worker._schedule_analysis = lambda symbol: None
        sequence = Counter()
        submitted = 0
        peak_critical = 0
        kinds = ("TRADE", "BOOK_TICKER", "DEPTH", "CANDLE")
        ingest_start = time.perf_counter()
        cpu_start = time.process_time()
        before_rss = rss_bytes()
        errors = []
        try:
            for index in range(events_per_asset):
                when = START + timedelta(seconds=10 + 30 * index / max(1, events_per_asset - 1))
                kind = kinds[index % len(kinds)]
                for symbol in worker.assets:
                    sequence[symbol, kind] += 1
                    event = native_fixture(symbol, kind, when, sequence[symbol, kind])
                    current[0] = event.processed_timestamp
                    # Explicit stress: simulate a chart-publication opportunity
                    # per round; no claim that these rounds are wall-clock1s.
                    worker._last_publish.pop(symbol, None)
                    worker.apply(event)
                    submitted += 1
                peak_critical = max(peak_critical, len(worker._critical_writes))
                await asyncio.sleep(0)
            # Objective trigger evidence uses resistance discovered during seed,
            # not a fabricated post-move convenient horizontal level.
            for symbol in worker.assets:
                sequence[symbol, "TRADE"] += 1
                event = native_fixture(symbol, "TRADE", START + timedelta(seconds=50),
                                       sequence[symbol, "TRADE"], breakout=True)
                current[0] = event.processed_timestamp
                worker._last_publish.pop(symbol, None)
                worker.apply(event)
                submitted += 1
            closed_priority_times = []
            for symbol in worker.assets:
                event = native_fixture(symbol, "CANDLE", START + timedelta(minutes=1), 1, breakout=True)
                current[0] = event.processed_timestamp
                began = time.perf_counter()
                worker.apply(event)
                closed_priority_times.append((time.perf_counter() - began) * 1000)
                submitted += 1
            ingestion_seconds = time.perf_counter() - ingest_start
            confirmed_immediately = sum(state.stage == FastStage.BREAKOUT_CONFIRMED
                                        for state in worker.monitor.states.values())
            queued_after_ingestion = worker.metrics()["persistence"]
            analysis_start = time.perf_counter()
            analysis_tasks = list(worker._analysis_tasks.values())
            if analysis_tasks:
                await asyncio.gather(*analysis_tasks)
            analysis_seconds = time.perf_counter() - analysis_start
            flush_start = time.perf_counter()
            await worker._finish_writer()
            flush_seconds = time.perf_counter() - flush_start
            wall_seconds = time.perf_counter() - ingest_start
            cpu_seconds = time.process_time() - cpu_start
            metrics = worker.metrics()
            latencies = worker._latencies
            final_bytes = database_bytes(database)
            archive = worker.store.objects("fast_event", current[0], limit=10000)
            archived_confirmed = sum(event["event_type"] == "1M_BREAKOUT_CONFIRMED" for event in archive)
            archived_closed = len(worker.store.objects("market_observation", current[0], limit=10000))
            return {
                "test": "SYNTHETIC_LOAD", "data_origin": SOURCE,
                "real_public_provider_test": False, "network_connections": 0,
                "database_scope": "TEMPORARY; removed after benchmark",
                "assets": count, "timeframes": list(FRAMES), "warmup_bars_per_timeframe": bars,
                "warmup_seconds": setup_seconds, "initial_states": dict(initial_states),
                "fast_settings": worker.fast_settings.model_dump(mode="json"),
                "background_analysis_enabled": analysis, "analysis_concurrency": feed.analysis_concurrency,
                "publication_policy": "one opportunity per synthetic round; not wall-clock realtime",
                "events_submitted": submitted, "events_accepted": metrics["events"],
                "events_rejected": metrics["rejected"],
                "unaccounted_events": submitted - metrics["events"] - metrics["rejected"],
                "expected_completed_1m_candles": count,
                "durably_archived_completed_1m_candles": archived_closed,
                "dropped_critical_records": max(0, count - archived_closed)
                                             + max(0, confirmed_immediately - archived_confirmed),
                "ingestion_seconds": ingestion_seconds,
                "events_per_second_ingestion": submitted / ingestion_seconds,
                "total_wall_seconds": wall_seconds, "total_cpu_seconds": cpu_seconds,
                "cpu_percent_one_core_equivalent": cpu_seconds / wall_seconds * 100,
                "rss_before_bytes": before_rss, "rss_after_bytes": rss_bytes(),
                "process_peak_rss_bytes": peak_rss_bytes(),
                "processing_p50_ms": percentile(latencies, .5),
                "processing_p95_ms": percentile(latencies, .95),
                "processing_p99_ms": percentile(latencies, .99),
                "processing_includes": "state ingestion, trigger evaluation, publication preparation/enqueue",
                "processing_excludes": "provider network and asynchronous SQLite/archive delivery",
                "closed_1m_priority_p50_ms": percentile(closed_priority_times, .5),
                "closed_1m_priority_p95_ms": percentile(closed_priority_times, .95),
                "closed_1m_priority_p99_ms": percentile(closed_priority_times, .99),
                "confirmed_immediately_in_memory": confirmed_immediately,
                "confirmations_durably_archived": archived_confirmed,
                "background_analysis_wait_seconds": analysis_seconds,
                "archive_flush_seconds": flush_seconds,
                "critical_backlog_peak_observed": max(peak_critical, metrics["persistence"]["critical_high_water"]),
                "persistence_after_ingestion": queued_after_ingestion,
                "persistence_after_flush": metrics["persistence"],
                "database_initial_bytes_including_wal": baseline_bytes,
                "database_final_bytes_including_wal": final_bytes,
                "database_growth_bytes_including_wal": final_bytes - baseline_bytes,
                "provider_errors": worker.store.states("provider_error"), "errors": errors,
                "live_trading": False,
                "limitations": ["No browser/client load, provider budget or network latency tested",
                                "Peak RSS is process-wide and may include earlier benchmark cases",
                                "Synthetic fixture behaviour is not evidence of predictive value"],
            }
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            worker.stop.set()
            for task in worker._analysis_tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*worker._analysis_tasks.values(), return_exceptions=True)
            if worker._writer_active:
                await worker._finish_writer()
            if worker._analysis_executor:
                worker._analysis_executor.shutdown(wait=True, cancel_futures=True)
            worker.store.close()


async def main(args):
    results = []
    for count in args.assets:
        result = await run_case(count, events_per_asset=args.events_per_asset,
                                bars=args.bars, analysis=not args.without_analysis)
        results.append(result)
        print(json.dumps({"completed_assets": count, "events": result["events_accepted"],
                          "wall_seconds": result["total_wall_seconds"],
                          "p95_ms": result["processing_p95_ms"],
                          "durable_confirmations": result["confirmations_durably_archived"]}), flush=True)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"test": "SYNTHETIC_LOAD", "cases": results}, indent=2) + "\n")
    print(str(output), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=int, nargs="+", default=[20, 50])
    parser.add_argument("--events-per-asset", type=int, default=100)
    parser.add_argument("--bars", type=int, default=240)
    parser.add_argument("--without-analysis", action="store_true")
    parser.add_argument("--output", default="reports/live-worker-load.json")
    asyncio.run(main(parser.parse_args()))
