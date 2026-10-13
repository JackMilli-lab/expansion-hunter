"""Provider contracts and explicit missing-data helpers.

Live providers retrieve present-day metadata. ``as_of`` is a query boundary,
never permission to backdate the time at which a response became available.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..models import Asset, Candle, Metric, Status, aware


class ProviderError(RuntimeError):
    """A provider failed to return a valid response; no data was fabricated."""

    def __init__(self, source: str, message: str, *, status_code: int | None = None):
        self.source = source
        self.status_code = status_code
        super().__init__(f"{source}: {message}")


@runtime_checkable
class MarketProvider(Protocol):
    async def universe(self, top_n: int, as_of: datetime) -> list[Asset]: ...
    async def market(self, asset: Asset, as_of: datetime) -> dict[str, Metric]: ...
    async def global_metrics(self, as_of: datetime) -> dict[str, Metric]: ...


@runtime_checkable
class SpotProvider(Protocol):
    async def resolve_assets(self, assets: list[Asset]) -> list[Asset]: ...
    async def candles(self, asset: Asset, timeframe: str, limit: int, as_of: datetime) -> list[Candle]: ...
    async def liquidity(self, asset: Asset, as_of: datetime) -> dict[str, Metric]: ...


@runtime_checkable
class DerivativesProvider(Protocol):
    async def metrics(self, asset: Asset, as_of: datetime) -> dict[str, Metric]: ...


def unavailable(source: str, reason: str, unit: str = "") -> Metric:
    return Metric(source=source, reason=reason, unit=unit)


def metric(
    value: object,
    source: str,
    unit: str,
    observed_at: datetime,
    available_at: datetime,
    *,
    nonnegative: bool = False,
) -> Metric:
    """Validate numeric payloads without converting null/boolean values to zero."""
    if value is None or isinstance(value, bool):
        return unavailable(source, "provider did not supply a numeric value", unit)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return unavailable(source, "provider returned a nonnumeric value", unit)
    if not math.isfinite(number) or (nonnegative and number < 0):
        return unavailable(source, "provider returned an invalid numeric value", unit)
    try:
        observed_at, available_at = aware(observed_at), aware(available_at)
    except (AttributeError, TypeError, ValueError):
        return unavailable(source, "provider returned an invalid or naive timestamp", unit)
    if observed_at > available_at:
        return unavailable(source, "provider timestamp is later than retrieval", unit)
    return Metric(
        value=number,
        source=source,
        unit=unit,
        observed_at=observed_at,
        available_at=available_at,
        status=Status.AVAILABLE,
    )
