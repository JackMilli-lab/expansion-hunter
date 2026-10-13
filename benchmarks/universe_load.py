"""Synthetic native-protocol throughput, never evidence of provider coverage."""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import statistics
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from expansion_hunter.config import Settings
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.providers.http import FetchedJSON
from expansion_hunter.providers.universe_stream import UniverseTicker
from expansion_hunter.universe_live import UniverseLiveSettings, UniverseLiveWorker


def percentile(values, fraction):
    return sorted(values)[int((len(values) - 1) * fraction)]


def measure(assets=500, seconds=120):
    now = datetime(2026, 10, 6, tzinfo=UTC)
    symbols = ["BTCUSDT"] + [f"LOAD{index:04d}USDT" for index in range(assets - 1)]

    class NativeFixtureHTTP:
        async def get_json(self, url, **kwargs):
            if url.endswith("exchangeInfo"):
                data = {"symbols": [{"symbol": symbol, "baseAsset": symbol[:-4], "quoteAsset": "USDT",
                    "status": "TRADING", "isSpotTradingAllowed": True} for symbol in symbols]}
            else:
                data = [{"symbol": symbol, "lastPrice": "100", "openPrice": "99", "lowPrice": "90",
                    "highPrice": "110", "volume": "100000", "quoteVolume": "10000000",
                    "closeTime": int(now.timestamp() * 1000)} for symbol in symbols]
            return FetchedJSON(data=data, retrieved_at=now)

    with tempfile.TemporaryDirectory(prefix="hunter-universe-synthetic-") as folder:
        path = Path(folder) / "synthetic.sqlite3"
        store = IntelligenceStore(str(path))
        worker = UniverseLiveWorker(Settings(), UniverseLiveSettings(max_assets=assets),
                                   clock=lambda: now, http=NativeFixtureHTTP(), store=store)
        asyncio.run(worker.bootstrap())
        worker._running = True
        timings, wire_sizes, archives = [], [], []
        cpu_started, started = time.process_time(), time.perf_counter()
        for second in range(1, seconds + 1):
            now += timedelta(seconds=1)
            native = [UniverseTicker(symbol=symbol, price=100 + (index % 13 - 6) * second / 1000,
                open24h=99, high24h=110, low24h=90, basevolume24h=100000 + second * 10,
                quotevolume24h=10000000 + second * 1000 + second ** 2,
                event_timestamp=now - timedelta(milliseconds=20), received_timestamp=now,
                processing_timestamp=now) for index, symbol in enumerate(symbols)]
            tick = time.perf_counter()
            worker.apply(native)
            snapshot = worker.snapshot()
            wire_sizes.append(len(json.dumps(snapshot, separators=(",", ":"))))
            timings.append((time.perf_counter() - tick) * 1000)
            if second % 60 == 0:
                tick = time.perf_counter()
                worker.archive(snapshot, worker.promotions(), now)
                archives.append((time.perf_counter() - tick) * 1000)
        duration, cpu = time.perf_counter() - started, time.process_time() - cpu_started
        database_bytes = sum(p.stat().st_size for p in Path(folder).glob("synthetic.sqlite3*"))
        record = {"kind": "SYNTHETIC_NATIVE_PROTOCOL_LOAD", "real_provider_test": False,
            "assets": assets, "seconds_simulated": seconds, "native_events_accepted": worker._accepted,
            "rejected": worker._rejected, "dropped": 0,
            "wall_seconds": duration, "cpu_seconds": cpu, "cpu_percent_of_one_core": 100 * cpu / duration,
            "processing_events_per_second": worker._accepted / duration,
            "processing_ms": {"median": statistics.median(timings), "p95": percentile(timings, .95), "p99": percentile(timings, .99)},
            "timing_scope": "500 native observations + cross-sectional ranking + detached API snapshot + JSON serialization per batch",
            "peak_rss_kib_linux": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "snapshot_json_bytes_mean": statistics.mean(wire_sizes),
            "database_bytes_including_wal": database_bytes, "compressed_archived_bytes": worker._archive_bytes,
            "coarse_archive_count": worker._archive_count, "archive_ms": archives,
            "maximum_history_per_asset": max(map(len, worker._observations.values())),
            "limitations": ["Fixture transport; not live Binance throughput/500-symbol freshness or 24/7 reliability",
                            "Synchronous temporary SQLite measurement; production writes run outside ingestion"]}
        store.close()
        return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=int, default=500)
    parser.add_argument("--seconds", type=int, default=120)
    args = parser.parse_args()
    if not 1 <= args.assets <= 1000 or not 1 <= args.seconds <= 600:
        parser.error("assets must be 1..1000 and seconds 1..600")
    print(json.dumps(measure(args.assets, args.seconds), indent=2))


if __name__ == "__main__":
    main()
