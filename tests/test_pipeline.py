"""Pipeline invariants using explicitly synthetic fixtures; never market claims.

No test calls an exchange or embeds historical asset outcomes. The fixture prices
exist solely to exercise availability boundaries, validation, and fault isolation.
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from expansion_hunter.config import Settings
from expansion_hunter.models import (
    TIMEFRAMES,
    Asset,
    AssetSnapshot,
    Candle,
    Catalyst,
    Metric,
    RegimeReport,
    ScanBatch,
    ScanResult,
    Stage,
    Status,
    TradePlan,
)
from expansion_hunter.replay import point_in_time, replay
from expansion_hunter.scanner import Scanner
from expansion_hunter.storage import Store

BOUNDARY = datetime(2026, 9, 1, tzinfo=UTC)
SYNTHETIC_SOURCE = "test-only:synthetic-spot"


def asset(identity: str = "synthetic-alpha", rank: int = 1) -> Asset:
    return Asset(asset_id=identity, symbol=identity.upper(), name=f"Synthetic fixture {identity}", rank=rank)


def synthetic_bar(close_time: datetime, frame: str = "1D", index: int = 0) -> Candle:
    # Mild deterministic variation prevents a zero-volatility numerical special case.
    center = 100 + 0.007 * index + 0.3 * math.sin(index / 8)
    return Candle(
        open_time=close_time - timedelta(seconds=TIMEFRAMES[frame]),
        close_time=close_time,
        available_at=close_time,
        open=center - 0.05,
        high=center + 0.4,
        low=center - 0.4,
        close=center + 0.05,
        volume=1000 + index % 13,
        quote_volume=(1000 + index % 13) * center,
        taker_buy_volume=(1000 + index % 13) / 2,
        source=SYNTHETIC_SOURCE,
    )


def synthetic_bars(frame: str, count: int = 240, end: datetime = BOUNDARY) -> list[Candle]:
    interval = timedelta(seconds=TIMEFRAMES[frame])
    return [synthetic_bar(end - interval * (count - i - 1), frame, i) for i in range(count)]


def known_metric(value: float, when: datetime = BOUNDARY, source: str = SYNTHETIC_SOURCE) -> Metric:
    return Metric(value=value, observed_at=when, available_at=when, source=source, status=Status.AVAILABLE)


def synthetic_snapshot(identity: str = "synthetic-alpha", when: datetime = BOUNDARY) -> AssetSnapshot:
    return AssetSnapshot(
        asset=asset(identity),
        as_of=when,
        candles={frame: synthetic_bars(frame, end=when) for frame in ("1D", "4h")},
        market={"market_cap": known_metric(200_000_000, when)},
    )


def stored_result(snapshot: AssetSnapshot) -> ScanResult:
    # This minimal report is a storage fixture, never a computed market decision.
    return ScanResult(
        asset=snapshot.asset,
        as_of=snapshot.as_of,
        stage=Stage.INSUFFICIENT_DATA,
        expansion_score=0,
        confidence=0,
        archetypes={},
        evidence=[],
        technical={},
        regime=RegimeReport(),
        trade=TradePlan(),
    )


def batch(scan_id: str, when: datetime, result: ScanResult) -> ScanBatch:
    return ScanBatch(
        scan_id=scan_id,
        started_at=when,
        as_of=when,
        completed_at=when,
        universe_size=1,
        evaluated=1,
        results=[result],
    )


@pytest.fixture
def store(tmp_path):
    instance = Store(str(tmp_path / "synthetic-tests.sqlite3"))
    yield instance
    instance.close()


@pytest.mark.parametrize("field", ["open_time", "close_time", "available_at"])
def test_candle_requires_timezone(field):
    values = synthetic_bar(BOUNDARY).model_dump()
    values[field] = values[field].replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone"):
        Candle.model_validate(values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("open", float("nan")),
        ("high", float("inf")),
        ("volume", float("-inf")),
        ("close", 0),
        ("volume", -1),
        ("quote_volume", -1),
        ("taker_buy_volume", 1001),
        ("low", 101),
        ("high", 99),
    ],
)
def test_candle_rejects_nonfinite_or_invalid_ohlcv(field, value):
    values = synthetic_bar(BOUNDARY).model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        Candle.model_validate(values)


def test_candle_requires_closed_event_before_availability():
    values = synthetic_bar(BOUNDARY).model_dump()
    values["available_at"] = BOUNDARY - timedelta(seconds=1)
    with pytest.raises(ValidationError, match="event/availability"):
        Candle.model_validate(values)
    values = synthetic_bar(BOUNDARY).model_dump()
    values["open_time"] = BOUNDARY
    with pytest.raises(ValidationError, match="event/availability"):
        Candle.model_validate(values)


def test_metric_availability_and_missing_values_are_explicit():
    with pytest.raises(ValidationError, match="requires"):
        Metric(value=1, source=SYNTHETIC_SOURCE, status=Status.AVAILABLE)
    with pytest.raises(ValidationError, match="availability"):
        Metric(
            value=1,
            source=SYNTHETIC_SOURCE,
            status=Status.AVAILABLE,
            observed_at=BOUNDARY,
            available_at=BOUNDARY - timedelta(seconds=1),
        )
    with pytest.raises(ValidationError):
        known_metric(float("nan"))
    with pytest.raises(ValidationError, match="timezone"):
        known_metric(1, BOUNDARY.replace(tzinfo=None))
    assert known_metric(0).usable(BOUNDARY)
    absent = Metric(source=SYNTHETIC_SOURCE, reason="Not supplied by synthetic fixture")
    assert absent.value is None
    assert not absent.usable(BOUNDARY)
    assert not known_metric(1, BOUNDARY + timedelta(seconds=1)).usable(BOUNDARY)
    assert not known_metric(1, BOUNDARY - timedelta(hours=2)).usable(BOUNDARY, 3600)


def test_snapshot_rejects_unknown_timeframe_and_naive_boundary():
    with pytest.raises(ValidationError, match="unsupported timeframe"):
        AssetSnapshot(asset=asset(), as_of=BOUNDARY, candles={"2m": []})
    with pytest.raises(ValidationError, match="timezone"):
        AssetSnapshot(asset=asset(), as_of=BOUNDARY.replace(tzinfo=None))


def test_quote_volume_must_match_base_volume_and_observed_price_bounds():
    values = synthetic_bar(BOUNDARY).model_dump()
    with pytest.raises(ValidationError, match="traded average"):
        Candle.model_validate({**values, "quote_volume": values["volume"] * values["high"] * 2})
    with pytest.raises(ValidationError, match="positive base volume"):
        Candle.model_validate({**values, "volume": 0, "taker_buy_volume": 0, "quote_volume": 1})


def test_catalyst_requires_causal_aware_times():
    data = dict(
        category="adoption",
        description="Synthetic fixture, not a real announcement",
        source_url="https://example.invalid/synthetic",
        observed_at=BOUNDARY,
        available_at=BOUNDARY,
    )
    with pytest.raises(ValidationError, match="timezone"):
        Catalyst(**{**data, "observed_at": BOUNDARY.replace(tzinfo=None)})
    with pytest.raises(ValidationError, match="precedes"):
        Catalyst(**{**data, "available_at": BOUNDARY - timedelta(seconds=1)})


def test_storage_hides_partial_and_failed_runs_from_published_history(store):
    first = synthetic_snapshot()
    first_result = stored_result(first)
    store.begin("complete", BOUNDARY)
    store.record("complete", first, first_result)
    store.record_global("complete", BOUNDARY, {"total_market_cap": known_metric(1)})
    assert store.latest() is None
    assert store.snapshots(first.asset.asset_id) == []
    assert store.globals_before(BOUNDARY) == []
    store.finish(batch("complete", BOUNDARY, first_result))
    later = BOUNDARY + timedelta(hours=1)
    pending = synthetic_snapshot(when=later)
    store.begin("partial", later)
    store.record("partial", pending, stored_result(pending))
    store.record_global("partial", later, {"total_market_cap": known_metric(999, later)})
    assert store.latest().scan_id == "complete"
    assert store.get_batch("partial") is None
    assert [s.as_of for s in store.snapshots(first.asset.asset_id)] == [BOUNDARY]
    assert [when for when, _ in store.globals_before(later)] == [BOUNDARY]
    store.fail("partial", "Synthetic interruption")
    assert store.latest().scan_id == "complete"
    assert store.get_batch("partial") is None
    assert store.run_status()[0]["status"] == "FAILED"


def test_storage_observations_and_completed_publication_are_immutable(store):
    snapshot = synthetic_snapshot()
    result = stored_result(snapshot)
    store.begin("immutable", BOUNDARY)
    store.record("immutable", snapshot, result)
    with pytest.raises((sqlite3.IntegrityError, RuntimeError, ValueError)):
        store.record("immutable", snapshot, result)
    original = batch("immutable", BOUNDARY, result)
    store.finish(original)
    changed = original.model_copy(update={"results": [], "evaluated": 0})
    store.finish(changed)
    store.fail("immutable", "Must not overwrite completed status")
    assert store.latest() == original
    assert store.run_status()[0]["status"] == "COMPLETE"
    other = synthetic_snapshot("synthetic-beta")
    with pytest.raises((sqlite3.IntegrityError, RuntimeError, ValueError)):
        store.record("immutable", other, stored_result(other))
    with pytest.raises((sqlite3.IntegrityError, RuntimeError, ValueError)):
        store.record_global("immutable", BOUNDARY, {"total_market_cap": known_metric(999)})
    assert store.snapshots("synthetic-beta") == []


def test_storage_point_in_time_history_cutoff(store):
    for index, when in enumerate([BOUNDARY, BOUNDARY + timedelta(days=1)]):
        snap = synthetic_snapshot(when=when)
        run = f"recorded-{index}"
        store.begin(run, when)
        store.record(run, snap, stored_result(snap))
        store.record_global(run, when, {"total_market_cap": known_metric(100 + index, when)})
        store.finish(batch(run, when, stored_result(snap)))
    assert [s.as_of for s in store.snapshots("synthetic-alpha", BOUNDARY)] == [BOUNDARY]
    assert [when for when, _ in store.globals_before(BOUNDARY)] == [BOUNDARY]


def test_recorded_regime_changes_use_completed_causal_baselines_only(store, monkeypatch):
    snapshot = synthetic_snapshot()
    result = stored_result(snapshot)
    store.begin("baseline", BOUNDARY)
    store.record("baseline", snapshot, result)
    store.record_global("baseline", BOUNDARY, {"total_market_cap": known_metric(100)})
    store.finish(batch("baseline", BOUNDARY, result))
    failed_time = BOUNDARY + timedelta(hours=1)
    store.begin("failed-baseline", failed_time)
    store.record_global("failed-baseline", failed_time, {"total_market_cap": known_metric(1, failed_time)})
    store.fail("failed-baseline", "Synthetic interruption")
    when = BOUNDARY + timedelta(days=7)
    engine = scanner(store, monkeypatch)
    output = engine._derive_global_history({"total_market_cap": known_metric(110, when)}, when)
    assert output["total_return_7d"].value == pytest.approx(0.1)
    assert output["total_return_7d"].available_at == when
    assert "total2_return_7d" not in output
    assert "total3_return_7d" not in output
    stale = engine._derive_global_history(
        {"total_market_cap": known_metric(110, when - timedelta(hours=2))}, when
    )
    assert "total_return_7d" not in stale


def test_replay_removes_future_candles_metrics_and_catalysts_without_mutating_input():
    snapshot = synthetic_snapshot()
    future = BOUNDARY + timedelta(hours=4)
    delayed = synthetic_bar(BOUNDARY).model_copy(update={"available_at": future})
    next_bar = synthetic_bar(future, "4h", 241)
    catalyst = Catalyst(
        category="adoption",
        description="Synthetic future event",
        source_url="https://example.invalid/synthetic",
        observed_at=BOUNDARY,
        available_at=future,
        verified=True,
        quantitative=True,
        strength=1,
    )
    snapshot = snapshot.model_copy(
        update={
            "candles": {"1D": [delayed], "4h": [synthetic_bar(BOUNDARY, "4h"), next_bar]},
            "market": {"market_cap": known_metric(1).model_copy(update={"available_at": future})},
            "derivatives": {"funding": known_metric(0.01, future)},
            "catalysts": [catalyst],
        }
    )
    cut = point_in_time(snapshot, BOUNDARY)
    assert cut.as_of == BOUNDARY
    assert cut.candles["1D"] == []
    assert len(cut.candles["4h"]) == 1
    assert cut.catalysts == []
    for item in [cut.market["market_cap"], cut.derivatives["funding"]]:
        assert item.value is None
        assert item.status == Status.UNAVAILABLE
        assert "replay boundary" in item.reason
    assert snapshot.market["market_cap"].value == 1
    assert len(snapshot.candles["4h"]) == 2
    assert snapshot.catalysts == [catalyst]


def test_appending_future_bars_cannot_change_prior_replay_output():
    before = synthetic_snapshot()
    future = BOUNDARY + timedelta(days=3)
    appended = {
        frame: bars
        + [
            synthetic_bar(BOUNDARY + timedelta(seconds=TIMEFRAMES[frame]) * i, frame, 240 + i)
            for i in range(1, 4)
        ]
        for frame, bars in before.candles.items()
    }
    # A giant future candle should not change prior range, anchors, score or plan.
    appended["1D"][-1] = appended["1D"][-1].model_copy(
        update={"open": 1000, "high": 1200, "low": 900, "close": 1100, "volume": 1_000_000}
    )
    after = before.model_copy(update={"as_of": future, "candles": appended})
    settings = Settings(top_n=2, timeframes=["4h", "1D"])
    original = replay(before, [BOUNDARY], settings)[0]
    extended = replay(after, [BOUNDARY], settings)[0]
    assert original.model_dump() == extended.model_dump()
    assert original.trade.action != "ENTRY"
    assert "historical_regime" in original.regime.missing
    assert replay(after, [future, BOUNDARY, BOUNDARY], settings)[0].as_of == BOUNDARY


class SyntheticMarketProvider:
    """Deliberately fake source, scoped exclusively to pipeline tests."""

    issues: list[str] = []

    def __init__(self, universe=None, global_failure=False):
        self.assets = universe if universe is not None else [asset(), asset("synthetic-beta", 2)]
        self.global_failure = global_failure
        self.requested_top_n = None

    async def universe(self, top_n, as_of):
        self.requested_top_n = top_n
        return self.assets

    async def market(self, item, as_of):
        return {"market_cap": known_metric(200_000_000, as_of), "turnover": known_metric(0.02, as_of)}

    async def global_metrics(self, as_of):
        if self.global_failure:
            raise RuntimeError("synthetic global source unavailable")
        return {"btc_dominance": known_metric(55, as_of)}


class SyntheticSpotProvider:
    issues: list[str] = []

    def __init__(self, *, malformed_asset=None, missing_liquidity=None, blocking=False, unmapped=None):
        self.malformed_asset = malformed_asset
        self.missing_liquidity = missing_liquidity
        self.blocking = blocking
        self.unmapped = unmapped
        self.collecting = asyncio.Event()
        self.resolved = []

    async def resolve_assets(self, assets):
        self.resolved.extend(a.asset_id for a in assets)
        return [
            a.model_copy(
                update={
                    "exchange_symbol": None if a.asset_id == self.unmapped else f"SYNTHETIC-{a.asset_id}-USDT"
                }
            )
            for a in assets
        ]

    async def liquidity(self, item, as_of):
        if item.asset_id == self.missing_liquidity:
            raise RuntimeError("synthetic liquidity unavailable")
        return {
            "quote_volume_24h": known_metric(10_000_000, as_of),
            "spread_bps": known_metric(5, as_of),
            "depth_usd": known_metric(100_000, as_of),
        }

    async def candles(self, item, timeframe, limit, as_of):
        if self.blocking:
            self.collecting.set()
            await asyncio.Event().wait()
        if item.asset_id == self.malformed_asset:
            return [{"invalid": "synthetic malformed provider payload"}]
        return synthetic_bars(timeframe, count=limit, end=as_of)


class SyntheticDerivativesProvider:
    issues: list[str] = []

    async def metrics(self, item, as_of):
        if item.asset_id == "synthetic-alpha":
            raise RuntimeError("synthetic derivatives unavailable")
        return {"funding_rate": Metric(source=SYNTHETIC_SOURCE, reason="Not supplied in fixture")}


def scanner(store, monkeypatch, market=None, spot=None, derivatives=None, top_n=2):
    monkeypatch.setattr("expansion_hunter.scanner.utcnow", lambda: BOUNDARY)
    settings = Settings(top_n=top_n, history_bars=220, timeframes=["4h", "1D"], concurrency=2)
    return Scanner(
        market or SyntheticMarketProvider(), spot or SyntheticSpotProvider(), derivatives, settings, store
    )


@pytest.mark.asyncio
async def test_scanner_success_publishes_exact_ranked_universe_and_real_missing_data(store, monkeypatch):
    engine = scanner(store, monkeypatch)
    report = await engine.scan()
    assert report.universe_size == report.evaluated == 2
    assert report.errors == {}
    assert {r.asset.asset_id for r in report.results} == {"synthetic-alpha", "synthetic-beta"}
    assert store.latest() == report
    assert store.run_status()[0]["status"] == "COMPLETE"
    assert all(r.trade.action != "ENTRY" for r in report.results)
    observations = store.snapshots("synthetic-alpha")
    assert len(observations) == 1
    assert observations[0].derivatives == {}
    assert all(bar.source == SYNTHETIC_SOURCE for bar in observations[0].candles["1D"])


@pytest.mark.asyncio
async def test_optional_provider_failures_are_visible_and_do_not_poison_other_assets(store, monkeypatch):
    engine = scanner(
        store,
        monkeypatch,
        market=SyntheticMarketProvider(global_failure=True),
        spot=SyntheticSpotProvider(missing_liquidity="synthetic-alpha"),
        derivatives=SyntheticDerivativesProvider(),
    )
    report = await engine.scan()
    assert report.evaluated == 2
    assert any("Global regime data unavailable" in issue for issue in report.provider_issues)
    alpha = store.snapshots("synthetic-alpha")[0]
    beta = store.snapshots("synthetic-beta")[0]
    assert any("liquidity" in issue for issue in alpha.issues)
    assert any("derivatives" in issue for issue in alpha.issues)
    assert "spread_bps" not in alpha.market
    assert alpha.derivatives == {}
    assert beta.market["spread_bps"].value == 5
    assert beta.derivatives["funding_rate"].value is None
    assert not beta.issues
    assert all(r.trade.action != "ENTRY" for r in report.results)


@pytest.mark.asyncio
async def test_malformed_asset_payload_isolated_with_explicit_error(store, monkeypatch):
    report = await scanner(
        store, monkeypatch, spot=SyntheticSpotProvider(malformed_asset="synthetic-alpha")
    ).scan()
    assert report.evaluated == 1
    assert report.results[0].asset.asset_id == "synthetic-beta"
    assert "Collection failed" in report.errors["synthetic-alpha"]
    assert "ValidationError" in report.errors["synthetic-alpha"]
    assert store.snapshots("synthetic-alpha") == []
    assert store.latest() == report


@pytest.mark.asyncio
async def test_unverified_pair_is_excluded_without_ticker_substitution(store, monkeypatch):
    report = await scanner(store, monkeypatch, spot=SyntheticSpotProvider(unmapped="synthetic-alpha")).scan()
    assert report.evaluated == 1
    assert "No verified exchange pair mapping" in report.errors["synthetic-alpha"]
    assert {r.asset.asset_id for r in report.results} == {"synthetic-beta"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "universe,expected",
    [
        ([], "empty universe"),
        ([asset(), asset().model_copy(update={"symbol": "OTHER"})], "duplicate asset"),
    ],
)
async def test_invalid_universe_fails_visibly_without_completed_batch(store, monkeypatch, universe, expected):
    with pytest.raises(RuntimeError, match=expected):
        await scanner(store, monkeypatch, market=SyntheticMarketProvider(universe=universe)).scan()
    assert store.latest() is None
    status = store.run_status()[0]
    assert status["status"] == "FAILED"
    assert expected in status["error"]


@pytest.mark.asyncio
async def test_malformed_universe_fails_visibly(store, monkeypatch):
    engine = scanner(store, monkeypatch, market=SyntheticMarketProvider(universe=[{"not": "an asset"}]))
    with pytest.raises((RuntimeError, ValidationError, TypeError, AttributeError)):
        await engine.scan()
    assert store.latest() is None
    assert store.run_status()[0]["status"] == "FAILED"
    assert store.run_status()[0]["error"]


@pytest.mark.asyncio
async def test_top_n_cap_applies_even_if_provider_returns_more_assets(store, monkeypatch):
    market = SyntheticMarketProvider(universe=[asset(f"synthetic-{i}", i + 1) for i in range(4)])
    report = await scanner(store, monkeypatch, market=market, top_n=2).scan()
    assert market.requested_top_n == 2
    assert report.universe_size == report.evaluated == 2
    assert {r.asset.asset_id for r in report.results} == {"synthetic-0", "synthetic-1"}


@pytest.mark.parametrize("top_n", [0, 10001])
def test_config_bounds_discoverable_universe(top_n):
    with pytest.raises(ValidationError):
        Settings(top_n=top_n)


@pytest.mark.asyncio
async def test_excluded_ids_remain_inside_ranked_universe_and_are_not_substituted(store, monkeypatch):
    market = SyntheticMarketProvider(universe=[asset("tether", 1), asset("synthetic-beta", 2)])
    report = await scanner(store, monkeypatch, market=market).scan()
    assert report.universe_size == 2
    assert report.evaluated == 1
    assert "explicit asset-ID universe policy" in report.errors["tether"]
    assert report.results[0].asset.asset_id == "synthetic-beta"
    assert store.snapshots("tether") == []


@pytest.mark.asyncio
async def test_repeated_ingestion_preserves_known_candles_but_never_backdates_revisions(store, monkeypatch):
    old = synthetic_snapshot()
    old = old.model_copy(update={"candles": {tf: bars[-220:] for tf, bars in old.candles.items()}})
    store.begin("original-observation", BOUNDARY)
    result = stored_result(old)
    store.record("original-observation", old, result)
    store.finish(batch("original-observation", BOUNDARY, result))
    later = BOUNDARY + timedelta(hours=1)

    class ReFetchedSpot(SyntheticSpotProvider):
        async def candles(self, item, timeframe, limit, as_of):
            bars = [b.model_copy(update={"available_at": later}) for b in old.candles[timeframe]]
            bars[-1] = bars[-1].model_copy(
                update={
                    "volume": bars[-1].volume + 1,
                    "quote_volume": bars[-1].quote_volume + bars[-1].quote_volume / bars[-1].volume,
                }
            )
            return bars

    engine = scanner(store, monkeypatch, spot=ReFetchedSpot())
    monkeypatch.setattr("expansion_hunter.scanner.utcnow", lambda: later)
    observed = await engine._collect(old.asset.model_copy(update={"exchange_symbol": "SYNTHETIC"}), later, [])
    for tf, bars in observed.candles.items():
        assert bars[-2].available_at == old.candles[tf][-2].available_at
        assert bars[-1].available_at == later
        assert old.candles[tf][-1].available_at == BOUNDARY


@pytest.mark.asyncio
async def test_confirmed_candidates_are_recollected_before_publication_with_portfolio(store, monkeypatch):
    from collections import Counter

    from expansion_hunter.risk import PortfolioState

    class CountingSpot(SyntheticSpotProvider):
        def __init__(self):
            super().__init__()
            self.books = Counter()

        async def liquidity(self, item, as_of):
            self.books[item.asset_id] += 1
            return await super().liquidity(item, as_of)

    # Deliberately synthetic eligible stage lets this test exercise orchestration
    # independently of the thoroughly tested technical classifier.
    def synthetic_candidate(snapshot, technical, regime, settings, **kwargs):
        return stored_result(snapshot).model_copy(
            update={
                "stage": Stage.BREAKOUT,
                "expansion_score": 80,
                "confidence": 0.9,
                "technical": technical,
                "regime": regime,
            }
        )

    monkeypatch.setattr("expansion_hunter.scanner.evaluate", synthetic_candidate)
    spot = CountingSpot()
    engine = scanner(store, monkeypatch, spot=spot)
    report = await engine.scan(portfolio=PortfolioState(as_of=BOUNDARY))
    assert report.evaluated == 2
    assert all(spot.books[key] == 2 for key in ["synthetic-alpha", "synthetic-beta", "bitcoin", "ethereum"])
    assert len(store.snapshots("synthetic-alpha")) == 1  # Final immutable decision only.
    assert all(r.as_of == r.regime.as_of == r.regime.available_at for r in report.results)


@pytest.mark.asyncio
async def test_cancellation_never_publishes_a_completed_scan(store, monkeypatch):
    spot = SyntheticSpotProvider(blocking=True)
    engine = scanner(store, monkeypatch, spot=spot)
    task = asyncio.create_task(engine.scan())
    await asyncio.wait_for(spot.collecting.wait(), timeout=2)
    # A second run must not start while the collection boundary is in progress.
    with pytest.raises(RuntimeError, match="already running"):
        await engine.scan()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.latest() is None
    status = store.run_status()[0]
    assert status["status"] == "FAILED"
    assert "CancelledError" in status["error"]
    assert store.get_batch(status["scan_id"]) is None
