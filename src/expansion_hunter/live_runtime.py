"""Independent public-data fast worker, persistent observation mode only.

Run in a separate process from broad discovery/research. Closed candles trigger
local fast evaluation; trade crossings can emit an attempt without waiting for a
broad scan. REST is used only for warmup/gap repair, never for each tick.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import sqlite3
import time
from collections import Counter, OrderedDict, deque
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import partial
from multiprocessing import get_context

from .config import Settings
from .discovery_config import DiscoverySettings
from .discovery_features import deep_scores
from .fast import FastIgnitionMonitor
from .fast_models import FastSettings, FastStage, FastState
from .intelligence import context_at
from .intelligence_store import IntelligenceStore
from .live_analysis import analyze_live, order_book_analysis
from .live_feed import LiveFeedState
from .live_models import LiveFeedSettings, normalize_symbols
from .market_context import LargeTradeTracker, session_context
from .models import Asset, AssetSnapshot, Metric, Status, utcnow
from .providers import AsyncHTTPClient, BinanceSpotProvider
from .providers.binance_ws import BinancePublicStream
from .war_room import analyze_war_room


def _native_metric(value, event, *, unit=""):
    when = event.exchange_timestamp or event.received_timestamp
    if value is None or when > event.received_timestamp:
        return Metric(source=event.source, unit=unit, reason="value or valid exchange timestamp unavailable")
    return Metric(value=value, observed_at=when, available_at=event.received_timestamp,
                  source=event.source, unit=unit, status=Status.AVAILABLE)


def fast_snapshot(asset: Asset, state: LiveFeedState, as_of: datetime) -> AssetSnapshot:
    candles = {tf: state.candles(asset.exchange_symbol, tf, as_of, limit=120)
               for tf in ("1m", "5m", "15m")}
    market = {}
    ticker = state.latest_event(asset.exchange_symbol, "BOOK_TICKER")
    if ticker and ticker.bid and ticker.ask:
        mid = (ticker.bid.price + ticker.ask.price) / 2
        market["spread_bps"] = _native_metric((ticker.ask.price - ticker.bid.price) / mid * 10000,
                                               ticker, unit="bps")
        market["book_mid"] = _native_metric(mid, ticker, unit=asset.quote_asset)
    depth = state.latest_event(asset.exchange_symbol, "DEPTH")
    if depth:
        observed = sum(level.price*level.quantity for level in depth.bids+depth.asks)
        market["observed_depth_quote"] = _native_metric(observed, depth, unit=asset.quote_asset)
    return AssetSnapshot(asset=asset, as_of=as_of, candles=candles, market=market)


def chart_snapshot(asset: Asset, state: LiveFeedState, as_of: datetime) -> AssetSnapshot:
    """Use one Binance venue with transport provenance retained in raw caches.

    REST warmup and native WebSocket final candles are the same spot instrument,
    not different price providers. Normalize only these explicit native sources
    in the derived analysis copy. Unrecognized mixed sources remain rejected.
    Never change source timestamps or their actual receipt/availability times.
    """
    snapshot = fast_snapshot(asset, state, as_of)
    snapshot.candles = {
        frame: state.candles(asset.exchange_symbol, frame, as_of, limit=240)
        for frame in ("1m", "5m", "15m", "1h", "4h", "1D")
    }
    for frame, bars in snapshot.candles.items():
        canonical = f"binance/spot/{asset.exchange_symbol}/{frame}"
        sources = {bar.source for bar in bars}
        if len(sources) > 1 and sources <= {canonical, "binance-public-ws"}:
            snapshot.candles[frame] = [bar.model_copy(update={"source": canonical}) for bar in bars]
    return snapshot


def _calculate_analysis(symbol, snapshot, depth, btc, eth, provenance, now,
                        fast_state, previous_structure, market_context, score_bias):
    """Pure, picklable analysis job; no worker, SQLite, or live-feed ownership.

    A spawn worker imports this module without starting the application. Every
    input is frozen at the parent's point-in-time snapshot before submission.
    """
    analysis = analyze_live(snapshot, depth)
    context_time = market_context.get("as_of")
    known = bool(context_time and datetime.fromisoformat(context_time) <= now)
    cycle = market_context.get("cycle", {}) if known else {}
    analysis["market_regime"] = cycle.get("regime", "UNAVAILABLE")
    analysis["cycle_context"] = cycle or {"phase": "UNAVAILABLE", "reason": "No available cached cycle context"}
    analysis["macro_context"] = market_context.get("macro", {}) if known else {
        "status": "UNAVAILABLE", "reason": "No available cached macro context"}
    analysis["context_available_at"] = context_time if known else None
    scores, levels = deep_scores(snapshot, DiscoverySettings(), btc, bias=score_bias)
    analysis["score_bias"] = score_bias
    analysis["scores"] = {name: score.model_dump(mode="json") for name, score in scores.items()}
    analysis["score_levels"] = levels
    analysis["transport_provenance"] = provenance
    analysis["war_room"] = analyze_war_room(
        symbol, snapshot.candles, now, analysis, fast_state, previous_structure
    )
    from .relative_context import analyze_relative_context
    analysis["relative_context"] = analyze_relative_context(snapshot, btc, eth)
    return analysis


class LiveObservationWorker:
    def __init__(self, settings: Settings, feed_settings: LiveFeedSettings | None = None,
                 fast_settings: FastSettings | None = None, *, symbols: list[str] | None = None,
                 clock=utcnow, stream=None):
        self.settings = settings
        self.feed_settings = feed_settings or LiveFeedSettings(timeframes=["1m", "5m", "15m", "1h", "4h", "1D"])
        self.fast_settings = fast_settings or FastSettings()
        self.clock = clock
        self.store = IntelligenceStore(settings.db_path)
        restored = [FastState.model_validate(s) for s in self.store.states("fast_state")]
        self.monitor = FastIgnitionMonitor(self.fast_settings, restored)
        self._retention = {row["symbol"]: row for row in self.store.states("fast_retention")}
        self._retained_symbols: set[str] = set()
        self._chart_interest_symbols: set[str] = set()
        self._chart_interest_issues: list[dict] = []
        self._war_room = {row["symbol"]: row for row in self.store.states("war_room_state")}
        self.state = LiveFeedState(self.feed_settings)
        self.large_trade_tracker = LargeTradeTracker()
        self.symbols = symbols or []
        self.stream = stream
        self.assets: dict[str, Asset] = {}
        self.stop = asyncio.Event()
        self._last_publish: dict[str, float] = {}
        self._context: dict = {}
        self._latencies: deque[float] = deque(maxlen=10000)
        self._cooperative_yields = 0
        self._latency_includes_persistence = True
        self._last_health = 0.0
        self._analysis: dict[str, dict] = {}
        self._analysis_tasks: dict[str, asyncio.Task] = {}
        self._analysis_slots = asyncio.Semaphore(self.feed_settings.analysis_concurrency)
        self._analysis_requested: dict[str, tuple] = {}
        self._history_cache: dict[str, dict[str, tuple[int, list[dict], datetime | None]]] = {}
        self._book_levels: dict[str, list[dict]] = {}
        self._warmup_slots = asyncio.Semaphore(self.feed_settings.warmup_concurrency)
        self._warming: set[str] = set()
        self._warmup_completed: set[str] = set()
        self._warmup_tasks: set[asyncio.Task] = set()
        self._repair_slots = asyncio.Semaphore(self.feed_settings.candle_repair_concurrency)
        self._repair_tasks: dict[str, asyncio.Task] = {}
        self._repair_attempts: dict[str, float] = {}
        self._repair_status: dict[str, dict] = {}
        self._repair_provider = None
        self._repair_epoch: int | None = None
        self._writer_executor: ThreadPoolExecutor | None = None
        self._analysis_executor: ProcessPoolExecutor | None = None
        self._analysis_backend = "DIRECT_THREAD"
        self._analysis_errors: dict[str, str] = {}
        self._analysis_failures = 0
        self._writer_task: asyncio.Task | None = None
        self._writer_wakeup = asyncio.Event()
        self._critical_writes: deque[tuple] = deque()
        self._latest_writes: OrderedDict[tuple, tuple] = OrderedDict()
        self._writer_closing = False
        self._writer_active = False
        self._writer_error: str | None = None
        self._writer_retries = 0
        self._writer_completed = 0
        self._writer_coalesced = 0
        self._writer_high_water = 0
        self._writer_overloaded = False
        self._writer_last_ms: float | None = None
        self._writer_inflight_since: float | None = None
        self._writer_inflight_critical = False
        self._writer_failed = False

    def _enqueue_write(self, operation, *args, latest_key=None):
        """Observation records are FIFO; replaceable UI caches are coalesced.

        Synchronous replay/direct unit callers retain immediate persistence.
        Production never waits for a SQLite lock on the ingestion event loop.
        At capacity, stop ingestion visibly and drain the accepted records;
        final candles and alerts are never discarded as replaceable ticks.
        """
        if not self._writer_active:
            return operation(*args)
        job = (operation, args, time.monotonic())
        if latest_key is not None:
            if latest_key in self._latest_writes:
                self._writer_coalesced += 1
            self._latest_writes[latest_key] = job
        else:
            self._critical_writes.append(job)
            self._writer_high_water = max(self._writer_high_water, len(self._critical_writes))
            if len(self._critical_writes) >= self.feed_settings.persistence_queue_size:
                self._writer_overloaded = True
                self._writer_error = "PERSISTENCE_BACKPRESSURE: ingestion stopped; accepted records pending"
                self.stop.set()
                if self.stream and hasattr(self.stream, "stop"):
                    self.stream.stop()
        self._writer_wakeup.set()

    def _put_state(self, kind, identity, at, payload):
        # Freeze mutable contracts before another candle advances their state.
        if hasattr(payload, "model_copy"):
            payload = payload.model_copy(deep=True)
        if kind == "fast_state":
            # Demotion and completed-candle evaluations share one chronological
            # stream. A later priority event must not leapfrog an older cached
            # demotion and make the persisted state move backward.
            return self._enqueue_write(self.store.put_state, kind, identity, at, payload)
        return self._enqueue_write(self.store.put_state, kind, identity, at, payload,
                                   latest_key=(kind, identity))

    def _start_writer(self):
        if self._writer_active:
            return
        # This worker owns its connection. Short waits plus explicit async retry
        # replace its inherited 30-second SQLite lock wait; other stores retain
        # their own configuration and no database schema/data is changed here.
        self.store._db.execute(f"PRAGMA busy_timeout={self.feed_settings.persistence_busy_timeout_ms}")
        self._writer_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eh-persistence")
        self._writer_active = True
        self._latency_includes_persistence = False
        self._writer_closing = False
        self._writer_task = asyncio.create_task(self._write_loop())

    async def _write_loop(self):
        loop = asyncio.get_running_loop()
        while self._critical_writes or self._latest_writes or not self._writer_closing:
            if not self._critical_writes and not self._latest_writes:
                self._writer_wakeup.clear()
                await self._writer_wakeup.wait()
                continue
            critical = bool(self._critical_writes)
            key = None if critical else next(
                (candidate for candidate in self._latest_writes
                 if candidate[0] in {"feed_health", "live_runtime", "live_metrics", "live_asset_summary"}),
                next(iter(self._latest_writes)),
            )
            operation, args, queued_at = (
                self._critical_writes[0] if critical else self._latest_writes[key]
            )
            self._writer_inflight_since = queued_at
            self._writer_inflight_critical = critical
            started = time.perf_counter()
            try:
                await loop.run_in_executor(self._writer_executor, partial(operation, *args))
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    self._writer_retries += 1
                    self._writer_error = f"PERSISTENCE_RETRY: {type(exc).__name__}: {exc}"
                    await asyncio.sleep(self.feed_settings.persistence_retry_seconds)
                    continue
                self._writer_failed = True
                self._writer_error = f"PERSISTENCE_FAILED: {type(exc).__name__}: {exc}"
                self.stop.set()
                if self.stream and hasattr(self.stream, "stop"):
                    self.stream.stop()
                raise
            except Exception as exc:
                self._writer_failed = True
                self._writer_error = f"PERSISTENCE_FAILED: {type(exc).__name__}: {exc}"
                self.stop.set()
                if self.stream and hasattr(self.stream, "stop"):
                    self.stream.stop()
                raise
            else:
                self._writer_last_ms = (time.perf_counter() - started) * 1000
                self._writer_completed += 1
                if critical:
                    self._critical_writes.popleft()
                elif self._latest_writes.get(key) is not None and self._latest_writes[key][2] == queued_at:
                    self._latest_writes.pop(key)
                elif key in self._latest_writes:
                    self._latest_writes.move_to_end(key)
                if not self._writer_overloaded:
                    self._writer_error = None
            finally:
                self._writer_inflight_since = None
                self._writer_inflight_critical = False

    async def _finish_writer(self):
        if not self._writer_active:
            return
        self._writer_closing = True
        self._writer_wakeup.set()
        try:
            # Accepted archive jobs must finish before closing their connection.
            # Database busy waits occur only on this owned persistence thread.
            await asyncio.wait_for(asyncio.shield(self._writer_task),
                                   self.feed_settings.persistence_shutdown_seconds)
        except TimeoutError as exc:
            self._writer_failed = True
            self._writer_error = ("PERSISTENCE_SHUTDOWN_TIMEOUT: "
                                  f"{len(self._critical_writes)} accepted archive jobs not durable")
            self._writer_task.cancel()
            await asyncio.gather(self._writer_task, return_exceptions=True)
            raise RuntimeError(self._writer_error) from exc
        finally:
            self._writer_executor.shutdown(wait=True, cancel_futures=True)
            self._writer_executor = None
            self._writer_active = False

    def _history_revision(self, symbol):
        return tuple(self.state.history_version(symbol, frame)
                     for frame in ("1m", "5m", "15m", "1h", "4h", "1D"))

    def membership(self):
        personal = self.store.watchlist()
        self._personal_symbols = {item["symbol"].upper() for item in personal} | {
            symbol.upper() for symbol in self.symbols
        }
        candidates = {}
        for item in personal:
            candidates[item["symbol"].upper()] = Asset(asset_id=item["asset_id"], symbol=item["symbol"],
                name=item["asset_id"], exchange_symbol=item["symbol"].upper())
        for symbol in self.symbols:
            candidates.setdefault(symbol.upper(), Asset(asset_id=f"exchange:binance:{symbol.upper()}",
                symbol=symbol.upper(), name=symbol.upper(), exchange_symbol=symbol.upper()))
        cycle_id = self.store.latest_cycle_id()
        if cycle_id != getattr(self, "_membership_cycle_id", None):
            cycle = self.store.latest_summary()
            self.discovery_coverage = {
                "counts": dict(cycle.counts) if cycle else {},
                "completed_at": cycle.completed_at.isoformat() if cycle else None,
                "freshness_counts": dict(Counter(observation.freshness.value for observation in cycle.observations)) if cycle else {},
                "scope": "LAST_COMPLETED_DISCOVERY_CYCLE",
            }
            dynamic = {}
            benchmarks = {}
            eligible = {}
            observed_bias = {}
            if cycle:
                rows = sorted(cycle.observations, key=lambda o: o.rank.current_rank or float("inf"))
                for observation in rows:
                    if observation.asset.exchange_symbol in self.feed_settings.benchmark_symbols:
                        benchmarks[observation.asset.exchange_symbol] = observation.asset
                    if (observation.eligible and observation.asset.exchange_symbol
                            and observation.disposition in {"COMPLETE", "PARTIAL"}):
                        eligible[observation.asset.exchange_symbol] = observation.asset
                        observed_bias[observation.asset.exchange_symbol] = observation.bias
                    if (observation.eligible and observation.asset.exchange_symbol
                            and observation.stage.value in {"RADAR", "WATCH", "PRE_IGNITION", "LOADED", "IGNITION"}):
                        dynamic.setdefault(observation.asset.exchange_symbol, observation.asset)
            self._membership_cycle_id = cycle.cycle_id if cycle else None
            self._discovery_candidates = dynamic
            self._benchmark_assets = benchmarks
            self._discovery_eligible = eligible
            self._discovery_bias = observed_bias
        required = len(candidates)
        if required > self.feed_settings.max_symbols:
            raise ValueError("personal watchlist exceeds feed capacity; increase configured max_symbols")
        native = self._native_universe_candidates(self.clock())
        self._native_universe_symbols = set(native)
        retained = self._active_setup_membership(self.clock())
        self._retained_symbols = set(retained)
        interests = self._chart_interests(self.clock())
        self._chart_interest_symbols = set(interests)
        dynamic = {**retained, **interests, **native, **getattr(self, "_discovery_candidates", {})}
        for symbol, asset in dynamic.items():
            if len(candidates) >= self.feed_settings.max_symbols:
                break
            candidates.setdefault(symbol, asset.model_copy(deep=True))
        return dict(list(candidates.items())[:self.feed_settings.max_symbols])

    def _native_universe_candidates(self, now):
        """Native ranked anomalies bypass CG identity coverage, never data safety.

        Eligibility is verified public USDT spot metadata, dated observations
        and a bounded explicit TTL. Watch membership grants OHLCV monitoring;
        it supplies no fabricated readiness/volume or automatic breakout.
        """
        candidates = {}
        native_eligible = {}
        issues = []
        for row in self.store.states("universe_promotion"):
            try:
                symbol = row["symbol"]
                normalize_symbols([symbol], self.feed_settings.max_symbols)
                created, expires = datetime.fromisoformat(row["created_at"]), datetime.fromisoformat(row["expires_at"])
                if not created.tzinfo or not expires.tzinfo or not 0 < (expires - created).total_seconds() <= 900:
                    raise ValueError("native promotion requires causal bounded timestamps")
                if not created <= now < max(expires, created + timedelta(
                        seconds=self.feed_settings.active_setup_retention_seconds)):
                    continue
                source_at = datetime.fromisoformat(row["source_timestamp"])
                if (not source_at.tzinfo or not source_at <= created
                        or (created - source_at).total_seconds() > 15
                        or row.get("native_instrument_verified") is not True
                        or row.get("source") != "NATIVE_UNIVERSE_DISCOVERY"):
                    raise ValueError("native promotion lacks current verified source")
                asset = Asset.model_validate(row["asset"])
                if asset.exchange_symbol != symbol or asset.quote_asset != "USDT" or symbol != asset.symbol.upper() + "USDT":
                    raise ValueError("native promotion instrument identity mismatch")
                if row.get("bias") not in {"LONG", "SHORT"}:
                    raise ValueError("native promotion has no measured directional bias")
                native_eligible.setdefault(symbol, asset)
                if now >= expires:
                    continue  # Expired ranking cannot grant a new membership.
                candidates.setdefault(symbol, asset)
                if not hasattr(self, "_discovery_bias"):
                    self._discovery_bias = {}
                self._discovery_bias[symbol] = row["bias"]
            except (KeyError, TypeError, ValueError) as error:
                issues.append({"symbol": row.get("symbol"), "status": "UNAVAILABLE", "reason": str(error)})
        self._native_universe_issues = issues
        self._native_universe_candidates_cache = native_eligible
        return candidates

    def _chart_interests(self, now):
        """Validated API requests grant bounded feed access, never a score bonus.

        No permanent watchlist entry is created. Native instrument validation
        belongs to the API/provider resolver; this layer enforces causal expiry
        and the same total feed budget as every other subscription.
        """
        interested = {}
        issues = []
        for row in self.store.states("chart_interest"):
            symbol = row.get("symbol", "")
            try:
                normalize_symbols([symbol], self.feed_settings.max_symbols)
                created = datetime.fromisoformat(row["created_at"])
                expires = datetime.fromisoformat(row["expires_at"])
                if not created.tzinfo or not expires.tzinfo:
                    raise ValueError("chart interest timestamps require a timezone")
                if not 0 < (expires - created).total_seconds() <= 900:
                    raise ValueError("chart interest lifetime must be between 0 and 900 seconds")
                if not created <= now < expires:
                    continue
                if row.get("asset"):
                    asset = Asset.model_validate(row["asset"])
                    if asset.asset_id != row["asset_id"] or asset.exchange_symbol != symbol:
                        raise ValueError("chart interest instrument identity differs from validated asset")
                else:
                    asset = Asset(asset_id=row["asset_id"], symbol=symbol, name=row["asset_id"],
                                  exchange_symbol=symbol, quote_asset=row.get("quote_asset", "USDT"))
                interested.setdefault(symbol, asset)
            except (KeyError, TypeError, ValueError) as exc:
                issues.append({"symbol": symbol, "status": "UNAVAILABLE", "reason": str(exc)})
        self._chart_interest_issues = issues
        return interested

    def _active_setup_membership(self, now):
        """Ranking cannot interrupt a fresh active setup solely by migration.

        Retention affects monitoring only. Hard current eligibility, stale-data
        safeguards and the total subscription cap remain authoritative. The
        fixed window starts at a material transition, never every new tick.
        """
        if not self.feed_settings.active_setup_retention_seconds:
            return {}
        active = {FastStage.BREAKOUT_PRESSURE, FastStage.BREAKOUT_ATTEMPT,
                  FastStage.BREAKOUT_CONFIRMED, FastStage.ACCEPTED,
                  FastStage.BREAKDOWN_PRESSURE, FastStage.BREAKDOWN_ATTEMPT,
                  FastStage.BREAKDOWN_CONFIRMED, FastStage.ACCEPTED_DOWN}
        eligible = {**getattr(self, "_discovery_eligible", {}),
                    **getattr(self, "_native_universe_candidates_cache", {})}
        candidates = []
        for state in list(self.monitor.states.values()):
            symbol = state.asset.exchange_symbol
            boundary = state.trigger_level
            if (symbol not in eligible or state.stage not in active or state.stale
                    or not boundary or not state.last_evaluation_at
                    or state.data_quality.get("spot_candles") != "AVAILABLE"
                    or not 0 <= (now - state.last_evaluation_at).total_seconds()
                            <= self.fast_settings.candle_stale_seconds):
                continue
            marker = self._retention.get(symbol, {})
            if (marker.get("trigger_price", marker.get("resistance_price")) == boundary.price
                    and marker.get("trigger_discovered_at", marker.get("resistance_discovered_at")) == boundary.discovered_at.isoformat()
                    and marker.get("bias", "LONG") == state.bias):
                anchor = datetime.fromisoformat(marker["started_at"])
            else:
                # Conservatively support pre-upgrade states without inventing
                # a later pressure discovery time from the current scan.
                anchor = state.accepted_at or state.confirmed_at or state.attempt_at or boundary.discovered_at
            age = (now - anchor).total_seconds()
            if 0 <= age < self.feed_settings.active_setup_retention_seconds:
                priority = {FastStage.ACCEPTED: 0, FastStage.BREAKOUT_CONFIRMED: 1,
                            FastStage.BREAKOUT_ATTEMPT: 2, FastStage.BREAKOUT_PRESSURE: 3,
                            FastStage.ACCEPTED_DOWN: 0, FastStage.BREAKDOWN_CONFIRMED: 1,
                            FastStage.BREAKDOWN_ATTEMPT: 2, FastStage.BREAKDOWN_PRESSURE: 3}[state.stage]
                candidates.append((priority, anchor, symbol, eligible[symbol]))
        return {symbol: asset for _, _, symbol, asset in sorted(candidates)}

    def _remember_setup_retention(self, result):
        active = {str(FastStage.BREAKOUT_PRESSURE), str(FastStage.BREAKOUT_ATTEMPT),
                  str(FastStage.BREAKOUT_CONFIRMED), str(FastStage.ACCEPTED),
                  str(FastStage.BREAKDOWN_PRESSURE), str(FastStage.BREAKDOWN_ATTEMPT),
                  str(FastStage.BREAKDOWN_CONFIRMED), str(FastStage.ACCEPTED_DOWN)}
        for event in result.events:
            symbol = result.state.asset.exchange_symbol
            boundary = event.support if event.bias == "SHORT" else event.resistance
            if event.event_type not in active or not boundary or not symbol:
                continue
            anchor = event.alert_created_timestamp
            marker = {"symbol": symbol, "asset_id": event.asset_id,
                      "stage": event.event_type, "started_at": anchor.isoformat(),
                      "expires_at": (anchor + timedelta(seconds=self.feed_settings.active_setup_retention_seconds)).isoformat(),
                      "resistance_price": event.resistance.price if event.resistance else None,
                      "resistance_discovered_at": event.resistance.discovered_at.isoformat() if event.resistance else None,
                      "support_price": event.support.price if event.support else None,
                      "trigger_price": boundary.price, "bias": event.bias,
                      "trigger_discovered_at": boundary.discovered_at.isoformat(),
                      "reason": "MONITORING_ONLY: retain fresh active setup despite broad rank migration"}
            self._retention[symbol] = marker
            self._put_state("fast_retention", symbol, anchor, marker)

    def _subscriptions(self, candidates):
        """Context subscriptions stay separate from real fast-watch membership.

        Reserve benchmark capacity from the last dynamic candidates only when
        necessary. Personal/explicit monitored assets are never silently evicted.
        No context-only asset can be promoted or emit technical fast alerts.
        """
        subscribed = dict(candidates)
        missing = [symbol for symbol in self.feed_settings.benchmark_symbols
                   if symbol not in subscribed]
        for symbol in list(reversed(subscribed)):
            if len(subscribed) + len(missing) <= self.feed_settings.max_symbols:
                break
            if symbol not in getattr(self, "_personal_symbols", set()) and symbol not in self.feed_settings.benchmark_symbols:
                subscribed.pop(symbol)
        context_only = set()
        for symbol in missing:
            if len(subscribed) >= self.feed_settings.max_symbols:
                break
            asset = getattr(self, "_benchmark_assets", {}).get(symbol)
            subscribed[symbol] = asset.model_copy(deep=True) if asset else Asset(
                asset_id="bitcoin" if symbol == "BTCUSDT" else "ethereum",
                symbol="BTC" if symbol == "BTCUSDT" else "ETH",
                name="Bitcoin" if symbol == "BTCUSDT" else "Ethereum", exchange_symbol=symbol,
            )
            context_only.add(symbol)
        self._context_symbols = context_only
        self._candidate_symbols = set(candidates) & set(subscribed)
        self._capacity_exclusions = sorted(set(candidates) - set(subscribed))
        for symbol in self._candidate_symbols:
            self._restart_demoted_episode(subscribed[symbol], self.clock())
        return subscribed

    async def warmup(self, http, symbols):
        spot = BinanceSpotProvider(http, self.settings)

        async def seed(symbol):
            if symbol in self._warming or symbol not in self.assets or self.stop.is_set():
                return
            self._warming.add(symbol)
            try:
                async with self._warmup_slots:
                    if symbol not in self.assets or self.stop.is_set():
                        return
                    asset = self.assets[symbol]
                    for frame in ("1m", "5m", "15m", "1h", "4h", "1D"):
                        if symbol not in self.assets or self.stop.is_set():
                            return
                        try:
                            bars = await asyncio.wait_for(spot.candles(
                                asset, frame, max(240, self.fast_settings.history_bars), self.clock()), 20)
                            self.state.seed_candles(symbol, frame, bars, self.clock())
                        except Exception as exc:
                            self._put_state("provider_error", f"{symbol}:{frame}", self.clock(),
                                {"symbol": symbol, "timeframe": frame, "status": "UNAVAILABLE",
                                 "error": f"{type(exc).__name__}: {exc}"})
                    # One ready coin need not wait for every other coin's history.
                    # Warmup establishes current resistance, never backdated alerts.
                    if symbol in self.assets and not self.stop.is_set():
                        self._evaluate(symbol, self.clock())
                        self._schedule_analysis(symbol)
            finally:
                self._warming.discard(symbol)
                if symbol in self.assets:
                    self._warmup_completed.add(symbol)
                    self._schedule_repair(symbol)

        await asyncio.gather(*(seed(symbol) for symbol in symbols))

    def _start_warmup(self, http, symbols):
        task = asyncio.create_task(self.warmup(http, symbols))
        self._warmup_tasks.add(task)
        task.add_done_callback(self._warmup_tasks.discard)
        return task

    def _schedule_repair(self, symbol):
        """Repair native candle continuity without blocking market ingestion."""
        if (self._repair_provider is None or symbol not in self.assets
                or symbol in self._warming or self.stop.is_set()
                or (symbol not in self._warmup_completed and not self.state.history_version(symbol, "1m"))):
            return
        existing = self._repair_tasks.get(symbol)
        if existing and not existing.done():
            return
        if time.monotonic() - self._repair_attempts.get(symbol, -float("inf")) < self.feed_settings.candle_repair_retry_seconds:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        from .live_repair import detect_repair_frames
        asset = self.assets[symbol]
        plans = detect_repair_frames(asset, self.state, self.monitor.states.get(asset.asset_id),
                                     self.clock(), max_bars=self.feed_settings.candle_repair_max_bars)
        if plans:
            self._repair_tasks[symbol] = loop.create_task(self._repair_candles(symbol, plans))

    async def _repair_candles(self, symbol, plans):
        from .live_repair import fetch_repair
        try:
            async with self._repair_slots:
                if symbol not in self.assets or self.stop.is_set():
                    return
                self._repair_attempts[symbol] = time.monotonic()
                asset = self.assets[symbol]
                status = {"symbol": symbol, "status": "REPAIRING", "requested_at": self.clock().isoformat(),
                          "frames": [], "bars_added": 0, "issues": []}
                self._repair_status[symbol] = status
                self._put_state("candle_repair", symbol, self.clock(), copy.deepcopy(status))
                for plan in plans:
                    if symbol not in self.assets or self.stop.is_set():
                        return
                    result = await fetch_repair(self._repair_provider, asset, plan, clock=self.clock,
                                               timeout_seconds=self.feed_settings.candle_repair_timeout_seconds)
                    if symbol not in self.assets or self.stop.is_set():
                        return
                    status["frames"].append({"timeframe": plan.timeframe, "status": result.status,
                                             "bars_added": len(result.bars)})
                    status["issues"].extend(result.issues)
                    if result.bars:
                        self.state.seed_candles(symbol, plan.timeframe, result.bars, result.received_at)
                        status["bars_added"] += len(result.bars)
                    restored = self.monitor.states.get(asset.asset_id)
                    if restored and result.status in {"REPAIRED", "PARTIAL"}:
                        # REST recovery repairs history only. Neither old gap
                        # closes nor old post-break acceptances become live
                        # alerts at receipt time; the next native close triggers.
                        bars = self.state.candles(symbol, plan.timeframe, result.received_at, limit=1)
                        if bars:
                            previous = restored.last_processed.get(plan.timeframe)
                            restored.last_processed[plan.timeframe] = max(
                                previous or bars[-1].close_time, bars[-1].close_time
                            )
                status["completed_at"] = self.clock().isoformat()
                status["status"] = "DEGRADED" if (
                    status["issues"] or any(frame["status"] != "REPAIRED" for frame in status["frames"])
                ) else "AVAILABLE"
                status["scope"] = "NATIVE_SPOT_REST_HISTORY_ONLY; no retrospective trigger alerts"
                self._evaluate(symbol, self.clock())
                current = self.monitor.states.get(asset.asset_id)
                if current and symbol not in getattr(self, "_context_symbols", set()):
                    status["post_evaluation_quality"] = {
                        frame["timeframe"]: current.data_quality.get(frame["timeframe"], "UNAVAILABLE")
                        for frame in status["frames"]
                    }
                    status["continuity_restored"] = all(
                        quality == "AVAILABLE" for quality in status["post_evaluation_quality"].values()
                    )
                    if not status["continuity_restored"]:
                        status["status"] = "DEGRADED"
                self._repair_status[symbol] = status
                self._put_state("candle_repair", symbol, self.clock(), copy.deepcopy(status))
                self._schedule_analysis(symbol)
                self._publish(symbol, self.clock())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status = {"symbol": symbol, "status": "FAILED", "completed_at": self.clock().isoformat(),
                      "error": f"{type(exc).__name__}: {exc}"}
            self._repair_status[symbol] = status
            self._put_state("candle_repair", symbol, self.clock(), status)

    def _schedule_analysis(self, symbol):
        # Heavy chart/research calculations cannot delay the completed 1m trigger.
        # Synchronous replay may deliberately run without an asyncio loop.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._analysis_requested[symbol] = self._history_revision(symbol)
        existing = self._analysis_tasks.get(symbol)
        if existing and not existing.done():
            return
        self._analysis_tasks[symbol] = loop.create_task(self._refresh_analysis(symbol))

    def _analysis_bias(self, symbol, fast_state):
        if fast_state and fast_state.trigger_level:
            return fast_state.bias if not fast_state.stale else "UNAVAILABLE"
        return getattr(self, "_discovery_bias", {}).get(symbol, "UNAVAILABLE")

    def _start_analysis(self):
        if self._analysis_executor is not None:
            return
        # Spawn is explicit on every platform: a process started from the ASGI
        # worker must not inherit its threads, sockets, or SQLite connection.
        self._analysis_executor = ProcessPoolExecutor(
            max_workers=self.feed_settings.analysis_concurrency,
            mp_context=get_context("spawn"),
        )
        self._analysis_backend = "PROCESS_SPAWN"

    async def _finish_analysis(self):
        tasks = list(self._analysis_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        executor = self._analysis_executor
        self._analysis_executor = None
        if executor is not None:
            # Cancelling an asyncio future cannot stop a running CPU job. Join
            # its bounded processes before persistence drains and SQLite closes.
            joining = asyncio.create_task(asyncio.to_thread(
                executor.shutdown, wait=True, cancel_futures=True
            ))
            try:
                await asyncio.shield(joining)
            except asyncio.CancelledError:
                await joining
                raise

    async def _refresh_analysis(self, symbol):
        try:
            while symbol in self.assets and not self.stop.is_set():
                # Limit CPU calculations across the entire promoted universe,
                # rather than creating one active CPU process per symbol.
                async with self._analysis_slots:
                    if symbol not in self.assets or self.stop.is_set():
                        return
                    now = self.clock()
                    revision = self._history_revision(symbol)
                    asset = self.assets[symbol]
                    snapshot = chart_snapshot(asset, self.state, now)
                    depth = self.state.latest_event(symbol, "DEPTH")
                    depth = depth.model_copy(deep=True) if depth else None
                    provenance = {
                        frame: sorted({bar.source for bar in self.state.candles(symbol, frame, now, limit=240)})
                        for frame in snapshot.candles
                    }
                    benchmark = self.assets.get("BTCUSDT")
                    btc = chart_snapshot(benchmark, self.state, now) if benchmark else None
                    eth_asset = self.assets.get("ETHUSDT")
                    eth = chart_snapshot(eth_asset, self.state, now) if eth_asset else None
                    previous_structure = copy.deepcopy(self._war_room.get(symbol))
                    market_context = copy.deepcopy(self._context)
                    fast_state = self.monitor.states.get(asset.asset_id)
                    fast_state = fast_state.model_copy(deep=True) if fast_state else None
                    score_bias = self._analysis_bias(symbol, fast_state)

                    calculate = partial(
                        _calculate_analysis, symbol, snapshot, depth, btc, eth,
                        provenance, now, fast_state, previous_structure, market_context, score_bias,
                    )

                    if self._analysis_executor:
                        analysis = await asyncio.get_running_loop().run_in_executor(
                            self._analysis_executor, calculate
                        )
                    else:
                        # Explicit direct/replay callers have no production
                        # executor. A failed process job never enters this path.
                        analysis = await asyncio.to_thread(calculate)
                    if symbol in self.assets and not self.stop.is_set():
                        self._analysis_errors.pop(symbol, None)
                        self._persist_structure(analysis["war_room"], now)
                        self._analysis[symbol] = analysis
                        self._publish(symbol, self.clock())
                # A finalized candle arriving while this analysis ran requests
                # one latest refresh on this same bounded task, not a lost update
                # or an unbounded second task per incoming tick.
                requested = self._analysis_requested.get(symbol, revision)
                if requested == revision:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._analysis_errors[symbol] = error
            self._analysis_failures += 1
            self._put_state("provider_error", f"{symbol}:analysis", self.clock(),
                            {"status": "FAILED", "error": error,
                             "analysis_backend": self._analysis_backend})

    def _persist_structure(self, report, evaluated_at):
        """Keep restart memory and immutable observed transitions, never retrofit history."""
        symbol = report["symbol"]
        previous = self._war_room.get(symbol, {}).get("timeframes", {})
        records = []
        for frame, state in report.get("timeframes", {}).items():
            before = previous.get(frame, {}).get("history", [])
            known = {self.store._json(item) for item in before}
            for transition in state.get("history", []):
                if self.store._json(transition) in known:
                    continue
                identity = hashlib.sha256(self.store._json(transition).encode()).hexdigest()[:24]
                discovered = datetime.fromisoformat(transition["discovered_at"])
                records.append(("structure_transition", f"{symbol}:{frame}:{identity}",
                                discovered, evaluated_at,
                                {"symbol": symbol, "timeframe": frame, **transition},
                                self.assets[symbol].asset_id))
        def persist():
            if records:
                self.store.append_many(records)
            self.store.put_state("war_room_state", symbol, evaluated_at, report)
        self._enqueue_write(persist)
        self._war_room[symbol] = report

    def _persist_evaluation(self, result):
        now = self.clock()
        # The pure monitor receives a decision-start timestamp for causal
        # replay. Live alerts additionally record when that calculation actually
        # finished, before any asynchronous SQLite/delivery queue wait.
        for index, event in enumerate(result.events):
            completed_at = max(now, event.evaluation_timestamp)
            result.events[index] = event.model_copy(update={
                "evaluation_timestamp": completed_at,
                "alert_created_timestamp": completed_at,
                "detection_latency_ms": (completed_at - event.market_event_timestamp).total_seconds() * 1000,
                "alert_latency_ms": (completed_at - event.market_event_timestamp).total_seconds() * 1000,
                "processing_latency_ms": (completed_at - event.data_received_timestamp).total_seconds() * 1000,
            })
        self._remember_setup_retention(result)
        if self._writer_active:
            # Every FastIgnitionMonitor return branch already detaches its
            # FastState with model_copy(deep=True). This result is owned solely
            # by this queue; another finalized candle cannot repaint it. Avoid
            # a second recursive copy of all360 immutable candle models here.
            context = copy.deepcopy(self._context)
            self._enqueue_write(self._persist_evaluation_now, result, now, context)
            return
        self._persist_evaluation_now(result, now, self._context)

    def _persist_evaluation_now(self, result, now, context):
        state = result.state
        self.store.put_state("fast_state", state.asset.asset_id, now, state)
        for event in result.events:
            self.store.append("fast_event", event.event_id, event.market_event_timestamp,
                              event.alert_created_timestamp, event, event.asset_id)
            self.store.record_context(event.event_id, event.alert_created_timestamp,
                                      {"cycle_macro": context, "regime": event.regime})
            from .event_learning import EventLearningEngine
            EventLearningEngine(self.store).record_fast_event(event, context)
            # Outbox is local. Delivery is a separate explicitly configured worker.
            self.store.enqueue_notification(event.event_id, "configured", event.alert_created_timestamp)

    def _restart_demoted_episode(self, asset, now):
        """Membership restoration is visible immediately, before REST warmup."""
        symbol = asset.exchange_symbol
        prior = self.monitor.states.get(asset.asset_id)
        if prior and prior.stage == FastStage.DEMOTED:
            previous_episode = prior.promoted_at
            previous = prior.model_dump(mode="json", exclude={"candles", "dedupe_keys"})
            current = self.monitor.promote(asset, now)
            self._retention.pop(symbol, None)
            reason = "PERSONAL_WATCHLIST" if symbol in getattr(self, "_personal_symbols", set()) else (
                "TEMPORARY_CHART_INTEREST" if symbol in self._chart_interest_symbols else (
                    "NATIVE_UNIVERSE_DISCOVERY" if symbol in getattr(self, "_native_universe_symbols", set()) else "DYNAMIC_DISCOVERY")
            )
            transition = {"symbol": symbol, "asset_id": asset.asset_id,
                          "previous_stage": "DEMOTED", "stage": "WATCH", "reason": reason,
                          "timestamp": now.isoformat(), "available_at": now.isoformat(),
                          "previous_promoted_at": previous_episode.isoformat() if previous_episode else None,
                          "previous_episode_snapshot": previous,
                          "scope": "NEW_MONITORING_EPISODE; fresh data required, no score bonus"}
            identity = hashlib.sha256(f"{asset.asset_id}|REPROMOTED|{now.isoformat()}".encode()).hexdigest()
            self._enqueue_write(self.store.append, "fast_membership_transition", identity,
                                now, now, transition, asset.asset_id)
            self._put_state("fast_state", asset.asset_id, now, current)

    def _evaluate(self, symbol, now):
        if symbol in getattr(self, "_context_symbols", set()):
            return
        asset = self.assets[symbol]
        self._restart_demoted_episode(asset, now)
        report = self.monitor.evaluate(asset, fast_snapshot(asset, self.state, now), evaluation_at=now,
            regime=self._context.get("cycle", {}).get("regime"), macro_context=self._context.get("macro", {}))
        self._persist_evaluation(report)

    def apply(self, event):
        start = time.perf_counter()
        if event.symbol not in self.assets or not self.state.apply(event):
            return
        now = max(self.clock(), event.processed_timestamp)
        if event.kind == "TRADE":
            self.large_trade_tracker.observe(event)
        if event.kind == "CANDLE" and event.candle.complete:
            self._evaluate(event.symbol, now)
            if event.candle.timeframe in {"1m", "5m", "15m"}:
                self._schedule_repair(event.symbol)
            if event.candle.timeframe in {"1m", "5m", "15m", "1h", "4h", "1D"}:
                self._schedule_analysis(event.symbol)
            if event.candle.timeframe == "1m":
                from .macro_models import MacroMarketObservation
                bar = event.candle
                observation = MacroMarketObservation(asset_id=self.assets[event.symbol].asset_id,
                    market_event_timestamp=bar.close_time, data_received_timestamp=event.received_timestamp,
                    interval_start_timestamp=bar.open_time, resolution_seconds=60, price=bar.close,
                    high=bar.high, low=bar.low, volume=bar.volume, quote_volume=bar.quote_volume,
                    taker_delta=(2 * bar.taker_buy_volume - bar.volume) if bar.taker_buy_volume is not None else None,
                    source=event.source)
                self._enqueue_write(self.store.append, "market_observation", f"{event.symbol}:1m:{bar.open_time.isoformat()}",
                    bar.close_time, event.received_timestamp, observation, observation.asset_id)
        elif event.kind == "TRADE":
            asset = self.assets[event.symbol]
            current = self.monitor.states.get(asset.asset_id)
            boundary = current.trigger_level if current else None
            # Crossing evaluation is cheap and at most one transition per setup;
            # no full candle-copy/scoring work is done for ordinary trades.
            if (event.symbol not in getattr(self, "_context_symbols", set())
                    and current and current.stage in {FastStage.BREAKOUT_PRESSURE, FastStage.BREAKDOWN_PRESSURE}
                    and boundary and ((event.price < boundary.price) if current.bias == "SHORT"
                                      else (event.price > boundary.price)) and event.exchange_timestamp
                    and event.exchange_timestamp <= event.received_timestamp):
                result = self.monitor.observe_trade(asset.asset_id, event.price,
                    market_event_at=event.exchange_timestamp, received_at=event.received_timestamp,
                    evaluation_at=now, source=event.source)
                if result.events:
                    self._persist_evaluation(result)
        monotonic = time.monotonic()
        if monotonic - self._last_publish.get(event.symbol, 0) >= self.feed_settings.chart_publish_seconds:
            self._last_publish[event.symbol] = monotonic
            self._publish(event.symbol, now)
        elapsed = (time.perf_counter() - start) * 1000
        self._latencies.append(elapsed)

    def _publish(self, symbol, now):
        live = self.state.snapshot(symbol, now, include_history=False)
        live["asset_id"] = self.assets[symbol].asset_id
        live["quote_asset"] = self.assets[symbol].quote_asset
        live["benchmark_context"] = symbol in self.feed_settings.benchmark_symbols
        live["context_only"] = symbol in getattr(self, "_context_symbols", set())
        live["large_trade_flow"] = self.large_trade_tracker.snapshot(
            symbol, now, quote_currency=self.assets[symbol].quote_asset
        )
        live["session_context"] = session_context(now)
        live["received_timestamp"] = live.get("last_received_timestamp")
        # Bounded latest chart cache, not an indefinite raw tick archive.
        cached = self._history_cache.setdefault(symbol, {})
        history = {}
        for frame in ("1m", "5m", "15m", "1h", "4h", "1D"):
            version = self.state.history_version(symbol, frame)
            prior = cached.get(frame)
            if prior is None or prior[0] != version:
                bars = self.state.candles(symbol, frame, limit=240)
                prior = (version, [bar.model_dump(mode="json") for bar in bars],
                         max((bar.available_at for bar in bars), default=None))
                cached[frame] = prior
            # Live clocks usually advance. Explicit replay/point-in-time callers
            # may query earlier times: never expose cached future availability.
            rows = prior[1]
            if rows and prior[2] > now:
                rows = [bar.model_dump(mode="json")
                        for bar in self.state.candles(symbol, frame, now, limit=240)]
            history[frame] = rows
        live["closed_candles"] = history
        analysis = self._analysis.get(symbol, {})
        for key in ("indicators", "overlays", "volume_profiles", "weekly_vwap", "fibonacci", "active_fibonacci", "sessions", "scores",
                    "score_levels", "transport_provenance", "war_room", "relative_context", "issues",
                    "market_regime", "cycle_context", "macro_context", "context_available_at", "score_bias"):
            if key in analysis:
                live[key] = analysis[key]
        live["analysis_at"] = analysis.get("as_of")
        live["analysis_quality"] = analysis.get("data_quality", {})
        atr = analysis.get("indicators", {}).get("5m", {}).get("metrics", {}).get("atr14")
        book = order_book_analysis(self.state.latest_event(symbol, "DEPTH"), now, price=live.get("price"),
                                   atr=atr, prior_levels=self._book_levels.get(symbol, []))
        self._book_levels[symbol] = [level for level in book["levels"] if level["status"] != "INVALIDATED"]
        structural = [level for level in analysis.get("liquidity_map", [])
                      if level.get("category") != "OBSERVED_ORDER_BOOK"]
        live["order_book"] = book
        live["liquidity_map"] = structural + book["levels"]
        self._put_state("live_asset_summary", symbol, now, self._live_summary(live))
        self._put_state("live_asset", symbol, now, live)

    @staticmethod
    def _live_summary(live):
        """Bounded overview cache; full native chart evidence stays separate."""
        fields = ("asset_id", "symbol", "as_of", "price", "price_quality", "data_quality",
                  "quote_asset", "price_source", "price_source_timestamp", "price_received_timestamp",
                  "received_timestamp", "last_received_timestamp", "last_exchange_timestamp",
                  "feed_latency_ms", "processing_latency_ms", "context_only", "benchmark_context",
                  "analysis_at", "analysis_quality")
        summary = {key: live.get(key) for key in fields}
        summary["source"] = live.get("price_source")
        summary["cache_source"] = "NATIVE_LIVE_CACHE"
        price = live.get("price")
        candidates = [level for level in live.get("liquidity_map", [])
                      if level.get("status") != "INVALIDATED" and level.get("price") is not None]
        candidates.sort(key=lambda level: abs(level["price"] - price) if price else float("inf"))
        if price:
            above = [level for level in candidates if level["price"] >= price][:4]
            below = [level for level in candidates if level["price"] < price][:4]
            selected = above + below
            identities = {id(level) for level in selected}
            selected += [level for level in candidates if id(level) not in identities][:8 - len(selected)]
            selected.sort(key=lambda level: abs(level["price"] - price))
        else:
            selected = candidates[:8]
        level_fields = ("level_id", "id", "category", "type", "kind", "side", "price", "lower", "upper",
                        "size", "notional", "relative_size", "distance_pct", "discovered_at", "updated_at",
                        "source", "quality", "status", "timeframe", "strength", "scope")
        summary["liquidity_map"] = [{key: level[key] for key in level_fields if key in level}
                                    for level in selected]
        summary["liquidity_map_scope"] = "NEAREST_MAX8_OVERVIEW; full map on selected chart"
        return summary

    @staticmethod
    def _liquidity(live):
        depth = live.get("depth")
        if not depth:
            return []
        price = live.get("price")
        levels = []
        for side, rows in (("BUY_SIDE", depth.get("asks", [])), ("SELL_SIDE", depth.get("bids", []))):
            for row in rows:
                levels.append({"side": side, "type": "OBSERVED_ORDER_BOOK", "price": row["price"],
                    "lower": row["price"], "upper": row["price"], "size": row["quantity"],
                    "notional": row["quantity"] * row["price"], "status": "RESTING",
                    "distance_pct": (row["price"] / price - 1) * 100 if price else None,
                    "source": depth["source"], "updated_at": depth["received_timestamp"],
                    "quality": live.get("data_quality", {}).get("order_book", "UNAVAILABLE"),
                    "scope": "TOP_20_SNAPSHOT; observed depth, not inferred stops"})
        return levels

    async def _housekeeping(self, http):
        while not self.stop.is_set():
            try:
                # After a reconnect, every existing subscription needs a
                # continuity check; warmup covers only newly promoted assets.
                epoch = self.stream.health.stream_epoch
                reconnected = self._repair_epoch is not None and epoch != self._repair_epoch
                self._repair_epoch = epoch
                for symbol in list(self.assets):
                    state = self.monitor.states.get(self.assets[symbol].asset_id)
                    if reconnected or (state and state.stale):
                        self._schedule_repair(symbol)
                self._context = await asyncio.to_thread(context_at, self.store, self.clock())
                prior_context = set(getattr(self, "_context_symbols", set()))
                membership = self._subscriptions(await asyncio.to_thread(self.membership))
                for symbol in self._context_symbols - prior_context:
                    if symbol in self.assets:
                        old = self.monitor.states.get(self.assets[symbol].asset_id)
                        if old:
                            old.stage = FastStage.DEMOTED
                            old.stale = True
                            old.demoted_at = self.clock()
                            self._put_state("fast_state", old.asset.asset_id, self.clock(), old)
                for symbol in prior_context - self._context_symbols:
                    if symbol in membership and symbol in self.assets:
                        self._evaluate(symbol, self.clock())
                added = [symbol for symbol in membership if symbol not in self.assets]
                if set(membership) != set(self.assets):
                    for removed in set(self.assets) - set(membership):
                        self.state.remove_symbol(removed)
                        self.large_trade_tracker.remove_symbol(removed)
                        self._history_cache.pop(removed, None)
                        self._last_publish.pop(removed, None)
                        self._book_levels.pop(removed, None)
                        self._analysis.pop(removed, None)
                        self._repair_status.pop(removed, None)
                        self._warmup_completed.discard(removed)
                        self._repair_attempts.pop(removed, None)
                        repair_task = self._repair_tasks.pop(removed, None)
                        if repair_task and not repair_task.done():
                            repair_task.cancel()
                        old = self.monitor.states.get(self.assets[removed].asset_id)
                        if old:
                            old.stage = FastStage.DEMOTED
                            old.demoted_at = self.clock()
                            self._put_state("fast_state", old.asset.asset_id, self.clock(), old)
                    self.assets = membership
                    self.stream.set_symbols(list(self.assets))
                    if added:
                        self._start_warmup(http, added)
                self._put_state("feed_health", "binance", self.clock(), self.stream.health)
                self._put_state("live_metrics", "worker", self.clock(), self.metrics())
                self._put_state("live_runtime", "worker", self.clock(),
                                     {"running": True, "updated_at": self.clock().isoformat()})
            except Exception as exc:
                self._put_state("provider_error", "housekeeping", self.clock(),
                                     {"status": "FAILED", "error": str(exc)})
            try:
                await asyncio.wait_for(self.stop.wait(), 2)
            except TimeoutError:
                pass

    def metrics(self):
        ordered = sorted(self._latencies)
        pending_times = [job[2] for job in self._critical_writes]
        if self._writer_inflight_since is not None and self._writer_inflight_critical:
            pending_times.append(self._writer_inflight_since)
        pending_age = (time.monotonic() - min(pending_times)) if pending_times else None
        cache_times = [job[2] for job in self._latest_writes.values()]
        cache_age = (time.monotonic() - min(cache_times)) if cache_times else None
        return {"events": self.state.events_accepted, "rejected": self.state.events_rejected,
                "sequence_gaps": self.state.sequence_gaps, "window_samples": len(ordered),
                "processing_p50_ms": ordered[int((len(ordered)-1)*.5)] if ordered else None,
                "processing_p95_ms": ordered[int((len(ordered)-1)*.95)] if ordered else None,
                "processing_p99_ms": ordered[int((len(ordered)-1)*.99)] if ordered else None,
                "includes_network_latency": False,
                "includes_persistence_latency": self._latency_includes_persistence,
                "includes_publish_preparation": True, "mode": "OBSERVATION",
                "analysis_concurrency": self.feed_settings.analysis_concurrency,
                "cooperative_ingestion_yields": self._cooperative_yields,
                "ingestion_yield_events": self.feed_settings.ingestion_yield_events,
                "ingestion_yield_seconds": self.feed_settings.ingestion_yield_seconds,
                "analysis_pending": sum(not task.done() for task in self._analysis_tasks.values()),
                "analysis_worker": {
                    "backend": self._analysis_backend,
                    "status": "FAILED" if self._analysis_errors else "AVAILABLE",
                    "failures": self._analysis_failures,
                    "errors": dict(self._analysis_errors),
                },
                "candle_repair": {
                    "concurrency": self.feed_settings.candle_repair_concurrency,
                    "max_bars_per_request": self.feed_settings.candle_repair_max_bars,
                    "pending": sum(not task.done() for task in self._repair_tasks.values()),
                    "states": copy.deepcopy(self._repair_status),
                    "scope": "BOUNDED_NATIVE_SPOT_HISTORY_ONLY; no retrospective trigger alerts",
                },
                "active_setup_retention": {
                    "ttl_seconds": self.feed_settings.active_setup_retention_seconds,
                    "retained_symbols": sorted(self._retained_symbols),
                    "scope": "MONITORING_ONLY; current eligibility and fresh completed candles required",
                },
                "chart_interest": {
                    "requested_symbols": sorted(self._chart_interest_symbols),
                    "subscribed_symbols": sorted(self._chart_interest_symbols & set(self.assets)),
                    "unavailable_due_capacity": sorted(self._chart_interest_symbols - set(self.assets)),
                    "invalid_requests": self._chart_interest_issues,
                    "maximum_ttl_seconds": 900,
                    "scope": "TEMPORARY_NATIVE_CHART_SUBSCRIPTION; no watchlist entry or score bonus",
                },
                "benchmark_context": {
                    "requested": list(self.feed_settings.benchmark_symbols),
                    "subscribed": [symbol for symbol in self.feed_settings.benchmark_symbols if symbol in self.assets],
                    "context_only": sorted(getattr(self, "_context_symbols", set())),
                    "unavailable_due_capacity": [symbol for symbol in self.feed_settings.benchmark_symbols
                                                 if symbol not in self.assets],
                    "dynamic_capacity_exclusions": getattr(self, "_capacity_exclusions", []),
                },
                "persistence": {
                    "background_writer": self._writer_active,
                    "critical_pending": len(self._critical_writes),
                    "latest_cache_pending": len(self._latest_writes),
                    "critical_capacity": self.feed_settings.persistence_queue_size,
                    "capacity_semantics": "INGESTION_STOP_THRESHOLD; accepted candle observation tail retained",
                    "critical_high_water": self._writer_high_water,
                    "oldest_critical_age_seconds": pending_age,
                    "oldest_cache_age_seconds": cache_age,
                    "writer_busy": self._writer_inflight_since is not None,
                    "writes_completed": self._writer_completed,
                    "cache_updates_coalesced": self._writer_coalesced,
                    "busy_retries": self._writer_retries,
                    "last_write_ms": self._writer_last_ms,
                    "overloaded": self._writer_overloaded,
                    "failed": self._writer_failed,
                    "last_error": self._writer_error,
                    "status": "FAILED" if self._writer_failed else "DEGRADED"
                              if self._writer_error or self._writer_overloaded else "AVAILABLE",
                }}

    async def run(self, *, seconds: float | None = None):
        self._start_writer()
        try:
            self._start_analysis()
            async with AsyncHTTPClient(self.settings) as http:
                self._repair_provider = BinanceSpotProvider(http, self.settings)
                candidates = await asyncio.to_thread(self.membership)
                if not candidates:
                    raise ValueError("No fast assets; add personal watchlist or run discovery")
                self.assets = self._subscriptions(candidates)
                self._context = await asyncio.to_thread(context_at, self.store, self.clock())
                self.stream = self.stream or BinancePublicStream(list(self.assets), self.feed_settings)
                warmup_task = self._start_warmup(http, list(self.assets))
                housekeeping = asyncio.create_task(self._housekeeping(http))
                timer = asyncio.create_task(self._stop_after(seconds)) if seconds else None
                try:
                    await self._consume_events()
                finally:
                    self.stop.set()
                    housekeeping.cancel()
                    warmup_task.cancel()
                    pending_warmups = list(self._warmup_tasks)
                    pending_repairs = list(self._repair_tasks.values())
                    for task in pending_repairs:
                        task.cancel()
                    for task in pending_warmups:
                        task.cancel()
                    if timer:
                        timer.cancel()
                    analysis_tasks = list(self._analysis_tasks.values())
                    for task in analysis_tasks:
                        task.cancel()
                    await asyncio.gather(housekeeping, warmup_task, *pending_warmups, *analysis_tasks,
                                         *pending_repairs,
                                         *([timer] if timer else []), return_exceptions=True)
                    self._put_state("feed_health", "binance", self.clock(), self.stream.health)
        finally:
            self.stop.set()
            try:
                await self._finish_analysis()
            finally:
                self._put_state("live_runtime", "worker", self.clock(),
                                {"running": False, "updated_at": self.clock().isoformat(),
                                 "analysis_worker": self.metrics()["analysis_worker"],
                                 "persistence": self.metrics()["persistence"]})
                try:
                    await self._finish_writer()
                    self.store.put_state("live_runtime", "worker", self.clock(),
                                         {"running": False, "updated_at": self.clock().isoformat(),
                                          "analysis_worker": self.metrics()["analysis_worker"],
                                          "persistence": self.metrics()["persistence"]})
                finally:
                    self.store.close()
        return self.metrics()

    async def _consume_events(self):
        """Buffered WebSocket recv need not suspend; enforce fair task progress.

        Yielding schedules API requests, heartbeats and writer completions. It
        does not drop/coalesce input events or finalized candles. Both batch and
        elapsed budgets are explicit because native trade rates vary widely.
        """
        batch = 0
        yielded_at = time.monotonic()
        async for event in self.stream.events(self.stop):
            if self.stop.is_set():
                break
            try:
                self.apply(event)
            except Exception as exc:
                self._put_state("provider_error", event.symbol, self.clock(),
                                {"status": "FAILED", "error": str(exc)})
            batch += 1
            if (batch >= self.feed_settings.ingestion_yield_events
                    or time.monotonic() - yielded_at >= self.feed_settings.ingestion_yield_seconds):
                self._cooperative_yields += 1
                await asyncio.sleep(0)
                batch = 0
                yielded_at = time.monotonic()

    async def _stop_after(self, seconds):
        await asyncio.sleep(seconds)
        self.stop.set()


async def live_smoke(symbols: list[str], *, seconds: float = 20, settings=None) -> dict:
    """Real public connection attempt. Never falls back to synthetic data."""
    settings = settings or LiveFeedSettings(connect_timeout_seconds=5, reconnect_max_seconds=5)
    stream = BinancePublicStream(symbols, settings)
    state = LiveFeedState(settings)
    stop = asyncio.Event()
    samples = []
    async def consume():
        async for event in stream.events(stop):
            state.apply(event)
            if len(samples) < 12:
                samples.append(event.model_dump(mode="json"))
    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(asyncio.shield(task), seconds)
    except TimeoutError:
        pass
    finally:
        stop.set()
        stream.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return {"test": "REAL_PUBLIC_WEBSOCKET", "success": state.events_accepted > 0,
            "duration_seconds": seconds, "health": stream.health.model_dump(mode="json"),
            "state": state.health(utcnow()), "samples": samples, "synthetic_fallback": False}
