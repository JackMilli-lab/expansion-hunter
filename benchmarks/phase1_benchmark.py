"""Offline, explicitly synthetic capacity benchmark; never historical market proof.

Run from the repository root:
  PYTHONPATH=src python benchmarks/phase1_benchmark.py --output reports/phase1-benchmark.json

The fake providers exercise the production engine, scoring and SQLite archive.
They do not model internet latency, market-data subscriptions, venue coverage or
actual throttling. Request weights below are hypothetical budget estimates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import tempfile
import time
import tracemalloc
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from expansion_hunter.config import Settings
from expansion_hunter.discovery import DiscoveryEngine
from expansion_hunter.discovery_config import DiscoverySettings
from expansion_hunter.discovery_store import DiscoveryStore
from expansion_hunter.models import TIMEFRAMES, Asset, Candle, Metric, Status
from expansion_hunter.providers.base import ProviderError, unavailable

SYNTHETIC_SOURCE = "TEST_ONLY_SYNTHETIC:phase1-capacity-fixture"


class FixtureClock:
    """Frozen, explicitly artificial knowledge boundary, movable between cycles."""

    def __init__(self, value: datetime | None = None):
        self.value = value or datetime(2026, 9, 1, 12, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int):
        self.value += timedelta(seconds=seconds)


def fixture_metric(value: float, as_of: datetime, unit: str = "", *, source: str = SYNTHETIC_SOURCE) -> Metric:
    return Metric(
        value=value,
        observed_at=as_of,
        available_at=as_of,
        source=source,
        unit=unit,
        status=Status.AVAILABLE,
    )


class SyntheticProviders:
    """No network or real price claims; automatic identity mapping is a test fake.

    ``failures`` and ``stale`` are asset IDs used by integration tests. Missing
    derivatives are deliberate and cannot become invented zero funding/OI.
    """

    def __init__(self, size: int, *, failures=(), stale=(), missing=(), fail_universe=False):
        self.size = size
        self.failures = set(failures)
        self.stale = set(stale)
        self.missing = set(missing)
        self.fail_universe = fail_universe
        self.calls: Counter = Counter()
        self.candle_limits: list[tuple[str, str, int]] = []
        self.issues: list[str] = []
        self._indices: dict[str, int] = {}
        identities = ["bitcoin", "ethereum"] + [f"unknown-fixture-{i:04d}" for i in range(max(0, size - 2))]
        self.assets = []
        for i, identity in enumerate(identities[:size]):
            self._indices[identity] = i
            self.assets.append(Asset(
                asset_id=identity,
                symbol=f"SYN{i:04d}",
                name=f"Synthetic capacity asset {i}; not market data",
                rank=i + 1,
            ))

    def _index(self, asset: Asset) -> int:
        return self._indices.get(asset.asset_id, 0)

    def _time(self, asset: Asset, as_of: datetime) -> datetime:
        return as_of - timedelta(days=2) if asset.asset_id in self.stale else as_of

    async def universe(self, top_n: int, as_of: datetime) -> list[Asset]:
        self.calls["universe"] += 1
        if self.fail_universe:
            raise ProviderError(SYNTHETIC_SOURCE, "synthetic universe outage")
        return self.assets[:top_n]

    async def resolve_assets(self, assets: list[Asset]) -> list[Asset]:
        self.calls["resolve_assets"] += 1
        return [a.model_copy(update={"exchange_symbol": f"SYN{self._index(a):04d}USDT"}) for a in assets]

    async def market(self, asset: Asset, as_of: datetime) -> dict[str, Metric]:
        self.calls["market"] += 1
        if asset.asset_id in self.failures:
            raise ProviderError(SYNTHETIC_SOURCE, "synthetic market metadata unavailable")
        if asset.asset_id in self.missing:
            return {"market_cap": unavailable(SYNTHETIC_SOURCE, "synthetic metadata missing", "USD")}
        when = self._time(asset, as_of)
        cap = 1_000_000_000 / (1 + self._index(asset) / 15)
        metadata_source = "coingecko/TEST_ONLY_SYNTHETIC_CAPACITY_FIXTURE"
        return {
            "market_cap": fixture_metric(cap, when, "USD", source=metadata_source),
            "circulating_supply": fixture_metric(cap / 100, when, "tokens"),
            "volume_24h": fixture_metric(10_000_000, when, "USD; TEST_ONLY_SYNTHETIC", source=metadata_source),
            "turnover": fixture_metric(10_000_000 / cap, when, "fraction per 24h"),
            "turnover_24h": fixture_metric(10_000_000 / cap, when, "fraction per 24h"),
        }

    async def global_metrics(self, as_of: datetime) -> dict[str, Metric]:
        self.calls["global_metrics"] += 1
        return {
            "btc_dominance": fixture_metric(52, as_of, "percent"),
            "total_market_cap": fixture_metric(2_000_000_000_000, as_of, "USD"),
        }

    async def light_market(self, assets: list[Asset], as_of: datetime) -> dict[str, dict[str, Metric]]:
        self.calls["light_market"] += 1
        output = {}
        for asset in assets:
            if asset.asset_id in self.failures:
                self.issues.append(f"{asset.asset_id}: synthetic bulk ticker unavailable")
                continue
            if asset.asset_id in self.missing:
                output[asset.asset_id] = {}
                continue
            index, when = self._index(asset), self._time(asset, as_of)
            price = 100 + index * 0.02
            output[asset.asset_id] = {
                "price": fixture_metric(price, when, "USDT"),
                "price_change_24h_pct": fixture_metric(-1 + index % 25 / 10, when, "percent"),
                "volume_24h": fixture_metric(100_000 + index * 50, when, "base units"),
                "quote_volume_24h": fixture_metric(10_000_000 + index * 5000, when, "USDT"),
                "best_bid": fixture_metric(price * 0.9995, when, "USDT"),
                "best_ask": fixture_metric(price * 1.0005, when, "USDT"),
                "book_mid": fixture_metric(price, when, "USDT"),
                "spread_bps": fixture_metric(10, when, "bps"),
            }
        return output

    async def candles(self, asset: Asset, timeframe: str, limit: int, as_of: datetime) -> list[Candle]:
        self.calls["candles"] += 1
        self.calls[f"candles:{timeframe}"] += 1
        self.candle_limits.append((asset.asset_id, timeframe, limit))
        if asset.asset_id in self.failures:
            raise ProviderError(SYNTHETIC_SOURCE, "synthetic candle outage")
        if asset.asset_id in self.missing:
            return []
        duration = TIMEFRAMES[timeframe]
        when = self._time(asset, as_of)
        end = datetime.fromtimestamp(int(when.timestamp()) // duration * duration, UTC)
        index = self._index(asset)
        bars = []
        # Absolute event index keeps unchanged candles byte-identical when the
        # production engine switches to incremental request lengths.
        for offset in range(limit - 1, -1, -1):
            close_time = end - timedelta(seconds=offset * duration)
            event_index = int(close_time.timestamp()) // duration
            phase = event_index % 512
            trend = 0.0004 * (1 + index % 7)
            center = 100 + index * 0.02 + trend * phase + 0.3 * math.sin(phase / 9)
            amplitude = 0.5 * (1 + 0.12 * math.sin(phase / 33))
            volume = (1000 + index % 37) * (1 + 0.2 * math.sin(phase / 17))
            bars.append(Candle(
                open_time=close_time - timedelta(seconds=duration),
                close_time=close_time,
                available_at=when,
                open=center - 0.05,
                high=center + amplitude,
                low=center - amplitude,
                close=center + 0.05,
                volume=volume,
                quote_volume=volume * center,
                taker_buy_volume=volume * (0.5 + index % 10 / 100),
                source=SYNTHETIC_SOURCE,
            ))
        return bars

    async def liquidity(self, asset: Asset, as_of: datetime) -> dict[str, Metric]:
        self.calls["liquidity"] += 1
        if asset.asset_id in self.failures:
            raise ProviderError(SYNTHETIC_SOURCE, "synthetic book outage")
        when = self._time(asset, as_of)
        price = 100 + self._index(asset) * 0.02
        return {
            "spread_bps": fixture_metric(10, when, "bps"),
            "depth_usd": fixture_metric(150_000, when, "USD"),
            "bid_depth_usd": fixture_metric(160_000, when, "USD"),
            "ask_depth_usd": fixture_metric(150_000, when, "USD"),
            "best_bid": fixture_metric(price * 0.9995, when, "USDT"),
            "best_ask": fixture_metric(price * 1.0005, when, "USDT"),
            "book_mid": fixture_metric(price, when, "USDT"),
            "quote_volume_24h": fixture_metric(10_000_000, when, "USDT"),
        }

    async def metrics(self, asset: Asset, as_of: datetime) -> dict[str, Metric]:
        self.calls["derivatives"] += 1
        return {
            "open_interest": unavailable(SYNTHETIC_SOURCE, "synthetic fixture intentionally has no derivatives"),
            "funding_rate": unavailable(SYNTHETIC_SOURCE, "synthetic fixture intentionally has no derivatives"),
        }


def database_bytes(path: Path) -> int:
    return sum(candidate.stat().st_size for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
               if candidate.exists())


def call_delta(before: Counter, after: Counter) -> dict[str, int]:
    return {key: after[key] - before[key] for key in sorted(after) if after[key] != before[key]}


def request_estimates(calls: dict[str, int], universe_size: int) -> dict:
    # Futures counts are provider-contract operations, not native HTTP counts;
    # the fixture explicitly returns unavailable without any futures network I/O.
    histories = calls.get("candles", 0)
    books = calls.get("liquidity", 0)
    bulk = calls.get("light_market", 0)
    rest_spot_requests = histories + books * 3 + bulk * 2 + calls.get("resolve_assets", 0)
    native_weight = histories * 2 + books * (50 + 2 + 2) + bulk * (80 + 4)
    return {
        "hypothetical_spot_http_requests": rest_spot_requests,
        "hypothetical_spot_request_weight": native_weight,
        "assumed_weight_limit_per_minute": 1200,
        "hypothetical_weight_limit_fraction": round(native_weight / 1200, 4),
        "hypothetical_minimum_budget_minutes": round(native_weight / 1200, 4),
        "market_universe_pages_at_250": math.ceil(universe_size / 250) * calls.get("universe", 0),
        "measured_network_requests": 0,
        "measured_rate_limit_usage": None,
        "note": "Endpoint-weight assumptions only; no network/rate-limiter throughput was measured. "
                "The configured runtime may use different weights, limits, cached endpoints and providers.",
    }


async def benchmark_size(size: int, deep_limit: int = 10) -> dict:
    provider = SyntheticProviders(size)
    clock = FixtureClock()
    with tempfile.TemporaryDirectory(prefix="expansion-hunter-synthetic-benchmark-") as temporary:
        path = Path(temporary) / "synthetic-only.sqlite3"
        store = DiscoveryStore(str(path))
        settings = Settings(top_n=size, concurrency=6, db_path=str(path))
        discovery = DiscoverySettings(
            universe_size=size,
            deep_per_cycle=deep_limit,
            deep_watchlist_size=min(size, 50),
            minimum_cross_section=min(size, 10),
            # Stable data fixtures exercise promotion, independent of whether
            # the default hypothetical production thresholds consider them interesting.
            radar_threshold=0,
            watch_threshold=0,
        )
        engine = DiscoveryEngine(provider, provider, provider, settings, discovery, store, clock=clock)
        cycles = []
        try:
            for name, advance in (("cold_bootstrap", 0), ("warm_same_closed_bar", 300)):
                clock.advance(advance)
                calls_before = provider.calls.copy()
                bytes_before = database_bytes(path)
                tracemalloc.start()
                started = time.perf_counter()
                cycle = await engine.run_once()
                elapsed = time.perf_counter() - started
                current_memory, peak_memory = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                calls = call_delta(calls_before, provider.calls)
                age_values = [
                    f.age_seconds for observation in cycle.observations for f in observation.features.values()
                    if f.age_seconds is not None and f.critical
                ]
                cycles.append({
                    "mode": name,
                    "wall_seconds": round(elapsed, 6),
                    "engine_light_seconds": cycle.statistics.get("light_duration_seconds"),
                    "engine_deep_seconds": cycle.statistics.get("deep_duration_seconds"),
                    "provider_contract_calls": calls,
                    "request_estimates": request_estimates(calls, size),
                    "memory_current_bytes": current_memory,
                    "memory_peak_bytes": peak_memory,
                    "database_growth_bytes": database_bytes(path) - bytes_before,
                    "database_total_bytes": database_bytes(path),
                    "critical_feature_age_seconds_max": max(age_values, default=None),
                    "critical_feature_age_plus_wall_seconds_max": (
                        round(max(age_values) + elapsed, 6) if age_values else None
                    ),
                    "critical_feature_freshness_counts": dict(Counter(
                        f.freshness.value for observation in cycle.observations for f in observation.features.values()
                        if f.critical
                    )),
                    "cycle_counts": cycle.counts,
                    "cycle_status": cycle.status,
                    "synthetic_output_sample": [{
                        "asset_id": observation.asset.asset_id,
                        "rank": observation.rank.current_rank,
                        "stage": observation.stage.value,
                        "freshness": observation.freshness.value,
                        "deep_scanned": observation.deep_scanned,
                        "scores": {name: {"value": score.value, "coverage": score.coverage,
                                          "calibration": score.calibration}
                                   for name, score in observation.scores.items()},
                        "reasons": observation.reasons,
                    } for observation in cycle.observations[:3]],
                    "statistics": cycle.statistics,
                })
        finally:
            store.close()
    return {"assets": size, "deep_limit": deep_limit, "cycles": cycles}


async def benchmark(sizes: list[int], deep_limit: int) -> dict:
    results = []
    for size in sizes:
        result = await benchmark_size(size, deep_limit)
        results.append(result)
        print(json.dumps({"assets": size, "cycles": [
            {"mode": c["mode"], "seconds": c["wall_seconds"], "counts": c["cycle_counts"]}
            for c in result["cycles"]
        ]}), flush=True)
    return {
        "benchmark_type": "OFFLINE_SYNTHETIC_ENGINE_AND_SQLITE_CAPACITY",
        "source": SYNTHETIC_SOURCE,
        "generated_at": datetime.now(UTC).isoformat(),
        "real_market_provider_coverage": None,
        "real_network_or_rate_limit_test": False,
        "notes": [
            "Every price, volume, identity mapping and timestamp inside the fixture is synthetic.",
            "This measures production computation/archive cost under local fake I/O, not live-provider latency.",
            "Tracemalloc adds overhead and does not measure total process resident memory.",
            "The frozen fixture clock excludes wall-time aging; age-plus-wall is reported separately as a conservative local estimate.",
            "Warm cycle advances 5 minutes within the same closed 15-minute candle; provider timestamps stay fresh.",
            "Promotion thresholds are zero in this infrastructure benchmark; these fixtures demonstrate no predictive quality.",
            "Database growth includes SQLite WAL/shared-memory files and has no retention configured.",
        ],
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[50, 100, 250, 500, 1000])
    parser.add_argument("--deep-limit", type=int, default=10)
    parser.add_argument("--output", default="reports/phase1-benchmark.json")
    args = parser.parse_args()
    if any(size < 10 or size > 10000 for size in args.sizes) or not 0 <= args.deep_limit <= 100:
        parser.error("sizes must be 10..10000 and deep-limit 0..100")
    result = asyncio.run(benchmark(args.sizes, args.deep_limit))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(f"Synthetic-only benchmark saved to {output}", flush=True)


if __name__ == "__main__":
    main()
