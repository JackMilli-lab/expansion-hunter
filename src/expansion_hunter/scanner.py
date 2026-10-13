"""Bounded-memory universe scanner. API errors stay attributable to their asset.

Capture all sources before choosing the common knowledge boundary. Snapshot data
is staged on disk; up to 500 complete multi-timeframe histories need not live in
RAM together. Closed candle requests share a scan-start price cutoff.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings
from .models import Asset, AssetSnapshot, Catalyst, Metric, ScanBatch, Stage, Status, utcnow
from .regime import classify_regime
from .rules import evaluate
from .storage import Store
from .technical import analyze

log = logging.getLogger(__name__)


class Scanner:
    def __init__(
        self, market_provider, spot_provider, derivatives_provider, settings: Settings, store: Store
    ):
        self.market_provider = market_provider
        self.spot_provider = spot_provider
        self.derivatives_provider = derivatives_provider
        self.settings = settings
        self.store = store
        self._lock = asyncio.Lock()

    async def scan(self, portfolio=None) -> ScanBatch:
        if self._lock.locked():
            raise RuntimeError("scan already running")
        async with self._lock:
            return await self._scan(portfolio)

    def _catalysts(self) -> dict[str, list[Catalyst]]:
        if not self.settings.catalyst_path:
            return {}
        data = json.loads(Path(self.settings.catalyst_path).read_text())
        return {key: [Catalyst.model_validate(item) for item in items] for key, items in data.items()}

    async def _collect(self, asset: Asset, cutoff: datetime, catalysts: list[Catalyst]) -> AssetSnapshot:
        issues = []
        tasks = [self.market_provider.market(asset, cutoff), self.spot_provider.liquidity(asset, cutoff)]
        labels = ["market", "liquidity"]
        if self.derivatives_provider:
            tasks.append(self.derivatives_provider.metrics(asset, cutoff))
            labels.append("derivatives")
        for tf in self.settings.timeframes:
            tasks.append(self.spot_provider.candles(asset, tf, self.settings.history_bars, cutoff))
            labels.append(tf)
        values = await asyncio.gather(*tasks, return_exceptions=True)
        market, derivatives, candles = {}, {}, {}
        for label, value in zip(labels, values, strict=True):
            if isinstance(value, BaseException):
                if isinstance(value, asyncio.CancelledError):
                    raise value
                issues.append(f"{label}: {type(value).__name__}: {value}")
                log.warning("asset=%s source=%s failed: %s", asset.asset_id, label, value)
            elif label in {"market", "liquidity"}:
                market.update(value)
            elif label == "derivatives":
                derivatives = value
            else:
                candles[label] = value
        knowledge_boundary = utcnow()
        prior = self.store.latest_snapshot(asset.asset_id, knowledge_boundary)
        if prior:
            # Re-fetching unchanged data must retain already known anchors.
            # Corrected versions earn a new retrieval timestamp.
            for tf, bars in candles.items():
                old = {bar.open_time: bar for bar in prior.candles.get(tf, [])}
                for index, bar in enumerate(bars):
                    known = old.get(bar.open_time)
                    if (
                        known
                        and known.available_at <= knowledge_boundary
                        and known.model_dump(exclude={"available_at"})
                        == bar.model_dump(exclude={"available_at"})
                    ):
                        bars[index] = bar.model_copy(
                            update={"available_at": min(known.available_at, bar.available_at)}
                        )
        return AssetSnapshot(
            asset=asset,
            as_of=knowledge_boundary,
            candles=candles,
            market=market,
            derivatives=derivatives,
            catalysts=catalysts,
            issues=issues,
        )

    def _derive_global_history(self, current: dict[str, Metric], as_of: datetime) -> dict[str, Metric]:
        """Use only recorded, completed snapshots; require a near-seven-day observation."""
        historical = self.store.globals_before(as_of - timedelta(days=6, hours=18))
        if not historical:
            return current
        timestamp, data = historical[-1]
        if not timedelta(days=6, hours=18) <= as_of - timestamp <= timedelta(days=7, hours=6):
            return current
        contracts = {
            "btc_dominance_change_7d": ("btc_dominance", False),
            "eth_btc_return_7d": ("eth_btc", True),
            "total_return_7d": ("total_market_cap", True),
            "total2_return_7d": ("total2_market_cap", True),
            "total3_return_7d": ("total3_market_cap", True),
            "stablecoin_mcap_change_7d": ("stablecoin_market_cap", True),
        }
        output = dict(current)
        for name, (base, relative) in contracts.items():
            now = current.get(base)
            old = Metric.model_validate(data[base]) if base in data else None
            if (
                now
                and old
                and now.usable(as_of, 3600)
                and old.usable(timestamp, 3600)
                and old.value
                and old.value > 0
            ):
                change = now.value / old.value - 1 if relative else now.value - old.value
                output[name] = Metric(
                    value=change,
                    observed_at=now.observed_at,
                    available_at=now.available_at,
                    status=Status.AVAILABLE,
                    source=f"recorded:{now.source}",
                    unit="fraction" if relative else "percentage_points",
                    reason=f"Recorded baseline {timestamp.isoformat()}; approximately 7 days",
                )
        return output

    async def _scan(self, portfolio=None) -> ScanBatch:
        scan_id = str(uuid.uuid4())
        started = utcnow()
        self.store.begin(scan_id, started)
        try:
            for provider in (self.market_provider, self.spot_provider, self.derivatives_provider):
                if provider is not None and hasattr(provider, "issues"):
                    provider.issues.clear()
            universe = await self.market_provider.universe(self.settings.top_n, started)
            if not universe:
                raise RuntimeError("market provider returned an empty universe")
            if len({a.asset_id for a in universe}) != len(universe):
                raise RuntimeError("duplicate asset identities in universe")
            universe = universe[: self.settings.top_n]
            excluded = set(self.settings.excluded_asset_ids)
            tradable_universe = [a for a in universe if a.asset_id not in excluded]
            assets = await self.spot_provider.resolve_assets(tradable_universe)
            errors = {
                a.asset_id: "Excluded by explicit asset-ID universe policy"
                for a in universe
                if a.asset_id in excluded
            }
            errors.update(
                {
                    a.asset_id: "No verified exchange pair mapping; excluded"
                    for a in assets
                    if not a.exchange_symbol
                }
            )
            mapped = [a for a in assets if a.exchange_symbol]
            provider_issues = []
            try:
                global_data = await self.market_provider.global_metrics(started)
            except Exception as exc:
                global_data = {}
                provider_issues.append(f"Global regime data unavailable: {type(exc).__name__}: {exc}")
            catalysts = self._catalysts()
            # Explicit BTC/ETH identities are required; never infer a different token by ticker.
            for asset_id, symbol, name in [("bitcoin", "BTC", "Bitcoin"), ("ethereum", "ETH", "Ethereum")]:
                if not any(a.asset_id == asset_id for a in mapped):
                    benchmark = await self.spot_provider.resolve_assets(
                        [Asset(asset_id=asset_id, symbol=symbol, name=name)]
                    )
                    mapped.extend(a for a in benchmark if a.exchange_symbol)
            semaphore = asyncio.Semaphore(self.settings.concurrency)
            with tempfile.TemporaryDirectory(prefix="expansion-scan-") as temporary:
                staged: dict[str, Path] = {}

                async def collect(asset):
                    async with semaphore:
                        try:
                            snapshot = await self._collect(asset, started, catalysts.get(asset.asset_id, []))
                            path = Path(temporary) / f"{uuid.uuid4()}.json.gz"
                            path.write_bytes(
                                gzip.compress(snapshot.model_dump_json().encode(), compresslevel=1)
                            )
                            staged[asset.asset_id] = path
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            errors[asset.asset_id] = f"Collection failed: {type(exc).__name__}: {exc}"

                await asyncio.gather(*(collect(asset) for asset in mapped))
                as_of = utcnow()

                def load(asset_id):
                    path = staged.get(asset_id)
                    return (
                        AssetSnapshot.model_validate_json(gzip.decompress(path.read_bytes())).model_copy(
                            update={"as_of": as_of}
                        )
                        if path
                        else None
                    )

                def technical(snapshot):
                    return (
                        {tf: analyze(bars, tf, as_of) for tf, bars in snapshot.candles.items()}
                        if snapshot
                        else {}
                    )

                btc, eth = load("bitcoin"), load("ethereum")
                btc_tech, eth_tech = technical(btc), technical(eth)
                breadth_values = []
                for asset in tradable_universe:
                    if asset.asset_id in {"bitcoin", "ethereum"}:
                        continue
                    snapshot = load(asset.asset_id)
                    if snapshot:
                        day = analyze(snapshot.candles.get("1D", []), "1D", as_of)
                        close, ema50, ema20 = (day.metrics.get(k) for k in ["close", "ema50", "ema20"])
                        if close is not None and ema50 is not None and ema20 is not None:
                            breadth_values.append(float(close > ema50 and ema20 > ema50))
                alt_count = sum(a.asset_id not in {"bitcoin", "ethereum"} for a in tradable_universe)
                breadth = (
                    sum(breadth_values) / len(breadth_values)
                    if breadth_values and len(breadth_values) >= max(1, alt_count * 0.5)
                    else None
                )
                provider_issues.append(
                    f"Breadth coverage: {len(breadth_values)}/{alt_count} ranked alts on the configured venue"
                )
                global_data = self._derive_global_history(global_data, as_of)
                regime = classify_regime(
                    btc, eth, btc_tech, eth_tech, global_data, breadth, as_of, self.settings
                )
                results = []
                for asset in tradable_universe:
                    snapshot = load(asset.asset_id)
                    if snapshot is None:
                        errors.setdefault(asset.asset_id, "No collected snapshot")
                        continue
                    try:
                        tech = technical(snapshot)
                        result = evaluate(
                            snapshot, tech, regime, self.settings, benchmark=btc, portfolio=portfolio
                        )
                        results.append(result)
                    except Exception as exc:
                        errors[asset.asset_id] = f"Evaluation failed: {type(exc).__name__}: {exc}"
                        log.exception("asset=%s evaluation failed", asset.asset_id)
                # A broad scan may take minutes. Before ENTRY, recapture selected
                # assets and macro context so quotes and closed structure agree.
                # Every refreshed result declares its own knowledge boundary.
                candidates = sorted(
                    (
                        r
                        for r in results
                        if r.stage in {Stage.IGNITION, Stage.BREAKOUT, Stage.RETEST}
                        and r.expansion_score >= self.settings.min_score
                        and r.confidence >= self.settings.min_confidence
                    ),
                    key=lambda r: (-r.expansion_score, r.asset.asset_id),
                )[: self.settings.entry_refresh_limit]
                refreshed = {}
                if portfolio is not None and candidates:
                    context_cutoff = utcnow()
                    if btc:
                        btc = await self._collect(btc.asset, context_cutoff, catalysts.get("bitcoin", []))
                    if eth:
                        eth = await self._collect(eth.asset, context_cutoff, catalysts.get("ethereum", []))
                    try:
                        global_data = await self.market_provider.global_metrics(context_cutoff)
                    except Exception as exc:
                        provider_issues.append(f"Entry regime refresh failed: {type(exc).__name__}: {exc}")
                    for candidate in candidates:
                        try:
                            fresh = await self._collect(
                                candidate.asset, utcnow(), catalysts.get(candidate.asset.asset_id, [])
                            )
                            when = utcnow()
                            fresh = fresh.model_copy(update={"as_of": when})
                            btc_current = btc.model_copy(update={"as_of": when}) if btc else None
                            eth_current = eth.model_copy(update={"as_of": when}) if eth else None
                            fresh_tech = {tf: analyze(bars, tf, when) for tf, bars in fresh.candles.items()}
                            btc_now = (
                                {tf: analyze(bars, tf, when) for tf, bars in btc_current.candles.items()}
                                if btc_current
                                else {}
                            )
                            eth_now = (
                                {tf: analyze(bars, tf, when) for tf, bars in eth_current.candles.items()}
                                if eth_current
                                else {}
                            )
                            fresh_global = self._derive_global_history(global_data, when)
                            fresh_regime = classify_regime(
                                btc_current,
                                eth_current,
                                btc_now,
                                eth_now,
                                fresh_global,
                                breadth,
                                when,
                                self.settings,
                            )
                            fresh_result = evaluate(
                                fresh,
                                fresh_tech,
                                fresh_regime,
                                self.settings,
                                benchmark=btc_current,
                                portfolio=portfolio,
                            )
                            refreshed[candidate.asset.asset_id] = (fresh, fresh_result)
                        except Exception as exc:
                            provider_issues.append(
                                f"{candidate.asset.asset_id}: entry refresh failed: {type(exc).__name__}: {exc}"
                            )
                # Publish only final decisions; never modify archived observations.
                final_results = []
                for result in results:
                    asset_id = result.asset.asset_id
                    if asset_id in refreshed:
                        snapshot, result = refreshed[asset_id]
                    else:
                        snapshot = load(asset_id)
                    self.store.record(scan_id, snapshot, result)
                    final_results.append(result)
                results = final_results
                as_of = max([as_of] + [r.as_of for r in results])
                self.store.record_global(scan_id, as_of, global_data)
                for provider in (self.market_provider, self.spot_provider, self.derivatives_provider):
                    if provider:
                        provider_issues.extend(getattr(provider, "issues", []))
                batch = ScanBatch(
                    scan_id=scan_id,
                    started_at=started,
                    as_of=as_of,
                    completed_at=utcnow(),
                    universe_size=len(universe),
                    evaluated=len(results),
                    results=sorted(
                        results, key=lambda r: (-r.expansion_score, -r.confidence, r.asset.asset_id)
                    ),
                    errors=errors,
                    provider_issues=list(dict.fromkeys(provider_issues)),
                )
                self.store.finish(batch)
                return batch
        except BaseException as exc:
            self.store.fail(scan_id, f"{type(exc).__name__}: {exc}")
            raise
