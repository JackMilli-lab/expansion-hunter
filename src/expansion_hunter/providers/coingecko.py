"""CoinGecko ID-based present-day market metadata (never historical replay)."""

from __future__ import annotations

import math
import os
from datetime import datetime
from typing import Any

from ..config import Settings
from ..models import Asset, Metric, aware
from .base import ProviderError, metric, unavailable
from .http import AsyncHTTPClient, FetchedJSON


def _timestamp(value: object) -> datetime:
    if isinstance(value, str):
        try:
            return aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            pass
    raise ValueError("provider did not supply a valid timezone-aware update timestamp")


def coingecko_headers() -> dict[str, str]:
    headers = {}
    if os.getenv("COINGECKO_DEMO_API_KEY"):
        headers["x-cg-demo-api-key"] = os.environ["COINGECKO_DEMO_API_KEY"]
    if os.getenv("COINGECKO_API_KEY"):
        headers["x-cg-pro-api-key"] = os.environ["COINGECKO_API_KEY"]
    return headers


class CoinGeckoProvider:
    """Pagination uses stable provider IDs, never symbol-only lookups.

    TOTAL2 and TOTAL3 subtract BTC and ETH components of the *same* global
    snapshot using its provider-reported dominance shares. Small rounding
    errors in those shares remain inherited provider measurement error.
    """

    def __init__(self, http: AsyncHTTPClient, settings: Settings):
        self.http = http
        self.settings = settings
        self.issues: list[str] = []
        self._markets: dict[str, tuple[dict[str, Any], datetime]] = {}
        self.headers = coingecko_headers()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> FetchedJSON:
        return await self.http.get_json(
            self.settings.coingecko_url.rstrip("/") + path, params=params, headers=self.headers
        )

    async def universe(self, top_n: int, as_of: datetime) -> list[Asset]:
        aware(as_of)
        if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 10000:
            raise ValueError("universe size must be between 1 and 10000")
        # Universe refreshes are scan boundaries. Removed assets and benchmark
        # lookups must not retain metadata from an earlier watch iteration.
        self._markets.clear()
        assets: dict[str, Asset] = {}
        for page in range(1, (top_n + 249) // 250 + 1):
            try:
                response = await self._get(
                    "/coins/markets",
                    {
                        "vs_currency": "usd",
                        "order": "market_cap_desc",
                        "per_page": 250,
                        "page": page,
                        "sparkline": "false",
                    },
                )
            except ProviderError as exc:
                if not assets:
                    raise
                self.issues.append(
                    f"universe page {page} failed: {exc}; retaining only {len(assets)} "
                    "verified asset IDs from completed pages"
                )
                break
            if not isinstance(response.data, list):
                raise ProviderError("coingecko/coins/markets", "expected a list of markets")
            for record in response.data:
                if not isinstance(record, dict) or not all(
                    isinstance(record.get(k), str) and record[k].strip() for k in ("id", "symbol", "name")
                ):
                    raise ProviderError("coingecko/coins/markets", "market record lacks asset identity")
                asset_id = str(record["id"])
                rank = record.get("market_cap_rank")
                if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
                    self.issues.append(
                        f"{asset_id}: unavailable market-cap rank; excluded from ranked universe"
                    )
                    continue
                # Pagination can drift while market ranks move. ID deduplication
                # avoids scanning a duplicate; report the incomplete universe.
                if asset_id in assets:
                    self.issues.append(f"{asset_id}: duplicate across live market pages")
                    continue
                self._markets[asset_id] = (record, response.retrieved_at)
                if rank <= top_n:
                    assets[asset_id] = Asset(
                        asset_id=asset_id, symbol=record["symbol"].upper(), name=record["name"], rank=rank
                    )
            if len(response.data) < 250:
                break
        ordered = sorted(assets.values(), key=lambda asset: (asset.rank or 10**9, asset.asset_id))[:top_n]
        if len(ordered) < top_n:
            self.issues.append(
                f"live universe returned {len(ordered)} of requested {top_n}; no historical universe implied"
            )
        return ordered

    async def market(self, asset: Asset, as_of: datetime) -> dict[str, Metric]:
        as_of = aware(as_of)
        cached = self._markets.get(asset.asset_id)
        if cached is None or cached[1] < as_of:
            try:
                response = await self._get(
                    "/coins/markets",
                    {
                        "vs_currency": "usd",
                        "ids": asset.asset_id,
                        "sparkline": "false",
                    },
                )
                records = response.data
                if not isinstance(records, list):
                    raise ProviderError("coingecko/coins/markets", "expected a list of markets")
                exact = [r for r in records if isinstance(r, dict) and r.get("id") == asset.asset_id]
                if len(exact) != 1:
                    raise ProviderError("coingecko/coins/markets", "ID lookup returned no unique exact asset")
                self._markets[asset.asset_id] = (exact[0], response.retrieved_at)
            except ProviderError as exc:
                self._markets.pop(asset.asset_id, None)
                self.issues.append(str(exc))
                return self._missing_market(str(exc))
        record, retrieved_at = self._markets[asset.asset_id]
        try:
            observed_at = _timestamp(record.get("last_updated"))
        except ValueError as exc:
            self.issues.append(f"{asset.asset_id}: {exc}")
            return self._missing_market(str(exc))
        fields = {
            "market_cap": ("market_cap", "USD"),
            "circulating_supply": ("circulating_supply", "base asset"),
            "fdv": ("fully_diluted_valuation", "USD"),
            "volume_24h": ("total_volume", "USD; CoinGecko aggregated reported 24h volume"),
            "ath": ("ath", "USD; CoinGecko tracked price ATH"),
            "price": ("current_price", "USD"),
        }
        output = {
            name: metric(
                record.get(field),
                f"coingecko/coins/markets:{asset.asset_id}",
                unit,
                observed_at,
                retrieved_at,
                nonnegative=True,
            )
            for name, (field, unit) in fields.items()
        }
        cap, volume = output["market_cap"], output["volume_24h"]
        if cap.value is not None and cap.value > 0 and volume.value is not None:
            turnover = metric(
                volume.value / cap.value,
                "coingecko/coins/markets:volume/market_cap",
                "fraction per 24h; aggregated reported volume",
                observed_at,
                retrieved_at,
            )
        else:
            turnover = unavailable(
                "coingecko", "turnover needs positive market cap and reported 24h volume", "fraction per 24h"
            )
        output["turnover"] = turnover
        output["turnover_24h"] = turnover.model_copy()
        price, ath = output["price"], output["ath"]
        if price.value is not None and ath.value is not None and 0 < price.value <= ath.value:
            output["distance_ath_pct"] = metric(
                (ath.value / price.value - 1) * 100,
                f"coingecko/coins/markets:{asset.asset_id}:tracked_USD_ATH/current_USD_price",
                "percentage points above current USD price; provider-tracked ATH",
                observed_at,
                retrieved_at,
            )
        else:
            output["distance_ath_pct"] = unavailable(
                "coingecko", "ATH distance requires consistent positive USD price and tracked USD ATH"
            )
        return output

    @staticmethod
    def _missing_market(reason: str) -> dict[str, Metric]:
        return {
            key: unavailable("coingecko", reason)
            for key in (
                "market_cap",
                "circulating_supply",
                "fdv",
                "volume_24h",
                "turnover",
                "turnover_24h",
                "ath",
                "price",
                "distance_ath_pct",
            )
        }

    async def global_metrics(self, as_of: datetime) -> dict[str, Metric]:
        aware(as_of)
        output = {
            key: unavailable("coingecko", "current global snapshot supplies no point-in-time 7d history")
            for key in (
                "btc_dominance_change_7d",
                "eth_btc_return_7d",
                "total_return_7d",
                "total2_return_7d",
                "total3_return_7d",
                "stablecoin_mcap_change_7d",
            )
        }
        output["stablecoin_market_cap"] = unavailable(
            "coingecko", "global endpoint does not report stablecoin capitalization"
        )
        output["stablecoin_liquidity"] = unavailable(
            "coingecko", "no authenticated stablecoin liquidity data source configured"
        )
        global_keys = (
            "btc_dominance",
            "total_market_cap",
            "total2_market_cap",
            "total3_market_cap",
            "eth_btc",
        )
        try:
            response = await self._get("/global")
            data = response.data.get("data") if isinstance(response.data, dict) else None
            if not isinstance(data, dict):
                raise ProviderError("coingecko/global", "expected global data object")
            retrieved = response.retrieved_at
            raw_time = data.get("updated_at")
            try:
                if raw_time is None or isinstance(raw_time, bool) or not math.isfinite(float(raw_time)):
                    raise ValueError("invalid source timestamp")
                observed = datetime.fromtimestamp(float(raw_time), tz=retrieved.tzinfo)
            except (TypeError, ValueError, OSError, OverflowError) as exc:
                raise ProviderError("coingecko/global", "invalid or missing global update timestamp") from exc
            totals = data.get("total_market_cap") or {}
            shares = data.get("market_cap_percentage") or {}
            if not isinstance(totals, dict) or not isinstance(shares, dict):
                raise ProviderError("coingecko/global", "invalid global capitalization fields")
            total = metric(
                totals.get("usd"), "coingecko/global", "USD", observed, retrieved, nonnegative=True
            )
            btc = metric(
                shares.get("btc"),
                "coingecko/global",
                "percentage points",
                observed,
                retrieved,
                nonnegative=True,
            )
            eth = metric(
                shares.get("eth"),
                "coingecko/global",
                "percentage points",
                observed,
                retrieved,
                nonnegative=True,
            )
            if btc.value is not None and btc.value > 100:
                btc = unavailable("coingecko/global", "BTC dominance exceeds 100%", "percentage points")
            if eth.value is not None and eth.value > 100:
                eth = unavailable("coingecko/global", "ETH dominance exceeds 100%", "percentage points")
            output["total_market_cap"] = total
            output["btc_dominance"] = btc
            if total.value is not None and btc.value is not None and 0 <= btc.value <= 100:
                output["total2_market_cap"] = metric(
                    total.value * (1 - btc.value / 100),
                    "coingecko/global:total*(1-btc_share), same snapshot",
                    "USD",
                    observed,
                    retrieved,
                    nonnegative=True,
                )
            else:
                output["total2_market_cap"] = unavailable(
                    "coingecko/global", "TOTAL2 needs total and valid BTC share"
                )
            if (
                total.value is not None
                and btc.value is not None
                and eth.value is not None
                and 0 <= btc.value + eth.value <= 100
            ):
                output["total3_market_cap"] = metric(
                    total.value * (1 - (btc.value + eth.value) / 100),
                    "coingecko/global:total*(1-btc_share-eth_share), same snapshot",
                    "USD",
                    observed,
                    retrieved,
                    nonnegative=True,
                )
            else:
                output["total3_market_cap"] = unavailable(
                    "coingecko/global", "TOTAL3 needs total and valid BTC/ETH shares"
                )
            # Both conversion rates in the single global cap object describe the
            # same total, so total-in-BTC / total-in-ETH is ETH price / BTC price.
            cap_btc = metric(
                totals.get("btc"), "coingecko/global", "BTC", observed, retrieved, nonnegative=True
            )
            cap_eth = metric(
                totals.get("eth"), "coingecko/global", "ETH", observed, retrieved, nonnegative=True
            )
            if (
                cap_btc.value is not None
                and cap_btc.value > 0
                and cap_eth.value is not None
                and cap_eth.value > 0
            ):
                output["eth_btc"] = metric(
                    cap_btc.value / cap_eth.value,
                    "coingecko/global:total_in_btc/total_in_eth, same snapshot",
                    "BTC per ETH",
                    observed,
                    retrieved,
                )
            else:
                output["eth_btc"] = unavailable(
                    "coingecko/global", "ETH/BTC needs same-snapshot BTC and ETH conversions"
                )
        except ProviderError as exc:
            self.issues.append(str(exc))
            output.update({key: unavailable("coingecko/global", str(exc)) for key in global_keys})
        return output
