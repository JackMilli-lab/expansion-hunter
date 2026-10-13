"""Bounded native-candle continuity repair after ordinary stream gaps.

Repair bars are historical observations received now, never an excuse to emit
backdated breakout alerts. The caller owns scheduling, rate budgets and seeding;
existing finalized feed/monitor candles remain immutable. No order APIs exist.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import Field

from .models import TIMEFRAMES, Asset, Candle, Model, aware, utcnow

REPAIR_FRAMES = ("1m", "5m", "15m")


class CandleRepairPlan(Model):
    asset_id: str
    symbol: str
    timeframe: str
    planned_at: datetime
    latest_expected_close: datetime
    requested_limit: int = Field(ge=0, le=120)
    missing_open_times: list[datetime]
    remote_missing_open_times: list[datetime]
    existing_feed_open_times: list[datetime]
    local_candles: list[Candle] = Field(default_factory=list)
    needs_monitor_merge: bool = False
    reason: str
    data_quality: str = "MISSING"
    historical_only: bool = True
    issues: list[str] = Field(default_factory=list)


class CandleRepairResult(Model):
    symbol: str
    timeframe: str
    bars: list[Candle] = Field(default_factory=list)
    received_at: datetime
    status: str
    issues: list[str] = Field(default_factory=list)
    historical_only: bool = True
    requested_limit: int = 0
    remaining_missing_open_times: list[datetime] = Field(default_factory=list)


class CandleProvider(Protocol):
    async def candles(self, asset: Asset, timeframe: str, limit: int, as_of: datetime) -> list[Candle]: ...


def _native_source(source: str, symbol: str, timeframe: str) -> bool:
    return source in {"binance-public-ws", f"binance/spot/{symbol}/{timeframe}"}


def _monitor_candles(state, timeframe: str, as_of: datetime) -> list[Candle]:
    raw = state.get("candles", {}) if isinstance(state, dict) else getattr(state, "candles", {})
    bars = []
    for value in raw.get(timeframe, []):
        candle = value if isinstance(value, Candle) else Candle.model_validate(value)
        if candle.available_at <= as_of and candle.close_time <= as_of:
            bars.append(candle)
    return bars


def detect_repair_frames(
    asset: Asset,
    feed_state,
    monitor_state,
    as_of: datetime,
    *,
    max_bars: int = 120,
    boundary_grace_seconds: float = 5,
) -> list[CandleRepairPlan]:
    """Plans only recent missing completed intervals, never an every-tick warmup.

    The grace allows the native final-close message to arrive at a boundary.
    Both caches are inspected: an old monitor gap still needs merging even if
    the current feed is contiguous. Known local bars avoid unnecessary I/O.
    At most three native frames, each at most 120 completed bars, are requested.
    """
    as_of = aware(as_of)
    if isinstance(max_bars, bool) or not isinstance(max_bars, int) or not 1 <= max_bars <= 120:
        raise ValueError("repair max_bars must be between 1 and 120")
    if not math.isfinite(boundary_grace_seconds) or not 0 <= boundary_grace_seconds < 60:
        raise ValueError("repair boundary grace must be finite and between 0 and 60 seconds")
    symbol = asset.exchange_symbol
    if not symbol:
        return []
    plans = []
    for frame in REPAIR_FRAMES:
        seconds = TIMEFRAMES[frame]
        expected_close = datetime.fromtimestamp(math.floor((as_of.timestamp() - boundary_grace_seconds) / seconds) * seconds, tz=UTC)
        cutoff = expected_close - timedelta(seconds=max_bars * seconds)
        live = feed_state.candles(symbol, frame, as_of, limit=max_bars)
        monitored = _monitor_candles(monitor_state, frame, as_of) if monitor_state else []
        recent = [bar for bar in live + monitored if bar.close_time > cutoff]
        first = max(cutoff, min((bar.open_time for bar in recent), default=cutoff))
        if first >= expected_close:
            continue
        live_by_open = {bar.open_time: bar for bar in live}
        monitor_by_open = {bar.open_time: bar for bar in monitored}
        expected_opens = [first + timedelta(seconds=seconds * index) for index in range(int((expected_close - first).total_seconds() // seconds))]
        feed_missing = [opened for opened in expected_opens if opened not in live_by_open]
        monitor_missing = [opened for opened in expected_opens if opened not in monitor_by_open] if monitored else []
        missing = sorted(set(feed_missing) | set(monitor_missing))
        if not missing:
            continue
        local = [monitor_by_open[opened].model_copy() for opened in feed_missing if opened in monitor_by_open]
        remote = [opened for opened in feed_missing if opened not in monitor_by_open]
        # The extra slot covers a new final close arriving around the grace
        # boundary; limit remains bounded independently of cached history size.
        limit = min(max_bars, int((as_of - remote[0]).total_seconds() // seconds) + 1) if remote else 0
        non_native = sorted({bar.source for bar in recent if not _native_source(bar.source, symbol, frame)})
        issues = [f"foreign_finalized_candle_source:{source}" for source in non_native]
        plans.append(CandleRepairPlan(asset_id=asset.asset_id, symbol=symbol, timeframe=frame,
                                     planned_at=as_of, latest_expected_close=expected_close,
                                     requested_limit=limit, missing_open_times=missing,
                                     remote_missing_open_times=remote,
                                     existing_feed_open_times=list(live_by_open), local_candles=local,
                                     needs_monitor_merge=bool(monitor_missing),
                                     reason="EMPTY_RECENT_FRAME" if not recent else "CANDLE_GAP_OR_MISSING_COMPLETED_CLOSE",
                                     data_quality="UNAVAILABLE" if issues else "MISSING", issues=issues))
    return plans


async def fetch_repair(
    provider: CandleProvider,
    asset: Asset,
    plan: CandleRepairPlan,
    *,
    clock: Callable[[], datetime] = utcnow,
    timeout_seconds: float = 15,
) -> CandleRepairResult:
    """Fetch only a bounded recent native frame and return missing observations.

    ``available_at`` is actual receipt, never the historical close. Seeding must
    occur at/after ``received_at``. Before evaluation, the caller must advance
    the repaired frame's historical last_processed marker so old repaired
    closes cannot become newly advertised live breakout/acceptance alerts.
    A native close received while REST was in flight wins in seed_candles.
    """
    if plan.asset_id != asset.asset_id or plan.symbol != asset.exchange_symbol:
        raise ValueError("repair plan belongs to a different instrument")
    if plan.timeframe not in REPAIR_FRAMES:
        raise ValueError("unsupported repair timeframe")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("repair timeout must be finite and positive")
    requested_at = aware(clock())
    if requested_at < plan.planned_at:
        raise ValueError("repair cannot access a future plan")
    if plan.issues:
        return CandleRepairResult(symbol=plan.symbol, timeframe=plan.timeframe, received_at=requested_at,
                                  status="UNAVAILABLE", issues=list(plan.issues), requested_limit=0,
                                  remaining_missing_open_times=list(plan.remote_missing_open_times))
    downloaded = []
    try:
        if plan.requested_limit:
            downloaded = await asyncio.wait_for(provider.candles(asset, plan.timeframe, plan.requested_limit, requested_at), timeout_seconds)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return CandleRepairResult(symbol=plan.symbol, timeframe=plan.timeframe, received_at=aware(clock()),
                                  status="UNAVAILABLE", issues=[f"{type(exc).__name__}: {exc}"],
                                  requested_limit=plan.requested_limit,
                                  remaining_missing_open_times=list(plan.remote_missing_open_times))
    received_at = aware(clock())
    if received_at < requested_at:
        return CandleRepairResult(symbol=plan.symbol, timeframe=plan.timeframe, received_at=requested_at,
                                  status="UNAVAILABLE", issues=["repair_clock_regressed"],
                                  requested_limit=plan.requested_limit,
                                  remaining_missing_open_times=list(plan.remote_missing_open_times))
    existing = set(plan.existing_feed_open_times)
    bars = {bar.open_time: bar.model_copy(update={"available_at": received_at}) for bar in plan.local_candles if bar.open_time not in existing}
    issues, seen = [], set()
    for candle in downloaded:
        if not isinstance(candle, Candle):
            issues.append("repair_provider_returned_non_candle")
            continue
        if candle.open_time in seen:
            issues.append("duplicate_repair_candle")
            continue
        seen.add(candle.open_time)
        if candle.open_time in existing:
            # Finalized data remains immutable even if native REST now differs.
            continue
        if not _native_source(candle.source, plan.symbol, plan.timeframe) or candle.source == "binance-public-ws":
            issues.append("repair_provider_source_not_native_spot_instrument")
            continue
        seconds = TIMEFRAMES[plan.timeframe]
        if ((candle.close_time - candle.open_time).total_seconds() != seconds
                or candle.open_time.timestamp() % seconds != 0):
            issues.append("repair_candle_timeframe_or_boundary_invalid")
            continue
        if candle.close_time > requested_at or candle.available_at > received_at:
            issues.append("future_or_unavailable_repair_candle_excluded")
            continue
        if candle.open_time not in bars:
            bars[candle.open_time] = candle.model_copy(update={"available_at": received_at})
    remaining = [opened for opened in plan.remote_missing_open_times if opened not in bars]
    if remaining:
        issues.append("native_repair_did_not_cover_all_missing_intervals")
    return CandleRepairResult(symbol=plan.symbol, timeframe=plan.timeframe,
                              bars=sorted(bars.values(), key=lambda bar: bar.open_time),
                              received_at=received_at, status="PARTIAL" if issues else "REPAIRED",
                              issues=issues, requested_limit=plan.requested_limit,
                              remaining_missing_open_times=remaining)
