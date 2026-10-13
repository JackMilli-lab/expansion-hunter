"""Background intelligence processing, isolated from market ingestion.

Only published point-in-time archives are read. Matching never reads outcomes.
Calendar/consensus/initial/revision vintages remain separate. No model fitting,
threshold optimization, price prediction or order submission happens here.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from hashlib import sha256

from .analog import AnalogSettings, AnalogVector, FactorQuality, FactorValue, match_analogs
from .intelligence_store import IntelligenceStore
from .macro import MacroEngine
from .macro_models import MacroConsensus, MacroEvent, MacroProviderReport, MacroRelease
from .models import aware, utcnow


def load_macro(store: IntelligenceStore, as_of: datetime | None = None) -> MacroEngine:
    cutoff = aware(as_of or utcnow())
    return MacroEngine(
        events=[MacroEvent.model_validate(o) for o in store.objects("macro_event", cutoff, limit=10000)],
        consensuses=[MacroConsensus.model_validate(o) for o in store.objects("macro_consensus", cutoff, limit=10000)],
        releases=[MacroRelease.model_validate(o) for o in store.objects("macro_release", cutoff, limit=10000)],
    )


def import_macro(store: IntelligenceStore, report: MacroProviderReport) -> dict[str, int]:
    # Validate all references and uniqueness before accepting a partial import.
    engine = load_macro(store, report.retrieved_at)
    for event in report.events:
        engine.add_event(event)
    for consensus in report.consensuses:
        engine.add_consensus(consensus)
    # Initial releases are validated first, while each keeps its original receipt.
    for release in sorted(report.releases, key=lambda r: (r.kind.value != "INITIAL", r.engine_received_timestamp)):
        engine.add_release(release)
    counts = {"events": 0, "consensuses": 0, "releases": 0}
    records = []
    for event in report.events:
        event = engine.events[event.event_id]
        records.append(("macro_event", event.event_id, event.available_at, event.available_at, event, None))
    for consensus in report.consensuses:
        records.append(("macro_consensus", consensus.consensus_id,
            consensus.consensus_timestamp, consensus.engine_received_timestamp, consensus, None))
    for release in report.releases:
        records.append(("macro_release", release.release_id,
            release.actual_release_timestamp, release.engine_received_timestamp, release, None))
    for record, added in zip(records, store.append_many(records), strict=True):
        counts[{"macro_event":"events", "macro_consensus":"consensuses", "macro_release":"releases"}[record[0]]] += added
    store.save_domain("macro_provider", report, event_at=report.retrieved_at,
                      available_at=report.retrieved_at)
    return counts


def observation_vector(observation, event_id: str, context: dict) -> AnalogVector:
    """Reconstruct original-decision features without later published context.

    Background cycle labels can be produced hours after the signal. Their
    publication time must not be replaced with the original observation time.
    Undated labels are deliberately unavailable, including legacy archives.
    """
    factors = {}
    for name, feature in observation.features.items():
        if not feature.source_timestamp or not feature.retrieved_at:
            continue
        if feature.source_timestamp > observation.as_of or feature.retrieved_at > observation.as_of:
            continue
        factors[name] = FactorValue(value=feature.value, event_timestamp=feature.source_timestamp,
            available_at=feature.retrieved_at, source=feature.provider, unit=feature.unit,
            quality=FactorQuality.DERIVED if feature.value is not None and feature.freshness.value == "FRESH"
                    else FactorQuality.STALE if feature.freshness.value in {"STALE", "DEGRADED"}
                    else FactorQuality.UNAVAILABLE)
    price = factors.get("price")

    def known_context(name: str):
        published = context.get(f"{name}_available_at")
        if not published:
            return None
        try:
            published = aware(datetime.fromisoformat(published) if isinstance(published, str) else published)
        except (TypeError, ValueError, AttributeError):
            return None
        return context.get(name) if published <= observation.as_of else None

    return AnalogVector(event_id=event_id, asset_id=observation.asset.asset_id,
        decision_time=observation.as_of, available_at=observation.as_of, factors=factors,
        reference_price=price, regime=known_context("regime"), cycle=known_context("cycle"),
        macro_context=known_context("macro_mode"), schema_version="analog-v2-point-in-time-context")


class IntelligenceWorker:
    """Run in a separate CLI process; optional to_thread never joins the fast path."""
    def __init__(self, store: IntelligenceStore, *, clock=utcnow):
        self.store = store
        self.clock = clock

    def process_latest(self) -> dict:
        from .cycle import classify_market_cycle, market_congruence
        from .cycle_patterns import research_cycle_patterns

        cycle = self.store.latest_cycle()
        if not cycle:
            return {"status": "UNAVAILABLE", "reason": "No published discovery cycle"}
        done = self.store.states("intelligence_progress")
        if done and done[0].get("cycle_id") == cycle.cycle_id:
            return {"status": "UNCHANGED", "cycle_id": cycle.cycle_id}
        boundary = cycle.completed_at
        snapshots = [o.snapshot for o in cycle.observations if o.snapshot]
        btc = next((s for s in snapshots if s.asset.asset_id == "bitcoin"), None)
        eth = next((s for s in snapshots if s.asset.asset_id == "ethereum"), None)
        global_metrics = {}
        # Only recorded global observations; no current download disguised as old context.
        records = self.store.globals_before(boundary)
        if records:
            from .models import Metric
            global_metrics = {k: Metric.model_validate(v) for k, v in records[-1][1].items()}
        prior_reports = self.store.objects("cycle_state", boundary, limit=1)
        from .cycle import MarketCycleReport
        previous = MarketCycleReport.model_validate(prior_reports[0]) if prior_reports else None
        report = classify_market_cycle(btc, eth, snapshots, global_metrics, boundary, previous=previous)
        publication = max(boundary, self.clock())
        self.store.save_domain("cycle_state", report, object_id=cycle.cycle_id,
                              event_at=boundary, available_at=publication)
        macro = load_macro(self.store, boundary).context(boundary)
        context = {"regime": cycle.statistics.get("regime"), "cycle": report.phase.value,
                   "cycle_report": report.model_dump(mode="json"),
                   "macro_mode": macro.mode.value, "macro": macro.model_dump(mode="json"),
                   "cycle_available_at": publication.isoformat(),
                   "regime_available_at": cycle.statistics.get("regime_available_at"),
                   "macro_mode_available_at": publication.isoformat(), "research_only": True}
        vectors = [AnalogVector.model_validate(v) for v in self.store.objects("factor_vector", boundary, limit=10000)]
        processed = 0
        for event in cycle.events:
            observation = next(o for o in cycle.observations if o.asset.asset_id == event.asset_id)
            congruence = market_congruence(observation.snapshot, report, boundary) if observation.snapshot else None
            event_context = dict(context)
            event_context["market_congruence"] = congruence.model_dump(mode="json") if congruence else None
            self.store.record_context(event.event_id, publication, event_context)
            vector = observation_vector(observation, event.event_id, event_context)
            self.store.append("factor_vector", vector.event_id, vector.decision_time, publication, vector, vector.asset_id)
            analogy = match_analogs(vector, vectors, AnalogSettings(minimum_coverage=0.5))
            self.store.save_domain("analog_query", analogy, object_id=event.event_id,
                event_at=boundary, available_at=publication, asset_id=event.asset_id)
            patterns = research_cycle_patterns(vector)
            self.store.save_domain("cycle_patterns", patterns, object_id=event.event_id,
                event_at=boundary, available_at=publication, asset_id=event.asset_id)
            processed += 1
        self.store.put_state("intelligence_progress", "worker", publication,
                             {"cycle_id": cycle.cycle_id, "processed_at": publication.isoformat()})
        return {"status": "COMPLETE", "cycle_id": cycle.cycle_id, "events_contextualized": processed,
                "cycle": report.model_dump(mode="json"), "macro": macro.model_dump(mode="json"),
                "database_path": self.store.database_path, "live_trading": False}

    async def run_once(self):
        return await asyncio.to_thread(self.process_latest)


def context_at(store: IntelligenceStore, as_of: datetime) -> dict:
    """Cheap cached context lookup. Never run similarity calculations here."""
    macro = load_macro(store, as_of).context(as_of)
    reports = store.objects("cycle_state", as_of, limit=1)
    return {"cycle": reports[0] if reports else {"phase": "UNAVAILABLE", "missing": ["cycle_state"]},
            "macro": macro.model_dump(mode="json"), "as_of": as_of.isoformat()}


def reaction_archive(store: IntelligenceStore, event_id: str, asset_id: str, as_of: datetime,
                     horizon_seconds: int):
    from .macro_models import MacroMarketObservation
    observations = [MacroMarketObservation.model_validate(o) for o in store.objects(
        "market_observation", as_of, asset_id=asset_id, limit=10000)]
    reaction = load_macro(store, as_of).reaction(event_id, asset_id, observations, as_of, horizon_seconds)
    identity = sha256(f"{event_id}|{asset_id}|{horizon_seconds}|{as_of.isoformat()}".encode()).hexdigest()
    store.append("macro_reaction", identity, as_of, as_of, reaction, asset_id)
    return reaction
