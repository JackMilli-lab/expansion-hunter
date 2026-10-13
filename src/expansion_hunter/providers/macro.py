"""Public macro-provider boundary and authoritative BLS calendar adapter.

The official calendar supplies schedules, not market consensus or numeric
initial releases. Those channels remain UNAVAILABLE. Calendar receipts carry
today's retrieval time; this adapter does not create historical availability.
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import httpx

from ..macro import scheduled_utc
from ..macro_models import MacroEvent, MacroProviderReport, MacroQuality
from ..models import aware, utcnow
from .http import _ssl_context


@runtime_checkable
class MacroProvider(Protocol):
    async def calendar(self, start: datetime, end: datetime) -> MacroProviderReport: ...
    async def releases(self, event_ids: list[str]) -> MacroProviderReport: ...


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def parse_bls_calendar(text: str, retrieved_at: datetime, source: str) -> tuple[list[MacroEvent], list[str]]:
    """Parse timed VEVENTs; uncertain time zones/all-day events are rejected visibly."""
    retrieved_at = aware(retrieved_at)
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    if "BEGIN:VCALENDAR" not in unfolded or "END:VCALENDAR" not in unfolded:
        raise ValueError("official calendar response is not an iCalendar document")
    records: list[dict[str, tuple[str, str]]] = []
    current = None
    for raw in unfolded.splitlines():
        line = raw.strip()
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current is not None:
                records.append(current)
            current = None
        elif current is not None and ":" in line:
            head, value = line.split(":", 1)
            key = head.split(";", 1)[0].upper()
            if key in current:
                # Repeated identity/time fields are ambiguous, not silently overwritten.
                if key in {"UID", "SUMMARY", "DTSTART"}:
                    current["INVALID"] = ("", f"repeated {key}")
            else:
                current[key] = (head, value)
    events, issues = [], []
    ids = set()
    for index, record in enumerate(records):
        try:
            if "INVALID" in record:
                raise ValueError(record["INVALID"][1])
            _, uid = record["UID"]
            _, summary = record["SUMMARY"]
            head, value = record["DTSTART"]
            if "VALUE=DATE" in head.upper() or len(value) == 8:
                raise ValueError("all-day release has no authoritative intraday release time")
            if value.endswith("Z"):
                scheduled = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            else:
                match = re.search(r"(?:^|;)TZID=([^;]+)", head, re.IGNORECASE)
                if not match:
                    raise ValueError("floating release time has no authoritative timezone")
                zone = match.group(1).strip('"')
                local = datetime.strptime(value, "%Y%m%dT%H%M%S")
                scheduled = scheduled_utc(local, zone)
            summary = _unescape(summary)
            category = "OTHER_BLS_RELEASE"
            importance = 2
            lower = summary.lower()
            for phrase, category_name in (
                ("consumer price index", "US_CPI"),
                ("employment situation", "US_NFP"),
                ("producer price index", "US_PPI"),
                ("job openings and labor turnover", "US_JOLTS"),
            ):
                if phrase in lower:
                    category, importance = category_name, 3
                    break
            event_id = "BLS:" + uid
            if event_id in ids:
                raise ValueError("duplicate calendar event identity")
            ids.add(event_id)
            events.append(
                MacroEvent(
                    event_id=event_id,
                    event_name=summary,
                    country="US",
                    category=category,
                    importance=importance,
                    scheduled_timestamp=scheduled,
                    available_at=retrieved_at,
                    source=source,
                    source_quality="AUTHORITATIVE",
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(f"calendar record {index}: {exc}")
    return sorted(events, key=lambda event: (event.scheduled_timestamp, event.event_id)), issues


class BLSCalendarProvider:
    """Bounded GET-only adapter. Never scrapes guesses for consensus or actual data."""

    URL = "https://www.bls.gov/schedule/news_release/bls.ics"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        retries: int = 2,
        request_timeout_seconds: float = 20,
        clock=utcnow,
    ):
        if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 5:
            raise ValueError("retries must be between zero and five")
        if not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0:
            raise ValueError("request timeout must be finite and positive")
        self._owned = client is None
        self.client = client or httpx.AsyncClient(
            timeout=15,
            verify=_ssl_context(),
            trust_env=True,
            follow_redirects=False,
            headers={"User-Agent": "expansion-hunter/0.1 public macro calendar", "Accept": "text/calendar"},
        )
        self.retries, self.clock = retries, clock
        self.request_timeout_seconds = request_timeout_seconds

    async def aclose(self) -> None:
        if self._owned:
            await self.client.aclose()

    async def __aenter__(self) -> BLSCalendarProvider:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def calendar(self, start: datetime, end: datetime) -> MacroProviderReport:
        start, end = aware(start), aware(end)
        if end < start:
            raise ValueError("calendar end precedes start")
        issues = ["BLS schedule does not supply consensus or numeric initial/revised releases."]
        for attempt in range(self.retries + 1):
            try:
                # HTTP per-stage timeouts alone do not cap the whole operation.
                async with asyncio.timeout(self.request_timeout_seconds):
                    response = await self.client.get(self.URL)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.retries:
                        # Respect short Retry-After; larger wait fails without hammering the source.
                        retry_after = response.headers.get("Retry-After")
                        try:
                            delay = float(retry_after) if retry_after else 0.25 * 2**attempt
                        except ValueError:
                            delay = 2.0
                        if not math.isfinite(delay) or delay < 0 or delay > 5:
                            raise ValueError("macro Retry-After outside bounded retry budget")
                        await asyncio.sleep(delay)
                        continue
                response.raise_for_status()
                retrieved = aware(self.clock())
                events, parse_issues = parse_bls_calendar(response.text, retrieved, self.URL)
                return MacroProviderReport(
                    provider="BLS_OFFICIAL_CALENDAR",
                    retrieved_at=retrieved,
                    events=[event for event in events if start <= event.scheduled_timestamp <= end],
                    calendar_quality=MacroQuality.AVAILABLE if events else MacroQuality.UNAVAILABLE,
                    issues=issues
                    + parse_issues
                    + ([] if events else ["No timed release events were parsed."]),
                )
            except httpx.TransportError as exc:
                if attempt < self.retries:
                    await asyncio.sleep(0.25 * 2**attempt)
                    continue
                issues.append(f"official calendar transport failure: {type(exc).__name__}")
            except TimeoutError:
                if attempt < self.retries:
                    await asyncio.sleep(0.25 * 2**attempt)
                    continue
                issues.append("official calendar total request timeout; calendar unavailable")
            except httpx.HTTPStatusError as exc:
                issues.append(f"official calendar HTTP {exc.response.status_code}; calendar unavailable")
            except ValueError as exc:
                issues.append(f"official calendar validation failure: {exc}")
            break
        return MacroProviderReport(provider="BLS_OFFICIAL_CALENDAR", retrieved_at=self.clock(), issues=issues)

    async def releases(self, event_ids: list[str]) -> MacroProviderReport:
        return MacroProviderReport(
            provider="BLS_OFFICIAL_CALENDAR",
            retrieved_at=self.clock(),
            issues=[
                "This authoritative schedule adapter does not provide numeric real-time releases, revisions or consensus. Those channels are UNAVAILABLE."
            ],
        )
