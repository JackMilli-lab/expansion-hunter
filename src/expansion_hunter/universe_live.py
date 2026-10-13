"""Bounded, native, all-market price discovery independent of fast ignition.

One public miniTicker stream observes changed pairs, not every pair every second.
Rolling 24h totals are never presented as candle volume, CVD or resting orders.
The optional archive samples once a minute; raw tick arrays are not persisted.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import time
import zlib
from collections import Counter, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from statistics import mean

from pydantic import Field, model_validator

from .config import Settings
from .intelligence_store import IntelligenceStore
from .live_models import LiveFeedSettings, normalize_symbols
from .models import Asset, Model, aware, utcnow
from .providers.binance import BinanceSpotProvider
from .providers.http import AsyncHTTPClient
from .providers.universe_stream import BinanceUniverseStream, UniverseTicker


class UniverseLiveSettings(Model):
    max_assets: int = Field(default=500, ge=1, le=1000)
    observation_capacity: int = Field(default=180, ge=65, le=3600)
    live_age_seconds: float = Field(default=3, gt=0, le=30)
    stale_age_seconds: float = Field(default=15, gt=0, le=300)
    baseline_tolerance_seconds: float = Field(default=15, gt=0, le=60)
    archive_seconds: float = Field(default=60, ge=30, le=3600)
    refresh_seconds: float = Field(default=3600, ge=60, le=86400)
    bootstrap_retry_seconds: float = Field(default=15, ge=1, le=300)
    min_quote_volume: float = Field(default=0, ge=0)
    promotion_count: int = Field(default=5, ge=0, le=20)
    promotion_min_quote_volume: float = Field(default=5_000_000, ge=0)
    promotion_min_return_1m_pct: float = Field(default=0.10, gt=0, le=100)
    promotion_min_relative_1m_pct: float = Field(default=0.05, ge=0, le=100)
    promotion_ttl_seconds: float = Field(default=180, ge=60, le=900)

    @model_validator(mode="after")
    def ages(self):
        if self.stale_age_seconds <= self.live_age_seconds:
            raise ValueError("stale age must exceed live age")
        return self


class _Observation(Model):
    symbol: str
    price: float = Field(gt=0)
    open24h: float = Field(gt=0)
    high24h: float = Field(gt=0)
    low24h: float = Field(gt=0)
    basevolume24h: float = Field(ge=0)
    quotevolume24h: float = Field(ge=0)
    event_timestamp: datetime
    received_timestamp: datetime
    source: str
    streamed: bool


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    """Cross-sectional midranks; ties receive the same percentile."""
    ordered = sorted(values, key=lambda key: (values[key], key))
    output, index = {}, 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and values[ordered[end]] == values[ordered[index]]:
            end += 1
        percentile = 50.0 if len(ordered) == 1 else 100 * (index + end - 1) / (2 * (len(ordered) - 1))
        output.update((key, percentile) for key in ordered[index:end])
        index = end
    return output


def decode_universe_archive(payload: dict) -> dict:
    """Read a bounded minute observation archive, never inject it as live data."""
    if payload.get("encoding") != "zlib-base64-json-v1":
        raise ValueError("Unknown universe archive encoding")
    raw = base64.b64decode(payload["data"], validate=True)
    decoder = zlib.decompressobj()
    decoded = decoder.decompress(raw, 4_000_001)
    if len(decoded) > 4_000_000 or decoder.unconsumed_tail or not decoder.eof:
        raise ValueError("Universe archive exceeds bounded size")
    return json.loads(decoded)


class UniverseLiveWorker:
    def __init__(self, settings: Settings | None = None,
                 universe_settings: UniverseLiveSettings | None = None, *,
                 clock: Callable[[], datetime] = utcnow, stream=None, http=None,
                 identities: dict[str, Asset] | None = None, store=None,
                 feed_settings: LiveFeedSettings | None = None):
        self.settings = settings or Settings()
        self.config = universe_settings or UniverseLiveSettings(
            max_assets=min(1000, self.settings.top_n),
            promotion_min_quote_volume=self.settings.min_quote_volume_24h)
        self.clock, self.stream, self.http, self.store = clock, stream, http, store
        self.feed_settings = feed_settings or LiveFeedSettings(max_message_bytes=8 * 1024 * 1024)
        self.identities = identities or {}
        self._assets: dict[str, Asset] = {}
        self._observations: dict[str, deque[_Observation]] = {}
        self._latest: dict[str, _Observation] = {}
        self._benchmark: _Observation | None = None
        self._benchmark_history: deque[_Observation] = deque(maxlen=self.config.observation_capacity)
        self._verified_at: datetime | None = None
        self._excluded: Counter = Counter()
        self._exclusion_details: list[dict] = []
        self._verified_count = 0
        self._seeded = 0
        self._accepted, self._rejected, self._batches, self._version = 0, 0, 0, 0
        self._cache: dict | None = None
        self._cache_version, self._cache_second = -1, None
        self._status, self._error = "UNAVAILABLE", None
        self._running = False
        self._last_archive, self._archive_error = None, None
        self._archive_count, self._archive_bytes = 0, 0
        self._last_processing_ms = None
        self._stop: asyncio.Event | None = None

    def asset(self, symbol: str) -> Asset | None:
        value = self._assets.get(symbol.upper())
        return value.model_copy() if value else None

    async def bootstrap(self, http=None):
        """Two cheap native REST requests, no histories/books/CG hard gate."""
        provider = BinanceSpotProvider(http or self.http, self.settings)
        exchange = await provider._get("/api/v3/exchangeInfo", weight=20)
        tickers = await provider._get("/api/v3/ticker/24hr", weight=80)
        if not isinstance(exchange.data, dict) or not isinstance(exchange.data.get("symbols"), list):
            raise ValueError("UNAVAILABLE: invalid native exchangeInfo")
        if not isinstance(tickers.data, list):
            raise ValueError("UNAVAILABLE: invalid native 24h ticker array")
        verified, excluded, details = {}, Counter(), []
        for row in exchange.data["symbols"]:
            reason = None
            if not isinstance(row, dict):
                reason = "MALFORMED_SYMBOL_DEFINITION"
            elif row.get("status") != "TRADING":
                reason = "NOT_TRADING"
            elif row.get("quoteAsset") != "USDT":
                reason = "NON_USDT_QUOTE"
            elif row.get("isSpotTradingAllowed") is not True:
                reason = "SPOT_PERMISSION_NOT_VERIFIED"
            else:
                try:
                    symbol = row["symbol"]
                    normalize_symbols([symbol])
                    if symbol != row.get("baseAsset", "") + "USDT":
                        raise ValueError("identity mismatch")
                    verified[symbol] = row
                except (KeyError, ValueError, TypeError):
                    reason = "MALFORMED_NATIVE_IDENTITY"
            if reason:
                excluded[reason] += 1
                if len(details) < 10000:
                    details.append({"symbol": row.get("symbol") if isinstance(row, dict) else None,
                                    "reason": reason})
        observations = {}
        receipt = aware(tickers.retrieved_at)
        for row in tickers.data:
            if not isinstance(row, dict) or row.get("symbol") not in verified:
                continue
            try:
                event = datetime.fromtimestamp(int(row["closeTime"]) / 1000, UTC)
                if event > receipt:
                    raise ValueError("future native time")
                observation = _Observation(symbol=row["symbol"], price=row["lastPrice"],
                    open24h=row["openPrice"], high24h=row["highPrice"], low24h=row["lowPrice"],
                    basevolume24h=row["volume"], quotevolume24h=row["quoteVolume"],
                    event_timestamp=event, received_timestamp=receipt,
                    source="binance/spot/ticker24h", streamed=False)
                if not observation.low24h <= min(observation.price, observation.open24h) <= max(
                    observation.price, observation.open24h) <= observation.high24h:
                    raise ValueError("inconsistent native price bounds")
                observations[observation.symbol] = observation
            except (KeyError, ValueError, TypeError, OverflowError):
                excluded["MALFORMED_OR_NONCAUSAL_TICKER"] += 1
                details.append({"symbol": row.get("symbol"), "reason": "MALFORMED_OR_NONCAUSAL_TICKER"})
        selected = sorted((symbol for symbol, obs in observations.items()
                           if obs.quotevolume24h >= self.config.min_quote_volume),
                          key=lambda symbol: (-observations[symbol].quotevolume24h, symbol))[:self.config.max_assets]
        if not selected:
            raise ValueError("UNAVAILABLE: no verified liquid USDT spot symbols")
        new_assets = {}
        for symbol in selected:
            base = verified[symbol]["baseAsset"]
            identity = self.identities.get(symbol)
            if not identity or identity.exchange_symbol != symbol or identity.symbol.upper() != base:
                identity = Asset(asset_id="binance:spot:" + symbol, symbol=base, name=base,
                                 exchange_symbol=symbol, quote_asset="USDT")
            new_assets[symbol] = identity
        self._assets = new_assets
        self._latest = {symbol: self._latest.get(symbol, observations[symbol]) for symbol in selected}
        self._observations = {symbol: self._observations.get(symbol, deque(maxlen=self.config.observation_capacity))
                              for symbol in selected}
        self._benchmark = self._benchmark or observations.get("BTCUSDT")
        self._verified_at, self._verified_count = aware(exchange.retrieved_at), len(verified)
        self._seeded, self._excluded, self._exclusion_details = len(selected), excluded, details
        self._status, self._error = "DELAYED", None
        self._version += 1

    def apply(self, batch: list[UniverseTicker]) -> int:
        started, accepted = time.perf_counter(), 0
        for ticker in batch:
            if ticker.symbol not in self._assets and ticker.symbol != "BTCUSDT":
                continue
            previous = self._latest.get(ticker.symbol) if ticker.symbol in self._assets else self._benchmark
            if previous and ticker.event_timestamp <= previous.event_timestamp:
                self._rejected += 1
                continue
            obs = _Observation(**ticker.model_dump(exclude={"quality", "sequence_supported", "processing_timestamp"}),
                               streamed=True)
            if ticker.symbol in self._assets:
                self._latest[ticker.symbol] = obs
                history = self._observations[ticker.symbol]
                # A bounded native observation per exchange-second is enough for
                # minute baselines; the actual most recent quote is still kept.
                if history and int(history[-1].event_timestamp.timestamp()) == int(obs.event_timestamp.timestamp()):
                    history[-1] = obs
                else:
                    history.append(obs)
            if ticker.symbol == "BTCUSDT":
                self._benchmark = obs
                if self._benchmark_history and int(self._benchmark_history[-1].event_timestamp.timestamp()) == int(obs.event_timestamp.timestamp()):
                    self._benchmark_history[-1] = obs
                else:
                    self._benchmark_history.append(obs)
            accepted += 1
        self._accepted += accepted
        self._batches += 1
        self._version += bool(accepted)
        self._last_processing_ms = (time.perf_counter() - started) * 1000
        return accepted

    def _freshness(self, obs: _Observation, at: datetime) -> str:
        if not self._running:
            return "STALE"
        age = max((at - obs.event_timestamp).total_seconds(), (at - obs.received_timestamp).total_seconds())
        if age < 0:
            return "UNAVAILABLE"
        if age > self.config.stale_age_seconds:
            return "STALE"
        return "LIVE" if obs.streamed and age <= self.config.live_age_seconds else "DELAYED"

    def _baseline(self, history, end: _Observation, seconds: int) -> _Observation | None:
        cutoff = end.event_timestamp - timedelta(seconds=seconds)
        for obs in reversed(history):
            if obs.event_timestamp <= cutoff:
                gap = (cutoff - obs.event_timestamp).total_seconds()
                return obs if gap <= self.config.baseline_tolerance_seconds else None
        return None

    def snapshot(self, as_of: datetime | None = None) -> dict:
        at = aware(as_of or self.clock())
        if as_of is not None and at < aware(self.clock()) - timedelta(seconds=1):
            # Current mutable universe membership is not historical evidence.
            return {"as_of": at.isoformat(), "status": "UNAVAILABLE", "assets": [],
                    "counts": {"requested": self.config.max_assets}, "health": self.health(at),
                    "limitations": ["POINT_IN_TIME_QUERY_REQUIRES_IMMUTABLE_MINUTE_ARCHIVE"]}
        second = int(at.timestamp())
        cache_after_decision = bool(self._cache and datetime.fromisoformat(self._cache["as_of"]) > at)
        if self._cache_version != self._version or self._cache_second != second or cache_after_decision:
            rows = []
            benchmark = self._benchmark
            benchmark_fresh = benchmark and self._freshness(benchmark, at) in {"LIVE", "DELAYED"}
            btc24 = (benchmark.price / benchmark.open24h - 1) * 100 if benchmark_fresh else None
            btcbase = self._baseline(self._benchmark_history, benchmark, 60) if benchmark_fresh else None
            btc1 = (benchmark.price / btcbase.price - 1) * 100 if btcbase else None
            for symbol, asset in self._assets.items():
                obs = self._latest.get(symbol)
                if not obs or obs.received_timestamp > at or obs.event_timestamp > at:
                    continue
                history = self._observations[symbol]
                baseline, middle = self._baseline(history, obs, 60), self._baseline(history, obs, 30)
                ret1 = (obs.price / baseline.price - 1) * 100 if baseline else None
                volume_acceleration = None
                if baseline and middle:
                    first = (middle.quotevolume24h - baseline.quotevolume24h) / (middle.event_timestamp - baseline.event_timestamp).total_seconds()
                    last = (obs.quotevolume24h - middle.quotevolume24h) / (obs.event_timestamp - middle.event_timestamp).total_seconds()
                    volume_acceleration = (last - first) / ((obs.event_timestamp - baseline.event_timestamp).total_seconds() / 2)
                change24 = (obs.price / obs.open24h - 1) * 100
                fresh = self._freshness(obs, at)
                rows.append({"asset_id": asset.asset_id, "symbol": symbol, "base_symbol": asset.symbol,
                    "price": obs.price, "open_24h": obs.open24h, "high_24h": obs.high24h, "low_24h": obs.low24h,
                    "price_change_pct": change24, "quote_volume": obs.quotevolume24h,
                    "base_volume": obs.basevolume24h, "return_1m": ret1,
                    "return_1m_window_start": baseline.event_timestamp.isoformat() if baseline else None,
                    "return_1m_window_end": obs.event_timestamp.isoformat() if baseline else None,
                    "return_1m_window_seconds": (obs.event_timestamp - baseline.event_timestamp).total_seconds() if baseline else None,
                    "relative_strength_btc": change24 - btc24 if btc24 is not None else None,
                    "relative_strength_btc_1m": ret1 - btc1 if ret1 is not None and btc1 is not None else None,
                    "volume_acceleration": volume_acceleration,
                    "volume_acceleration_basis": "DERIVED_ROLLING_24H_QUOTE_VOLUME_PROXY",
                    "volume_acceleration_quality": "DERIVED" if volume_acceleration is not None else "UNAVAILABLE",
                    "source": obs.source, "source_timestamp": obs.event_timestamp.isoformat(),
                    "received_timestamp": obs.received_timestamp.isoformat(), "quality": "NATIVE",
                    "freshness": fresh, "age_seconds": max(0, (at - obs.event_timestamp).total_seconds()),
                    "feed_latency_ms": (obs.received_timestamp - obs.event_timestamp).total_seconds() * 1000,
                    "market_cap": None, "market_cap_quality": "UNAVAILABLE", "rank": None,
                    "rank_kind": "LIVE_ACTIVITY_RANK", "score_kind": "UNCALIBRATED_ACTIVITY_NOT_EXPANSION_PROBABILITY",
                    "activity_score": None, "activity_components": {}, "cross_sectional_percentiles": {},
                    "bias": "LONG" if ret1 is not None and ret1 > 0 else "SHORT" if ret1 is not None and ret1 < 0 else "UNAVAILABLE"})
            eligible = [row for row in rows if row["freshness"] in {"LIVE", "DELAYED"}]
            raw = {"price_activity": {r["symbol"]: abs(r["return_1m"] if r["return_1m"] is not None else r["price_change_pct"]) for r in eligible},
                   "quote_liquidity": {r["symbol"]: math.log1p(r["quote_volume"]) for r in eligible},
                   "relative_strength": {r["symbol"]: abs(r["relative_strength_btc"]) for r in eligible if r["relative_strength_btc"] is not None},
                   "rolling_volume_change": {r["symbol"]: abs(r["volume_acceleration"]) for r in eligible if r["volume_acceleration"] is not None}}
            percentiles = {name: _percentiles(values) for name, values in raw.items()}
            for row in eligible:
                components = {name: values[row["symbol"]] for name, values in percentiles.items() if row["symbol"] in values}
                row["activity_components"] = components
                row["cross_sectional_percentiles"] = dict(components)
                row["activity_score"] = mean(components.values()) if components else None
            ranked = sorted(eligible, key=lambda row: (-row["activity_score"], row["symbol"]))
            for rank, row in enumerate(ranked, 1):
                row["rank"] = rank
            counts = {"requested": self.config.max_assets, "verified_usdt_spot": self._verified_count,
                      "selected": len(self._assets), "seeded": self._seeded, "available": len(rows),
                      "live": sum(r["freshness"] == "LIVE" for r in rows),
                      "delayed": sum(r["freshness"] == "DELAYED" for r in rows),
                      "stale": sum(r["freshness"] == "STALE" for r in rows),
                      "excluded": sum(self._excluded.values()), "exclusion_reasons": dict(self._excluded)}
            self._cache = {"as_of": at.isoformat(), "status": "LIVE" if counts["live"] else "DELAYED" if counts["delayed"] else "STALE" if rows else "UNAVAILABLE",
                "assets": ranked + [r for r in rows if r["rank"] is None], "counts": counts,
                "limitations": ["CHANGED_SYMBOLS_ONLY; no promise of 500 new quotes each second",
                    "TOP_NATIVE_QUOTE_LIQUIDITY; not top market cap", "ROLLING_24H_TOTALS_NOT_1M_VOLUME_OR_CVD",
                    "LIVE_ACTIVITY_RANK_NOT_CALIBRATED_EXPANSION_SCORE", "NO_SEQUENCE_IDS_ON_NATIVE_MINITICKER"]}
            self._cache_version, self._cache_second = self._version, second
        # Return detached shallow rows so API clients cannot mutate shared cache.
        return {**self._cache, "assets": [{**row, "activity_components": dict(row["activity_components"]),
                    "cross_sectional_percentiles": dict(row["cross_sectional_percentiles"])} for row in self._cache["assets"]],
                "counts": {**self._cache["counts"], "exclusion_reasons": dict(self._cache["counts"]["exclusion_reasons"])},
                "health": self.health(at), "limitations": list(self._cache["limitations"])}

    def promotions(self, as_of: datetime | None = None) -> list[dict]:
        at = aware(as_of or self.clock())
        output = []
        for row in self.snapshot(at)["assets"]:
            relative = row["relative_strength_btc_1m"]
            if (row["freshness"] != "LIVE" or row["return_1m"] is None
                    or abs(row["return_1m"]) < self.config.promotion_min_return_1m_pct
                    or row["quote_volume"] < self.config.promotion_min_quote_volume
                    or relative is None or abs(relative) < self.config.promotion_min_relative_1m_pct):
                continue
            asset = self._assets[row["symbol"]]
            output.append({"asset": asset.model_dump(mode="json"), "asset_id": asset.asset_id,
                "symbol": row["symbol"], "bias": row["bias"], "rank": row["rank"],
                "created_at": at.isoformat(), "expires_at": (at + timedelta(seconds=self.config.promotion_ttl_seconds)).isoformat(),
                "source_timestamp": row["source_timestamp"], "source": "NATIVE_UNIVERSE_DISCOVERY",
                "reason": "Fresh observed 1m price activity and BTC-relative anomaly; fast pressure still unconfirmed",
                "return_1m": row["return_1m"], "relative_strength_btc_1m": relative,
                "quote_volume": row["quote_volume"], "native_instrument_verified": True,
                "readiness": None, "readiness_quality": "UNAVAILABLE"})
            if len(output) >= self.config.promotion_count:
                break
        return output if self.config.promotion_count else []

    def health(self, as_of: datetime | None = None) -> dict:
        at = aware(as_of or self.clock())
        feed = self.stream.health.model_dump(mode="json") if self.stream else None
        status = self._status if self._running else "STOPPED"
        if self._running and self._status == "LIVE" and (
                not feed or feed.get("stale", True) or feed.get("connection") != "CONNECTED"):
            status = "STALE"
        return {"status": status, "running": self._running,
            "updated_at": at.isoformat(), "provider": "binance-public-miniTicker", "feed": feed,
            "verified_at": self._verified_at.isoformat() if self._verified_at else None,
            "assets": len(self._assets), "requested": self.config.max_assets,
            "events_accepted": self._accepted, "events_rejected": self._rejected, "batches": self._batches,
            "last_processing_ms": self._last_processing_ms, "error": self._error,
            "archive_count": self._archive_count, "archive_bytes": self._archive_bytes,
            "archive_last_at": self._last_archive.isoformat() if self._last_archive else None,
            "archive_error": self._archive_error, "archive_resolution_seconds": self.config.archive_seconds,
            "settings": self.config.model_dump(mode="json"), "live_trading": False}

    def archive(self, snapshot: dict, promotions: list[dict], at: datetime):
        """Only called on the worker's background SQLite thread."""
        if not self.store:
            return
        raw = json.dumps(snapshot, separators=(",", ":"), allow_nan=False).encode()
        encoded = base64.b64encode(zlib.compress(raw, level=3)).decode()
        archive = {"encoding": "zlib-base64-json-v1", "data": encoded,
                   "as_of": at.isoformat(), "row_count": len(snapshot["assets"]),
                   "resolution_seconds": self.config.archive_seconds}
        self.store.append("universe_live_observation", at.isoformat(), at, at, archive)
        self.store.put_state("universe_live_health", "worker", at, self.health(at))
        for promotion in promotions:
            self.store.put_state("universe_promotion", promotion["symbol"], at, promotion)
        self._archive_count += 1
        self._archive_bytes += len(encoded)
        self._last_archive, self._archive_error = at, None

    async def _archive_loop(self, stop):
        while not stop.is_set():
            if self.store:
                at = aware(self.clock())
                snapshot, promotions = self.snapshot(at), self.promotions(at)
                task = asyncio.create_task(asyncio.to_thread(self.archive, snapshot, promotions, at))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task  # Drain the owned connection before shutdown.
                    raise
                except Exception as error:
                    self._archive_error = type(error).__name__
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.config.archive_seconds)
            except TimeoutError:
                pass

    async def _refresh_loop(self, http, stop):
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.config.refresh_seconds)
                return
            except TimeoutError:
                pass
            try:
                await self.bootstrap(http)
            except Exception as error:
                self._error = "NATIVE_UNIVERSE_REFRESH:" + type(error).__name__

    async def run(self, stop_event: asyncio.Event, seconds: float | None = None):
        self._stop, self._running = stop_event, True
        self._version += 1
        owned_http = self.http is None
        http = self.http or AsyncHTTPClient(self.settings)
        timer = asyncio.create_task(self._timer(stop_event, seconds)) if seconds else None
        archive, refresh = None, None
        try:
            while not stop_event.is_set() and not self._assets:
                try:
                    await self.bootstrap(http)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._error, self._status = "NATIVE_BOOTSTRAP:" + type(error).__name__, "UNAVAILABLE"
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=self.config.bootstrap_retry_seconds)
                    except TimeoutError:
                        pass
            if stop_event.is_set():
                return
            if self.stream is None or getattr(self.stream, "_stop", None) and self.stream._stop.is_set():
                self.stream = BinanceUniverseStream(self.feed_settings, clock=self.clock)
            archive = asyncio.create_task(self._archive_loop(stop_event))
            refresh = asyncio.create_task(self._refresh_loop(http, stop_event))
            async for batch in self.stream.events(stop_event):
                if stop_event.is_set():
                    break
                try:
                    self.apply(batch)
                    self._status, self._error = "LIVE", None
                except Exception as error:
                    self._error = "NATIVE_BATCH:" + type(error).__name__
                await asyncio.sleep(0)  # Buffered array receive must not starve 1m/API.
        finally:
            stop_event.set()
            if self.stream:
                self.stream.stop()
            tasks = [task for task in (archive, refresh, timer) if task]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if owned_http:
                await http.aclose()
            self._running, self._status = False, "STOPPED"
            self._version += 1

    @staticmethod
    async def _timer(stop, seconds):
        await asyncio.sleep(seconds)
        stop.set()


async def run_universe_worker(app, *, universe_settings=None, clock=utcnow):
    """Application supervisor: explicit Start/Stop, restart-safe current map."""
    store = None
    stop = asyncio.Event()
    app.state.universe_stop_event = stop
    worker = UniverseLiveWorker(app.state.settings, universe_settings, clock=clock)
    app.state.universe_worker = worker
    task, observation_stop = None, None
    try:
        while not stop.is_set():
            requested = getattr(app.state, "live_requested", False)
            if requested and (task is None or task.done()):
                if task:
                    result = task.exception() if not task.cancelled() else None
                    if result:
                        worker._error = "UNIVERSE_WORKER:" + type(result).__name__
                if store is None:
                    opening = asyncio.create_task(asyncio.to_thread(IntelligenceStore, app.state.settings.db_path))
                    try:
                        store = await asyncio.shield(opening)
                    except asyncio.CancelledError:
                        store = await opening
                        raise
                    worker.store = store
                observation_stop = asyncio.Event()
                task = asyncio.create_task(worker.run(observation_stop))
            elif not requested and task and not task.done():
                observation_stop.set()
                if worker.stream:
                    worker.stream.stop()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            app.state.universe_health = worker.health()
            await asyncio.sleep(0.5)
    finally:
        if observation_stop:
            observation_stop.set()
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if store:
            closing = asyncio.create_task(asyncio.to_thread(store.close))
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                await closing
        app.state.universe_health = worker.health()
