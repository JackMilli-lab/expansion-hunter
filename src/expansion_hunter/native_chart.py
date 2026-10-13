"""Bounded chart-only projections and historical native candle pages.

Historical downloads never enter the scanner's rolling state or replay archive.
Only already received, completed rolling candles supply the current AutoFib.
"""
from __future__ import annotations

import asyncio
import copy
import math
import time
from collections import OrderedDict
from datetime import datetime

from .live_analysis import LiveAnalysisSettings, active_fibonacci_view, fibonacci_research
from .models import TIMEFRAMES, Candle, aware, utcnow
from .providers.base import ProviderError

MAX_FIBONACCI = 30 * 6
MAX_HISTORY_PAGES = 8
HISTORY_TTL_SECONDS = 300


def history_boundary(before: datetime, frame: str) -> datetime:
    """The cursor is an exclusive native bar-open boundary, in UTC."""
    before = aware(before)
    seconds = TIMEFRAMES[frame]
    return datetime.fromtimestamp(math.floor(before.timestamp() / seconds) * seconds, before.tzinfo)


def _calculate_fibonacci(bars: list[Candle], frame: str, as_of: datetime, config) -> dict:
    # Reuse the deterministic confirmed-swing implementation, with no full
    # analysis, borrowed timeframe anchors, profiles, fitting or deep worker.
    history = {frame: bars}
    research = fibonacci_research(history, [], as_of=as_of, config=config)
    return active_fibonacci_view(history, research, as_of, config=config)[frame]


class NativeChartService:
    def __init__(self):
        self._fibonacci: OrderedDict = OrderedDict()
        self._history_pages: OrderedDict = OrderedDict()
        self._fib_pending: dict = {}
        self._history_pending: dict = {}
        self._fib_slots = asyncio.Semaphore(2)
        self._history_slots = asyncio.Semaphore(2)
        self.config = LiveAnalysisSettings()

    async def fibonacci(self, worker, symbol: str, frame: str, bars, as_of: datetime) -> dict:
        as_of = aware(as_of)
        # Retain the existing endpoint's serialized rows independently. Invalid
        # or incomplete evidence cannot yield a research anchor.
        known = [bar for bar in bars if isinstance(bar, Candle)
                 and bar.close_time <= as_of and bar.available_at <= as_of]
        known.sort(key=lambda bar: bar.open_time)
        state = worker._state(symbol)
        version = state.history_version(symbol, frame) if hasattr(state, "history_version") else None
        # The digest also handles callers without history_version and an
        # availability boundary moving through a previously received revision.
        revision = tuple((bar.open_time, bar.close_time, bar.available_at, bar.open,
                          bar.high, bar.low, bar.close, bar.volume, bar.source) for bar in known)
        stale = bool(known and (as_of - known[-1].close_time).total_seconds() > TIMEFRAMES[frame] * 1.5)
        signature = (id(worker), version, revision, stale, self.config.model_dump_json())
        key = (symbol, frame)
        cached = self._fibonacci.get(key)
        if cached is None or cached[0] != signature:
            pending_key = (key, signature)
            task = self._fib_pending.get(pending_key)
            if task is None:
                async def calculate():
                    async with self._fib_slots:
                        view = await asyncio.to_thread(_calculate_fibonacci, known, frame, as_of, self.config)
                    result = (signature, view, as_of)
                    self._fibonacci[key] = result
                    self._fibonacci.move_to_end(key)
                    while len(self._fibonacci) > MAX_FIBONACCI:
                        self._fibonacci.popitem(last=False)
                    return result

                task = asyncio.create_task(calculate())
                self._fib_pending[pending_key] = task
                task.add_done_callback(lambda completed: self._fib_pending.pop(pending_key, None))
            cached = await asyncio.shield(task)
        if key in self._fibonacci:
            self._fibonacci.move_to_end(key)
        view = copy.deepcopy(cached[1])
        # Clock-only metadata is cheap; the confirmed geometry is reused until
        # a closed revision or quality transition changes it.
        view["evaluated_at"] = as_of.isoformat()
        if known:
            view["age_seconds"] = (as_of - known[-1].close_time).total_seconds()
        if view.get("discovered_at") and "anchor_age_seconds" in view["context"]:
            view["context"]["anchor_age_seconds"] = (as_of - datetime.fromisoformat(view["discovered_at"])).total_seconds()
        return {"active_fibonacci": {frame: view}, "fibonacci_as_of": {
            "timeframe": frame, "as_of": as_of.isoformat(),
            "calculated_at": cached[2].isoformat(), "status": view["status"],
            "quality": view["quality"], "source": view["source"],
            "source_timestamp": view["source_timestamp"],
            "received_timestamp": view["received_timestamp"],
            "candle_count": len(known), "scope": "COMPLETED_NATIVE_ROLLING_CANDLES",
        }}

    @staticmethod
    def _payload(symbol, frame, bars, as_of, *, has_more, status="AVAILABLE", received_at=None,
                 source=None, reason=None):
        result = {"symbol": symbol, "timeframe": frame,
                  "candles": [bar.model_dump(mode="json") for bar in bars],
                  "has_more": has_more,
                  "next_before": bars[0].open_time.isoformat() if bars and has_more else None,
                  "as_of": as_of.isoformat(), "status": status,
                  "source": source or (bars[-1].source if bars else "UNAVAILABLE"),
                  "received_at": (received_at or max((bar.available_at for bar in bars), default=as_of)).isoformat()}
        if reason:
            result["reason"] = reason
        return result

    async def history(self, worker, symbol: str, frame: str, before: datetime | None,
                      limit: int, now: datetime) -> tuple[dict, int]:
        now = aware(now)
        if before is None:
            bars = sorted((bar for bar in worker._history(symbol, frame, now)
                           if isinstance(bar, Candle) and bar.close_time <= now and bar.available_at <= now),
                          key=lambda bar: bar.open_time)
            bars = list({bar.open_time: bar for bar in bars}.values())[-limit:]
            if not bars:
                return self._payload(symbol, frame, [], now, has_more=False, status="UNAVAILABLE",
                                     reason="Completed native rolling candles are not available"), 503
            return self._payload(symbol, frame, bars, now, has_more=True), 200

        bound = min(history_boundary(before, frame), history_boundary(now, frame))
        key = (id(worker), symbol, frame, bound, limit)
        cached = self._history_pages.get(key)
        monotonic = time.monotonic()
        for old_key, (expires, _) in list(self._history_pages.items()):
            if expires <= monotonic:
                self._history_pages.pop(old_key, None)
        if cached is not None and cached[0] > monotonic:
            self._history_pages.move_to_end(key)
            return copy.deepcopy(cached[1]), 200
        task = self._history_pending.get(key)
        if task is None:
            async def fetch():
                try:
                    async with self._history_slots:
                        provider = getattr(worker, "provider", None)
                        if provider is None:
                            raise ProviderError("native-chart", "Native history provider is not available")
                        downloaded = await provider.candles(worker.assets[symbol], frame, limit, as_of=bound)
                    # Historical bars were received now, not at their event
                    # boundary. Preserve available_at; these pages are chart-only.
                    received = max(utcnow(), now)
                    indexed = {}
                    for bar in downloaded:
                        if not isinstance(bar, Candle):
                            raise ProviderError("native-chart", "Invalid native candle response")
                        if ((bar.close_time - bar.open_time).total_seconds() != TIMEFRAMES[frame]
                                or history_boundary(bar.open_time, frame) != bar.open_time):
                            raise ProviderError("native-chart", "Native candle timeframe does not match")
                        if bar.open_time < bound and bar.close_time <= bound and bar.available_at <= received:
                            prior = indexed.get(bar.open_time)
                            if prior and prior.model_dump(exclude={"available_at"}) != bar.model_dump(exclude={"available_at"}):
                                raise ProviderError("native-chart", "Conflicting native candle revisions")
                            indexed[bar.open_time] = prior or bar
                    bars = sorted(indexed.values(), key=lambda bar: bar.open_time)[-limit:]
                    payload = self._payload(symbol, frame, bars, bound, has_more=len(bars) == limit,
                                            received_at=max((bar.available_at for bar in bars), default=received),
                                            source=bars[-1].source if bars else f"binance/spot/{symbol}/{'1d' if frame == '1D' else frame}")
                    self._history_pages[key] = (time.monotonic() + HISTORY_TTL_SECONDS, payload)
                    self._history_pages.move_to_end(key)
                    while len(self._history_pages) > MAX_HISTORY_PAGES:
                        self._history_pages.popitem(last=False)
                    return payload, 200
                except (ProviderError, ValueError, TypeError) as exc:
                    return self._payload(symbol, frame, [], bound, has_more=False, status="UNAVAILABLE",
                                         received_at=max(utcnow(), now), reason=str(exc)), 503

            task = asyncio.create_task(fetch())
            self._history_pending[key] = task
            task.add_done_callback(lambda completed: self._history_pending.pop(key, None))
        payload, code = await asyncio.shield(task)
        return copy.deepcopy(payload), code
