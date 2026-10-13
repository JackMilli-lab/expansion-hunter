"""Bounded GET retries, environment proxy trust, and per-host rate limits."""

from __future__ import annotations

import asyncio
import math
import os
import random
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import Settings
from ..models import utcnow
from .base import ProviderError


@dataclass(frozen=True)
class FetchedJSON:
    data: Any
    retrieved_at: datetime
    url: str = ""


class _HostLimiter:
    def __init__(self, interval: float):
        self.interval = interval
        self.next_request = 0.0
        self.lock = asyncio.Lock()

    async def acquire(self, weight: int = 1) -> float:
        async with self.lock:
            delay = self.next_request - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.next_request = time.monotonic() + self.interval * weight
            return max(0.0, delay)


def _ssl_context() -> ssl.SSLContext:
    # Honour the managed environment CA, and requests-compatible deployment
    # variables when a corporate proxy uses its own root certificate.
    ca = os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE")
    return ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()


class AsyncHTTPClient:
    """A reusable GET-only client; never retries a mutating request.

    CoinGecko public API is limited to 25 requests/minute conservatively.
    Binance requests are capped at 20 request-weight units/second; providers
    pass native endpoint weights. Shared instances share host limiters.
    Long Retry-After values fail explicitly rather than ignoring the server.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        host_intervals: dict[str, float] | None = None,
        max_retry_wait: float = 30.0,
    ):
        settings = settings or Settings()
        if not math.isfinite(max_retry_wait) or max_retry_wait <= 0:
            raise ValueError("maximum retry wait must be finite and positive")
        self.retries = settings.http_retries
        self.max_retry_wait = max_retry_wait
        self.host_intervals = host_intervals or {}
        self._limiters: dict[str, _HostLimiter] = {}
        self._request_count = 0
        self._request_weight = 0
        self._rate_limit_wait_seconds = 0.0
        self._retry_wait_seconds = 0.0
        self._host_diagnostics: dict[str, dict[str, Any]] = {}
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout),
            limits=httpx.Limits(
                max_connections=settings.concurrency, max_keepalive_connections=settings.concurrency
            ),
            transport=transport,
            trust_env=True,
            verify=_ssl_context(),
            follow_redirects=False,
            headers={"User-Agent": "expansion-hunter/0.1", "Accept": "application/json"},
        )

    async def __aenter__(self) -> AsyncHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Session counters include failed attempts and configured native weights.

        Wait counters are allocated limiter/retry delay seconds, rather than an
        assertion about the provider's quota. Native headers report observed
        server usage when supplied; absence remains explicit.
        """
        return {
            "request_count": self._request_count,
            "request_weight": self._request_weight,
            "rate_limit_wait_seconds": self._rate_limit_wait_seconds,
            "retry_wait_seconds": self._retry_wait_seconds,
            "per_host": {host: dict(values) for host, values in self._host_diagnostics.items()},
        }

    async def _retry_sleep(self, seconds: float) -> None:
        self._retry_wait_seconds += seconds
        await asyncio.sleep(seconds)

    def _limiter(self, url: str) -> _HostLimiter:
        host = urlsplit(url).netloc
        if host not in self._limiters:
            default = 2.4 if "coingecko" in host else 0.05
            interval = self.host_intervals.get(host, default)
            if not math.isfinite(interval) or interval < 0:
                raise ValueError("rate-limit interval must be finite and nonnegative")
            self._limiters[host] = _HostLimiter(interval)
        return self._limiters[host]

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After")
        if header:
            try:
                wait = float(header)
            except ValueError:
                try:
                    date = parsedate_to_datetime(header)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=UTC)
                    wait = (date - utcnow()).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    raise ProviderError(str(response.url), "invalid Retry-After header") from None
            if not math.isfinite(wait):
                raise ProviderError(str(response.url), "invalid Retry-After header")
            if wait > self.max_retry_wait:
                raise ProviderError(
                    str(response.url),
                    "Retry-After exceeds bounded retry budget",
                    status_code=response.status_code,
                )
            return max(0.0, wait)
        return min(self.max_retry_wait, random.uniform(0.25, min(8.0, 0.5 * 2**attempt)))

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        weight: int = 1,
    ) -> FetchedJSON:
        if isinstance(weight, bool) or not isinstance(weight, int) or weight < 1:
            raise ValueError("request weight must be a positive integer")
        source = f"{urlsplit(url).netloc}{urlsplit(url).path}"
        for attempt in range(self.retries + 1):
            self._rate_limit_wait_seconds += await self._limiter(url).acquire(weight)
            self._request_count += 1
            self._request_weight += weight
            host = urlsplit(url).netloc
            host_stats = self._host_diagnostics.setdefault(
                host, {"request_count": 0, "request_weight": 0, "used_weight_1m": None}
            )
            host_stats["request_count"] += 1
            host_stats["request_weight"] += weight
            try:
                response = await self._client.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    raise ProviderError(source, f"transport failure ({type(exc).__name__})") from exc
                await self._retry_sleep(
                    min(self.max_retry_wait, random.uniform(0.25, min(8.0, 0.5 * 2**attempt)))
                )
                continue
            header = response.headers.get("X-MBX-USED-WEIGHT-1M")
            if header is not None:
                try:
                    usage = int(header)
                    if usage >= 0:
                        host_stats["used_weight_1m"] = usage
                except ValueError:
                    pass
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == self.retries:
                    raise ProviderError(
                        source, "GET retry budget exhausted", status_code=response.status_code
                    )
                await self._retry_sleep(self._retry_after(response, attempt))
                continue
            if not response.is_success:
                raise ProviderError(source, "GET rejected", status_code=response.status_code)
            retrieved_at = utcnow()
            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderError(source, "response is not valid JSON") from exc
            return FetchedJSON(data=data, retrieved_at=retrieved_at, url=str(response.url))
        raise AssertionError("unreachable retry state")


HTTPClient = AsyncHTTPClient
