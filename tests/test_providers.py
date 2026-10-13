"""Synthetic transport fixtures test contracts; no fixture is live market data."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from expansion_hunter.config import Settings
from expansion_hunter.models import TIMEFRAMES, Asset, Status
from expansion_hunter.providers import (
    AsyncHTTPClient,
    BinanceDerivativesProvider,
    BinanceSpotProvider,
    CoinGeckoProvider,
    FetchedJSON,
    ProviderError,
)
from expansion_hunter.providers.base import metric

NOW = datetime(2026, 1, 2, 12, tzinfo=UTC)
PAST = NOW - timedelta(days=30)


class MemoryHTTP:
    def __init__(self, fixtures):
        self.fixtures = fixtures
        self.calls = []

    async def get_json(self, url, **kwargs):
        path = httpx.URL(url).path
        self.calls.append((path, kwargs))
        fixture = self.fixtures[path]
        if isinstance(fixture, Exception):
            raise fixture
        data = fixture(kwargs.get("params", {})) if callable(fixture) else fixture
        return FetchedJSON(data=data, retrieved_at=NOW)


def asset(asset_id="bitcoin", symbol="BTC", exchange_symbol="BTCUSDT", name="Bitcoin"):
    return Asset(asset_id=asset_id, symbol=symbol, exchange_symbol=exchange_symbol, name=name)


def market_record(index):
    return {
        "id": f"asset-{index}",
        "symbol": f"X{index}",
        "name": f"Asset {index}",
        "market_cap_rank": index,
        "market_cap": 1000,
        "total_volume": 50,
        "current_price": 10,
        "circulating_supply": 100,
        "fully_diluted_valuation": None,
        "ath": 20,
        "last_updated": NOW.isoformat(),
    }


def kline(opened, *, close=100, duration=60):
    return [
        int(opened.timestamp() * 1000),
        "100",
        "110",
        "90",
        str(close),
        "10",
        int((opened + timedelta(seconds=duration)).timestamp() * 1000) - 1,
        "1000",
        10,
        "4",
        "400",
        "0",
    ]


async def test_http_retries_429_and_5xx_only(monkeypatch):
    attempts = []
    waits = []

    def respond(request):
        attempts.append(request)
        codes = [429, 503, 200]
        return httpx.Response(codes[len(attempts) - 1], headers={"Retry-After": "0"}, json={"ok": True})

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("expansion_hunter.providers.http.asyncio.sleep", fake_sleep)
    async with AsyncHTTPClient(
        Settings(http_retries=2), transport=httpx.MockTransport(respond), host_intervals={"mock": 0}
    ) as http:
        result = await http.get_json("https://mock/test")
    assert result.data == {"ok": True}
    assert result.retrieved_at.tzinfo is not None
    assert len(attempts) == 3 and waits == [0, 0]


async def test_http_does_not_retry_4xx_or_parse_errors():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(400, json={"error": "rejected"})

    async with AsyncHTTPClient(transport=httpx.MockTransport(respond), host_intervals={"mock": 0}) as http:
        with pytest.raises(ProviderError) as exc:
            await http.get_json("https://mock/test")
    assert exc.value.status_code == 400 and len(requests) == 1


async def test_http_retry_after_cannot_exceed_budget():
    async with AsyncHTTPClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "999"})),
        host_intervals={"mock": 0},
    ) as http:
        with pytest.raises(ProviderError, match="bounded retry budget"):
            await http.get_json("https://mock/test")


async def test_http_network_errors_are_bounded(monkeypatch):
    requests = []

    def fail(request):
        requests.append(request)
        raise httpx.ConnectError("synthetic outage", request=request)

    async def fake_sleep(_):
        return None

    monkeypatch.setattr("expansion_hunter.providers.http.asyncio.sleep", fake_sleep)
    async with AsyncHTTPClient(
        Settings(http_retries=1), transport=httpx.MockTransport(fail), host_intervals={"mock": 0}
    ) as http:
        with pytest.raises(ProviderError, match="ConnectError"):
            await http.get_json("https://mock/test")
    assert len(requests) == 2


async def test_http_malformed_json_is_explicit():
    async with AsyncHTTPClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="not json")),
        host_intervals={"mock": 0},
    ) as http:
        with pytest.raises(ProviderError, match="valid JSON"):
            await http.get_json("https://mock/test")


async def test_host_limiter_is_shared_across_paths_and_weights_are_checked():
    async with AsyncHTTPClient(host_intervals={"mock": 0}) as http:
        assert http._limiter("https://mock/a") is http._limiter("https://mock/b")
        assert http._limiter("https://other/a") is not http._limiter("https://mock/a")
        with pytest.raises(ValueError, match="weight"):
            await http.get_json("https://mock/a", weight=0)


async def test_universe_paginates_500_and_uses_ids():
    http = MemoryHTTP(
        {
            "/api/v3/coins/markets": lambda p: [
                market_record(i) for i in range(1 + (p["page"] - 1) * 250, 1 + p["page"] * 250)
            ]
        }
    )
    provider = CoinGeckoProvider(http, Settings())
    universe = await provider.universe(500, PAST)
    assert len(universe) == 500
    assert universe[0].asset_id == "asset-1" and universe[-1].rank == 500
    assert [call[1]["params"]["page"] for call in http.calls] == [1, 2]
    result = await provider.market(universe[0], PAST)
    assert result["market_cap"].available_at == NOW
    assert not result["market_cap"].usable(PAST)
    assert result["turnover_24h"].value == pytest.approx(0.05)
    assert result["fdv"].status == Status.UNAVAILABLE


async def test_duplicate_market_symbols_remain_separate_ids():
    records = [dict(market_record(1), symbol="SAME"), dict(market_record(2), symbol="SAME")]
    provider = CoinGeckoProvider(MemoryHTTP({"/api/v3/coins/markets": records}), Settings())
    universe = await provider.universe(2, NOW)
    assert [item.asset_id for item in universe] == ["asset-1", "asset-2"]


async def test_global_computations_share_one_snapshot_and_missing_history_is_explicit():
    http = MemoryHTTP(
        {
            "/api/v3/global": {
                "data": {
                    "updated_at": int(NOW.timestamp()),
                    "total_market_cap": {"usd": 1000, "btc": 0.02, "eth": 0.4},
                    "market_cap_percentage": {"btc": 60, "eth": 20},
                }
            }
        }
    )
    result = await CoinGeckoProvider(http, Settings()).global_metrics(PAST)
    assert result["total2_market_cap"].value == pytest.approx(400)
    assert result["total3_market_cap"].value == pytest.approx(200)
    assert result["eth_btc"].value == pytest.approx(0.05)
    assert result["btc_dominance"].unit == "percentage points"
    assert result["btc_dominance_change_7d"].value is None
    assert result["stablecoin_market_cap"].value is None
    assert result["total_market_cap"].available_at == NOW


async def test_optional_global_failure_reports_missing_not_zeros():
    provider = CoinGeckoProvider(MemoryHTTP({"/api/v3/global": ProviderError("mock", "offline")}), Settings())
    result = await provider.global_metrics(NOW)
    assert all(item.value is None for item in result.values())
    assert provider.issues


def exchange_rows(*symbols):
    return {
        "symbols": [
            {
                "symbol": symbol,
                "baseAsset": symbol[:-4],
                "quoteAsset": "USDT",
                "status": "TRADING",
                "isSpotTradingAllowed": True,
            }
            for symbol in symbols
        ]
    }


async def test_auto_identity_rejects_globally_ambiguous_symbols():
    provider = BinanceSpotProvider(
        MemoryHTTP(
            {
                "/api/v3/exchangeInfo": exchange_rows("BTCUSDT", "QNTUSDT"),
                "/api/v3/coins/list": [
                    {"id": "bitcoin", "symbol": "btc"},
                    {"id": "other-btc", "symbol": "btc"},
                    {"id": "quant-network", "symbol": "qnt"},
                ],
            }
        ),
        Settings(),
    )
    result = await provider.resolve_assets([asset(), asset("quant-network", "QNT", None, "Quant")])
    assert result[0].exchange_symbol is None
    assert result[1].exchange_symbol == "QNTUSDT"
    assert any("ambiguous" in issue for issue in provider.issues)


async def test_explicit_id_mapping_resolves_collision_even_if_identity_endpoint_fails(tmp_path):
    mapping = tmp_path / "symbols.json"
    mapping.write_text(json.dumps({"bitcoin": "BTCUSDT"}), encoding="utf-8")
    provider = BinanceSpotProvider(
        MemoryHTTP(
            {
                "/api/v3/exchangeInfo": exchange_rows("BTCUSDT"),
                "/api/v3/coins/list": ProviderError("mock", "offline"),
            }
        ),
        Settings(symbol_map_path=str(mapping)),
    )
    result = await provider.resolve_assets([asset(), asset("unknown", "BTC", None, "Unknown")])
    assert result[0].exchange_symbol == "BTCUSDT" and result[1].exchange_symbol is None


async def test_wrapped_identity_requires_explicit_mapping():
    provider = BinanceSpotProvider(
        MemoryHTTP(
            {
                "/api/v3/exchangeInfo": exchange_rows("WBTCUSDT"),
                "/api/v3/coins/list": [{"id": "wrapped-bitcoin", "symbol": "wbtc"}],
            }
        ),
        Settings(),
    )
    result = await provider.resolve_assets([asset("wrapped-bitcoin", "WBTC", None, "Wrapped Bitcoin")])
    assert result[0].exchange_symbol is None


async def test_explicit_empty_identity_tickers_do_not_disable_other_valid_mappings():
    provider = BinanceSpotProvider(
        MemoryHTTP(
            {
                "/api/v3/exchangeInfo": exchange_rows("BNBUSDT", "XRPUSDT"),
                "/api/v3/coins/list": [
                    {"id": "binancecoin", "symbol": "bnb"},
                    {"id": "ripple", "symbol": "xrp"},
                    {"id": "no-ticker", "symbol": ""},
                    {"id": "blank-ticker", "symbol": "  "},
                ],
            }
        ),
        Settings(),
    )
    result = await provider.resolve_assets(
        [
            asset("binancecoin", "BNB", None, "BNB"),
            asset("ripple", "XRP", None, "XRP"),
            asset("no-ticker", "UNKNOWN", None, "No Ticker"),
        ]
    )
    assert [item.exchange_symbol for item in result] == ["BNBUSDT", "XRPUSDT", None]
    assert any("2 asset IDs have empty tickers" in issue for issue in provider.issues)
    assert not any("automatic symbol matching disabled" in issue for issue in provider.issues)


@pytest.mark.parametrize("unknown_symbol", [None, 123, False])
async def test_missing_or_nonstring_identity_ticker_preserves_collision_safety(unknown_symbol):
    provider = BinanceSpotProvider(
        MemoryHTTP(
            {
                "/api/v3/exchangeInfo": exchange_rows("BNBUSDT"),
                "/api/v3/coins/list": [
                    {"id": "binancecoin", "symbol": "bnb"},
                    {"id": "unknown", "symbol": unknown_symbol},
                ],
            }
        ),
        Settings(),
    )
    result = await provider.resolve_assets([asset("binancecoin", "BNB", None, "BNB")])
    assert result[0].exchange_symbol is None
    assert any("automatic symbol matching disabled" in issue for issue in provider.issues)


async def test_candles_filter_forming_bars_and_preserve_first_seen_time():
    rows = [kline(NOW - timedelta(minutes=1)), kline(NOW)]
    http = MemoryHTTP({"/api/v3/klines": rows})
    result = await BinanceSpotProvider(http, Settings()).candles(asset(), "1m", 1, NOW)
    assert len(result) == 1 and result[0].close_time == NOW
    assert result[0].available_at == NOW and result[0].taker_buy_volume == 4
    assert http.calls[0][1]["params"]["interval"] == "1m"


async def test_historical_candle_fetch_is_not_historically_available():
    http = MemoryHTTP({"/api/v3/klines": [kline(PAST - timedelta(minutes=1))]})
    result = await BinanceSpotProvider(http, Settings()).candles(asset(), "1m", 1, PAST)
    assert result[0].close_time == PAST and result[0].available_at == NOW


@pytest.mark.parametrize("frame,expected", [("1D", "1d"), ("1W", "1w")])
async def test_native_timeframe_case_mapping(frame, expected):
    http = MemoryHTTP({"/api/v3/klines": []})
    await BinanceSpotProvider(http, Settings()).candles(asset(), frame, 1, NOW)
    assert http.calls[0][1]["params"]["interval"] == expected


async def test_invalid_candle_cannot_be_silently_used():
    http = MemoryHTTP({"/api/v3/klines": [kline(NOW - timedelta(minutes=1), close=500)]})
    with pytest.raises(ProviderError, match="invalid candle"):
        await BinanceSpotProvider(http, Settings()).candles(asset(), "1m", 1, NOW)


async def test_liquidity_uses_weaker_side_not_total_and_labels_usdt():
    http = MemoryHTTP(
        {
            "/api/v3/depth": {"bids": [["100", "5"], ["99", "100"]], "asks": [["101", "1"], ["102", "100"]]},
            "/api/v3/ticker/24hr": {"closeTime": int(NOW.timestamp() * 1000), "quoteVolume": "10000"},
        }
    )
    result = await BinanceSpotProvider(http, Settings()).liquidity(asset(), PAST)
    assert result["depth_usd"].value == pytest.approx(101)
    assert result["bid_depth_usd"].value == pytest.approx(500)
    assert result["spread_bps"].value == pytest.approx(1 / 100.5 * 10000)
    assert result["best_bid"].value == 100 and result["best_ask"].value == 101
    assert result["book_mid"].value == pytest.approx(100.5)
    assert result["best_ask"].available_at == NOW
    assert "USDT" in result["depth_usd"].unit
    assert result["quote_volume_24h"].available_at == NOW


async def test_capped_book_does_not_claim_complete_one_percent_liquidity():
    bids = [[str(100 - i * 0.0005), "1"] for i in range(1000)]
    asks = [[str(100.001 + i * 0.0005), "1"] for i in range(1000)]
    http = MemoryHTTP(
        {
            "/api/v3/depth": {"bids": bids, "asks": asks},
            "/api/v3/ticker/24hr": ProviderError("mock", "ticker offline"),
        }
    )
    result = await BinanceSpotProvider(http, Settings()).liquidity(asset(), NOW)
    assert result["depth_usd"].value is None
    assert result["spread_bps"].value is not None
    assert result["quote_volume_24h"].value is None


def derivatives_http(*, funding_failure=False):
    hourly = [
        {
            "timestamp": int((NOW - timedelta(hours=24 - i)).timestamp() * 1000),
            "sumOpenInterest": str(100 + i / 24 * 100),
            "sumOpenInterestValue": str(1000 + i / 24 * 8000),
        }
        for i in range(25)
    ]

    def volume_rows(multiplier):
        rows = []
        for index in range(24):
            opened = NOW - timedelta(hours=24 - index)
            row = kline(opened)
            row[6] = int((opened + timedelta(hours=1)).timestamp() * 1000) - 1
            row[7] = str(100 * multiplier)
            rows.append(row)
        return rows

    return MemoryHTTP(
        {
            "/fapi/v1/exchangeInfo": {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                    }
                ]
            },
            "/futures/data/openInterestHist": hourly,
            "/fapi/v1/fundingRate": ProviderError("mock", "funding offline")
            if funding_failure
            else [
                {"fundingTime": int((NOW - timedelta(hours=2)).timestamp() * 1000), "fundingRate": "0.001"},
                {"fundingTime": int(NOW.timestamp() * 1000), "fundingRate": "0.002"},
            ],
            "/fapi/v1/premiumIndex": {
                "time": int(NOW.timestamp() * 1000),
                "markPrice": "101",
                "indexPrice": "100",
            },
            "/fapi/v1/ticker/24hr": {"closeTime": int(NOW.timestamp() * 1000), "quoteVolume": "100000"},
            "/fapi/v1/klines": volume_rows(2),
            "/api/v3/klines": volume_rows(1),
        }
    )


async def test_oi_change_uses_native_units_and_actual_funding_cadence():
    result = await BinanceDerivativesProvider(derivatives_http(), Settings()).metrics(asset(), PAST)
    assert result["open_interest"].value == pytest.approx(200)
    assert result["open_interest_notional"].value == pytest.approx(9000)
    assert result["open_interest_change_pct"].value == pytest.approx(1)
    assert result["funding_interval_hours"].value == 2
    assert result["funding_rate"].value == pytest.approx(0.002)
    assert "settled" in result["funding_rate"].source
    assert result["basis_pct"].value == pytest.approx(0.01)
    assert result["open_interest"].available_at == NOW
    assert all(
        result[key].value is None
        for key in ("liquidation_cluster_usd", "long_short_ratio", "futures_spot_volume_ratio")
    )


async def test_optional_derivatives_endpoint_failure_preserves_other_evidence():
    result = await BinanceDerivativesProvider(derivatives_http(funding_failure=True), Settings()).metrics(
        asset(), NOW
    )
    assert result["funding_rate"].value is None
    assert result["open_interest_change_pct"].value == pytest.approx(1)
    assert result["basis_pct"].value == pytest.approx(0.01)


async def test_spot_futures_divergence_requires_matching_completed_windows():
    http = derivatives_http()
    provider = BinanceDerivativesProvider(http, Settings())
    result = await provider.metrics(asset(), NOW)
    ratio = result["futures_spot_volume_ratio"]
    assert ratio.value == pytest.approx(2)
    assert ratio.observed_at == NOW and ratio.available_at == NOW
    http.fixtures["/api/v3/klines"] = http.fixtures["/api/v3/klines"][:-1]
    result = await provider.metrics(asset(), NOW)
    assert result["futures_spot_volume_ratio"].value is None
    assert "incomplete" in result["futures_spot_volume_ratio"].reason


async def test_spot_futures_divergence_does_not_compare_rolling_ticker_volumes():
    http = derivatives_http()
    http.fixtures["/api/v3/klines"] = ProviderError("mock", "common window offline")
    result = await BinanceDerivativesProvider(http, Settings()).metrics(asset(), NOW)
    assert result["futures_volume_24h"].value == 100000
    assert result["futures_spot_volume_ratio"].value is None


def test_numeric_missing_nan_boolean_and_future_timestamps_are_not_zero():
    for value in (None, True, "not-a-number", float("nan")):
        assert metric(value, "synthetic", "fraction", NOW, NOW).value is None
    assert metric(1, "synthetic", "fraction", NOW + timedelta(seconds=1), NOW).value is None


@pytest.mark.parametrize("header", ["nan", "inf", "-inf", "invalid date"])
async def test_invalid_retry_after_fails_without_unbounded_sleep(header):
    async with AsyncHTTPClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": header})),
        host_intervals={"mock": 0},
    ) as http:
        with pytest.raises(ProviderError, match="invalid Retry-After"):
            await http.get_json("https://mock/test")


@pytest.mark.parametrize("wait", [float("nan"), float("inf"), 0, -1])
def test_retry_wait_configuration_must_be_bounded(wait):
    with pytest.raises(ValueError, match="finite and positive"):
        AsyncHTTPClient(max_retry_wait=wait)


async def test_nonfinite_host_throttle_cannot_disable_rate_limits():
    async with AsyncHTTPClient(host_intervals={"mock": float("nan")}) as http:
        with pytest.raises(ValueError, match="finite and nonnegative"):
            await http.get_json("https://mock/test")


async def test_universe_refresh_discards_removed_asset_metadata():
    http = MemoryHTTP({"/api/v3/coins/markets": [market_record(1), market_record(2)]})
    provider = CoinGeckoProvider(http, Settings())
    first = await provider.universe(2, NOW)
    updated = dict(market_record(1), market_cap=2000)
    replacement = dict(market_record(2), market_cap_rank=1)
    http.fixtures["/api/v3/coins/markets"] = lambda params: [updated] if params.get("ids") else [replacement]
    second = await provider.universe(1, NOW + timedelta(minutes=1))
    assert second[0].asset_id == "asset-2"
    refreshed = await provider.market(first[0], NOW + timedelta(minutes=1))
    assert refreshed["market_cap"].value == 2000
    assert http.calls[-1][1]["params"]["ids"] == "asset-1"


async def test_direct_market_queries_do_not_reuse_previous_scan_metadata():
    http = MemoryHTTP({"/api/v3/coins/markets": [market_record(1)]})
    provider = CoinGeckoProvider(http, Settings())
    target = asset("asset-1", "X1", None, "Asset 1")
    assert (await provider.market(target, NOW))["market_cap"].value == 1000
    http.fixtures["/api/v3/coins/markets"] = [dict(market_record(1), market_cap=2000)]
    assert (await provider.market(target, NOW + timedelta(seconds=1)))["market_cap"].value == 2000
    assert len(http.calls) == 2


async def test_ranked_universe_does_not_fill_missing_top_ranks_with_lower_ranks():
    provider = CoinGeckoProvider(
        MemoryHTTP({"/api/v3/coins/markets": [market_record(1), market_record(501)]}), Settings()
    )
    assert [item.rank for item in await provider.universe(2, NOW)] == [1]
    assert any("1 of requested 2" in issue for issue in provider.issues)


@pytest.mark.parametrize("timestamp", [None, "bad date", "2026-01-02T12:00:00"])
async def test_missing_or_invalid_market_source_time_is_not_freshened(timestamp):
    provider = CoinGeckoProvider(
        MemoryHTTP({"/api/v3/coins/markets": [dict(market_record(1), last_updated=timestamp)]}), Settings()
    )
    result = await provider.market(asset("asset-1", "X1", None, "Asset 1"), NOW)
    assert all(value.value is None for value in result.values())
    assert all("timestamp" in value.reason for value in result.values())


@pytest.mark.parametrize(
    "timestamp", [None, True, float("nan"), int((NOW + timedelta(minutes=1)).timestamp())]
)
async def test_invalid_or_future_global_time_does_not_create_fresh_metrics(timestamp):
    result = await CoinGeckoProvider(
        MemoryHTTP(
            {
                "/api/v3/global": {
                    "data": {
                        "updated_at": timestamp,
                        "total_market_cap": {"usd": 1000, "btc": 1, "eth": 2},
                        "market_cap_percentage": {"btc": 60, "eth": 20},
                    }
                }
            }
        ),
        Settings(),
    ).global_metrics(NOW)
    assert all(value.value is None for value in result.values())


async def test_impossible_dominance_is_not_a_market_regime_input():
    result = await CoinGeckoProvider(
        MemoryHTTP(
            {
                "/api/v3/global": {
                    "data": {
                        "updated_at": int(NOW.timestamp()),
                        "total_market_cap": {"usd": 1000},
                        "market_cap_percentage": {"btc": 120, "eth": 20},
                    }
                }
            }
        ),
        Settings(),
    ).global_metrics(NOW)
    assert result["total_market_cap"].value == 1000
    assert result["btc_dominance"].value is None
    assert result["total2_market_cap"].value is None
    assert result["total3_market_cap"].value is None


@pytest.mark.parametrize("frame", list(TIMEFRAMES))
async def test_every_native_timeframe_requires_exact_duration_and_alignment(frame):
    duration = TIMEFRAMES[frame]
    offset = 4 * 86400 if frame == "1W" else 0
    opened = datetime.fromtimestamp(
        ((int(NOW.timestamp()) - offset) // duration - 1) * duration + offset, UTC
    )
    http = MemoryHTTP({"/api/v3/klines": [kline(opened, duration=duration)]})
    provider = BinanceSpotProvider(http, Settings())
    result = await provider.candles(asset(), frame, 1, NOW)
    assert len(result) == 1
    assert (result[0].close_time - result[0].open_time).total_seconds() == duration
    http.fixtures["/api/v3/klines"] = [kline(opened, duration=duration + 1)]
    with pytest.raises(ProviderError, match="invalid candle"):
        await provider.candles(asset(), frame, 1, NOW)
    http.fixtures["/api/v3/klines"] = [kline(opened + timedelta(seconds=1), duration=duration)]
    with pytest.raises(ProviderError, match="invalid candle"):
        await provider.candles(asset(), frame, 1, NOW)


async def test_boolean_candle_volume_cannot_be_coerced_into_market_data():
    row = kline(NOW - timedelta(minutes=1))
    row[5] = True
    provider = BinanceSpotProvider(MemoryHTTP({"/api/v3/klines": [row]}), Settings())
    with pytest.raises(ProviderError, match="invalid candle"):
        await provider.candles(asset(), "1m", 1, NOW)


@pytest.mark.parametrize("bids", [["100"], [["100", "0"]], [["100", True]]])
async def test_malformed_or_nonexecutable_book_is_missing_evidence(bids):
    result = await BinanceSpotProvider(
        MemoryHTTP(
            {
                "/api/v3/depth": {"bids": bids, "asks": [["101", "1"]]},
                "/api/v3/ticker/24hr": {"closeTime": int(NOW.timestamp() * 1000), "quoteVolume": "10000"},
            }
        ),
        Settings(),
    ).liquidity(asset(), NOW)
    assert result["best_bid"].value is None and result["depth_usd"].value is None
    assert result["quote_volume_24h"].value == 10000


async def test_futures_instruments_refresh_once_per_scan_and_observe_delistings():
    http = derivatives_http()
    provider = BinanceDerivativesProvider(http, Settings())
    first, concurrent = await asyncio.gather(provider.metrics(asset(), NOW), provider.metrics(asset(), NOW))
    assert first["basis_pct"].value == concurrent["basis_pct"].value == pytest.approx(0.01)
    assert sum(path == "/fapi/v1/exchangeInfo" for path, _ in http.calls) == 1
    http.fixtures["/fapi/v1/exchangeInfo"] = {"symbols": []}
    second = await provider.metrics(asset(), NOW + timedelta(minutes=1))
    assert second["basis_pct"].value is None
    assert sum(path == "/fapi/v1/exchangeInfo" for path, _ in http.calls) == 2


async def test_futures_identity_outage_is_shared_within_scan_and_retried_next_scan():
    http = derivatives_http()
    supported = http.fixtures["/fapi/v1/exchangeInfo"]
    http.fixtures["/fapi/v1/exchangeInfo"] = ProviderError("mock", "offline")
    provider = BinanceDerivativesProvider(http, Settings())
    first, second = await asyncio.gather(provider.metrics(asset(), NOW), provider.metrics(asset(), NOW))
    assert first["funding_rate"].value is second["funding_rate"].value is None
    assert len(http.calls) == 1
    http.fixtures["/fapi/v1/exchangeInfo"] = supported
    recovered = await provider.metrics(asset(), NOW + timedelta(minutes=1))
    assert recovered["basis_pct"].value == pytest.approx(0.01)


async def test_future_funding_event_is_not_an_observed_settlement():
    http = derivatives_http()
    http.fixtures["/fapi/v1/fundingRate"].append(
        {"fundingTime": int((NOW + timedelta(hours=1)).timestamp() * 1000), "fundingRate": "0.9"}
    )
    result = await BinanceDerivativesProvider(http, Settings()).metrics(asset(), NOW)
    assert result["funding_rate"].value == pytest.approx(0.002)
    assert result["funding_interval_hours"].value == 2
    assert result["funding_rate"].observed_at == NOW


async def test_duplicate_oi_records_do_not_become_false_oi_expansion():
    http = derivatives_http()
    http.fixtures["/futures/data/openInterestHist"].append(
        dict(http.fixtures["/futures/data/openInterestHist"][-1], sumOpenInterest="10000000")
    )
    result = await BinanceDerivativesProvider(http, Settings()).metrics(asset(), NOW)
    assert result["open_interest_change_pct"].value is None
    assert result["open_interest"].value is None
    assert result["funding_rate"].value == pytest.approx(0.002)
    assert "invalid" in result["open_interest"].reason


async def test_wrong_derivatives_identity_preserves_other_valid_endpoints():
    http = derivatives_http()
    http.fixtures["/fapi/v1/premiumIndex"]["symbol"] = "ETHUSDT"
    result = await BinanceDerivativesProvider(http, Settings()).metrics(asset(), NOW)
    assert result["basis_pct"].value is None
    assert result["open_interest_change_pct"].value == pytest.approx(1)
    assert result["funding_rate"].value == pytest.approx(0.002)


def test_huge_numeric_and_naive_time_cannot_escape_missing_data_contract():
    assert metric(10**10000, "synthetic", "fraction", NOW, NOW).value is None
    assert metric(1, "synthetic", "fraction", NOW.replace(tzinfo=None), NOW).value is None
