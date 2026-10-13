"""Append-only economic event intelligence, independent of the breakout hot path.

No price-direction rule, profitable hypothesis or probability is implemented.
Storage adapters may reconstruct this engine from immutable record vintages.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from statistics import mean, stdev
from zoneinfo import ZoneInfo

from .macro_models import (
    MacroConsensus,
    MacroContext,
    MacroEvent,
    MacroMarketObservation,
    MacroMode,
    MacroQuality,
    MacroReaction,
    MacroRelease,
    MacroSettings,
    MacroSurprise,
    ReleaseKind,
)
from .models import aware


def scheduled_utc(local: datetime, timezone: str, *, fold: int | None = None) -> datetime:
    """Convert a published wall-clock schedule; reject nonexistent/ambiguous DST times.

    Ambiguous times require an explicit fold from the authoritative schedule.
    Naively attaching tzinfo to a nonexistent wall time is forbidden.
    """
    if local.tzinfo is not None:
        raise ValueError("local schedule must be a naive wall-clock datetime")
    if fold not in (None, 0, 1):
        raise ValueError("fold must be 0 or 1")
    zone = ZoneInfo(timezone)
    candidates = []
    for occurrence in (0, 1):
        value = local.replace(tzinfo=zone, fold=occurrence)
        utc = aware(value)
        if utc.astimezone(zone).replace(tzinfo=None) == local:
            candidates.append((occurrence, utc))
    if not candidates:
        raise ValueError("schedule falls in a nonexistent DST wall-clock time")
    unique = {candidate for _, candidate in candidates}
    if len(unique) > 1 and fold is None:
        raise ValueError("ambiguous DST schedule requires explicit fold")
    for occurrence, candidate in candidates:
        if fold is None or fold == occurrence:
            return candidate
    raise ValueError("requested fold is not valid for this wall-clock time")


class MacroEngine:
    def __init__(
        self,
        events: list[MacroEvent] | None = None,
        consensuses: list[MacroConsensus] | None = None,
        releases: list[MacroRelease] | None = None,
        settings: MacroSettings | None = None,
    ):
        self.settings = settings or MacroSettings()
        self.events: dict[str, MacroEvent] = {}
        self.consensuses: dict[str, MacroConsensus] = {}
        self.releases: dict[str, MacroRelease] = {}
        for event in events or []:
            self.add_event(event)
        for consensus in consensuses or []:
            self.add_consensus(consensus)
        for release in releases or []:
            self.add_release(release)

    @staticmethod
    def _append(records: dict, key: str, value) -> bool:
        if key in records:
            if records[key] != value:
                raise ValueError(f"immutable record collision: {key}")
            return False
        records[key] = value.model_copy(deep=True)
        return True

    def add_event(self, event: MacroEvent) -> bool:
        existing = self.events.get(event.event_id)
        if existing is not None and event.available_at >= existing.available_at:
            if existing.model_dump(exclude={"available_at"}) == event.model_dump(exclude={"available_at"}):
                # Repeated calendar polls preserve the earliest archived availability.
                return False
        return self._append(self.events, event.event_id, event)

    def add_consensus(self, consensus: MacroConsensus) -> bool:
        # Consensus can arrive before a calendar response: do not lose the receipt.
        return self._append(self.consensuses, consensus.consensus_id, consensus)

    def add_release(self, release: MacroRelease) -> bool:
        if release.kind == ReleaseKind.INITIAL:
            for known in self.releases.values():
                if known.event_id == release.event_id and known.kind == ReleaseKind.INITIAL:
                    if known.release_id != release.release_id:
                        raise ValueError("one initial release per event; use a separate revision record")
                if known.revision_of == release.release_id and (
                    known.event_id != release.event_id
                    or known.actual_release_timestamp < release.actual_release_timestamp
                ):
                    raise ValueError("pending revision conflicts with its newly received initial release")
        elif release.revision_of in self.releases:
            initial = self.releases[release.revision_of]
            if initial.event_id != release.event_id or initial.kind != ReleaseKind.INITIAL:
                raise ValueError("revision must refer to the same event's initial release")
            if release.actual_release_timestamp < initial.actual_release_timestamp:
                raise ValueError("revision publication cannot precede initial publication")
        # Out-of-order revision receipts remain stored, without becoming initial values.
        return self._append(self.releases, release.release_id, release)

    def calendar(
        self, as_of: datetime, start: datetime | None = None, end: datetime | None = None
    ) -> list[MacroEvent]:
        as_of = aware(as_of)
        start, end = aware(start) if start else None, aware(end) if end else None
        return sorted(
            [
                event.model_copy(deep=True)
                for event in self.events.values()
                if event.available_at <= as_of
                and (start is None or event.scheduled_timestamp >= start)
                and (end is None or event.scheduled_timestamp <= end)
            ],
            key=lambda event: (event.scheduled_timestamp, event.event_id),
        )

    def initial_release(self, event_id: str, as_of: datetime) -> MacroRelease | None:
        as_of = aware(as_of)
        releases = [
            release
            for release in self.releases.values()
            if release.event_id == event_id
            and release.kind == ReleaseKind.INITIAL
            and release.actual_release_timestamp <= as_of
            and release.engine_received_timestamp <= as_of
        ]
        return releases[0].model_copy(deep=True) if releases else None

    def _consensus_before(self, event_id: str, boundary: datetime, as_of: datetime) -> MacroConsensus | None:
        candidates = [
            consensus
            for consensus in self.consensuses.values()
            if consensus.event_id == event_id
            and consensus.consensus_timestamp < boundary
            and consensus.engine_received_timestamp < boundary
            and consensus.engine_received_timestamp <= as_of
            and consensus.data_quality == MacroQuality.AVAILABLE
            and consensus.value is not None
        ]
        return (
            max(candidates, key=lambda item: (item.consensus_timestamp, item.consensus_id))
            if candidates
            else None
        )

    def event_view(self, event_id: str, as_of: datetime) -> dict:
        as_of = aware(as_of)
        event = self.events.get(event_id)
        if event is None or event.available_at > as_of:
            return {
                "event_id": event_id,
                "as_of": as_of,
                "data_quality": "UNAVAILABLE",
                "reason": "calendar event unavailable at decision time",
            }
        initial = self.initial_release(event_id, as_of)
        boundary = initial.actual_release_timestamp if initial else as_of
        consensus = self._consensus_before(event_id, boundary, as_of)
        revisions = sorted(
            [
                release.model_copy(deep=True)
                for release in self.releases.values()
                if release.event_id == event_id
                and release.kind == ReleaseKind.REVISION
                and release.actual_release_timestamp <= as_of
                and release.engine_received_timestamp <= as_of
            ],
            key=lambda item: (item.actual_release_timestamp, item.engine_received_timestamp, item.release_id),
        )
        return {
            "event": event.model_copy(deep=True),
            "consensus": consensus.model_copy(deep=True) if consensus else None,
            "initial_release": initial,
            "revisions": revisions,
            "surprise": self.surprise(event_id, as_of),
        }

    def surprise(self, event_id: str, as_of: datetime) -> MacroSurprise:
        as_of = aware(as_of)
        result = MacroSurprise(event_id=event_id, as_of=as_of)
        release = self.initial_release(event_id, as_of)
        if release is None or release.value is None or release.data_quality != MacroQuality.AVAILABLE:
            result.reasons.append("initial release unavailable at decision time")
            return result
        result.initial_release_id = release.release_id
        result.actual_initial_value, result.unit = release.value, release.unit
        consensus = self._consensus_before(event_id, release.actual_release_timestamp, as_of)
        if consensus is None:
            result.reasons.append("no consensus was received before the initial release")
            return result
        result.consensus_id, result.consensus_value = consensus.consensus_id, consensus.value
        if consensus.unit != release.unit:
            result.reasons.append("consensus and initial release units disagree")
            return result
        if (
            consensus.previous_value_timestamp
            and consensus.previous_value_timestamp < release.actual_release_timestamp
        ):
            result.previous_value_as_known_before_release = consensus.previous_value
        result.surprise_absolute = release.value - consensus.value
        if consensus.value > 0:
            result.surprise_percent = result.surprise_absolute / consensus.value * 100
        else:
            result.reasons.append("relative percentage surprise undefined for nonpositive consensus")
        result.data_quality = MacroQuality.AVAILABLE
        event = self.events.get(event_id)
        historical = []
        if event:
            for prior in self.releases.values():
                prior_event = self.events.get(prior.event_id)
                if (
                    prior.kind != ReleaseKind.INITIAL
                    or prior.actual_release_timestamp >= release.actual_release_timestamp
                    or prior.engine_received_timestamp >= release.actual_release_timestamp
                    or prior.engine_received_timestamp > as_of
                    or prior.value is None
                    or prior.data_quality != MacroQuality.AVAILABLE
                    or prior.unit != release.unit
                    or prior_event is None
                    or prior_event.available_at > release.actual_release_timestamp
                    or prior_event.country != event.country
                    or prior_event.category != event.category
                ):
                    continue
                previous_consensus = self._consensus_before(
                    prior.event_id, prior.actual_release_timestamp, as_of
                )
                if previous_consensus and previous_consensus.unit == prior.unit:
                    historical.append(prior.value - previous_consensus.value)
        result.historical_sample_count = len(historical)
        if len(historical) >= self.settings.minimum_standardization_samples:
            sigma = stdev(historical)
            if sigma > 0:
                result.standardized_surprise = (result.surprise_absolute - mean(historical)) / sigma
            else:
                result.reasons.append("historical surprises have zero variance")
        else:
            result.reasons.append("insufficient prior independent releases for standardized surprise")
        return result

    def context(self, as_of: datetime) -> MacroContext:
        as_of = aware(as_of)
        candidates = []
        priority = {
            MacroMode.NORMAL: 0,
            MacroMode.EVENT_COOLDOWN: 1,
            MacroMode.EVENT_RISK: 2,
            MacroMode.EVENT_REACTION: 3,
            MacroMode.EVENT_IMMINENT: 4,
            MacroMode.EVENT_RELEASED: 5,
        }
        for event in self.calendar(as_of):
            if (
                event.importance < self.settings.minimum_importance
                or event.data_quality != MacroQuality.AVAILABLE
            ):
                continue
            seconds = (event.scheduled_timestamp - as_of).total_seconds()
            release = self.initial_release(event.event_id, as_of)
            context = MacroContext(
                as_of=as_of,
                event_id=event.event_id,
                event_name=event.event_name,
                scheduled_timestamp=event.scheduled_timestamp,
                seconds_to_release=seconds,
                data_quality=MacroQuality.AVAILABLE,
            )
            if release and release.data_quality == MacroQuality.AVAILABLE:
                context.actual_release_timestamp = release.actual_release_timestamp
                elapsed = (as_of - release.engine_received_timestamp).total_seconds()
                if elapsed <= self.settings.released_window_seconds:
                    context.mode = MacroMode.EVENT_RELEASED
                elif elapsed <= self.settings.reaction_window_seconds:
                    context.mode = MacroMode.EVENT_REACTION
                elif elapsed <= self.settings.cooldown_window_seconds:
                    context.mode = MacroMode.EVENT_COOLDOWN
                if (
                    release.engine_received_timestamp - release.actual_release_timestamp
                ).total_seconds() > self.settings.released_window_seconds:
                    context.reasons.append(
                        "late release receipt; reaction had already begun before this engine received it"
                    )
            elif -self.settings.overdue_window_seconds <= seconds <= self.settings.imminent_window_seconds:
                context.mode = MacroMode.EVENT_IMMINENT
                if seconds < 0:
                    context.data_quality = MacroQuality.UNAVAILABLE
                    context.reasons.append("scheduled time passed; actual release has not been received")
            elif 0 < seconds <= self.settings.risk_window_seconds:
                context.mode = MacroMode.EVENT_RISK
            else:
                continue
            if context.mode != MacroMode.NORMAL:
                context.reasons.append(
                    "Scheduled macro context supplements technical signals; no directional prediction."
                )
                candidates.append(context)
        if not candidates:
            return MacroContext(
                as_of=as_of,
                data_quality=MacroQuality.AVAILABLE if self.calendar(as_of) else MacroQuality.UNAVAILABLE,
                reasons=[
                    "No active high-impact event in the locally known calendar."
                    if self.calendar(as_of)
                    else "No point-in-time macro calendar is available; this is not evidence of no events."
                ],
            )
        return max(candidates, key=lambda item: (priority[item.mode], -abs(item.seconds_to_release or 0)))

    def reaction(
        self,
        event_id: str,
        asset_id: str,
        observations: list[MacroMarketObservation],
        as_of: datetime,
        horizon_seconds: int,
    ) -> MacroReaction:
        as_of = aware(as_of)
        result = MacroReaction(
            event_id=event_id, asset_id=asset_id, as_of=as_of, horizon_seconds=horizon_seconds
        )
        release = self.initial_release(event_id, as_of)
        if release is None:
            result.reasons.append("initial release unavailable at decision time")
            return result
        at = release.actual_release_timestamp
        result.release_timestamp = at
        end = at + timedelta(seconds=horizon_seconds)
        if as_of < end:
            result.reasons.append("reaction horizon has not matured")
            return result
        available = sorted(
            [
                observation
                for observation in observations
                if observation.asset_id == asset_id
                and observation.data_received_timestamp <= as_of
                and observation.market_event_timestamp <= as_of
                and observation.data_quality == MacroQuality.AVAILABLE
            ],
            key=lambda item: (item.market_event_timestamp, item.data_received_timestamp),
        )
        baseline = [
            item
            for item in available
            if item.market_event_timestamp <= at and item.data_received_timestamp <= at
        ]
        if not baseline:
            result.reasons.append("no price was actually known before the macro release")
            return result
        base = baseline[-1]
        if (at - base.market_event_timestamp).total_seconds() > self.settings.maximum_baseline_age_seconds:
            result.reasons.append("pre-event baseline is stale")
            result.data_quality = MacroQuality.STALE
            return result
        path = [
            item
            for item in available
            if at < item.market_event_timestamp <= end
            and (item.interval_start_timestamp is None or item.interval_start_timestamp >= at)
        ]
        if not path:
            result.reasons.append("no observed post-release path at the requested resolution")
            return result
        sources = {base.source} | {item.source for item in path}
        if len(sources) != 1:
            result.reasons.append("reaction path mixes providers/instruments; normalize separately")
            return result
        resolution = max(item.resolution_seconds for item in path)
        result.resolution_seconds = resolution
        if resolution > horizon_seconds:
            result.reasons.append("data resolution cannot support this reaction horizon")
            return result
        max_gap = max(resolution, self.settings.maximum_tick_gap_seconds)
        endpoint_tolerance = (
            resolution
            if resolution > 0
            else min(self.settings.maximum_tick_gap_seconds, horizon_seconds * 0.1)
        )
        if (end - path[-1].market_event_timestamp).total_seconds() > endpoint_tolerance:
            result.reasons.append("no sufficiently timely observation at the reaction horizon endpoint")
            result.data_quality = MacroQuality.STALE
            return result
        times = [at] + [item.market_event_timestamp for item in path] + [end]
        if (
            max((right - left).total_seconds() for left, right in zip(times, times[1:], strict=False))
            > max_gap
        ):
            result.reasons.append("observed reaction path contains a data gap")
            result.data_quality = MacroQuality.STALE
            return result
        if resolution > 0 and any(item.resolution_seconds != resolution for item in path):
            result.reasons.append("reaction path mixes sampling resolutions")
            return result
        unique_times = {item.market_event_timestamp for item in path}
        if len(unique_times) != len(path):
            result.reasons.append("duplicate event timestamps require explicit source deduplication")
            return result
        finish = path[-1]
        result.baseline_price, result.baseline_event_timestamp = base.price, base.market_event_timestamp
        result.end_price, result.end_event_timestamp = finish.price, finish.market_event_timestamp
        result.return_pct = (finish.price / base.price - 1) * 100
        result.event_mfe_pct = max((item.high or item.price) / base.price - 1 for item in path) * 100
        result.event_mae_pct = min((item.low or item.price) / base.price - 1 for item in path) * 100
        result.event_direction = (
            "UP" if result.return_pct > 0 else "DOWN" if result.return_pct < 0 else "FLAT"
        )
        result.first_move_size_pct = (path[0].price / base.price - 1) * 100
        result.first_move_direction = (
            "UP" if result.first_move_size_pct > 0 else "DOWN" if result.first_move_size_pct < 0 else "FLAT"
        )
        result.reversed_first_move = result.return_pct * result.first_move_size_pct < 0
        result.samples, result.data_quality = len(path), MacroQuality.AVAILABLE
        if all(item.volume is not None for item in path):
            result.volume = math.fsum(item.volume for item in path)
            # A volume shock needs a complete, equally sampled previous window.
            before = [
                item
                for item in available
                if at - timedelta(seconds=horizon_seconds) < item.market_event_timestamp <= at
                and item.data_received_timestamp <= at
                and (
                    item.interval_start_timestamp is None
                    or item.interval_start_timestamp >= at - timedelta(seconds=horizon_seconds)
                )
            ]
            before_times = (
                [at - timedelta(seconds=horizon_seconds)]
                + [item.market_event_timestamp for item in before]
                + [at]
            )
            if (
                before
                and all(item.volume is not None and item.resolution_seconds == resolution for item in before)
                and len({item.market_event_timestamp for item in before}) == len(before)
                and max(
                    (right - left).total_seconds()
                    for left, right in zip(before_times, before_times[1:], strict=False)
                )
                <= max_gap
            ):
                previous_volume = math.fsum(item.volume for item in before)
                if previous_volume > 0:
                    result.volume_shock = result.volume / previous_volume
        if base.book_depth is not None and finish.book_depth is not None and base.book_depth > 0:
            result.liquidity_shock = finish.book_depth / base.book_depth - 1
        result.reasons.append("Observed subsequent market path; association does not establish causation.")
        return result
