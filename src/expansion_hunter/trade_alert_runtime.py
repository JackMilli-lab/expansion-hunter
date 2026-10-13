"""Optional light-stream Trade Alert service; exchange access remains public/read-only.

Connections and REST warmup are independent of the legacy scanner. Completed
bars are archived; open updates only refresh prices and bounded evaluations.
No throughput or 500-pair coverage is inferred from configured capacity.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
from collections import Counter, deque
from datetime import datetime, timedelta

from .config import Settings
from .intelligence_store import IntelligenceStore
from .live_feed import LiveFeedState
from .live_models import LiveFeedSettings, normalize_symbols
from .models import TIMEFRAMES, Asset, Candle, aware, utcnow
from .pre_ignition import PreIgnitionEngine, PreIgnitionSettings, causal_fast_features
from .pre_move import PreMoveRanker, PreMoveSettings
from .providers.binance import BinanceSpotProvider
from .providers.binance_ws import BinancePublicStream
from .providers.http import AsyncHTTPClient
from .trade_alert import TradeAlertEngine
from .trade_alert_models import TradeAlertRecord, TradeAlertSettings

FRAMES = ("1m", "5m", "15m", "1h", "4h", "1D")


class TradeAlertWorker:
    def __init__(self, settings: Settings, config: TradeAlertSettings | None = None, *,
                 store=None, provider=None, stream_factory=None, engine=None, ranker=None,
                 pre_ignition_engine=None, clock=utcnow):
        self.settings, self.config = settings, config or TradeAlertSettings()
        self.clock, self.provider = clock, provider
        self.store = store or IntelligenceStore(settings.db_path)
        self._owns_store = store is None
        self.engine = engine or TradeAlertEngine(self.config)
        prior = self.store.get_state("trade_alert_engine", "default")
        if prior is None:
            # Preserve checkpoints written before the notification integration
            # standardized the singleton state key; never delete that evidence.
            prior = self.store.get_state("trade_alert_engine", "active")
        if prior is not None:
            self.engine.restore_state(prior)
        self._episode_state = self.engine.export_state()
        self.ranker = ranker or PreMoveRanker(PreMoveSettings.model_validate(getattr(self.config, "pre_move", {})))
        rank_state = self.store.get_state("trade_alert_pre_move", "default")
        if rank_state is not None:
            self.ranker.restore_state(rank_state)
        self._pre_move_rows: list[dict] = []
        self.pre_ignition = pre_ignition_engine or PreIgnitionEngine(
            PreIgnitionSettings.model_validate(self.config.pre_ignition))
        ignition_state = self.store.get_state("trade_alert_pre_ignition", "default")
        if ignition_state is not None:
            self.pre_ignition.restore_state(ignition_state)
        saved_rows = self.store.get_state("trade_alert_pre_ignition_rows", "default") or {}
        self._pre_ignition_rows = [dict(row, status="STALE", reason="Restart requires fresh market evidence")
                                  for row in saved_rows.get("rows", [])]
        self._pre_ignition_alerts: deque[dict] = deque(maxlen=100)
        self._fast_features_cache: dict[str, tuple[tuple, dict]] = {}
        alert_state = self.store.get_state("trade_alert_pre_ignition_alerts", "default") or {}
        self._ready_alert_seen: dict[str, str] = dict(alert_state.get("seen", {}))
        self._ready_alert_cooldowns: dict[str, str] = dict(alert_state.get("cooldowns", {}))
        self._trade_alert_boundaries: dict[str, str] = dict(alert_state.get("boundaries", {}))
        self._ignition_checkpoint_minute = None
        self._rank_checkpoint_minute = None
        state_alerts = self.store.get_state("trade_alert_pre_move_alerts", "default") or {}
        self._state_alert_seen: dict[str, str] = dict(state_alerts.get("seen", {}))
        self._state_alert_cooldowns: dict[str, str] = dict(state_alerts.get("cooldowns", {}))
        self._state_alerts: deque[dict] = deque(maxlen=100)
        self.stream_factory = stream_factory or BinancePublicStream
        self.stop = asyncio.Event()
        self.assets: dict[str, Asset] = {}
        self.tick_sizes: dict[str, float | None] = {}
        self._states: list[LiveFeedState] = []
        self._streams = []
        self._shard: dict[str, int] = {}
        self._cache: dict[tuple[str, str], tuple[int, list[Candle]]] = {}
        self._prices: dict[str, tuple[float, datetime]] = {}
        self._price_meta: dict[str, dict] = {}
        self._issues: dict[str, list[str]] = {}
        self._warming: set[str] = set()
        self._warmup_tasks: set[asyncio.Task] = set()
        self._dirty: set[str] = set()
        self._pending_open: set[str] = set()
        self._evaluations = asyncio.Queue(maxsize=self.config.queue_size)
        self._writes = asyncio.Queue(maxsize=self.config.queue_size)
        self._events: deque[dict] = deque(maxlen=100)
        self._latest_records = {row["symbol"]: row for row in self.store.states("trade_alert_asset")}
        for latest in self._latest_records.values():
            if latest.get("phase") == "TRADE_ALERT":
                boundary = self._trade_alert_boundary(latest)
                if boundary:
                    self._trade_alert_boundaries.setdefault(boundary, latest["detected_at"])
        self._observations: dict[str, dict] = {}
        self._rows: dict[str, dict] = {}
        self._counts: Counter = Counter()
        self._exclusions: Counter = Counter()
        self._verified_assets = 0
        self._status = "STARTING"
        self._error: str | None = None
        self._running = False
        self._rest_cache: dict[tuple[str, str], dict] = {}

    async def bootstrap(self):
        exchange, ticker = await asyncio.gather(
            self.provider._get("/api/v3/exchangeInfo", weight=20),
            self.provider._get("/api/v3/ticker/24hr", weight=80),
        )
        definitions = exchange.data.get("symbols") if isinstance(exchange.data, dict) else None
        if not isinstance(definitions, list) or not isinstance(ticker.data, list):
            raise ValueError("UNAVAILABLE: invalid native universe response")
        verified, self._exclusions = {}, Counter()
        for row in definitions:
            try:
                if not isinstance(row, dict) or row.get("status") != "TRADING":
                    raise ValueError("NOT_TRADING")
                if row.get("quoteAsset") != "USDT" or row.get("isSpotTradingAllowed") is not True:
                    raise ValueError("NOT_VERIFIED_USDT_SPOT")
                if row.get("baseAsset") in self.config.excluded_bases:
                    raise ValueError("STABLECOIN_BASE")
                symbol = normalize_symbols([row["symbol"]])[0]
                if symbol != row["baseAsset"] + "USDT":
                    raise ValueError("IDENTITY_MISMATCH")
                verified[symbol] = row
            except (KeyError, TypeError, ValueError) as exc:
                self._exclusions[str(exc)] += 1
        self._verified_assets = len(verified)
        eligible = []
        receipt = aware(ticker.retrieved_at)
        for row in ticker.data:
            if not isinstance(row, dict) or row.get("symbol") not in verified:
                continue
            try:
                volume = float(row["quoteVolume"])
                price = float(row["lastPrice"])
                source = datetime.fromtimestamp(float(row["closeTime"]) / 1000, receipt.tzinfo)
                if not math.isfinite(volume) or volume < 0 or not math.isfinite(price) or price <= 0 or source > receipt:
                    raise ValueError("MALFORMED_NATIVE_TICKER")
                if volume < self.config.min_quote_volume:
                    self._exclusions["INSUFFICIENT_QUOTE_VOLUME"] += 1
                    continue
                eligible.append((volume, row["symbol"]))
            except (KeyError, TypeError, ValueError, OverflowError):
                self._exclusions["MALFORMED_NATIVE_TICKER"] += 1
        selected = sorted(set(eligible), key=lambda item: (-item[0], item[1]))[:self.config.max_assets]
        if not selected:
            raise ValueError("UNAVAILABLE: no eligible liquid non-stablecoin USDT spot pairs")
        self.assets.clear()
        self._states.clear()
        self._streams.clear()
        self._shard.clear()
        for _, symbol in selected:
            definition = verified[symbol]
            base = definition["baseAsset"]
            self.assets[symbol] = Asset(asset_id=f"binance:spot:{symbol}", symbol=base,
                                        name=base, exchange_symbol=symbol, quote_asset="USDT")
            sizes = [item.get("tickSize") for item in definition.get("filters", [])
                     if isinstance(item, dict) and item.get("filterType") == "PRICE_FILTER"]
            try:
                size = float(sizes[0])
                self.tick_sizes[symbol] = size if math.isfinite(size) and size > 0 else None
            except (IndexError, TypeError, ValueError):
                self.tick_sizes[symbol] = None
        streams_per_symbol = len(FRAMES) + {"CANDLES": 0, "LIGHT": 2, "FULL": 3}[self.config.stream_mode]
        width = min(self.config.symbols_per_connection, 150, 1024 // streams_per_symbol)
        symbols = list(self.assets)
        for index in range(0, len(symbols), width):
            group = symbols[index:index + width]
            feed = LiveFeedSettings(timeframes=list(FRAMES), max_symbols=width,
                                    candle_capacity=max(32, self.config.history_bars), stream_mode=self.config.stream_mode)
            state = LiveFeedState(feed)
            stream = self.stream_factory(group, feed)
            self._states.append(state)
            self._streams.append(stream)
            self._shard.update({symbol: len(self._states) - 1 for symbol in group})
        self._counts.update(eligible=len(eligible), selected=len(self.assets), excluded=sum(self._exclusions.values()))
        self._status = "WARMING_UP"
        self._error = None
        saved = await asyncio.to_thread(self.store.states, "trade_alert_history")
        self._rest_cache = {(row["symbol"], row["timeframe"]): row for row in saved
                            if row.get("symbol") in self.assets and row.get("timeframe") in FRAMES}

    def _state(self, symbol):
        return self._states[self._shard[symbol]]

    def _history(self, symbol, frame, cutoff):
        state = self._state(symbol)
        version = state.history_version(symbol, frame)
        cached = self._cache.get((symbol, frame))
        if cached is None or cached[0] != version:
            cached = (version, state.candles(symbol, frame, limit=self.config.history_bars))
            self._cache[(symbol, frame)] = cached
        bars = [bar for bar in cached[1] if bar.close_time <= cutoff and bar.available_at <= cutoff]
        for index in range(len(bars) - 1, 0, -1):
            if bars[index - 1].close_time != bars[index].open_time:
                return bars[index:]
        return bars

    def _native_fast_features(self, symbol, history, cutoff):
        """Candle baselines change only with an available completed-bar revision.

        Native current flow is supplied separately in ``market_snapshot``. It is
        never cached as if an old flow window were a fresh measurement.
        """
        signature = tuple((frame, self._state(symbol).history_version(symbol, frame),
            len(history.get(frame, [])),
            history[frame][-1].close_time if history.get(frame) else None,
            history[frame][-1].available_at if history.get(frame) else None)
            for frame in ("1m", "5m"))
        cached = self._fast_features_cache.get(symbol)
        if cached is None or cached[0] != signature:
            cached = (signature, causal_fast_features(history, cutoff))
            self._fast_features_cache[symbol] = cached
            self._counts["fast_feature_revisions"] += 1
        return copy.deepcopy(cached[1])

    @staticmethod
    def _trade_alert_boundary(value):
        """One early TRADE_ALERT for the same previously discovered boundary."""
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        level = value.get("trigger_level", value.get("trigger"))
        discovered = value.get("trigger_discovered_at")
        if level is None or not discovered:
            return None
        return hashlib.sha256(json.dumps([value.get("asset_id"), value.get("bias"),
            value.get("setup_type", "CONTINUATION"), level, discovered],
            separators=(",", ":")).encode()).hexdigest()[:32]

    def _register_record(self, record):
        value = record.model_dump(mode="json")
        self._events.append(value)
        self._latest_records[record.symbol] = value
        self._counts[record.phase.lower()] += 1
        if record.phase == "TRADE_ALERT":
            boundary = self._trade_alert_boundary(record)
            if boundary:
                self._trade_alert_boundaries[boundary] = record.detected_at.isoformat()

    def _enqueue(self, queue, payload, label):
        try:
            queue.put_nowait(payload)
            return True
        except asyncio.QueueFull:
            self._counts[label + "_dropped"] += 1
            self._error = label + " queue full; coverage degraded"
            return False

    async def _warmup(self, symbols=None):
        semaphore = asyncio.Semaphore(self.config.warmup_concurrency)

        async def seed(symbol):
            self._warming.add(symbol)
            try:
                async with semaphore:
                    for frame in FRAMES:
                        if self.stop.is_set():
                            return
                        try:
                            state, now = self._state(symbol), aware(self.clock())
                            saved = self._rest_cache.pop((symbol, frame), None)
                            if saved:
                                state.seed_candles(symbol, frame, [Candle.model_validate(b) for b in saved["candles"]], now)
                            known = state.candles(symbol, frame, now, limit=self.config.history_bars)
                            endpoint = math.floor(now.timestamp() / TIMEFRAMES[frame]) * TIMEFRAMES[frame]
                            limit = self.config.history_bars
                            if len(known) >= limit:
                                missing = max(0, math.ceil((endpoint - known[-1].close_time.timestamp()) / TIMEFRAMES[frame]))
                                contiguous = all(left.close_time == right.open_time
                                                 for left, right in zip(known, known[1:], strict=False))
                                if contiguous:
                                    limit = min(limit, missing + 1) if missing else 0
                            if limit:
                                bars = await asyncio.wait_for(self.provider.candles(self.assets[symbol], frame, limit, now), 20)
                                state.seed_candles(symbol, frame, bars, aware(self.clock()))
                            history = state.candles(symbol, frame, aware(self.clock()), limit=self.config.history_bars)
                            self._enqueue(self._writes, ("state", "trade_alert_history", f"{symbol}:{frame}",
                                {"symbol": symbol, "timeframe": frame,
                                 "candles": [bar.model_dump(mode="json") for bar in history]}), "persistence")
                            self._issues[symbol] = [issue for issue in self._issues.get(symbol, [])
                                                    if not issue.startswith(frame + ":")]
                        except Exception as exc:
                            self._issues.setdefault(symbol, []).append(f"{frame}: {type(exc).__name__}: {exc}")
                    self._dirty.add(symbol)
            finally:
                self._warming.discard(symbol)
        await asyncio.gather(*(seed(symbol) for symbol in (symbols or self.assets)))

    async def _consume(self, index):
        stream, state = self._streams[index], self._states[index]
        try:
            async for event in stream.events(self.stop):
                if event.symbol not in self.assets or not state.apply(event):
                    continue
                self._counts["events_received"] += 1
                self._counts[event.kind.lower() + "_events"] += 1
                source_timestamp = event.exchange_timestamp or event.received_timestamp
                age = (aware(self.clock()) - source_timestamp).total_seconds()
                stale = not 0 <= age <= self.config.alert_max_age_seconds
                if not stale:
                    price = event.price
                    price_type = "NATIVE_TRADE"
                    if event.candle is not None:
                        price, price_type = event.candle.close, "NATIVE_CANDLE_CLOSE"
                    elif event.kind == "BOOK_TICKER" and event.bid and event.ask:
                        price = (event.bid.price + event.ask.price) / 2
                        price_type = "ESTIMATED_BEST_BID_ASK_MIDPOINT"
                    if price is not None:
                        self._prices[event.symbol] = (price, event.received_timestamp)
                        self._price_meta[event.symbol] = {"type": price_type, "source": event.source,
                            "exchange_timestamp": event.exchange_timestamp.isoformat() if event.exchange_timestamp else None}
                        self._dirty.add(event.symbol)
                else:
                    self._counts["stale_messages"] += 1
                if event.candle and event.candle.complete:
                    self._enqueue(self._writes, ("candle", event), "persistence")
                    if not stale and event.candle.timeframe in {"1m", "5m"}:
                        self._enqueue(self._evaluations, (event.symbol, event.received_timestamp,
                                                          event.candle.close, False), "evaluation")
                if self._counts["events_received"] % 64 == 0:
                    await asyncio.sleep(0)
            if not self.stop.is_set():
                self._counts["provider_failures"] += 1
                self._error = f"stream {index} terminated unexpectedly"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._counts["provider_failures"] += 1
            self._error = f"stream {index}: {type(exc).__name__}: {exc}"

    async def _evaluate_loop(self):
        while not self.stop.is_set() or not self._evaluations.empty():
            try:
                symbol, cutoff, price, opened = await asyncio.wait_for(self._evaluations.get(), .5)
            except TimeoutError:
                continue
            try:
                now = aware(self.clock())
                if (now - cutoff).total_seconds() > self.config.alert_max_age_seconds:
                    self._counts["stale_evaluations"] += 1
                    continue
                history = {frame: self._history(symbol, frame, cutoff) for frame in FRAMES}
                btc = self._history("BTCUSDT", "5m", cutoff) if "BTCUSDT" in self.assets else []
                market_snapshot = self._state(symbol).snapshot(symbol, cutoff, include_history=False)
                market_snapshot["order_flow_windows"] = {}
                for frame, seconds in (("1m", 60), ("5m", 300)):
                    try:
                        market_snapshot["order_flow_windows"][frame] = self._state(symbol).flow_window(symbol, cutoff, seconds)
                    except ValueError:
                        market_snapshot["order_flow_windows"][frame] = {
                            "quality": "UNAVAILABLE", "reason": "Newer live flow exists than this decision receipt"}
                records = await asyncio.to_thread(self.engine.evaluate, self.assets[symbol], history, now,
                    cutoff, price, btc, self.tick_sizes[symbol], market_snapshot=market_snapshot)
                self._counts["evaluations"] += 1
                observation = copy.deepcopy(
                    getattr(self.engine, "observations", {}).get(self.assets[symbol].asset_id, {}))
                # A missing engine observation stays missing. The feature cache
                # cannot manufacture an evaluated setup or missing market data.
                if observation and self.pre_ignition.settings.enabled:
                    observation["fast_features"] = self._native_fast_features(symbol, history, cutoff)
                    observation["market_snapshot"] = copy.deepcopy(market_snapshot)
                    observation["received_at"] = cutoff.isoformat()
                    observation["market_event_timestamp"] = market_snapshot.get("price_source_timestamp")
                self._observations[self.assets[symbol].asset_id] = observation
                if not opened and history["1m"]:
                    candle = history["1m"][-1]
                    observation = copy.deepcopy(self._observations[self.assets[symbol].asset_id])
                    available = bool(observation)
                    observation.update(asset_id=self.assets[symbol].asset_id, symbol=symbol,
                        event_timestamp=candle.close_time.isoformat(), received_at=cutoff.isoformat(),
                        evaluated_at=now.isoformat(), source_candle=candle.model_dump(mode="json"),
                        observation_status="AVAILABLE" if available else "UNAVAILABLE")
                    if not available:
                        observation["data_quality"] = {"evaluation": "UNAVAILABLE"}
                    identity = f"{self.assets[symbol].asset_id}:{candle.close_time.isoformat()}"
                    self._enqueue(self._writes, ("observation", identity, candle.close_time,
                        now, observation, self.assets[symbol].asset_id), "persistence")
                accepted = []
                for record in records:
                    boundary = self._trade_alert_boundary(record)
                    if (self.pre_ignition.settings.enabled and record.phase == "TRADE_ALERT"
                            and boundary in self._trade_alert_boundaries):
                        self._counts["duplicate_early_alerts_suppressed"] += 1
                        continue
                    accepted.append(record)
                    self._register_record(record)
                records = accepted
                if records or not opened:
                    self._episode_state = self.engine.export_state()
                    self._enqueue(self._writes, ("evaluation", records, self._episode_state), "persistence")
            except Exception as exc:
                self._issues.setdefault(symbol, []).append(f"evaluation: {type(exc).__name__}: {exc}")
            finally:
                if opened:
                    self._pending_open.discard(symbol)
                self._evaluations.task_done()

    def _write(self, packet):
        kind = packet[0]
        now = aware(self.clock())
        if kind == "state":
            self.store.put_state(packet[1], packet[2], now, packet[3])
        elif kind == "archive":
            self.store.append(*packet[1:])
        elif kind == "observation":
            identity, event_at, available_at, payload, asset_id = packet[1:]
            with self.store._archive_lock:
                old = self.store._db.execute(
                    "SELECT payload FROM intelligence_objects WHERE kind='trade_alert_observation' AND object_id=?",
                    (identity,),
                ).fetchone()
                if old:
                    previous = Candle.model_validate(json.loads(old[0])["source_candle"])
                    current = Candle.model_validate(payload["source_candle"])
                    if previous.model_dump(exclude={"available_at"}) == current.model_dump(exclude={"available_at"}):
                        # Reconnects and a simultaneous 5m close may re-evaluate
                        # this same 1m bar. Keep the first actual decision.
                        return
                    raise ValueError("immutable observation source candle changed")
            self.store.append("trade_alert_observation", identity, event_at, available_at, payload, asset_id)
        elif kind == "candle":
            event = packet[1]
            candle = event.candle.as_closed(event.received_timestamp, event.source)
            identity = f"{event.symbol}:{event.candle.timeframe}:{candle.open_time.isoformat()}"
            # A reconnect may repeat an already archived native final candle.
            # Preserve its first real receipt; never rewrite a corrected price.
            with self.store._archive_lock:
                old = self.store._db.execute(
                    "SELECT payload FROM intelligence_objects WHERE kind='trade_alert_candle' AND object_id=?",
                    (identity,),
                ).fetchone()
                if old:
                    previous = Candle.model_validate(json.loads(old[0])["candle"])
                    if previous.model_dump(exclude={"available_at"}) == candle.model_dump(exclude={"available_at"}):
                        return
            self.store.append("trade_alert_candle", identity,
                candle.close_time, event.received_timestamp,
                {"symbol": event.symbol, "timeframe": event.candle.timeframe, "candle": candle.model_dump(mode="json")},
                self.assets[event.symbol].asset_id)
        elif kind == "ignition":
            record, checkpoint, alert_state = packet[1:]
            # A crash can occur after immutable append and before checkpoint.
            # Reuse that first observation rather than overwrite its features.
            existing = self.store.object("trade_alert", record.event_id, now)
            if existing is None:
                self.store.append("trade_alert", record.event_id, record.event_timestamp,
                                  record.detected_at, record, record.asset_id)
                self.store.put_state("trade_alert_asset", record.symbol, record.detected_at, record)
            if os.getenv("COCKPIT_TELEGRAM_BOT_TOKEN") and os.getenv("COCKPIT_TELEGRAM_CHAT_ID"):
                self.store.enqueue_notification(record.event_id, "telegram", record.detected_at)
            self.store.put_state("trade_alert_pre_ignition", "default", now, checkpoint)
            self.store.put_state("trade_alert_pre_ignition_alerts", "default", now, alert_state)
        else:
            for record in packet[1]:
                self.store.append("trade_alert", record.event_id, record.event_timestamp, record.detected_at,
                                  record, record.asset_id)
                self.store.put_state("trade_alert_asset", record.symbol, record.detected_at, record)
                if os.getenv("COCKPIT_TELEGRAM_BOT_TOKEN") and os.getenv("COCKPIT_TELEGRAM_CHAT_ID"):
                    self.store.enqueue_notification(record.event_id, "telegram", record.detected_at)
            self.store.put_state("trade_alert_engine", "default", now, packet[2])
            self.store.put_state("trade_alert_pre_ignition_alerts", "default", now,
                                 self._ready_alert_checkpoint())

    async def _writer(self):
        while True:
            packet = await self._writes.get()
            try:
                if packet is None:
                    return
                await asyncio.to_thread(self._write, packet)
            except Exception as exc:
                self._counts["persistence_failed"] += 1
                self._error = f"persistence: {type(exc).__name__}: {exc}"
            finally:
                self._writes.task_done()

    def _emit_pre_move_state_alert(self, row, now):
        """Notify actual pre-trigger transitions without changing native episodes."""
        if not self.config.legacy_pre_move_alerts:
            return None
        stage = row.get("state")
        symbol, bias = row.get("symbol"), row.get("bias")
        if (stage not in {"PRE_IGNITION", "LOADED", "TRIGGER_IMMINENT"}
                or not row.get("state_changed") or row.get("status") != "AVAILABLE"
                or symbol not in self.assets or bias not in {"LONG", "SHORT"}):
            return None
        now = aware(now)
        asset_id = self.assets[symbol].asset_id
        observation = self._observations.get(asset_id, {})
        current = self._prices.get(symbol)
        evidence = self._price_meta.get(symbol)
        if not current or not evidence:
            return None
        price, received = current
        quote_max_age = min(self.config.alert_max_age_seconds, self._state(symbol).settings.stale_after_seconds)
        source_time = (aware(datetime.fromisoformat(evidence["exchange_timestamp"]))
                       if evidence.get("exchange_timestamp") else received)
        if (not 0 <= (now-received).total_seconds() <= quote_max_age
                or not 0 <= (now-source_time).total_seconds() <= quote_max_age):
            return None
        try:
            observed = aware(datetime.fromisoformat(observation["as_of"]))
            discovered = aware(datetime.fromisoformat(row["trigger_discovered_at"]))
        except (KeyError, TypeError, ValueError):
            return None
        if (not 0 <= (now-observed).total_seconds() <= self.ranker.settings.stale_seconds
                or discovered > observed or observation.get("status") != "EVALUATED"):
            return None
        trigger, score = row.get("trigger"), row.get("score")
        if (not isinstance(trigger, (int, float)) or not math.isfinite(trigger) or trigger <= 0
                or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 100):
            return None
        direction = 1 if bias == "LONG" else -1
        if direction * (trigger-price) < 0:
            return None  # A newer quote crossed it; do not manufacture an early claim.
        history = {frame: self._history(symbol, frame, received) for frame in ("1m", "5m")}
        for frame, maximum_age in (("1m", self.config.candle_stale_seconds),
                                   ("5m", self.config.five_minute_stale_seconds)):
            if len(history[frame]) < 36 or (now-history[frame][-1].close_time).total_seconds() > maximum_age:
                return None
        quality = dict(observation.get("data_quality", {}))
        if any(quality.get(frame) != "AVAILABLE" for frame in ("1m", "5m")):
            return None
        metrics = copy.deepcopy(row.get("metrics", {}))
        ratio = metrics.get("live_taker_buy_ratio", metrics.get("taker_buy_ratio"))
        flow = 1-ratio if bias == "SHORT" and isinstance(ratio, (int, float)) else ratio
        if not isinstance(flow, (int, float)) or not math.isfinite(flow) or not self.ranker.settings.minimum_taker_pressure <= flow <= 1:
            return None
        setup_type = row.get("setup_type", "CONTINUATION")
        episode = self._episode_state.get("episodes", {}).get(f"{asset_id}|{bias}|{setup_type}", {})
        atr = episode.get("atr") if episode else metrics.get("trigger_atr")
        if isinstance(atr, (int, float)) and math.isfinite(atr) and atr > 0:
            distance_atr = direction * (trigger-price) / atr
            maximum_distance = self.ranker.settings.imminent_atr if stage == "TRIGGER_IMMINENT" else self.ranker.settings.proximity_atr
            if distance_atr > maximum_distance:
                return None
            metrics["current_distance_atr"] = distance_atr
        elif price != row.get("price"):
            return None  # Cannot assert changed-price proximity without actual ATR.
        if episode.get("restart_stale") or episode.get("status") in {"INVALIDATED", "LATE", "MISSED"}:
            return None
        token = f"{asset_id}|{bias}|{stage}|{trigger}|{discovered.isoformat()}"
        event_id = hashlib.sha256(token.encode()).hexdigest()[:32]
        cooldown_key = f"{asset_id}|{bias}|{stage}"
        prior = self._state_alert_cooldowns.get(cooldown_key)
        if event_id in self._state_alert_seen or (prior and
                (now-datetime.fromisoformat(prior)).total_seconds() < self.config.cooldown_seconds):
            return None
        event_at = (datetime.fromisoformat(evidence["exchange_timestamp"])
                    if evidence.get("exchange_timestamp") else history["1m"][-1].close_time)
        if not event_at <= received <= now:
            return None
        expires_at = min(now + timedelta(seconds=self.config.alert_max_age_seconds),
            source_time + timedelta(seconds=quote_max_age), received + timedelta(seconds=quote_max_age),
            observed + timedelta(seconds=self.ranker.settings.stale_seconds),
            history["1m"][-1].close_time + timedelta(seconds=self.config.candle_stale_seconds),
            history["5m"][-1].close_time + timedelta(seconds=self.config.five_minute_stale_seconds))
        if expires_at <= now:
            return None
        quality["price"] = "ESTIMATED" if evidence["type"].startswith("ESTIMATED") else "AVAILABLE"
        quality["price_event_timestamp"] = "AVAILABLE" if evidence.get("exchange_timestamp") else "UNAVAILABLE"
        metrics.update(feature_snapshot_at=observed.isoformat(), price_evidence=dict(evidence),
            timestamp_basis="NATIVE_EXCHANGE_EVENT" if evidence.get("exchange_timestamp") else "LAST_COMPLETED_1M_CLOSE")
        episode_id = row.get("episode_id") or hashlib.sha256(
            f"pre-move|{asset_id}|{bias}|{trigger}|{discovered.isoformat()}".encode()).hexdigest()[:32]
        record = TradeAlertRecord(event_id=event_id, episode_id=episode_id, asset_id=asset_id, symbol=symbol,
            phase="STRUCTURE_WATCH" if stage == "PRE_IGNITION" else "TRADE_ALERT", signal_state=stage,
            bias=bias, setup_type=setup_type, event_timestamp=event_at, received_at=received, detected_at=now,
            expires_at=expires_at, price=price,
            trigger_level=trigger, trigger_discovered_at=discovered,
            distance_pct=direction*(trigger-price)/price*100, invalidation=row.get("invalidation"),
            targets=list(row.get("targets", [])), score=score, components=copy.deepcopy(row.get("components", {})),
            metrics=metrics, data_quality=quality, btc_context=copy.deepcopy(observation.get("btc_context", {})),
            reason=f"{stage}: {row.get('reason', 'Measured pre-trigger structure')}; known trigger remains unbroken. "
                   "This is an uncalibrated pre-move observation, not a confirmed breakout.",
            detection_latency_ms=(now-event_at).total_seconds()*1000,
            alert_latency_ms=(now-event_at).total_seconds()*1000,
            processing_latency_ms=(now-received).total_seconds()*1000)
        boundary = self._trade_alert_boundary(record)
        if (self.pre_ignition.settings.enabled and record.phase == "TRADE_ALERT"
                and boundary in self._trade_alert_boundaries):
            self._counts["duplicate_early_alerts_suppressed"] += 1
            return None
        if not self._enqueue(self._writes, ("evaluation", [record], self._episode_state), "persistence"):
            return None
        self._state_alert_seen[event_id] = now.isoformat()
        self._state_alert_cooldowns[cooldown_key] = now.isoformat()
        self._enqueue(self._writes, ("state", "trade_alert_pre_move_alerts", "default",
            {"seen": dict(self._state_alert_seen), "cooldowns": dict(self._state_alert_cooldowns)}), "persistence")
        value = record.model_dump(mode="json")
        self._events.append(value)
        self._state_alerts.append(value)
        self._latest_records[symbol] = value
        self._counts["pre_move_alerts"] += 1
        if record.phase == "TRADE_ALERT" and boundary:
            self._trade_alert_boundaries[boundary] = now.isoformat()
        return record

    def _ready_alert_checkpoint(self):
        return {"seen": dict(self._ready_alert_seen), "cooldowns": dict(self._ready_alert_cooldowns),
                "boundaries": dict(self._trade_alert_boundaries)}

    def _emit_pre_ignition_ready(self, row, now):
        """Persist an early observation at READY, never a new entry/confirmation.

        Live eventual move consumption is unknowable. Any optional causal
        extension estimate stays a separately labelled estimate in the record.
        """
        if (not self.pre_ignition.settings.enabled or row.get("state") != "READY"
                or row.get("status") != "AVAILABLE" or not row.get("state_changed")):
            return None
        now = aware(now)
        symbol, bias = row.get("symbol"), row.get("bias")
        if symbol not in self.assets or bias not in {"LONG", "SHORT"}:
            return None
        asset_id = self.assets[symbol].asset_id
        observation = self._observations.get(asset_id, {})
        current, evidence = self._prices.get(symbol), self._price_meta.get(symbol)
        if not current or not evidence or observation.get("status") != "EVALUATED":
            return None
        try:
            observed = aware(datetime.fromisoformat(row["observation_timestamp"]))
            discovered = aware(datetime.fromisoformat(row["trigger_discovered_at"]))
            received = aware(current[1])
            source_at = (aware(datetime.fromisoformat(evidence["exchange_timestamp"]))
                         if evidence.get("exchange_timestamp") else received)
        except (TypeError, ValueError, KeyError):
            return None
        maximum_age = min(self.config.alert_max_age_seconds, self._state(symbol).settings.stale_after_seconds)
        if (not discovered <= observed <= now or not source_at <= received <= now
                or (now-observed).total_seconds() > self.pre_ignition.settings.stale_seconds
                or (now-received).total_seconds() > maximum_age
                or (now-source_at).total_seconds() > maximum_age):
            return None
        price, trigger = current[0], row.get("trigger")
        score = row.get("pre_ignition_score", row.get("score"))
        if any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
               for value in (price, trigger)) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
            return None
        direction = 1 if bias == "LONG" else -1
        if direction*(trigger-price) < 0:
            return None
        estimate = row.get("move_consumed_estimate_pct")
        if (row.get("extension_status") in {"LATE", "EXTENDED", "CONTINUATION_SETUP"}
                or isinstance(estimate, (int, float)) and estimate > 20):
            return None
        quality = dict(observation.get("data_quality", {}))
        bars = {frame: self._history(symbol, frame, received) for frame in ("1m", "5m")}
        for frame, age in (("1m", self.config.candle_stale_seconds),
                           ("5m", self.config.five_minute_stale_seconds)):
            if (quality.get(frame) != "AVAILABLE" or len(bars[frame]) < 36
                    or (now-bars[frame][-1].close_time).total_seconds() > age):
                return None
        boundary = self._trade_alert_boundary(row)
        token = f"pre-ignition-ready|{boundary}"
        event_id = hashlib.sha256(token.encode()).hexdigest()[:32]
        cooldown_key = f"{asset_id}|{bias}|READY"
        prior = self._ready_alert_cooldowns.get(cooldown_key)
        if (event_id in self._ready_alert_seen or boundary in self._trade_alert_boundaries
                or prior and (now-datetime.fromisoformat(prior)).total_seconds() < self.config.cooldown_seconds):
            return None
        expires = min(now+timedelta(seconds=self.config.alert_max_age_seconds),
            received+timedelta(seconds=maximum_age), source_at+timedelta(seconds=maximum_age),
            observed+timedelta(seconds=self.pre_ignition.settings.stale_seconds),
            bars["1m"][-1].close_time+timedelta(seconds=self.config.candle_stale_seconds),
            bars["5m"][-1].close_time+timedelta(seconds=self.config.five_minute_stale_seconds))
        if expires <= now:
            return None
        event_at = source_at if evidence.get("exchange_timestamp") else bars["1m"][-1].close_time
        if event_at > received:
            return None
        immutable = copy.deepcopy(row)
        # Retrospective eventual excursion cannot be a production feature.
        immutable["move_consumed_pct"] = None
        immutable["move_consumed_source"] = "UNKNOWN_UNTIL_OUTCOME_MATURES"
        metrics = copy.deepcopy(row.get("metrics", {}))
        metrics.update(pre_ignition=immutable, price_evidence=copy.deepcopy(evidence),
            feature_snapshot_at=observed.isoformat(), move_consumed_pct=None,
            move_consumed_source="UNKNOWN_UNTIL_OUTCOME_MATURES",
            calibration="RESEARCH_HYPOTHESIS_NOT_VALIDATED")
        quality["price"] = "ESTIMATED" if evidence["type"].startswith("ESTIMATED") else "AVAILABLE"
        record = TradeAlertRecord(event_id=event_id,
            episode_id=row.get("episode_id") or hashlib.sha256(token.encode()).hexdigest()[:32],
            asset_id=asset_id, symbol=symbol, phase="TRADE_ALERT", signal_state="READY", bias=bias,
            setup_type=row.get("setup_type", "CONTINUATION"), event_timestamp=event_at,
            received_at=received, detected_at=now, expires_at=expires, price=price,
            trigger_level=trigger, trigger_discovered_at=discovered,
            distance_pct=direction*(trigger-price)/price*100, invalidation=row.get("invalidation"),
            targets=list(row.get("targets", [])), score=score,
            components=copy.deepcopy(row.get("components", row.get("family_decomposition", {}))),
            metrics=metrics, data_quality=quality, btc_context=copy.deepcopy(observation.get("btc_context", {})),
            reason=f"READY — {bias} PRE-IGNITION: {row.get('reason', 'Measured internal change')}. "
                   "Known trigger remains unbroken; early observation, entry confirmation is separate. "
                   "Initial weights are unvalidated research hypotheses; no calibrated probability.",
            detection_latency_ms=(now-event_at).total_seconds()*1000,
            alert_latency_ms=(now-event_at).total_seconds()*1000,
            processing_latency_ms=(now-received).total_seconds()*1000)
        checkpoint = self._ready_alert_checkpoint()
        checkpoint["seen"][event_id] = now.isoformat()
        checkpoint["cooldowns"][cooldown_key] = now.isoformat()
        checkpoint["boundaries"][boundary] = now.isoformat()
        if not self._enqueue(self._writes, ("ignition", record, self.pre_ignition.checkpoint_state(), checkpoint),
                             "persistence"):
            return None
        self._ready_alert_seen = checkpoint["seen"]
        self._ready_alert_cooldowns = checkpoint["cooldowns"]
        self._trade_alert_boundaries = checkpoint["boundaries"]
        self._register_record(record)
        self._pre_ignition_alerts.append(record.model_dump(mode="json"))
        self._counts["pre_ignition_ready_alerts"] += 1
        return record

    def _refresh_pre_ignition(self, now):
        if not self.pre_ignition.settings.enabled:
            self._pre_ignition_rows = []
            return
        self._pre_ignition_rows = self.pre_ignition.update(self._observations, self._episode_state, now)
        changed = False
        for row in self._pre_ignition_rows:
            if not row.get("state_changed"):
                continue
            changed = True
            identity = hashlib.sha256(json.dumps([row["asset_id"], row["bias"],
                row["state_changed_at"], row["state"], row["status"]], separators=(",", ":")).encode()).hexdigest()
            self._enqueue(self._writes, ("archive", "trade_alert_pre_ignition_transition", identity,
                now, now, copy.deepcopy(row), row["asset_id"]), "persistence")
            self._emit_pre_ignition_ready(row, now)
        minute = int(now.timestamp()//60)
        if changed or self._ignition_checkpoint_minute != minute:
            self._enqueue(self._writes, ("state", "trade_alert_pre_ignition", "default",
                self.pre_ignition.checkpoint_state()), "persistence")
            self._enqueue(self._writes, ("state", "trade_alert_pre_ignition_rows", "default",
                {"rows": copy.deepcopy(self._pre_ignition_rows)}), "persistence")
            self._enqueue(self._writes, ("state", "trade_alert_pre_ignition_alerts", "default",
                self._ready_alert_checkpoint()), "persistence")
            self._ignition_checkpoint_minute = minute

    async def _publish_loop(self):
        retries = set()
        last_retry = {}
        while not self.stop.is_set():
            now = aware(self.clock())
            try:
                # New READY observations get first claim on the early-alert
                # boundary. Native completed-1m confirmations remain separate.
                self._refresh_pre_ignition(now)
            except Exception as exc:
                self._counts["pre_ignition_failed"] += 1
                self._error = f"pre-ignition: {type(exc).__name__}: {exc}"
            try:
                self._pre_move_rows = self.ranker.update(self._observations, self._episode_state, now)
                for row in self._pre_move_rows:
                    if row["state_changed"]:
                        identity = f"{row['asset_id']}:{row['bias']}:{row['state_changed_at']}:{row['state']}:{row['status']}"
                        self._enqueue(self._writes, ("archive", "trade_alert_pre_move_transition", identity,
                            now, now, copy.deepcopy(row), row["asset_id"]), "persistence")
                        self._emit_pre_move_state_alert(row, now)
                minute = int(now.timestamp() // 60)
                if self._rank_checkpoint_minute != minute:
                    self._enqueue(self._writes, ("state", "trade_alert_pre_move", "default",
                        self.ranker.checkpoint_state()), "persistence")
                    self._enqueue(self._writes, ("state", "trade_alert_pre_move_alerts", "default",
                        {"seen": dict(self._state_alert_seen), "cooldowns": dict(self._state_alert_cooldowns)}), "persistence")
                    self._rank_checkpoint_minute = minute
            except Exception as exc:
                self._counts["ranking_failed"] += 1
                self._error = f"ranking: {type(exc).__name__}: {exc}"
            directional = {}
            for row in self._pre_move_rows:
                directional.setdefault(row["asset_id"], {})[row["bias"]] = row
            ignition_directional = {}
            for row in self._pre_ignition_rows:
                ignition_directional.setdefault(row["asset_id"], {})[row["bias"]] = row
            for symbol in list(self._dirty):
                current = self._prices.get(symbol)
                if current and symbol not in self._pending_open:
                    self._pending_open.add(symbol)
                    if not self._enqueue(self._evaluations, (symbol, current[1], current[0], True), "evaluation"):
                        self._pending_open.discard(symbol)
                self._dirty.discard(symbol)
            for symbol in self.assets:
                price = self._prices.get(symbol)
                histories = {frame: self._history(symbol, frame, now) for frame in FRAMES}
                critical = {frame: len(histories[frame]) for frame in ("1m", "5m")}
                fresh = bool(price and 0 <= (now - price[1]).total_seconds() <= self.config.alert_max_age_seconds)
                observation = self._observations.get(self.assets[symbol].asset_id, {})
                quality = dict(observation.get("data_quality", {}))
                for frame, bars in histories.items():
                    maximum_age = self.config.candle_stale_seconds if frame == "1m" else (
                        self.config.five_minute_stale_seconds if frame == "5m" else
                        TIMEFRAMES[frame] + self.config.candle_stale_seconds)
                    quality[frame] = "UNAVAILABLE" if len(bars) < 36 else (
                        "STALE" if (now - bars[-1].close_time).total_seconds() > maximum_age else "AVAILABLE")
                critical_stale = any(quality[frame] == "STALE" for frame in critical)
                ready = all(quality[frame] == "AVAILABLE" for frame in critical)
                latest = self._latest_records.get(symbol)
                self._rows[symbol] = {"symbol": symbol, "asset_id": self.assets[symbol].asset_id,
                    "price": price[0] if price else None, "received_at": price[1].isoformat() if price else None,
                    "price_evidence": dict(self._price_meta.get(symbol, {})),
                    "pre_move": {bias: {key: copy.deepcopy(value) for key, value in row.items()
                        if key not in {"metrics", "components", "conditional_next_steps"}}
                        for bias, row in directional.get(self.assets[symbol].asset_id, {}).items()},
                    "pre_ignition": copy.deepcopy(ignition_directional.get(self.assets[symbol].asset_id, {})),
                    "tick_size": self.tick_sizes[symbol],
                    "history_counts": {frame: len(bars) for frame, bars in histories.items()},
                    "latest_signal": copy.deepcopy(latest),
                    "latest_signal_fresh": bool(latest and fresh and ready and
                        now < datetime.fromisoformat(latest["expires_at"])),
                    "data_quality": quality,
                    "status": "WARMING_UP" if symbol in self._warming else "STALE" if not fresh or critical_stale else
                        "READY" if ready else "PARTIAL",
                    "issues": list(dict.fromkeys(self._issues.get(symbol, [])))[-10:]}
                if ((self._issues.get(symbol) or not ready)
                        and symbol not in self._warming
                        and len(retries) < self.config.warmup_concurrency
                        and (now - last_retry.get(symbol, datetime.min.replace(tzinfo=now.tzinfo))).total_seconds()
                            >= self.config.warmup_retry_seconds):
                    last_retry[symbol] = now
                    retry = asyncio.create_task(self._warmup([symbol]))
                    retries.add(retry)
                    retry.add_done_callback(retries.discard)
                    self._warmup_tasks.add(retry)
                    retry.add_done_callback(self._warmup_tasks.discard)
            self._status = "RUNNING"
            self._enqueue(self._writes, ("state", "trade_alert_runtime", "status", self.snapshot()), "persistence")
            try:
                await asyncio.wait_for(self.stop.wait(), self.config.publish_seconds)
            except TimeoutError:
                pass

    def snapshot(self):
        rows = [copy.deepcopy(row) for row in self._rows.values()]
        statuses = Counter(row["status"] for row in rows)
        now = aware(self.clock())
        connected_shards = {index for index, stream in enumerate(self._streams)
                            if str(stream.health.connection) == "CONNECTED" and not stream.health.stale}
        live_symbols = {symbol for symbol, (_, received) in self._prices.items()
                        if self._running and self._shard.get(symbol) in connected_shards
                        and 0 <= (now-received).total_seconds() <= self._state(symbol).settings.stale_after_seconds}
        ignition_eligible = {row["symbol"] for row in self._pre_ignition_rows
                             if row.get("status") == "AVAILABLE" and row.get("symbol") in live_symbols}
        seeded = sum(all(row.get("history_counts", {}).get(frame, 0) >= 36 for frame in FRAMES)
                     for row in rows)
        scanner_status = "SCANNER_OFFLINE" if not self._running else (
            "LIVE" if self.assets and len(live_symbols) == len(self.assets) and statuses["READY"] == len(self.assets)
            else "DATA_GAP")
        coverage = {"configured": self.config.max_assets, "verified_usdt_spot": self._verified_assets,
            "selected": len(self.assets), "seeded_all_six_timeframes": seeded, "live": len(live_symbols),
            "critical_candles_ready": statuses["READY"], "pre_ignition_eligible": len(ignition_eligible),
            "scanner_status": scanner_status, "as_of": now.isoformat(),
            "scope": "Verified Binance Spot USDT subset; configured capacity is not observed live coverage",
            "outside_universe": "NOT_IN_UNIVERSE", "historical_monitoring": "UNKNOWN_WITHOUT_ARCHIVED_HEALTH"}
        return {"status": self._status, "running": self._running, "as_of": aware(self.clock()).isoformat(),
            "mode": "OBSERVATION", "live_trading": False, "error": self._error,
            "counts": {"requested": self.config.max_assets, **dict(self._counts),
                "scanned": len(rows), "ready": statuses["READY"], "partial": statuses["PARTIAL"],
                "stale": statuses["STALE"], "warming_up": statuses["WARMING_UP"],
            "failed": sum(bool(issues) for issues in self._issues.values())}, "exclusion_reasons": dict(self._exclusions),
            "assets": rows, "events": copy.deepcopy(list(self._events)),
            "pre_move": copy.deepcopy(self._pre_move_rows),
            "pre_move_statealerts": copy.deepcopy(list(self._state_alerts)),
            "pre_ignition": copy.deepcopy(self._pre_ignition_rows),
            "pre_ignition_statealerts": copy.deepcopy(list(self._pre_ignition_alerts)),
            "pre_ignition_enabled": self.pre_ignition.settings.enabled,
            "pre_ignition_calibration": "RESEARCH_HYPOTHESIS_NOT_VALIDATED",
            "coverage": coverage,
            "connections": [stream.health.model_dump(mode="json") for stream in self._streams],
            "evaluation_queue": self._evaluations.qsize(), "persistence_queue": self._writes.qsize()}

    async def run(self, stop_event=None, seconds=None):
        async def supervise_stop():
            if stop_event is not None:
                await stop_event.wait()
                self.stop.set()
            elif seconds is not None:
                await asyncio.sleep(seconds)
                self.stop.set()
            else:
                await self.stop.wait()
        self._running = True
        watcher = asyncio.create_task(supervise_stop())
        try:
            if self.provider is None:
                async with AsyncHTTPClient(self.settings) as http:
                    self.provider = BinanceSpotProvider(http, self.settings)
                    await self._run()
            else:
                await self._run()
        finally:
            self.stop.set()
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            self._running, self._status = False, "STOPPED"
            if self._owns_store:
                self.store.close()

    async def _run(self):
        while not self.stop.is_set():
            try:
                await self.bootstrap()
                break
            except Exception as exc:
                self._status = "PROVIDER_FAILURE"
                self._counts["provider_failures"] += 1
                self._error = f"bootstrap: {type(exc).__name__}: {exc}"
                try:
                    await asyncio.wait_for(self.stop.wait(), self.config.warmup_retry_seconds)
                except TimeoutError:
                    pass
        if self.stop.is_set():
            return
        writer = asyncio.create_task(self._writer())
        evaluator = asyncio.create_task(self._evaluate_loop())
        tasks = [asyncio.create_task(self._consume(index)) for index in range(len(self._streams))]
        tasks += [asyncio.create_task(self._warmup()), asyncio.create_task(self._publish_loop())]
        try:
            await self.stop.wait()
        finally:
            self.stop.set()
            for stream in self._streams:
                stream.stop()
            tasks += list(self._warmup_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.wait_for(evaluator, 10)
                await asyncio.wait_for(self._writes.join(), 10)
            except TimeoutError:
                self._error = "shutdown queues did not drain within grace period"
                evaluator.cancel()
            await self._writes.put(None)
            await writer


async def run_trade_alert_worker(app, config):
    worker = TradeAlertWorker(app.state.settings, config, store=app.state.store)
    app.state.trade_alert_worker = worker
    try:
        await worker.run()
    finally:
        worker.stop.set()
