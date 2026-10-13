"""Native chart requests preserve causal evidence and bounded history paging."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from test_active_fibonacci import ASSET, POINT, source

from expansion_hunter import native_chart
from expansion_hunter.config import Settings
from expansion_hunter.models import TIMEFRAMES
from expansion_hunter.native_chart import NativeChartService
from expansion_hunter.providers import ProviderError
from expansion_hunter.trade_alert_api import install_trade_alert_routes


class ChartProvider:
    """Recording transport replacement; every bar remains synthetic test data."""

    def __init__(self, bars=(), failure=None, *, filter_by_cutoff=True):
        self.bars = list(bars)
        self.failure = failure
        self.calls = []
        self.filter_by_cutoff = filter_by_cutoff

    async def candles(self, asset, frame, limit, as_of):
        self.calls.append((asset, frame, limit, as_of))
        await asyncio.sleep(0)
        if self.failure:
            raise self.failure
        bars = [bar for bar in self.bars if bar.close_time <= as_of] if self.filter_by_cutoff else self.bars
        return bars[-limit:]


def worker_with(bars, *, frame="5m", provider=None):
    histories = bars if isinstance(bars, dict) else {frame: bars}
    return SimpleNamespace(
        assets={ASSET.exchange_symbol: ASSET},
        settings=Settings(),
        config=SimpleNamespace(auto_fibonacci_enabled=True),
        provider=provider or ChartProvider(),
        _history=lambda symbol, timeframe, as_of: histories.get(timeframe, []),
        _state=lambda symbol: SimpleNamespace(snapshot=lambda *args, **kwargs: {"price": 112.}),
        snapshot=lambda: {"status": "RUNNING", "running": True, "assets": []},
    )


@pytest.fixture
def chart_app(monkeypatch):
    monkeypatch.delenv("COCKPIT_PUBLIC_MARKET_DATA", raising=False)
    monkeypatch.setattr("expansion_hunter.models.utcnow", lambda: POINT)
    app = FastAPI()
    app.state.trade_alert_worker = worker_with({frame: source(frame) for frame in TIMEFRAMES if frame != "1W"})

    async def authorize(authorization: str | None = Header(default=None)):
        if authorization != "Bearer CHART_TEST_ACCESS":
            raise HTTPException(401, "Valid bearer token required")

    install_trade_alert_routes(app, authorize)
    return app


@pytest.mark.parametrize("short", [False, True])
async def test_chart_fibonacci_is_derived_from_selected_native_frame(short):
    bars = source("5m", short=short)
    worker = worker_with({"5m": bars, "4h": source("4h", offset=250)})
    result = await NativeChartService().fibonacci(worker, ASSET.exchange_symbol, "5m", bars, POINT)
    assert set(result["active_fibonacci"]) == {"5m"}
    view = result["active_fibonacci"]["5m"]
    assert view["status"] == "AVAILABLE"
    assert view["bias"] == ("SHORT" if short else "LONG")
    assert view["context"]["current_price"] == bars[-1].close
    assert view["golden_pocket"]["ratios"] == [0.618, 0.65]
    assert all(level["timeframe"] == "5m" for level in view["levels"])
    metadata = result["fibonacci_as_of"]
    assert metadata["timeframe"] == "5m"
    assert metadata["candle_count"] == len(bars)
    assert metadata["source_timestamp"] == bars[-1].close_time.isoformat()
    assert metadata["received_timestamp"] == bars[-1].available_at.isoformat()


@pytest.mark.parametrize("bad", ["forming", "delayed", "gap", "stale"])
async def test_chart_fibonacci_filters_unavailable_bars_and_fails_closed(bad):
    bars = source()
    service = NativeChartService()
    worker = worker_with(bars)
    as_of = POINT
    if bad == "forming":
        altered = bars + [bars[-1].model_copy(update={
            "open_time": POINT, "close_time": POINT + timedelta(minutes=5),
            "available_at": POINT + timedelta(minutes=5), "high": 10000.,
        })]
        expected = await service.fibonacci(worker, ASSET.exchange_symbol, "5m", bars, as_of)
    elif bad == "delayed":
        altered = bars[:-1] + [bars[-1].model_copy(update={"available_at": POINT + timedelta(minutes=5)})]
        expected = await service.fibonacci(worker, ASSET.exchange_symbol, "5m", bars[:-1], as_of)
    elif bad == "gap":
        altered = bars[:20] + bars[21:]
    else:
        altered = bars
        as_of += timedelta(hours=1)
    result = await service.fibonacci(worker, ASSET.exchange_symbol, "5m", altered, as_of)
    if bad in {"forming", "delayed"}:
        assert result["active_fibonacci"] == expected["active_fibonacci"]
    else:
        view = result["active_fibonacci"]["5m"]
        assert view["status"] == ("STALE" if bad == "stale" else "INSUFFICIENT_DATA")
        assert view["levels"] == []
        assert view["golden_pocket"] is None


async def test_fibonacci_coalesces_reuses_geometry_and_refreshes_candle_revisions(monkeypatch):
    calculate = native_chart._calculate_fibonacci
    calculations = []

    def record_calculation(*args):
        calculations.append(args)
        return calculate(*args)

    monkeypatch.setattr(native_chart, "_calculate_fibonacci", record_calculation)
    bars = source()
    worker = worker_with(bars)
    service = NativeChartService()
    results = await asyncio.gather(*[
        service.fibonacci(worker, ASSET.exchange_symbol, "5m", bars, POINT) for _ in range(3)
    ])
    assert len(calculations) == 1
    original_price = results[1]["active_fibonacci"]["5m"]["levels"][0]["price"]
    results[0]["active_fibonacci"]["5m"]["levels"][0]["price"] = 99999
    later = await service.fibonacci(worker, ASSET.exchange_symbol, "5m", bars, POINT + timedelta(seconds=5))
    assert len(calculations) == 1
    assert later["active_fibonacci"]["5m"]["levels"][0]["price"] == original_price
    assert later["fibonacci_as_of"]["as_of"] == (POINT + timedelta(seconds=5)).isoformat()
    assert later["fibonacci_as_of"]["calculated_at"] == POINT.isoformat()
    revised = bars[:]
    revised[10] = bars[10].model_copy(update={"high": bars[10].high + 0.01})
    await service.fibonacci(worker, ASSET.exchange_symbol, "5m", revised, POINT + timedelta(seconds=5))
    assert len(calculations) == 2
    stale = await service.fibonacci(worker, ASSET.exchange_symbol, "5m", revised, POINT + timedelta(hours=1))
    assert len(calculations) == 3
    assert stale["active_fibonacci"]["5m"]["status"] == "STALE"


async def test_chart_history_first_page_reuses_current_candles_without_rest():
    bars = source()
    worker = worker_with(bars)
    payload, status = await NativeChartService().history(worker, ASSET.exchange_symbol, "5m", None, 12, POINT)
    assert status == 200
    assert payload["status"] == "AVAILABLE"
    assert len(payload["candles"]) == 12
    assert datetime.fromisoformat(payload["candles"][0]["open_time"]) == bars[-12].open_time
    assert payload["next_before"] == bars[-12].open_time.isoformat()
    assert payload["has_more"] is True
    assert worker.provider.calls == []


async def test_history_missing_current_candles_is_unavailable_without_rest():
    worker = worker_with([])
    payload, status = await NativeChartService().history(worker, ASSET.exchange_symbol, "5m", None, 12, POINT)
    assert status == 503
    assert payload["status"] == "UNAVAILABLE"
    assert payload["candles"] == []
    assert payload["next_before"] is None
    assert worker.provider.calls == []


async def test_older_history_normalizes_cursor_and_preserves_retrieval_availability():
    bars = source()
    bound = bars[-10].open_time
    older = [bar.model_copy(update={"available_at": POINT}) for bar in bars if bar.close_time <= bound]
    # A faulty upstream overlap must never leak into the older page.
    provider = ChartProvider(older[-12:] + [bars[-10]], filter_by_cutoff=False)
    worker = worker_with(bars, provider=provider)
    payload, status = await NativeChartService().history(
        worker, ASSET.exchange_symbol, "5m", bound + timedelta(seconds=17), 13, POINT)
    assert status == 200
    assert provider.calls[0][3] == bound
    returned = payload["candles"]
    assert len(returned) == 12
    assert all(datetime.fromisoformat(bar["close_time"]) <= bound for bar in returned)
    assert [bar["open_time"] for bar in returned] == sorted(bar["open_time"] for bar in returned)
    assert all(datetime.fromisoformat(bar["available_at"]) == POINT for bar in returned)
    assert payload["received_at"] == POINT.isoformat()
    assert payload["has_more"] is False
    assert payload["next_before"] is None


async def test_older_history_coalesces_equal_requests_and_uses_cached_page():
    bars = source()
    provider = ChartProvider(bars)
    worker = worker_with(bars, provider=provider)
    service = NativeChartService()
    cursor = bars[-10].open_time
    results = await asyncio.gather(*[
        service.history(worker, ASSET.exchange_symbol, "5m", cursor, 10, POINT)
        for _ in range(3)
    ])
    assert all(status == 200 for _, status in results)
    assert all(payload == results[0][0] for payload, _ in results)
    await service.history(worker, ASSET.exchange_symbol, "5m", cursor, 10, POINT)
    assert len(provider.calls) == 1


async def test_older_history_cache_is_bounded_expires_and_isolated_from_callers(monkeypatch):
    elapsed = [100.]
    monkeypatch.setattr(native_chart, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    bars = source()
    provider = ChartProvider(bars)
    worker = worker_with(bars, provider=provider)
    service = NativeChartService()
    cursors = [bars[-index].open_time for index in range(1, 10)]
    for cursor in cursors:
        payload, status = await service.history(worker, ASSET.exchange_symbol, "5m", cursor, 1, POINT)
        assert status == 200
    assert len(provider.calls) == 9
    assert len(service._history_pages) == 8
    # The oldest ninth-page entry has been evicted.
    await service.history(worker, ASSET.exchange_symbol, "5m", cursors[0], 1, POINT)
    assert len(provider.calls) == 10
    payload["candles"].clear()
    cached, _ = await service.history(worker, ASSET.exchange_symbol, "5m", cursors[-1], 1, POINT)
    assert len(cached["candles"]) == 1
    assert len(provider.calls) == 10
    elapsed[0] += native_chart.HISTORY_TTL_SECONDS + 1
    await service.history(worker, ASSET.exchange_symbol, "5m", cursors[-1], 1, POINT)
    assert len(provider.calls) == 11


async def test_older_history_limits_concurrent_native_downloads():
    gate, entered = asyncio.Event(), asyncio.Event()

    class HoldingProvider(ChartProvider):
        active = 0
        peak = 0

        async def candles(self, *args, **kwargs):
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == 2:
                entered.set()
            try:
                await gate.wait()
                return await super().candles(*args, **kwargs)
            finally:
                self.active -= 1

    bars = source()
    provider = HoldingProvider(bars)
    worker = worker_with(bars, provider=provider)
    service = NativeChartService()
    requests = [asyncio.create_task(service.history(worker, ASSET.exchange_symbol, "5m", bar.open_time, 1, POINT))
                for bar in bars[-5:-1]]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0)
        assert provider.peak == 2
    finally:
        gate.set()
        results = await asyncio.gather(*requests)
    assert all(status == 200 for _, status in results)
    assert len(provider.calls) == 4
    assert provider.peak == 2


async def test_history_provider_failure_and_empty_success_are_distinct():
    bars = source()
    cursor = bars[0].open_time
    provider = ChartProvider(failure=ProviderError("synthetic", "offline"))
    worker = worker_with(bars, provider=provider)
    service = NativeChartService()
    payload, status = await service.history(worker, ASSET.exchange_symbol, "5m", cursor, 10, POINT)
    assert status == 503
    assert payload["status"] == "UNAVAILABLE"
    assert payload["candles"] == []
    assert payload["next_before"] is None
    provider.failure = None
    empty, status = await service.history(worker, ASSET.exchange_symbol, "5m", cursor, 10, POINT)
    assert status == 200
    assert empty["status"] == "AVAILABLE"
    assert empty["candles"] == []
    assert empty["has_more"] is False
    assert empty["next_before"] is None
    assert len(provider.calls) == 2


async def test_native_chart_and_history_routes_require_authorization(chart_app):
    paths = ("/api/market/assets/SYNUSDT", "/api/market/assets/SYNUSDT/history",
             "/api/trade-alert/assets/SYNUSDT/history")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=chart_app), base_url="http://test") as client:
        for path in paths:
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers={"Authorization": "Bearer WRONG"})).status_code == 401
        client.headers["Authorization"] = "Bearer CHART_TEST_ACCESS"
        for path in paths:
            assert (await client.get(path)).status_code == 200


async def test_public_native_history_and_fibonacci_do_not_open_private_routes(chart_app, monkeypatch):
    monkeypatch.setenv("COCKPIT_PUBLIC_MARKET_DATA", "true")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=chart_app), base_url="http://test") as client:
        response = await client.get("/api/market/assets/SYNUSDT", params={"timeframe": "5m"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["active_fibonacci"]["5m"]["status"] == "AVAILABLE"
        assert payload["fibonacci_as_of"]["timeframe"] == "5m"
        assert "pre_move" not in payload and "pre_ignition" not in payload
        history = await client.get("/api/market/assets/SYNUSDT/history", params={"timeframe": "5m", "limit": 12})
        assert history.status_code == 200
        assert len(history.json()["candles"]) == 12
        for path in ("/api/trade-alert/status", "/api/trade-alert/history", "/api/trade-alert/assets/SYNUSDT/history"):
            assert (await client.get(path)).status_code == 401


@pytest.mark.parametrize("params", [
    {"timeframe": "2m"}, {"limit": 0}, {"limit": 1001},
    {"before": "not-a-time"}, {"before": "2026-10-06T00:00:00"},
])
async def test_native_history_route_rejects_invalid_queries(chart_app, params):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=chart_app), base_url="http://test",
                                headers={"Authorization": "Bearer CHART_TEST_ACCESS"}) as client:
        assert (await client.get("/api/market/assets/SYNUSDT/history", params=params)).status_code == 422
        assert chart_app.state.trade_alert_worker.provider.calls == []


async def test_native_history_does_not_expand_market_coverage(chart_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=chart_app), base_url="http://test",
                                headers={"Authorization": "Bearer CHART_TEST_ACCESS"}) as client:
        assert (await client.get("/api/market/assets/UNKNOWNUSDT/history")).status_code == 404
        assert chart_app.state.trade_alert_worker.provider.calls == []
