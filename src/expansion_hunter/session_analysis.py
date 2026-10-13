"""Point-in-time crypto session evidence, not a stock-market opening calendar.

Local windows are explicit research conventions. London/New York boundaries use
IANA DST. Observed highs/lows are structural references, not actual stop orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import Candle, aware


@dataclass(frozen=True)
class SessionSpec:
    name: str
    timezone: str
    start_hour: int
    end_hour: int


DEFAULT_SESSIONS = (
    SessionSpec("ASIA", "Asia/Tokyo", 9, 17),
    SessionSpec("LONDON", "Europe/London", 8, 16),
    SessionSpec("NEW_YORK", "America/New_York", 8, 17),
)


def session_bounds(day, spec: SessionSpec) -> tuple[datetime, datetime]:
    zone = ZoneInfo(spec.timezone)
    if not 0 <= spec.start_hour < 24 or not 0 <= spec.end_hour < 24 or spec.start_hour == spec.end_hour:
        raise ValueError("Session hours must define a nonempty local window")
    start = datetime.combine(day, time(spec.start_hour), zone)
    end_day = day + timedelta(days=1) if spec.end_hour < spec.start_hour else day
    end = datetime.combine(end_day, time(spec.end_hour), zone)
    return aware(start), aware(end)


def analyze_sessions(candles: list[Candle], as_of: datetime, *, specs=DEFAULT_SESSIONS,
                     days: int = 3, level_buffer_pct: float = 0) -> dict:
    now = aware(as_of)
    if not 1 <= days <= 14 or not 0 <= level_buffer_pct <= 0.05:
        raise ValueError("Invalid bounded session research settings")
    bars = sorted((bar for bar in candles if bar.close_time <= now and bar.available_at <= now),
                  key=lambda bar: (bar.open_time, bar.available_at))
    if len({bar.open_time for bar in bars}) != len(bars):
        raise ValueError("Duplicate candle openings are ambiguous session evidence")
    base = {"as_of": now.isoformat(), "sessions": [], "session_events": [], "sequences": [],
            "production_weight": 0, "research_status": "UNVALIDATED", "liquidity_type": "HISTORICAL_STRUCTURAL",
            "source": "CLOSED_CANDLE_DERIVED", "data_quality": "UNAVAILABLE"}
    if not bars:
        return {**base, "reason": "CLOSED_CANDLES_UNAVAILABLE"}
    duration = round((bars[0].close_time - bars[0].open_time).total_seconds())
    if duration <= 0 or any(abs((bar.close_time - bar.open_time).total_seconds() - duration) > 0.01 for bar in bars):
        raise ValueError("Session analysis requires a consistent candle timeframe")
    summaries = []
    for spec in specs:
        local_day = now.astimezone(ZoneInfo(spec.timezone)).date()
        for offset in range(days - 1, -1, -1):
            day = local_day - timedelta(days=offset)
            start, end = session_bounds(day, spec)
            if start > now:
                continue
            selected = [bar for bar in bars if start <= bar.open_time < end and bar.close_time <= end]
            effective_end = min(end, now)
            expected = max(0, int((effective_end - start).total_seconds() // duration))
            coverage = min(1.0, len(selected) / expected) if expected else 0.0
            quantity = sum(bar.volume for bar in selected) if selected else None
            quote = sum(bar.quote_volume for bar in selected) if selected and all(
                bar.quote_volume is not None for bar in selected) else None
            vwap = (quote / quantity if quote is not None else sum(
                ((bar.high + bar.low + bar.close) / 3) * bar.volume for bar in selected) / quantity
                ) if quantity else None
            summaries.append({"session": spec.name, "session_date": day.isoformat(), "timezone": spec.timezone,
                "start": start.isoformat(), "end": end.isoformat(), "completed": now >= end,
                "available_at": max((bar.available_at for bar in selected), default=now).isoformat(),
                "open": selected[0].open if selected else None, "close": selected[-1].close if selected else None,
                "high": max((bar.high for bar in selected), default=None),
                "low": min((bar.low for bar in selected), default=None), "volume": quantity, "quote_volume": quote,
                "vwap": vwap, "vwap_quality": "NATIVE_VOLUME_RATIO" if quote is not None and quantity else
                    "ESTIMATED_CANDLE_TYPICAL_PRICE" if vwap is not None else "UNAVAILABLE",
                "bars": len(selected), "expected_bars": expected, "coverage": coverage,
                "quality": "AVAILABLE" if expected and len(selected) == expected else "PARTIAL" if selected else "UNAVAILABLE",
                "source": sorted({bar.source for bar in selected})})
    events = []
    for reference in summaries:
        if not reference["completed"] or reference["high"] is None:
            continue
        end = datetime.fromisoformat(reference["end"])
        known = max(end, datetime.fromisoformat(reference["available_at"]))
        for bar in bars:
            if bar.open_time < end or bar.available_at < known:
                continue
            # Only the first next-day window is relevant; levels do not remain
            # permanent/session-independent resistance lines.
            if bar.open_time >= end + timedelta(hours=24):
                continue
            for side, level in (("HIGH", reference["high"]), ("LOW", reference["low"])):
                swept = bar.high > level * (1 + level_buffer_pct) if side == "HIGH" else bar.low < level * (1 - level_buffer_pct)
                if not swept:
                    continue
                accepted = bar.close > level if side == "HIGH" else bar.close < level
                event_type = ("BREAKOUT_ACCEPTANCE" if side == "HIGH" else "BREAKDOWN_ACCEPTANCE") if accepted else "SWEEP_RECLAIM"
                active = [spec.name for spec in specs if session_bounds(bar.open_time.astimezone(
                    ZoneInfo(spec.timezone)).date(), spec)[0] <= bar.open_time < session_bounds(
                    bar.open_time.astimezone(ZoneInfo(spec.timezone)).date(), spec)[1]]
                events.append({"event": event_type, "side": side, "level": level,
                    "reference_session": reference["session"], "reference_date": reference["session_date"],
                    "reference_discovered_at": known.isoformat(), "active_sessions": active,
                    "event_at": bar.close_time.isoformat(), "available_at": bar.available_at.isoformat(),
                    "close": bar.close, "volume": bar.volume, "reference_quality": reference["quality"],
                    "interpretation": "Observable candle reaction; stop orders and future outcome are unknown"})
                break
            if events and events[-1]["reference_session"] == reference["session"] and events[-1]["reference_date"] == reference["session_date"]:
                break  # First known reaction per completed session; no alert spam.
    events.sort(key=lambda event: (event["available_at"], event["reference_session"]))
    sequences = []
    for first, second in zip(events, events[1:], strict=False):
        if first["event"] == "SWEEP_RECLAIM" and second["event"] in {"BREAKOUT_ACCEPTANCE", "BREAKDOWN_ACCEPTANCE"}:
            sequences.append({"type": "SESSION_SWEEP_THEN_STRUCTURE_BREAK", "first": first, "second": second,
                              "research_status": "UNVALIDATED", "production_weight": 0})
    return {**base, "data_quality": "AVAILABLE" if any(item["quality"] == "AVAILABLE" for item in summaries) else "PARTIAL",
            "sessions": summaries, "session_events": events, "sequences": sequences}
