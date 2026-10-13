"""Discovery-first, two-tier scanner. No execution or external alert delivery.

Broad features/ranks are frozen before selected deep enrichment. All attempts,
including unmapped identities and timeouts, are published in one atomic archive.
The clock is injectable for causal integration tests; fixtures are not market data.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from datetime import datetime
from pathlib import Path

from .config import Settings
from .discovery_config import DiscoverySettings
from .discovery_features import deep_scores, light_features
from .discovery_models import DiscoveryCycle, DiscoveryFeature, DiscoveryObservation, Freshness
from .discovery_ranking import (
    dark_horses,
    rank_observations,
    ranking_formula_version,
    ranking_history_compatible,
)
from .discovery_scheduler import DiscoveryScheduler, transition
from .discovery_store import DiscoveryStore
from .models import TIMEFRAMES, Asset, AssetSnapshot, Metric, utcnow
from .providers.base import unavailable
from .regime import classify_regime
from .technical import analyze


class DiscoveryEngine:
    def __init__(self, market_provider, spot_provider, derivatives_provider,
                 settings: Settings, discovery_settings: DiscoverySettings,
                 store: DiscoveryStore, *, clock=utcnow):
        self.market_provider = market_provider
        self.spot_provider = spot_provider
        self.derivatives_provider = derivatives_provider
        self.settings = settings
        self.discovery_settings = discovery_settings
        self.store = store
        self.clock = clock
        self.scheduler = DiscoveryScheduler(discovery_settings)
        self._lock = asyncio.Lock()
        self._latest = store.latest_cycle()
        self._global: dict[str, Metric] = {}
        self._snapshots: dict[str, AssetSnapshot] = {}
        if self._latest:
            self._snapshots = {o.asset.asset_id: o.snapshot for o in self._latest.observations if o.snapshot}
        self._last_broad = max((o.last_light_at for o in self._latest.observations
                                if o.last_light_at), default=None) if self._latest else None
        if self._latest and (
            self._latest.config.get("discovery", {}).get("universe_size") != discovery_settings.universe_size
            or len(self._latest.observations) > discovery_settings.universe_size
            or not ranking_history_compatible(self._latest, discovery_settings)
        ):
            # A changed target must not silently reuse the old 1000-asset cohort
            # until its broad timer expires when the app requests TOP500.
            self._last_broad = None
        self._deadline = 0.0

    async def run_once(self) -> DiscoveryCycle:
        if self._lock.locked():
            raise RuntimeError("discovery cycle already running")
        async with self._lock:
            return await self._run_once()

    async def _request(self, awaitable):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TimeoutError("discovery cycle time budget exhausted")
        return await asyncio.wait_for(awaitable, min(remaining, self.discovery_settings.collection_timeout_seconds))

    def _diagnostics(self) -> dict:
        result = {}
        seen = set()
        for provider in (self.market_provider, self.spot_provider, self.derivatives_provider):
            http = getattr(provider, "http", None)
            if http and id(http) not in seen:
                seen.add(id(http))
                for key, value in getattr(http, "diagnostics", {}).items():
                    if isinstance(value, (float, int)):
                        result[key] = result.get(key, 0) + value
        return result

    def _market_regime(self, as_of: datetime):
        """Use only cached benchmark history already available at this boundary.

        Collection times differ between assets, so align a copy of the snapshot
        to the shared decision boundary. ``analyze`` and the existing regime
        safeguards still reject future, late, stale or insufficient candles.
        This adds no market requests and never changes archived snapshots.
        """
        def benchmark_context(asset_id):
            snapshot = self._snapshots.get(asset_id)
            if snapshot is None:
                return None, {}
            aligned = snapshot.model_copy(update={"as_of": as_of})
            technical = {frame: analyze(aligned.candles[frame], frame, as_of)
                         for frame in ("4h", "1D") if aligned.candles.get(frame)}
            return aligned, technical

        btc, btc_technical = benchmark_context("bitcoin")
        eth, eth_technical = benchmark_context("ethereum")
        return classify_regime(btc, eth, btc_technical, eth_technical,
                               self._global, None, as_of, self.settings)

    def _config(self) -> dict:
        modules = ("discovery.py", "discovery_features.py", "discovery_ranking.py",
                   "discovery_scheduler.py", "discovery_config.py")
        return {
            "settings": self.settings.model_dump(mode="json"),
            "discovery": self.discovery_settings.model_dump(mode="json"),
            "formula_hashes": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                               for name in modules},
            "live_trading": False,
        }

    @staticmethod
    def _carry_observation(observation: DiscoveryObservation) -> DiscoveryObservation:
        """Copy mutable decision fields; cached market history is copy-on-write.

        Aging/ranking never mutates a snapshot. ``_deep`` creates a full copy
        before changing a selected asset, and ``_light`` builds a new snapshot.
        Avoid copying every unchanged history for each short monitoring cycle.
        """
        carried = observation.model_copy(update={"snapshot": None}).model_copy(deep=True)
        carried.snapshot = observation.snapshot
        return carried

    def _transition(self, observation, previous, cycle_id):
        # State rules consume decision features, not candle histories. Their
        # defensive deep copy must not duplicate every unchanged cached history.
        result, event = transition(observation.model_copy(update={"snapshot": None}),
                                   previous, self.discovery_settings, cycle_id)
        result.snapshot = observation.snapshot
        return result, event

    async def _candles(self, asset: Asset, frame: str, limit: int, cutoff: datetime):
        old = self._snapshots.get(asset.asset_id)
        prior = old.candles.get(frame, []) if old else []
        valid = [b for b in prior if b.close_time <= cutoff and b.available_at <= cutoff]
        seconds = TIMEFRAMES[frame]
        end = math.floor(cutoff.timestamp() / seconds) * seconds
        enough = len(valid) >= limit
        if enough and valid[-1].close_time.timestamp() >= end:
            return valid[-limit:]
        fetch_limit = limit
        if enough:
            fetch_limit = min(limit, max(2, math.ceil((end - valid[-1].close_time.timestamp()) / seconds) + 2))
        received = await self._request(self.spot_provider.candles(asset, frame, fetch_limit, cutoff))
        boundary = max(cutoff, self.clock())
        old_by_time = {b.open_time: b for b in valid}
        for bar in received:
            if bar.close_time > cutoff or bar.available_at > boundary:
                continue
            known = old_by_time.get(bar.open_time)
            if known and known.model_dump(exclude={"available_at"}) == bar.model_dump(exclude={"available_at"}):
                bar = bar.model_copy(update={"available_at": min(known.available_at, bar.available_at)})
            old_by_time[bar.open_time] = bar
        return sorted(old_by_time.values(), key=lambda b: b.open_time)[-limit:]

    @staticmethod
    def _native_merge(metadata: dict[str, Metric], native: dict[str, Metric]) -> dict[str, Metric]:
        result = dict(metadata)
        for key, metric in native.items():
            # CoinGecko aggregate USD metadata cannot be overwritten by USDT venue data.
            result[{"price": "spot_price", "volume_24h": "native_volume_24h"}.get(key, key)] = metric
        return result

    def _metric_feature(self, metric: Metric | None, as_of: datetime, *, critical=False):
        if not metric or not metric.usable(as_of, float("inf")):
            return DiscoveryFeature(provider=metric.source if metric else "unavailable", timeframe="snapshot",
                                    critical=critical, reason=metric.reason if metric else "metric missing")
        age = (as_of - metric.observed_at).total_seconds()
        d = self.discovery_settings
        freshness = Freshness.FRESH if age <= d.market_fresh_seconds else (
            Freshness.DEGRADED if age <= d.market_degraded_seconds else Freshness.STALE)
        return DiscoveryFeature(value=metric.value, source_timestamp=metric.observed_at,
                                retrieved_at=metric.available_at, provider=metric.source,
                                timeframe="snapshot", age_seconds=age, freshness=freshness,
                                unit=metric.unit, critical=critical)

    def _age(self, observation: DiscoveryObservation, when: datetime):
        observation.as_of = when
        d = self.discovery_settings
        for feature in observation.features.values():
            if feature.value is None or not feature.source_timestamp:
                continue
            age = (when - feature.source_timestamp).total_seconds()
            feature.age_seconds = max(0, age)
            seconds = TIMEFRAMES.get(feature.timeframe)
            fresh = seconds * d.feature_fresh_frame_multiple + 60 if seconds else d.market_fresh_seconds
            degraded = seconds * d.feature_degraded_frame_multiple + 60 if seconds else d.market_degraded_seconds
            feature.freshness = Freshness.FRESH if age <= fresh else (
                Freshness.DEGRADED if age <= degraded else Freshness.STALE)
        critical = [f.freshness for f in observation.features.values() if f.critical]
        observation.freshness = next((state for state in (Freshness.MISSING, Freshness.STALE, Freshness.DEGRADED)
                                      if state in critical), Freshness.FRESH if critical else Freshness.MISSING)
        if observation.last_deep_at and (when - observation.last_deep_at).total_seconds() > max(
            300, d.intervals.get(observation.stage.value, 300) * 4
        ):
            for key, score in observation.scores.items():
                if key not in {"light_discovery", "light_discovery_long", "light_discovery_short"}:
                    score.value = None
                    score.coverage = 0
            observation.reasons.append("cached deep scores expired; refresh required")

    async def _light(self, asset: Asset, cutoff: datetime, native: dict[str, Metric], prior):
        d = self.discovery_settings
        values = await asyncio.gather(
            self._request(self.market_provider.market(asset, cutoff)),
            self._candles(asset, d.light_timeframe, d.light_history_bars, cutoff), return_exceptions=True)
        issues = []
        metadata, bars = {}, []
        for label, value in zip(("market metadata", "light candles"), values, strict=True):
            if isinstance(value, BaseException):
                if isinstance(value, asyncio.CancelledError):
                    raise value
                issues.append(f"{label}: {type(value).__name__}: {value}")
            elif label == "market metadata":
                metadata = value
            else:
                bars = value
        old = self._snapshots.get(asset.asset_id)
        candles = dict(old.candles) if old else {}
        candles[d.light_timeframe] = bars
        snapshot = AssetSnapshot(asset=asset, as_of=max(cutoff, self.clock()), candles=candles,
                                 market=self._native_merge(metadata, native),
                                 derivatives=old.derivatives if old else {}, issues=issues)
        observation = DiscoveryObservation(
            asset=asset, as_of=snapshot.as_of, snapshot=snapshot, eligible=True,
            scores={k: v.model_copy(deep=True) for k, v in prior.scores.items()
                    if k not in {"light_discovery", "light_discovery_long", "light_discovery_short"}} if prior else {},
            levels=dict(prior.levels) if prior else {},
            stage=prior.stage if prior else "DORMANT", last_light_at=snapshot.as_of,
            last_deep_at=prior.last_deep_at if prior else None,
            monitoring_due_at=prior.monitoring_due_at if prior else None, reasons=issues,
        )
        return observation

    async def _deep(self, observation: DiscoveryObservation, cutoff: datetime, benchmark, regime):
        d = self.discovery_settings
        observation.deep_scanned = True
        labels = ["liquidity"] + list(d.deep_timeframes)
        tasks = [self._request(self.spot_provider.liquidity(observation.asset, cutoff))]
        tasks += [self._candles(observation.asset, tf, d.deep_history_bars, cutoff) for tf in d.deep_timeframes]
        if self.derivatives_provider:
            labels.append("derivatives")
            tasks.append(self._request(self.derivatives_provider.metrics(observation.asset, cutoff)))
        values = await asyncio.gather(*tasks, return_exceptions=True)
        snapshot = observation.snapshot.model_copy(deep=True)
        failures = []
        for label, value in zip(labels, values, strict=True):
            if isinstance(value, BaseException):
                if isinstance(value, asyncio.CancelledError):
                    raise value
                failures.append(f"deep {label}: {type(value).__name__}: {value}")
            elif label == "liquidity":
                snapshot.market = self._native_merge(snapshot.market, value)
            elif label == "derivatives":
                snapshot.derivatives = value
            else:
                snapshot.candles[label] = value
        for key in ("open_interest", "funding_rate"):
            snapshot.derivatives.setdefault(key, unavailable("derivatives", "provider unavailable or did not return metric"))
        snapshot.as_of = max(cutoff, self.clock())
        snapshot.issues.extend(failures)
        observation.as_of = snapshot.as_of
        observation.snapshot = snapshot
        observation.reasons.extend(failures)
        scores, levels = deep_scores(snapshot, d, benchmark=benchmark, regime=regime,
                                     bias=observation.bias if d.ranking_mode == "BOTH" else "LONG")
        observation.scores.update(scores)
        if d.ranking_mode == "BOTH" and observation.bias != "LONG":
            observation.reasons.append(f"{observation.bias} deep directional scores unavailable; native fast pressure evaluates separately")
        observation.levels = levels
        observation.last_deep_at = snapshot.as_of
        observation.features["price"] = self._metric_feature(
            snapshot.market.get("book_mid") or snapshot.market.get("spot_price"), snapshot.as_of, critical=True)
        for tf in ("1m", "5m", "15m", "1h"):
            bars = snapshot.candles.get(tf, [])
            value = levels.get(f"rvol_{tf}") or levels.get(f"micro_rvol_{tf}")
            if bars and value is not None:
                bar = bars[-1]
                observation.features[f"rvol_{tf}"] = DiscoveryFeature(
                    value=value, provider=bar.source, timeframe=tf, unit="RVOL multiple",
                    source_timestamp=bar.close_time, retrieved_at=bar.available_at,
                    freshness=Freshness.FRESH, age_seconds=(snapshot.as_of - bar.close_time).total_seconds())
        frame = "4h"
        bars = snapshot.candles.get(frame, [])
        if bars and levels.get("extension_atr") is not None:
            bar = bars[-1]
            observation.features["atr_distance"] = DiscoveryFeature(
                value=levels["extension_atr"], provider=bar.source, timeframe=frame, unit="ATR multiples",
                source_timestamp=bar.close_time, retrieved_at=bar.available_at,
                freshness=Freshness.FRESH, age_seconds=(snapshot.as_of - bar.close_time).total_seconds())
        observation.too_late = bool(levels.get("extension_atr") is not None and levels["extension_atr"] > 3)
        observation.missing.extend(f"derivatives.{key}: {value.reason or value.status}"
                                   for key, value in snapshot.derivatives.items() if value.value is None)
        if failures or observation.missing:
            observation.disposition = "PARTIAL"
        self._age(observation, snapshot.as_of)
        return observation

    async def _run_once(self):
        d = self.discovery_settings
        started = self.clock()
        self._deadline = time.monotonic() + d.max_cycle_seconds
        cycle_id = str(uuid.uuid4())
        prior = {o.asset.asset_id: o for o in self._latest.observations} if self._latest else {}
        broad = self._last_broad is None or (started - self._last_broad).total_seconds() >= d.broad_interval_seconds
        if not broad and self._latest and not any(o.eligible and (o.monitoring_due_at is None or
                   o.monitoring_due_at <= started) for o in self._latest.observations):
            return self._latest
        diagnostics = self._diagnostics()
        issues = []
        observations = []
        before_light = time.perf_counter()
        status = "COMPLETE"
        semaphore = asyncio.Semaphore(self.settings.concurrency)
        if broad:
            try:
                universe = await self._request(self.market_provider.universe(d.universe_size, started))
                if not universe or len({a.asset_id for a in universe}) != len(universe):
                    raise RuntimeError("empty universe or duplicate verified asset identities")
                universe = universe[:d.universe_size]
                excluded = set(self.settings.excluded_asset_ids)
                try:
                    resolved = await self._request(self.spot_provider.resolve_assets(
                        [a for a in universe if a.asset_id not in excluded]))
                    mappings = {a.asset_id: a for a in resolved}
                except Exception as exc:
                    mappings = {}
                    issues.append(f"identity mapping unavailable: {type(exc).__name__}: {exc}")
                mapped = [mappings[a.asset_id] for a in universe
                          if a.asset_id in mappings and mappings[a.asset_id].exchange_symbol]
                try:
                    native = await self._request(self.spot_provider.light_market(mapped, started))
                except Exception as exc:
                    native = {}
                    issues.append(f"bulk native market unavailable: {type(exc).__name__}: {exc}")
                try:
                    self._global = await self._request(self.market_provider.global_metrics(started))
                except Exception as exc:
                    self._global = {}
                    issues.append(f"global regime data unavailable: {type(exc).__name__}: {exc}")

                async def collect(asset):
                    mapped_asset = mappings.get(asset.asset_id, asset)
                    if asset.asset_id in excluded or not mapped_asset.exchange_symbol:
                        return DiscoveryObservation(asset=mapped_asset, as_of=self.clock(), disposition="EXCLUDED",
                            reasons=["explicit asset-ID exclusion policy" if asset.asset_id in excluded else
                                     "no verified spot pair mapping; universe coverage exclusion"])
                    async with semaphore:
                        try:
                            return await self._light(mapped_asset, started, native.get(asset.asset_id, {}), prior.get(asset.asset_id))
                        except Exception as exc:
                            return DiscoveryObservation(asset=mapped_asset, as_of=self.clock(), disposition="FAILED",
                                reasons=[f"light collection failed: {type(exc).__name__}: {exc}"])
                observations = list(await asyncio.gather(*(collect(a) for a in universe)))
                self._last_broad = max(started, self.clock())
                boundary = max(started, self.clock())
                benchmark = next((o.snapshot for o in observations if o.asset.asset_id == "bitcoin"), None)
                for row in observations:
                    if not row.snapshot:
                        continue
                    row.as_of = boundary
                    row.snapshot.as_of = boundary
                    row.features = light_features(row.snapshot, d, benchmark=benchmark)
                    row.features["price"] = self._metric_feature(
                        row.snapshot.market.get("spot_price") or row.snapshot.market.get("book_mid"), boundary, critical=True)
                    self._age(row, boundary)
                    row.missing = [f"{key}: {f.reason or f.freshness}" for key, f in row.features.items() if f.value is None]
                    quote = row.snapshot.market.get("quote_volume_24h")
                    spread = row.snapshot.market.get("spread_bps")
                    if quote and quote.usable(boundary, d.market_degraded_seconds) and quote.value < d.min_quote_volume_24h:
                        row.eligible = False
                        row.disposition = "EXCLUDED"
                        row.reasons.append("liquidity: native quote volume below configured minimum")
                    elif spread and spread.usable(boundary, d.market_degraded_seconds) and spread.value > d.max_spread_bps:
                        row.eligible = False
                        row.disposition = "EXCLUDED"
                        row.reasons.append("liquidity: spread exceeds configured maximum")
                    elif not row.snapshot.candles.get(d.light_timeframe):
                        row.eligible = False
                        row.disposition = "FAILED"
                        row.reasons.append("DATA_INCOMPLETE: no valid light history")
                    else:
                        row.disposition = "PARTIAL" if row.missing or row.reasons else "COMPLETE"
                    self._snapshots[row.asset.asset_id] = row.snapshot
                history = self.store.ranking_history(boundary) if hasattr(self.store, "ranking_history") else self.store.history(boundary, limit=3)
                observations = rank_observations(observations, history, d)
            except Exception as exc:
                issues.append(f"universe discovery failed: {type(exc).__name__}: {exc}")
                status = "FAILED"
                observations = [self._carry_observation(o) for o in prior.values()]
                for row in observations:
                    row.as_of = max(started, self.clock())
                    row.eligible = False
                    row.disposition = "FAILED"
                    row.deep_scanned = False
                    row.reasons = ["universe unavailable; identity is last-known membership", issues[-1]]
                    self._age(row, row.as_of)
        else:
            observations = [self._carry_observation(o) for o in prior.values()]
            for row in observations:
                row.deep_scanned = False
                row.reasons = ["light features carried from archived broad cycle"]
                self._age(row, started)
        light_duration = time.perf_counter() - before_light
        # Preliminary states select monitoring priority; only final transitions emit events.
        observations = [self._transition(o, prior.get(o.asset.asset_id), cycle_id)[0] for o in observations]
        ordered = sorted((o for o in observations if o.eligible and o.rank.current_rank is not None),
                         key=lambda o: (o.rank.current_rank, o.asset.asset_id))[:d.abnormal_limit]
        watchlist = ordered[:d.deep_watchlist_size]
        now = max(started, self.clock())
        selected = self.scheduler.due(watchlist, now, d.deep_per_cycle, d.deep_weight_budget,
                                      cost_per_asset=self.scheduler.estimated_deep_cost()) if status != "FAILED" else []
        benchmark = self._snapshots.get("bitcoin")
        regime = self._market_regime(now)
        before_deep = time.perf_counter()

        async def deepen(row):
            async with semaphore:
                try:
                    return await self._deep(row, self.clock(), benchmark, regime)
                except Exception as exc:
                    row.deep_scanned = True
                    row.disposition = "PARTIAL"
                    row.reasons.append(f"deep analysis failed: {type(exc).__name__}: {exc}")
                    row.as_of = max(row.as_of, self.clock())
                    return row
        updates = {o.asset.asset_id: o for o in await asyncio.gather(*(deepen(o) for o in selected))}
        observations = [updates.get(o.asset.asset_id, o) for o in observations]
        deep_duration = time.perf_counter() - before_deep
        final = max([started, self.clock()] + [o.as_of for o in observations])
        events = []
        for index, row in enumerate(observations):
            self._age(row, final)
            row.reasons = list(dict.fromkeys(row.reasons))
            row.missing = list(dict.fromkeys(row.missing))
            observations[index], event = self._transition(row, prior.get(row.asset.asset_id), cycle_id)
            if event:
                events.append(event)
            if row.snapshot:
                self._snapshots[row.asset.asset_id] = row.snapshot
        deferred = {item["asset_id"]: item["reason"] for item in self.scheduler.deferred}
        for row in observations:
            if row.asset.asset_id in deferred:
                row.reasons.append(f"MONITORING_DELAYED: {deferred[row.asset.asset_id]}")
        for provider in (self.market_provider, self.spot_provider, self.derivatives_provider):
            if provider:
                issues.extend(getattr(provider, "issues", []))
        counts = {
            "assets_requested": d.universe_size,
            "assets_discovered": len(observations) if status != "FAILED" else 0,
            "assets_eligible": sum(o.eligible for o in observations),
            "assets_excluded": sum(o.disposition == "EXCLUDED" for o in observations),
            "assets_light_scanned": sum(o.snapshot is not None and bool(o.snapshot.candles.get(d.light_timeframe))
                                        for o in observations) if broad else 0,
            "assets_deep_scanned": sum(o.deep_scanned for o in observations),
            "assets_partial": sum(o.disposition == "PARTIAL" for o in observations),
            "assets_failed": sum(o.disposition == "FAILED" for o in observations),
        }
        if status != "FAILED" and (issues or counts["assets_partial"] or counts["assets_failed"]):
            status = "DEGRADED"
        config = self._config()
        statistics = {"light_duration_seconds": light_duration, "deep_duration_seconds": deep_duration,
                      "monitoring_delayed": len(deferred), "deep_reserved_weight": self.scheduler.used_weight,
                      "broad_refreshed": int(broad), "rank_refreshed": int(broad),
                      "regime": regime.regime.value, "regime_confidence": regime.confidence,
                      "regime_available_at": regime.available_at.isoformat() if regime.available_at else None,
                      "regime_missing": "; ".join(regime.missing),
                      "spot_host": getattr(self.spot_provider, "active_spot_url", None)}
        for key, value in self._diagnostics().items():
            statistics[key] = value - diagnostics.get(key, 0)
        lists = {stage: [o.asset.asset_id for o in sorted(observations, key=lambda r: r.rank.current_rank or math.inf)
                         if o.stage.value == stage][:limit]
                 for stage, limit in (("RADAR", 50), ("WATCH", 25), ("PRE_IGNITION", 20), ("LOADED", 10), ("IGNITION", 10))}
        lists["DARK_HORSE"] = dark_horses(observations, d)
        lists["DEEP_WATCHLIST"] = [o.asset.asset_id for o in watchlist]
        lists["RANK_IMPROVERS"] = [o.asset.asset_id for o in sorted(observations,
            key=lambda r: -(r.rank.rank_velocity or 0)) if o.rank.rank_velocity and o.rank.rank_velocity > 0][:25]
        cycle = DiscoveryCycle(cycle_id=cycle_id, started_at=started, as_of=final,
            completed_at=max(final, self.clock()), database_path=self.store.database_path,
            config_hash=hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
            code_version=ranking_formula_version(d),
            config=config, counts=counts, observations=observations, events=events, lists=lists,
            provider_issues=list(dict.fromkeys(issues)), statistics=statistics, status=status)
        self.store.save_cycle(cycle)
        self._latest = cycle
        return cycle
