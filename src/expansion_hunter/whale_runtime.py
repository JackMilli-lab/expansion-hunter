"""Independent async observer with immutable first receipts and restart dedup."""
from __future__ import annotations

import asyncio
from datetime import datetime
from hashlib import sha256
from itertools import product

from .intelligence_store import IntelligenceStore
from .journal_runtime import _complete_thread_call
from .models import aware, utcnow
from .providers.whales import EtherscanV2WhaleProvider, WhaleTransferProvider
from .whale_flow import WalletLabel, WhaleSettings, explain_transfer, whale_status


class WhaleObservationWorker:
    def __init__(self, settings: WhaleSettings, store: IntelligenceStore,
                 provider: WhaleTransferProvider | None = None, *, labels: tuple[WalletLabel, ...] = (), clock=utcnow):
        self.settings, self.store, self.clock, self.labels = settings, store, clock, labels
        self.provider = provider or EtherscanV2WhaleProvider(settings)
        self._owned = provider is None
        self.last_success_at: datetime | None = None
        self._last = whale_status(settings, bool(getattr(self.provider, "key_configured", False)), clock())

    def status(self) -> dict:
        value = dict(self._last)
        if self.last_success_at and (aware(self.clock()) - self.last_success_at).total_seconds() > self.settings.stale_seconds:
            value.update(status="STALE", reasons=["PROVIDER_OBSERVATIONS_STALE"])
        return value

    async def cycle(self) -> dict:
        now = aware(self.clock())
        initial = whale_status(self.settings, bool(getattr(self.provider, "key_configured", False)), now)
        if initial["status"] == "UNAVAILABLE":
            self._last = initial
            await _complete_thread_call(self.store.put_state, "whale_runtime", "etherscan_v2", now, initial)
            return initial
        inserted, issues, successful, latest = 0, [], 0, []
        queries = list(product(self.settings.wallets, self.settings.contracts or (None,)))
        scheduler_id = sha256(repr((self.settings.chain_id, queries)).encode()).hexdigest()
        schedule = await _complete_thread_call(self.store.get_state, "whale_scheduler", scheduler_id)
        offset = int(schedule.get("next_index", 0)) % len(queries) if schedule else 0
        count = min(len(queries), self.settings.max_queries_per_cycle)
        selected = [queries[(offset + index) % len(queries)] for index in range(count)]
        if count < len(queries):
            issues.append("QUERY_BUDGET_LIMITED")
        for wallet, contract in selected:
            key = f"{self.settings.chain_id}:{wallet}:{contract or '*'}"
            cursor = await _complete_thread_call(self.store.get_state, "whale_cursor", key)
            start = max(0, int(cursor.get("block_number", 0)) - self.settings.overlap_blocks) if cursor else 0
            try:
                batch = await self.provider.transfers(wallet, contract, start_block=start)
                issues.extend(batch.issues)
                if batch.status != "UNAVAILABLE":
                    successful += 1
                for event in batch.events:
                    if event.available_at > aware(self.clock()):
                        issues.append("FUTURE_PROVIDER_RECEIPT")
                        continue
                    if await _complete_thread_call(self.store.object, "whale_transfer", event.event_id, self.clock()):
                        continue
                    explained = explain_transfer(event, as_of=self.clock(), labels=self.labels)
                    await _complete_thread_call(self.store.append, "whale_transfer", event.event_id,
                                            event.event_at, event.available_at, explained,
                                            f"{event.chain_id}:{event.contract}")
                    inserted += 1
                    latest.append(explained.model_dump(mode="json"))
                # Never advance past a failed/partial page: the skipped rows
                # must remain eligible for retry rather than disappear silently.
                if batch.status == "AVAILABLE" and batch.events:
                    await _complete_thread_call(self.store.put_state, "whale_cursor", key, self.clock(), {
                        "block_number": max(event.block_number for event in batch.events),
                        "updated_at": aware(self.clock()).isoformat()})
            except Exception as error:
                issues.append("PROVIDER_OR_STORAGE_FAILURE:" + type(error).__name__)
        completed = aware(self.clock())
        await _complete_thread_call(self.store.put_state, "whale_scheduler", scheduler_id, completed,
                                    {"next_index": (offset + count) % len(queries)})
        if successful:
            self.last_success_at = completed
        self._last = {**initial, "status": "PARTIAL" if successful and issues else "AVAILABLE" if successful else "UNAVAILABLE",
                      "reasons": list(dict.fromkeys(issues)), "new_events": inserted,
                      "events": latest[-100:], "updated_at": completed.isoformat(),
                      "queries_completed": count, "queries_configured": len(queries),
                      "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None}
        await _complete_thread_call(self.store.put_state, "whale_runtime", "etherscan_v2", completed, self._last)
        return self.status()

    async def run(self, stop: asyncio.Event):
        try:
            while not stop.is_set():
                try:
                    await self.cycle()
                except Exception as error:
                    self._last.update(status="UNAVAILABLE", reasons=["WORKER_FAILURE:" + type(error).__name__])
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.settings.poll_seconds)
                except TimeoutError:
                    pass
        finally:
            if self._owned:
                await self.provider.aclose()


def whale_events(store: IntelligenceStore, *, as_of: datetime | None = None,
                 chain_id: int | None = None, contract: str | None = None, limit: int = 100) -> list[dict]:
    """Canonical chain/contract identity only; ticker collisions are not matches."""
    from .whale_flow import address

    if contract is not None and chain_id is None:
        raise ValueError("A contract filter requires its chain identity")
    asset = f"{chain_id}:{address(contract)}" if contract is not None else None
    events = store.objects("whale_transfer", as_of, asset_id=asset, limit=limit)
    return [event for event in events if chain_id is None or event["chain_id"] == chain_id]


def whale_asset_context(store: IntelligenceStore, asset_id: str, as_of: datetime,
                        settings: WhaleSettings) -> dict:
    """Map canonical identities explicitly; wallet flows never inflate a score."""
    now = aware(as_of)
    base = {"asset_id": asset_id, "status": "UNAVAILABLE", "transfers": [],
            "production_weight": 0, "identity_uncertainty": "Wallet purpose/beneficial owner is UNKNOWN",
            "source": "ETHERSCAN_V2", "available_at": now.isoformat()}
    if settings.mapping_available_at > now:
        return {**base, "reason": "ASSET_MAPPING_NOT_YET_AVAILABLE"}
    matches = [key for key, mapped in settings.token_assets.items() if mapped == asset_id]
    if not matches:
        return {**base, "reason": "CANONICAL_CONTRACT_MAPPING_REQUIRED"}
    runtime = store.get_state("whale_runtime", "etherscan_v2")
    if not runtime:
        return {**base, "reason": "NO_PROVIDER_OBSERVATIONS"}
    # Operational health is current cache data and is never backdated into an
    # historical query. The event archive remains independently point-in-time.
    updated = aware(datetime.fromisoformat(runtime["updated_at"]))
    if updated > now:
        return {**base, "reason": "PROVIDER_STATUS_NOT_YET_AVAILABLE"}
    state = runtime["status"]
    if (now - updated).total_seconds() > settings.stale_seconds:
        state = "STALE"
    transfers, contracts = [], []
    for key in matches:
        chain, contract = key.split(":", 1)
        if int(chain) != settings.chain_id:
            continue
        contracts.append({"chain_id": int(chain), "contract": contract})
        transfers.extend(whale_events(store, as_of=now, chain_id=int(chain), contract=contract))
    if not contracts:
        return {**base, "reason": "MAPPED_CHAIN_NOT_MONITORED"}
    return {**base, "status": state, "reason": runtime.get("reasons", []), "contracts": contracts,
            "provider_updated_at": updated.isoformat(), "transfers": sorted(transfers,
                key=lambda event: (event["available_at"], event["event_id"]), reverse=True)[:100]}


async def run_whale_worker(app, *, settings: WhaleSettings | None = None,
                           provider: WhaleTransferProvider | None = None):
    """Own a separate SQLite connection and wait for writes before shutdown."""
    owned_store, worker = None, None
    stop = asyncio.Event()
    app.state.whale_stop_event = stop
    try:
        try:
            config = settings or WhaleSettings.from_environment()
        except (TypeError, ValueError):
            app.state.whale_health = {"status": "UNAVAILABLE", "reasons": ["INVALID_WHALE_CONFIGURATION"],
                                      "production_weight": 0, "updated_at": utcnow().isoformat()}
            return
        app.state.whale_settings = config
        creating = asyncio.create_task(asyncio.to_thread(IntelligenceStore, app.state.settings.db_path))
        try:
            owned_store = await asyncio.shield(creating)
        except asyncio.CancelledError:
            owned_store = await creating
            raise
        worker = WhaleObservationWorker(config, owned_store, provider)
        while not stop.is_set():
            try:
                await worker.cycle()
                app.state.whale_health = worker.status()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                app.state.whale_health = {"status": "UNAVAILABLE", "reasons": [
                    "WHALE_WORKER_FAILURE:" + type(error).__name__], "production_weight": 0,
                    "updated_at": utcnow().isoformat()}
            try:
                await asyncio.wait_for(stop.wait(), timeout=config.poll_seconds)
            except TimeoutError:
                pass
    finally:
        if worker and worker._owned:
            await worker.provider.aclose()
        if owned_store:
            await _complete_thread_call(owned_store.close)
        if worker:
            app.state.whale_health = {**worker.status(), "status": "STOPPED"}
