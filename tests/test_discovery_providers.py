"""Synthetic native transport data verifies Phase 1 provider contracts."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from expansion_hunter.config import Settings
from expansion_hunter.models import Asset, Status
from expansion_hunter.providers import AsyncHTTPClient, BinanceSpotProvider, CoinGeckoProvider
from expansion_hunter.providers.base import ProviderError
from expansion_hunter.providers.http import FetchedJSON

NOW = datetime(2026, 1, 2, 12, tzinfo=UTC)


class MemoryHTTP:
    def __init__(self, fixtures):
        self.fixtures = fixtures
        self.calls = []

    async def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        data = self.fixtures[httpx.URL(url).path]
        if isinstance(data, Exception):
            raise data
        if callable(data):
            data = data(kwargs.get("params", {}))
        return FetchedJSON(data, NOW, url)


def asset(index=1, mapped=True):
    return Asset(
        asset_id=f"asset-{index}",
        symbol=f"X{index}",
        name=f"Asset {index}",
        rank=index,
        exchange_symbol=f"X{index}USDT" if mapped else None,
    )


def ticker(index=1):
    return {
        "symbol": f"X{index}USDT",
        "lastPrice": "10",
        "priceChangePercent": "-1.5",
        "quoteVolume": "50000",
        "volume": "5000",
        "highPrice": "12",
        "lowPrice": "9",
        "count": 1000,
        "closeTime": int(NOW.timestamp() * 1000),
    }


def book(index=1):
    return {"symbol": f"X{index}USDT", "bidPrice": "9.99", "askPrice": "10.01"}


def bulk_fixtures(tickers=None, books=None):
    return {
        "/api/v3/ticker/24hr": [ticker()] if tickers is None else tickers,
        "/api/v3/ticker/bookTicker": [book()] if books is None else books,
    }


async def test_thousand_asset_universe_uses_four_pages_and_current_ids():
    def records(params):
        start = (params["page"] - 1) * 250 + 1
        return [
            {"id": f"asset-{i}", "symbol": f"x{i}", "name": f"Asset {i}", "market_cap_rank": i}
            for i in range(start, start + 250)
        ]

    http = MemoryHTTP({"/api/v3/coins/markets": records})
    provider = CoinGeckoProvider(http, Settings(top_n=1000))
    universe = await provider.universe(1000, NOW)
    assert len(universe) == 1000 and universe[-1].rank == 1000
    assert [kwargs["params"]["page"] for _, kwargs in http.calls] == [1, 2, 3, 4]
    assert all(kwargs["params"]["sparkline"] == "false" for _, kwargs in http.calls)


async def test_later_universe_page_failure_retains_only_verified_prefix():
    def records(params):
        if params["page"] == 2:
            raise ProviderError("synthetic", "page unavailable")
        return [
            {"id": f"asset-{i}", "symbol": f"x{i}", "name": f"Asset {i}", "market_cap_rank": i}
            for i in range(1, 251)
        ]

    provider = CoinGeckoProvider(MemoryHTTP({"/api/v3/coins/markets": records}), Settings())
    result = await provider.universe(1000, NOW)
    assert len(result) == 250
    assert any("page 2 failed" in issue for issue in provider.issues)
    assert any("250 of requested 1000" in issue for issue in provider.issues)


async def test_first_universe_page_failure_remains_fatal_and_visible():
    provider = CoinGeckoProvider(
        MemoryHTTP({"/api/v3/coins/markets": ProviderError("synthetic", "offline")}), Settings()
    )
    with pytest.raises(ProviderError, match="offline"):
        await provider.universe(1000, NOW)


async def test_native_bulk_market_cost_does_not_scale_with_requested_assets():
    http = MemoryHTTP(bulk_fixtures([ticker(i) for i in range(1, 1001)], [book(i) for i in range(1, 1001)]))
    result = await BinanceSpotProvider(http, Settings()).light_market([asset(i) for i in range(1, 1001)], NOW)
    assert len(result) == 1000 and len(http.calls) == 2
    assert [kwargs["weight"] for _, kwargs in http.calls] == [80, 4]
    assert result["asset-1000"]["quote_volume_24h"].value == 50000
    assert result["asset-1"]["base_volume_24h"].value == 5000
    assert result["asset-1"]["price_change_24h_pct"].value == -1.5
    assert result["asset-1"]["spread_bps"].value == pytest.approx(20)
    assert "retrieval_snapshot" in result["asset-1"]["spread_bps"].source
    assert "relative_volume" not in result["asset-1"]
    assert "depth_usd" not in result["asset-1"]


async def test_bulk_missing_and_malformed_assets_are_reported_individually():
    http = MemoryHTTP(
        bulk_fixtures(
            [ticker(), {"bad": "row"}, ticker(2), ticker(2)], [book(), {**book(3), "bidPrice": "13"}]
        )
    )
    provider = BinanceSpotProvider(http, Settings())
    result = await provider.light_market([asset(1), asset(2), asset(3), asset(4, False)], NOW)
    assert result["asset-1"]["price"].status == Status.AVAILABLE
    assert result["asset-2"]["price"].value is None
    assert "absent or duplicate" in result["asset-2"]["price"].reason
    assert result["asset-3"]["spread_bps"].value is None
    assert "invalid" in result["asset-3"]["spread_bps"].reason
    assert all(value.value is None for value in result["asset-4"].values())
    assert any("malformed" in issue for issue in provider.issues)
    assert any("duplicate X2USDT" in issue for issue in provider.issues)


async def test_bulk_ticker_outage_preserves_book_and_missing_data():
    http = MemoryHTTP(bulk_fixtures(ProviderError("synthetic", "offline"), [book()]))
    provider = BinanceSpotProvider(http, Settings())
    result = (await provider.light_market([asset()], NOW))["asset-1"]
    assert result["price"].value is None and "offline" in result["price"].reason
    assert result["spread_bps"].value == pytest.approx(20)
    assert provider.issues


async def test_bulk_newly_fetched_data_is_not_available_at_past_checkpoint():
    provider = BinanceSpotProvider(MemoryHTTP(bulk_fixtures()), Settings())
    result = (await provider.light_market([asset()], NOW - timedelta(hours=1)))["asset-1"]
    assert result["price"].available_at == NOW
    assert not result["price"].usable(NOW - timedelta(hours=1))


async def test_bulk_bad_timestamp_never_becomes_fresh_by_retrieval():
    stale = {**ticker(), "closeTime": int((NOW - timedelta(hours=3)).timestamp() * 1000)}
    provider = BinanceSpotProvider(MemoryHTTP(bulk_fixtures([stale], [])), Settings())
    result = (await provider.light_market([asset()], NOW))["asset-1"]
    assert not result["price"].usable(NOW, max_age_seconds=600)
    future = {**ticker(), "closeTime": int((NOW + timedelta(hours=1)).timestamp() * 1000)}
    provider = BinanceSpotProvider(MemoryHTTP(bulk_fixtures([future], [])), Settings())
    result = (await provider.light_market([asset()], NOW))["asset-1"]
    assert result["price"].status == Status.UNAVAILABLE


async def test_no_verified_assets_creates_no_native_requests():
    http = MemoryHTTP({})
    result = await BinanceSpotProvider(http, Settings()).light_market([asset(mapped=False)], NOW)
    assert len(result) == 1 and not http.calls


async def test_explicit_official_fallback_is_sticky_and_issues_visible():
    calls = []

    def respond(request):
        calls.append(request.url.host)
        if request.url.host == "api.binance.com":
            return httpx.Response(451, json={"error": "unavailable"})
        return httpx.Response(200, json=[])

    settings = Settings(http_retries=0, binance_spot_fallback_urls=["https://data-api.binance.vision"])
    async with AsyncHTTPClient(
        settings,
        transport=httpx.MockTransport(respond),
        host_intervals={"api.binance.com": 0, "data-api.binance.vision": 0},
    ) as http:
        provider = BinanceSpotProvider(http, settings)
        response = await provider._get("/api/v3/ticker/24hr")
        await provider._get("/api/v3/ticker/24hr")
    assert calls == ["api.binance.com", "data-api.binance.vision", "data-api.binance.vision"]
    assert response.url.startswith("https://data-api.binance.vision")
    assert provider.active_spot_url == "https://data-api.binance.vision"
    assert any("fallback activated" in issue for issue in provider.issues)
    assert any("host unavailable" in issue for issue in provider.issues)


@pytest.mark.parametrize("status_code", [400, 401, 404, 429])
async def test_fallback_does_not_evade_rate_limit_or_bad_request(status_code):
    calls = []

    def respond(request):
        calls.append(request.url.host)
        return httpx.Response(status_code, json={"error": "synthetic"})

    settings = Settings(http_retries=0, binance_spot_fallback_urls=["https://data-api.binance.vision"])
    async with AsyncHTTPClient(
        settings, transport=httpx.MockTransport(respond), host_intervals={"api.binance.com": 0}
    ) as http:
        with pytest.raises(ProviderError):
            await BinanceSpotProvider(http, settings)._get("/api/v3/ticker/24hr")
    assert calls == ["api.binance.com"]


@pytest.mark.parametrize(
    "url",
    [
        "http://api.binance.com",
        "https://user:secret@api.binance.com",
        "https://api.binance.com/path",
        "https://api.binance.com?token=x",
        "https://untrusted.invalid",
    ],
)
def test_fallback_requires_explicit_native_https_origin(url):
    with pytest.raises(ValidationError, match="native Binance HTTPS"):
        Settings(binance_spot_fallback_urls=[url])


async def test_identity_cache_avoids_per_minute_full_list_and_expires(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("expansion_hunter.providers.binance.time.monotonic", lambda: clock[0])
    http = MemoryHTTP(
        {
            "/api/v3/exchangeInfo": {
                "symbols": [
                    {"symbol": "X1USDT", "baseAsset": "X1", "quoteAsset": "USDT", "status": "TRADING"}
                ]
            },
            "/api/v3/coins/list": [{"id": "asset-1", "symbol": "x1"}],
        }
    )
    provider = BinanceSpotProvider(http, Settings(identity_cache_ttl_seconds=300))
    assert (await provider.resolve_assets([asset(mapped=False)]))[0].exchange_symbol == "X1USDT"
    clock[0] += 60
    await provider.resolve_assets([asset(mapped=False)])
    assert len(http.calls) == 2
    clock[0] += 300
    await provider.resolve_assets([asset(mapped=False)])
    assert len(http.calls) == 4


async def test_http_diagnostics_count_failed_attempts_weights_and_server_usage():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            451 if len(calls) == 1 else 200, json=[], headers={"X-MBX-USED-WEIGHT-1M": "123"}
        )

    async with AsyncHTTPClient(
        Settings(http_retries=0), transport=httpx.MockTransport(respond), host_intervals={"synthetic": 0}
    ) as http:
        with pytest.raises(ProviderError):
            await http.get_json("https://synthetic/failure", weight=20)
        await http.get_json("https://synthetic/success", weight=80)
        stats = http.diagnostics
    assert stats["request_count"] == 2 and stats["request_weight"] == 100
    assert stats["per_host"]["synthetic"]["used_weight_1m"] == 123
    assert stats["rate_limit_wait_seconds"] == 0
