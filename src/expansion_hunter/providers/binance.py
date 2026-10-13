"""Exchange-native Binance spot and USD-M perpetual observations.

Quotes are USDT, not USD: dollar-labelled liquidity controls use explicitly
documented USDT-equivalent notionals and do not assert a stablecoin peg.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..config import Settings
from ..models import TIMEFRAMES, Asset, Candle, Metric, aware
from .base import ProviderError, metric, unavailable
from .coingecko import coingecko_headers
from .http import AsyncHTTPClient, FetchedJSON


def _millis(value: object) -> datetime:
    if value is None or isinstance(value, bool):
        raise ValueError("missing exchange timestamp")
    return datetime.fromtimestamp(float(value) / 1000, UTC)


def _finite(value: object, *, positive: bool = False) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError("missing numeric exchange value")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError("invalid numeric exchange value")
    return result


class BinanceSpotProvider:
    def __init__(self, http: AsyncHTTPClient, settings: Settings):
        self.http = http
        self.settings = settings
        self.issues: list[str] = []
        self._symbols: dict[str, dict[str, Any]] | None = None
        self._active_spot_url = settings.binance_spot_url.rstrip("/")
        self._identity_refreshed_at: float | None = None
        self._identity_counts: Counter[str] | None = None
        self._identity_ids: dict[str, str] = {}
        self._identity_issue: str | None = None
        self._identity_lock = asyncio.Lock()

    @property
    def active_spot_url(self) -> str:
        return self._active_spot_url

    async def _get(self, path: str, params: dict[str, Any] | None = None, *, weight: int = 1) -> FetchedJSON:
        """Try explicitly configured native origins and retain the working host.

        Rate-limit and invalid-request responses never cause host rotation.
        Each failed attempt and any fallback activation remains observable.
        """
        origins = list(
            dict.fromkeys(
                [
                    self._active_spot_url,
                    self.settings.binance_spot_url.rstrip("/"),
                    *self.settings.binance_spot_fallback_urls,
                ]
            )
        )
        last_error: ProviderError | None = None
        for origin in origins:
            try:
                response = await self.http.get_json(origin + path, params=params, weight=weight)
            except ProviderError as exc:
                last_error = exc
                if len(origins) > 1:
                    self.issues.append(f"spot host unavailable {origin}: {exc}")
                if (
                    exc.status_code is not None
                    and exc.status_code not in (403, 451)
                    and exc.status_code < 500
                ):
                    raise
                continue
            if origin != self._active_spot_url:
                self.issues.append(f"spot fallback activated: {self._active_spot_url} -> {origin}")
                self._active_spot_url = origin
            return response
        assert last_error is not None
        raise last_error

    def _mapping(self) -> dict[str, str]:
        if not self.settings.symbol_map_path:
            return {}
        try:
            data = json.loads(Path(self.settings.symbol_map_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProviderError("symbol_map", "cannot read explicit asset-ID symbol mapping") from exc
        if not isinstance(data, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not k or not v for k, v in data.items()
        ):
            raise ProviderError("symbol_map", "expected JSON object {coingecko_id: BINANCE_SYMBOL}")
        symbols = [value.upper() for value in data.values()]
        if len(set(symbols)) != len(symbols):
            raise ProviderError("symbol_map", "two asset IDs cannot map to the same exchange instrument")
        return {key: value.upper() for key, value in data.items()}

    async def resolve_assets(self, assets: list[Asset]) -> list[Asset]:
        explicit = self._mapping()
        async with self._identity_lock:
            now = time.monotonic()
            if (
                self._identity_refreshed_at is None
                or now - self._identity_refreshed_at >= self.settings.identity_cache_ttl_seconds
            ):
                await self._refresh_identity()
                self._identity_refreshed_at = time.monotonic()
            elif self._identity_issue:
                self.issues.append(self._identity_issue)
        assert self._symbols is not None
        base_counts = Counter(str(row.get("baseAsset", "")).upper() for row in self._symbols.values())
        counts, listing_ids = self._identity_counts, self._identity_ids
        output = []
        used: set[str] = set()
        for asset in assets:
            selected = explicit.get(asset.asset_id)
            reason = "no exact unique spot instrument"
            if selected:
                if selected not in self._symbols:
                    reason = "explicit mapping is not a currently trading USDT spot instrument"
                    selected = None
            else:
                name = asset.name.lower()
                transformed = any(
                    term in name for term in ("wrapped", "bridged", "binance-peg", "wormhole", "staked")
                )
                if counts is None:
                    reason = "full identity list unavailable; explicit mapping required"
                elif asset.asset_id not in listing_ids or listing_ids[asset.asset_id] != asset.symbol.upper():
                    reason = "asset ID/symbol not confirmed in full identity list; explicit mapping required"
                elif counts[asset.symbol.upper()] != 1:
                    reason = "globally ambiguous symbol; explicit asset-ID mapping required"
                elif transformed:
                    reason = "wrapped/bridged/staked identity requires explicit asset-ID mapping"
                elif base_counts[asset.symbol.upper()] == 1:
                    candidate = asset.symbol.upper() + "USDT"
                    if (
                        candidate in self._symbols
                        and self._symbols[candidate].get("baseAsset") == asset.symbol.upper()
                    ):
                        selected = candidate
            if selected in used:
                reason = "instrument already assigned to another asset ID"
                selected = None
            if selected:
                used.add(selected)
            else:
                self.issues.append(f"{asset.asset_id}: unmapped ({reason})")
            output.append(asset.model_copy(update={"exchange_symbol": selected, "quote_asset": "USDT"}))
        return output

    async def _refresh_identity(self) -> None:
        """Cache only current verified identities; failures remain explicit."""
        exchange = await self._get("/api/v3/exchangeInfo", weight=20)
        rows = exchange.data.get("symbols") if isinstance(exchange.data, dict) else None
        if not isinstance(rows, list):
            raise ProviderError("binance/exchangeInfo", "expected symbol definitions")
        self._symbols = {
            str(row["symbol"]): row
            for row in rows
            if isinstance(row, dict)
            and row.get("symbol")
            and row.get("status") == "TRADING"
            and row.get("quoteAsset") == "USDT"
            and row.get("isSpotTradingAllowed", True)
        }
        counts: Counter[str] | None = None
        listing_ids: dict[str, str] = {}
        self._identity_issue = None
        try:
            listing = await self.http.get_json(
                self.settings.coingecko_url.rstrip("/") + "/coins/list",
                params={"include_platform": "false"},
                headers=coingecko_headers(),
            )
            if not isinstance(listing.data, list):
                raise ProviderError("coingecko/coins/list", "expected complete identity list")
            seen_ids: set[str] = set()
            empty_tickers = 0
            for row in listing.data:
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get("id"), str)
                    or not row["id"].strip()
                    or not isinstance(row.get("symbol"), str)
                ):
                    raise ProviderError("coingecko/coins/list", "invalid identity record")
                if row["id"] in seen_ids:
                    raise ProviderError(
                        "coingecko/coins/list", "duplicate asset ID in complete identity list"
                    )
                seen_ids.add(row["id"])
                # CoinGecko legitimately lists assets with explicit empty
                # tickers. They cannot collide with a nonempty exchange ticker;
                # keep other identities usable and report the skipped records.
                if not row["symbol"].strip():
                    empty_tickers += 1
                    continue
                listing_ids[str(row["id"])] = str(row["symbol"]).upper()
            if empty_tickers:
                self.issues.append(
                    f"coingecko/coins/list: {empty_tickers} asset IDs have empty tickers; "
                    "excluded individually from automatic identity matching"
                )
            counts = Counter(listing_ids.values())
        except ProviderError as exc:
            self._identity_issue = (
                f"{exc}; automatic symbol matching disabled; explicit mappings remain usable"
            )
            self.issues.append(self._identity_issue)
        self._identity_counts, self._identity_ids = counts, listing_ids

    @staticmethod
    def _symbol(asset: Asset) -> str:
        if not asset.exchange_symbol or asset.quote_asset != "USDT":
            raise ProviderError("binance/spot", f"{asset.asset_id} has no verified USDT exchange identity")
        return asset.exchange_symbol

    async def light_market(self, assets: list[Asset], as_of: datetime) -> dict[str, dict[str, Metric]]:
        """Two native requests cover all verified spot instruments, without depth.

        Binance all-symbol 24h ticker costs 80 request-weight units; all-symbol
        bookTicker costs 4. Relative volume and compression are deliberately
        absent: rolling 24h volume alone cannot establish a causal baseline.
        bookTicker has no event timestamp; its retrieval snapshot is explicitly
        identified rather than pretending to have a native exchange event time.
        """
        aware(as_of)
        keys = (
            "price",
            "price_change_24h_pct",
            "quote_volume_24h",
            "volume_24h",
            "base_volume_24h",
            "high_24h",
            "low_24h",
            "trade_count",
            "best_bid",
            "best_ask",
            "book_mid",
            "spread_bps",
        )
        output = {
            asset.asset_id: {
                key: unavailable("binance/spot/light", "no verified exchange symbol") for key in keys
            }
            for asset in assets
        }
        verified = [asset for asset in assets if asset.exchange_symbol and asset.quote_asset == "USDT"]
        if not verified:
            return output
        responses = await asyncio.gather(
            self._get("/api/v3/ticker/24hr", weight=80),
            self._get("/api/v3/ticker/bookTicker", weight=4),
            return_exceptions=True,
        )

        def index_rows(response: FetchedJSON | BaseException, endpoint: str) -> tuple[dict[str, Any], str]:
            if isinstance(response, BaseException):
                if not isinstance(response, ProviderError):
                    raise response
                self.issues.append(str(response))
                return {}, str(response)
            if not isinstance(response.data, list):
                reason = f"{endpoint}: expected all-symbol ticker array"
                self.issues.append(reason)
                return {}, reason
            indexed: dict[str, Any] = {}
            duplicates: set[str] = set()
            malformed = 0
            for row in response.data:
                if not isinstance(row, dict) or not isinstance(row.get("symbol"), str) or not row["symbol"]:
                    malformed += 1
                    continue
                symbol = row["symbol"]
                if symbol in indexed:
                    duplicates.add(symbol)
                indexed[symbol] = row
            for symbol in duplicates:
                indexed.pop(symbol, None)
                self.issues.append(f"{endpoint}: duplicate {symbol}; instrument data unavailable")
            if malformed:
                self.issues.append(f"{endpoint}: {malformed} malformed rows excluded individually")
            return indexed, f"{endpoint}: symbol absent or duplicate in native bulk response"

        ticker, book = responses
        ticker_rows, ticker_reason = index_rows(ticker, "binance/spot/ticker24h")
        book_rows, book_reason = index_rows(book, "binance/spot/bookTicker")
        fields = {
            "price": ("lastPrice", "USDT per base asset", True),
            "price_change_24h_pct": ("priceChangePercent", "percent over rolling native 24h", False),
            "quote_volume_24h": ("quoteVolume", "USDT per rolling 24h; native spot venue", True),
            "volume_24h": ("quoteVolume", "USDT per rolling 24h; native spot venue", True),
            "base_volume_24h": ("volume", "base asset per rolling 24h", True),
            "high_24h": ("highPrice", "USDT per base asset", True),
            "low_24h": ("lowPrice", "USDT per base asset", True),
            "trade_count": ("count", "number of native spot trades per rolling 24h", True),
        }
        for asset in verified:
            assert asset.exchange_symbol is not None
            symbol = asset.exchange_symbol
            values = output[asset.asset_id]
            row = ticker_rows.get(symbol)
            if row is None or isinstance(ticker, BaseException):
                for key in fields:
                    values[key] = unavailable("binance/spot/ticker24h", ticker_reason)
            else:
                try:
                    observed = _millis(row["closeTime"])
                    source = f"binance/spot/{symbol}/ticker24h@{ticker.url or self._active_spot_url}"
                    for key, (field, unit, nonnegative) in fields.items():
                        values[key] = metric(
                            row.get(field),
                            source,
                            unit,
                            observed,
                            ticker.retrieved_at,
                            nonnegative=nonnegative,
                        )
                    if values["price"].value is not None and values["price"].value <= 0:
                        values["price"] = unavailable(source, "native price must be positive")
                except (ValueError, TypeError, KeyError, OSError, OverflowError):
                    for key in fields:
                        values[key] = unavailable("binance/spot/ticker24h", "invalid native ticker timestamp")
            row = book_rows.get(symbol)
            book_keys = ("best_bid", "best_ask", "book_mid", "spread_bps")
            if row is None or isinstance(book, BaseException):
                for key in book_keys:
                    values[key] = unavailable("binance/spot/bookTicker", book_reason)
            else:
                try:
                    bid, ask = (
                        _finite(row["bidPrice"], positive=True),
                        _finite(row["askPrice"], positive=True),
                    )
                    if ask <= bid:
                        raise ValueError("crossed or locked native book")
                    mid = (bid + ask) / 2
                    source = f"binance/spot/{symbol}/bookTicker:retrieval_snapshot@{book.url or self._active_spot_url}"
                    for key, value, unit in (
                        ("best_bid", bid, "USDT per base asset"),
                        ("best_ask", ask, "USDT per base asset"),
                        ("book_mid", mid, "USDT per base asset"),
                        ("spread_bps", (ask - bid) / mid * 10000, "basis points"),
                    ):
                        values[key] = metric(value, source, unit, book.retrieved_at, book.retrieved_at)
                except (ValueError, TypeError, KeyError, OSError, OverflowError):
                    self.issues.append(f"{asset.asset_id}: invalid native bulk bookTicker payload")
                    for key in book_keys:
                        values[key] = unavailable(
                            "binance/spot/bookTicker", "invalid native bulk bookTicker payload"
                        )
        return output

    async def candles(self, asset: Asset, timeframe: str, limit: int, as_of: datetime) -> list[Candle]:
        as_of = aware(as_of)
        if timeframe not in TIMEFRAMES:
            raise ValueError("unsupported timeframe")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("candle limit must be between 1 and 1000")
        symbol = self._symbol(asset)
        interval = {"1D": "1d", "1W": "1w"}.get(timeframe, timeframe)
        end = int(as_of.timestamp() * 1000) - 1
        candles: dict[datetime, Candle] = {}
        # At most two pages: one currently forming bar may be excluded from a
        # full 1000-row response. Each page retains its own retrieval timestamp.
        for _ in range(2):
            response = await self._get(
                "/api/v3/klines",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "limit": min(1000, limit - len(candles) + 1),
                    "endTime": end,
                },
                weight=2,
            )
            if not isinstance(response.data, list):
                raise ProviderError("binance/klines", "expected an array of candles")
            cutoff = min(as_of, response.retrieved_at)
            oldest: datetime | None = None
            for row in response.data:
                try:
                    if not isinstance(row, list) or len(row) < 11:
                        raise ValueError("incomplete kline record")
                    open_time = _millis(row[0])
                    close_time = _millis(row[6]) + timedelta(milliseconds=1)
                    if (close_time - open_time).total_seconds() != TIMEFRAMES[timeframe]:
                        raise ValueError("kline duration differs from requested timeframe")
                    # Binance weekly bars start Monday UTC; all other native
                    # intervals are aligned to the Unix epoch in UTC.
                    offset = 4 * 86400 if timeframe == "1W" else 0
                    if (open_time.timestamp() - offset) % TIMEFRAMES[timeframe] != 0:
                        raise ValueError("kline open is not aligned to requested timeframe")
                    oldest = min(oldest, open_time) if oldest else open_time
                    if close_time > cutoff:
                        continue
                    candle = Candle(
                        open_time=open_time,
                        close_time=close_time,
                        available_at=response.retrieved_at,
                        open=_finite(row[1], positive=True),
                        high=_finite(row[2], positive=True),
                        low=_finite(row[3], positive=True),
                        close=_finite(row[4], positive=True),
                        volume=_finite(row[5]),
                        quote_volume=_finite(row[7]),
                        taker_buy_volume=_finite(row[9]),
                        trade_count=row[8],
                        source=f"binance/spot/{symbol}/{interval}",
                    )
                except (ValueError, TypeError, OSError, OverflowError, ValidationError) as exc:
                    raise ProviderError("binance/klines", f"invalid candle for {symbol}/{interval}") from exc
                if open_time in candles:
                    raise ProviderError("binance/klines", "duplicate candle open time")
                candles[open_time] = candle
            if len(candles) >= limit or not response.data or oldest is None:
                break
            end = int(oldest.timestamp() * 1000) - 1
        return sorted(candles.values(), key=lambda bar: bar.open_time)[-limit:]

    async def liquidity(self, asset: Asset, as_of: datetime) -> dict[str, Metric]:
        aware(as_of)
        keys = (
            "spread_bps",
            "depth_usd",
            "bid_depth_usd",
            "ask_depth_usd",
            "best_bid",
            "best_ask",
            "book_mid",
            "quote_volume_24h",
        )
        output = {key: unavailable("binance/spot", "no verified exchange symbol") for key in keys}
        try:
            symbol = self._symbol(asset)
        except ProviderError:
            return output
        results = await asyncio.gather(
            self._get("/api/v3/depth", {"symbol": symbol, "limit": 1000}, weight=50),
            self._get("/api/v3/ticker/24hr", {"symbol": symbol}, weight=2),
            return_exceptions=True,
        )
        depth, ticker = results
        if isinstance(depth, BaseException):
            if not isinstance(depth, ProviderError):
                raise depth
            self.issues.append(str(depth))
            for key in keys[:-1]:
                output[key] = unavailable("binance/spot/depth", str(depth))
        else:
            try:
                book = depth.data
                if not isinstance(book, dict):
                    raise ValueError("invalid order book")
                if not all(
                    isinstance(book.get(side), list)
                    and 0 < len(book[side]) <= 1000
                    and all(isinstance(row, list) and len(row) == 2 for row in book[side])
                    for side in ("bids", "asks")
                ):
                    raise ValueError("invalid order-book levels")
                bids = [(_finite(row[0], positive=True), _finite(row[1])) for row in book.get("bids", [])]
                asks = [(_finite(row[0], positive=True), _finite(row[1])) for row in book.get("asks", [])]
                if not bids or not asks or any(qty <= 0 for _, qty in bids + asks):
                    raise ValueError("empty or invalid order book")
                if bids != sorted(bids, reverse=True) or asks != sorted(asks):
                    raise ValueError("unsorted order book")
                if len({price for price, _ in bids}) != len(bids) or len({price for price, _ in asks}) != len(
                    asks
                ):
                    raise ValueError("duplicate order-book price levels")
                bid, ask = bids[0][0], asks[0][0]
                if ask <= bid:
                    raise ValueError("crossed order book")
                mid = (bid + ask) / 2
                retrieved = depth.retrieved_at
                observed = _millis(book["T"]) if "T" in book else retrieved
                for name, value in (("best_bid", bid), ("best_ask", ask), ("book_mid", mid)):
                    output[name] = metric(
                        value, f"binance/spot/{symbol}/depth", "USDT per base asset", observed, retrieved
                    )
                output["spread_bps"] = metric(
                    (ask - bid) / mid * 10000,
                    f"binance/spot/{symbol}/depth",
                    "basis points",
                    observed,
                    retrieved,
                )
                # If the capped snapshot stops within the band, summing its
                # rows would be an incomplete liquidity estimate. Reject it.
                bid_complete = bids[-1][0] <= mid * 0.99 or len(bids) < 1000
                ask_complete = asks[-1][0] >= mid * 1.01 or len(asks) < 1000
                unit = "USDT-equivalent within 1% mid; assumes no USD peg"
                for key, rows, complete, side in (
                    ("bid_depth_usd", bids, bid_complete, "bid"),
                    ("ask_depth_usd", asks, ask_complete, "ask"),
                ):
                    value = sum(
                        price * quantity
                        for price, quantity in rows
                        if (price >= mid * 0.99 if side == "bid" else price <= mid * 1.01)
                    )
                    output[key] = (
                        metric(value, f"binance/spot/{symbol}/depth", unit, observed, retrieved)
                        if complete
                        else unavailable(
                            "binance/spot/depth", f"capped {side} book does not reach 1% band", unit
                        )
                    )
                if output["bid_depth_usd"].value is not None and output["ask_depth_usd"].value is not None:
                    output["depth_usd"] = metric(
                        min(output["bid_depth_usd"].value, output["ask_depth_usd"].value),
                        f"binance/spot/{symbol}/depth:min(bid,ask)",
                        unit,
                        observed,
                        retrieved,
                    )
                else:
                    output["depth_usd"] = unavailable(
                        "binance/spot/depth", "incomplete two-sided 1% liquidity", unit
                    )
            except (ValueError, TypeError, KeyError, IndexError, OSError, OverflowError) as exc:
                self.issues.append(f"{asset.asset_id}: invalid spot depth ({type(exc).__name__})")
                for key in keys[:-1]:
                    output[key] = unavailable("binance/spot/depth", "invalid order-book payload")
        if isinstance(ticker, BaseException):
            if not isinstance(ticker, ProviderError):
                raise ticker
            self.issues.append(str(ticker))
            output["quote_volume_24h"] = unavailable("binance/spot/ticker24h", str(ticker))
        else:
            try:
                if not isinstance(ticker.data, dict) or ticker.data.get("symbol", symbol) != symbol:
                    raise ValueError("mismatched ticker identity")
                observed = _millis(ticker.data["closeTime"])
                output["quote_volume_24h"] = metric(
                    ticker.data.get("quoteVolume"),
                    f"binance/spot/{symbol}/ticker24h",
                    "USDT per rolling 24h; native spot venue",
                    observed,
                    ticker.retrieved_at,
                    nonnegative=True,
                )
            except (ValueError, TypeError, KeyError, OSError, OverflowError):
                output["quote_volume_24h"] = unavailable(
                    "binance/spot/ticker24h", "invalid 24h ticker payload"
                )
        return output


class BinanceDerivativesProvider:
    def __init__(self, http: AsyncHTTPClient, settings: Settings):
        self.http = http
        self.settings = settings
        self.issues: list[str] = []
        self._symbols: set[str] | None = None
        self._symbol_lock = asyncio.Lock()
        self._symbols_boundary: datetime | None = None
        self._symbols_error: ProviderError | None = None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> FetchedJSON:
        return await self.http.get_json(self.settings.binance_futures_url.rstrip("/") + path, params=params)

    async def _supports(self, symbol: str, as_of: datetime) -> bool:
        async with self._symbol_lock:
            # One shared instrument fetch per scan cutoff, including failures.
            # A later watch iteration must observe listings and delistings.
            if self._symbols_boundary is None or as_of > self._symbols_boundary:
                self._symbols_boundary = as_of
                self._symbols = None
                self._symbols_error = None
                try:
                    response = await self._get("/fapi/v1/exchangeInfo")
                    rows = response.data.get("symbols") if isinstance(response.data, dict) else None
                    if not isinstance(rows, list):
                        raise ProviderError("binance/futures/exchangeInfo", "expected instrument definitions")
                    self._symbols = {
                        str(row["symbol"])
                        for row in rows
                        if isinstance(row, dict)
                        and row.get("symbol")
                        and row.get("status") == "TRADING"
                        and row.get("contractType") == "PERPETUAL"
                        and row.get("quoteAsset") == "USDT"
                    }
                except ProviderError as exc:
                    self._symbols_error = exc
            if self._symbols_error is not None:
                raise self._symbols_error
            return symbol in (self._symbols or set())

    async def metrics(self, asset: Asset, as_of: datetime) -> dict[str, Metric]:
        as_of = aware(as_of)
        source = "binance/USD-M"
        output = {
            key: unavailable(source, "no verified matching USDT perpetual instrument")
            for key in (
                "open_interest",
                "open_interest_notional",
                "open_interest_change_pct",
                "funding_rate",
                "funding_interval_hours",
                "basis_pct",
                "futures_volume_24h",
                "futures_spot_volume_ratio",
            )
        }
        output["liquidation_cluster_usd"] = unavailable(
            source, "liquidation clusters require a separate trustworthy source"
        )
        output["liquidations_24h"] = unavailable(
            source, "public REST does not supply complete liquidation history"
        )
        output["long_short_ratio"] = unavailable(
            source, "public account-count ratios do not establish genuine trader positioning"
        )
        output["predicted_funding_rate"] = unavailable(
            source, "settled funding is available; no trustworthy predicted funding feed configured"
        )
        symbol = asset.exchange_symbol
        if not symbol or asset.quote_asset != "USDT":
            return output
        try:
            if not await self._supports(symbol, as_of):
                return output
        except ProviderError as exc:
            self.issues.append(str(exc))
            return output
        requests = [
            ("history", "/futures/data/openInterestHist", {"symbol": symbol, "period": "1h", "limit": 25}),
            ("funding", "/fapi/v1/fundingRate", {"symbol": symbol, "limit": 2}),
            ("premium", "/fapi/v1/premiumIndex", {"symbol": symbol}),
            ("ticker", "/fapi/v1/ticker/24hr", {"symbol": symbol}),
        ]
        window_end = as_of.replace(minute=0, second=0, microsecond=0)
        window_start = window_end - timedelta(hours=24)
        common_params = {
            "symbol": symbol,
            "interval": "1h",
            "limit": 24,
            "startTime": int(window_start.timestamp() * 1000),
            "endTime": int(window_end.timestamp() * 1000) - 1,
        }
        responses = await asyncio.gather(
            *(self._get(path, params) for _, path, params in requests),
            self._get("/fapi/v1/klines", common_params),
            self.http.get_json(
                self.settings.binance_spot_url.rstrip("/") + "/api/v3/klines", params=common_params, weight=2
            ),
            return_exceptions=True,
        )
        futures_window, spot_window = responses[-2:]
        responses = responses[:-2]
        for (kind, _, _), response in zip(requests, responses, strict=True):
            if isinstance(response, BaseException):
                if not isinstance(response, ProviderError):
                    raise response
                self.issues.append(str(response))
                affected = {
                    "history": ("open_interest", "open_interest_notional", "open_interest_change_pct"),
                    "funding": ("funding_rate", "funding_interval_hours"),
                    "premium": ("basis_pct",),
                    "ticker": ("futures_volume_24h",),
                }[kind]
                for key in affected:
                    output[key] = unavailable(source, str(response))
                continue
            try:
                retrieved = response.retrieved_at
                data = response.data
                if isinstance(data, dict) and data.get("symbol", symbol) != symbol:
                    raise ValueError("mismatched derivatives instrument")
                if kind == "history":
                    if not isinstance(data, list) or not data:
                        raise ValueError("missing OI samples")
                    if not all(isinstance(row, dict) and row.get("symbol", symbol) == symbol for row in data):
                        raise ValueError("mismatched OI instrument")
                    rows = sorted(data, key=lambda row: _millis(row["timestamp"]))
                    if len({_millis(row["timestamp"]) for row in rows}) != len(rows):
                        raise ValueError("duplicate OI timestamps")
                    rows = [row for row in rows if _millis(row["timestamp"]) <= retrieved]
                    if not rows:
                        raise ValueError("no closed OI observations")
                    latest = rows[-1]
                    observed = _millis(latest["timestamp"])
                    output["open_interest"] = metric(
                        latest.get("sumOpenInterest"),
                        f"{source}/{symbol}/openInterestHist",
                        "base asset (native USD-M openInterest units)",
                        observed,
                        retrieved,
                        nonnegative=True,
                    )
                    output["open_interest_notional"] = metric(
                        latest.get("sumOpenInterestValue"),
                        f"{source}/{symbol}/openInterestHist",
                        "USDT notional (includes price effect)",
                        observed,
                        retrieved,
                        nonnegative=True,
                    )
                    target = observed - timedelta(hours=24)
                    past = min(
                        rows, key=lambda row: abs((_millis(row["timestamp"]) - target).total_seconds())
                    )
                    old_time = _millis(past["timestamp"])
                    old = _finite(past["sumOpenInterest"], positive=True)
                    new = _finite(latest["sumOpenInterest"])
                    if abs((old_time - target).total_seconds()) <= 60 and old_time < observed and new >= 0:
                        output["open_interest_change_pct"] = metric(
                            new / old - 1,
                            f"{source}/{symbol}:24h same-unit OI change (not notional)",
                            "fraction per 24h",
                            observed,
                            retrieved,
                        )
                    else:
                        output["open_interest_change_pct"] = unavailable(
                            source, "no OI base-unit observations exactly 24h apart", "fraction per 24h"
                        )
                elif kind == "funding":
                    if not isinstance(data, list) or not data:
                        raise ValueError("missing settled funding")
                    if not all(isinstance(row, dict) and row.get("symbol", symbol) == symbol for row in data):
                        raise ValueError("mismatched funding instrument")
                    rows = sorted(data, key=lambda row: _millis(row["fundingTime"]))
                    if len({_millis(row["fundingTime"]) for row in rows}) != len(rows):
                        raise ValueError("duplicate funding event timestamps")
                    rows = [row for row in rows if _millis(row["fundingTime"]) <= retrieved]
                    if not rows:
                        raise ValueError("no settled funding event")
                    latest = rows[-1]
                    observed = _millis(latest["fundingTime"])
                    output["funding_rate"] = metric(
                        latest.get("fundingRate"),
                        f"{source}/{symbol}/fundingRate:settled",
                        "fraction per settled funding interval",
                        observed,
                        retrieved,
                    )
                    if len(rows) >= 2:
                        interval = (observed - _millis(rows[-2]["fundingTime"])).total_seconds() / 3600
                        if 0 < interval <= 24:
                            output["funding_interval_hours"] = metric(
                                interval,
                                f"{source}/{symbol}:last two actual settlements",
                                "hours",
                                observed,
                                retrieved,
                            )
                elif kind == "premium":
                    observed = _millis(data["time"])
                    mark, index = (
                        _finite(data["markPrice"], positive=True),
                        _finite(data["indexPrice"], positive=True),
                    )
                    output["basis_pct"] = metric(
                        mark / index - 1,
                        f"{source}/{symbol}/premiumIndex",
                        "fraction; perpetual mark/index premium, not dated futures basis",
                        observed,
                        retrieved,
                    )
                elif kind == "ticker":
                    observed = _millis(data["closeTime"])
                    output["futures_volume_24h"] = metric(
                        data.get("quoteVolume"),
                        f"{source}/{symbol}/ticker24h",
                        "USDT per rolling 24h; futures venue",
                        observed,
                        retrieved,
                        nonnegative=True,
                    )
            except (ValueError, TypeError, KeyError, IndexError, OSError, OverflowError):
                self.issues.append(f"{asset.asset_id}: invalid derivatives {kind} payload")
                affected = {
                    "history": ("open_interest", "open_interest_notional", "open_interest_change_pct"),
                    "funding": ("funding_rate", "funding_interval_hours"),
                    "premium": ("basis_pct",),
                    "ticker": ("futures_volume_24h",),
                }[kind]
                for key in affected:
                    output[key] = unavailable(source, f"invalid derivatives {kind} payload")
        output["futures_spot_volume_ratio"] = self._volume_ratio(
            symbol,
            futures_window,
            spot_window,
            window_start,
            window_end,
        )
        return output

    def _volume_ratio(
        self,
        symbol: str,
        futures: FetchedJSON | BaseException,
        spot: FetchedJSON | BaseException,
        start: datetime,
        end: datetime,
    ) -> Metric:
        source = f"binance/{symbol}:matched spot+USD-M 24 completed 1h quote-volume candles"
        for response in (futures, spot):
            if isinstance(response, BaseException):
                if not isinstance(response, ProviderError):
                    raise response
                self.issues.append(str(response))
                return unavailable(source, str(response), "futures/spot USDT-volume ratio")
        try:
            expected = [start + timedelta(hours=index) for index in range(24)]
            sums = []
            for response in (futures, spot):
                if not isinstance(response.data, list) or len(response.data) != 24:
                    raise ValueError("incomplete native 24-hour volume window")
                opens = []
                total = 0.0
                for row in response.data:
                    if not isinstance(row, list) or len(row) < 8:
                        raise ValueError("incomplete native kline")
                    opened = _millis(row[0])
                    closed = _millis(row[6]) + timedelta(milliseconds=1)
                    if closed != opened + timedelta(hours=1) or closed > min(end, response.retrieved_at):
                        raise ValueError("native kline is not a completed common-window hour")
                    quote_volume = _finite(row[7])
                    if quote_volume < 0:
                        raise ValueError("negative quote volume")
                    total += quote_volume
                    opens.append(opened)
                if opens != expected:
                    raise ValueError("spot/futures volume hours do not match expected window")
                sums.append(total)
            if sums[1] <= 0:
                raise ValueError("spot common-window quote volume is zero")
            return metric(
                sums[0] / sums[1],
                source,
                "futures/spot USDT-volume ratio; same completed 24h window",
                end,
                max(futures.retrieved_at, spot.retrieved_at),
                nonnegative=True,
            )
        except (ValueError, TypeError, KeyError, IndexError, OSError, OverflowError) as exc:
            return unavailable(source, str(exc), "futures/spot USDT-volume ratio")
