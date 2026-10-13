"""Deterministic native protocol fixtures; these are not live-provider claims."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.config import Settings
from expansion_hunter.fast_models import FastResistance, FastStage, FastState
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.live_models import FeedHealth, LiveFeedSettings
from expansion_hunter.live_runtime import LiveObservationWorker
from expansion_hunter.models import Asset
from expansion_hunter.providers.http import FetchedJSON
from expansion_hunter.providers.universe_stream import UniverseTicker
from expansion_hunter.universe_live import (
    UniverseLiveSettings,
    UniverseLiveWorker,
    _percentiles,
    decode_universe_archive,
)

NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def definition(symbol):
    return {"symbol": symbol, "status": "TRADING", "baseAsset": symbol[:-4],
            "quoteAsset": "USDT", "isSpotTradingAllowed": True}


def rest(symbol, volume=10_000_000):
    return {"symbol": symbol, "lastPrice": "100", "openPrice": "99", "highPrice": "110", "lowPrice": "90",
            "volume": str(volume / 100), "quoteVolume": str(volume), "closeTime": int(NOW.timestamp() * 1000)}


class HTTP:
    def __init__(self, definitions=None, tickers=None):
        self.definitions = definitions or [definition("BTCUSDT"), definition("UNKNOWNUSDT")]
        self.tickers = tickers or [rest("BTCUSDT"), rest("UNKNOWNUSDT")]
        self.calls = []

    async def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        data = {"symbols": self.definitions} if url.endswith("exchangeInfo") else self.tickers
        return FetchedJSON(data=data, retrieved_at=NOW)


def ticker(symbol="UNKNOWNUSDT", seconds=1, price=100, volume=10_000_000):
    at = NOW + timedelta(seconds=seconds)
    return UniverseTicker(symbol=symbol, price=price, open24h=99, high24h=110, low24h=90,
        basevolume24h=volume / 100, quotevolume24h=volume, event_timestamp=at,
        received_timestamp=at + timedelta(milliseconds=20), processing_timestamp=at + timedelta(milliseconds=21))


def build(config=None, http=None, store=None):
    clock = Clock()
    worker = UniverseLiveWorker(Settings(), config, clock=clock, http=http or HTTP(), store=store)
    asyncio.run(worker.bootstrap())
    worker._running = True
    return worker, clock


def test_two_native_requests_not_500_histories_or_books():
    worker, _ = build()
    assert len(worker.http.calls) == 2
    assert [call[1]["weight"] for call in worker.http.calls] == [20, 80]
    assert all("klines" not in call[0] and "depth" not in call[0] for call in worker.http.calls)
    assert worker.asset("UNKNOWNUSDT").asset_id == "binance:spot:UNKNOWNUSDT"
    snapshot = worker.snapshot()
    assert snapshot["counts"]["live"] == 0
    assert snapshot["counts"]["delayed"] == 2
    assert snapshot["assets"][0]["market_cap_quality"] == "UNAVAILABLE"


def test_500_native_liquid_assets_without_manual_or_cg_identity():
    symbols = [f"COIN{index:04d}USDT" for index in range(600)]
    http = HTTP([definition(symbol) for symbol in symbols],
                [rest(symbol, volume=1000 + index) for index, symbol in enumerate(symbols)])
    worker, _ = build(http=http)
    assert len(worker.snapshot()["assets"]) == 500
    assert worker.snapshot()["counts"]["verified_usdt_spot"] == 600
    assert worker.asset("COIN0000USDT") is None
    assert worker.asset("COIN0599USDT") is not None
    assert len(worker._latest) == len(worker._observations) == 500


def test_verified_spot_status_quote_and_permissions_are_required():
    rows = [definition("GOODUSDT"), definition("HALTEDUSDT"), definition("FUTUREUSDT"), definition("XRPUSDC")]
    rows[1]["status"] = "BREAK"
    rows[2]["isSpotTradingAllowed"] = False
    rows[3]["quoteAsset"] = "USDC"
    worker, _ = build(http=HTTP(rows, [rest(row["symbol"]) for row in rows]))
    assert list(worker._assets) == ["GOODUSDT"]
    assert worker.snapshot()["counts"]["exclusion_reasons"] == {
        "NOT_TRADING": 1, "SPOT_PERMISSION_NOT_VERIFIED": 1, "NON_USDT_QUOTE": 1}


def test_safe_existing_asset_identity_is_reused_only_on_exact_native_pair():
    identity = Asset(asset_id="unknown-project", symbol="UNKNOWN", name="Unknown",
                     exchange_symbol="UNKNOWNUSDT")
    worker = UniverseLiveWorker(Settings(), clock=lambda: NOW, http=HTTP(), identities={"UNKNOWNUSDT": identity})
    asyncio.run(worker.bootstrap())
    assert worker.asset("UNKNOWNUSDT").asset_id == "unknown-project"
    worker.identities["UNKNOWNUSDT"] = identity.model_copy(update={"symbol": "OTHER"})
    asyncio.run(worker.bootstrap())
    assert worker.asset("UNKNOWNUSDT").asset_id == "binance:spot:UNKNOWNUSDT"


def test_changed_only_native_stream_does_not_refresh_other_asset():
    worker, clock = build()
    clock.now = NOW + timedelta(seconds=20)
    assert worker.apply([ticker(seconds=20)]) == 1
    clock.now += timedelta(milliseconds=30)
    rows = {row["symbol"]: row for row in worker.snapshot()["assets"]}
    assert rows["UNKNOWNUSDT"]["freshness"] == "LIVE"
    assert rows["BTCUSDT"]["freshness"] == "STALE"
    assert rows["BTCUSDT"]["relative_strength_btc"] is None


def test_duplicate_and_out_of_order_native_updates_rejected():
    worker, _ = build()
    assert worker.apply([ticker(seconds=5)]) == 1
    assert worker.apply([ticker(seconds=5), ticker(seconds=3)]) == 0
    assert worker.health()["events_rejected"] == 2
    assert worker._latest["UNKNOWNUSDT"].event_timestamp == NOW + timedelta(seconds=5)


def test_observed_minute_returns_and_rolling_volume_proxy_are_separate():
    worker, clock = build()
    for seconds in range(1, 63):
        worker.apply([ticker(seconds=seconds, price=100 + seconds / 100,
                            volume=10_000_000 + seconds ** 2), ticker("BTCUSDT", seconds, 100)])
    clock.now += timedelta(seconds=62, milliseconds=30)
    row = next(row for row in worker.snapshot()["assets"] if row["symbol"] == "UNKNOWNUSDT")
    assert row["return_1m"] == pytest.approx((100.62 / 100.02 - 1) * 100)
    assert row["relative_strength_btc_1m"] == row["return_1m"]
    assert row["volume_acceleration"] == pytest.approx(2)
    assert row["volume_acceleration_basis"] == "DERIVED_ROLLING_24H_QUOTE_VOLUME_PROXY"
    assert not {"CVD", "rvol_1m", "taker_buy", "1m_candle"} & row.keys()
    assert worker.promotions()[0]["symbol"] == "UNKNOWNUSDT"
    assert worker.promotions()[0]["readiness"] is None


def test_early_sparse_missing_benchmark_cannot_fake_minute_baseline_or_promotion():
    worker, clock = build()
    worker.apply([ticker(seconds=1), ticker(seconds=120, price=105)])
    clock.now += timedelta(seconds=120, milliseconds=30)
    row = next(row for row in worker.snapshot()["assets"] if row["symbol"] == "UNKNOWNUSDT")
    assert row["return_1m"] is None
    assert row["volume_acceleration"] is None
    assert worker.promotions() == []


def test_short_native_promotions_require_same_objective_evidence_as_long():
    worker, clock = build()
    for seconds in range(1, 63):
        worker.apply([ticker(seconds=seconds, price=100 - seconds / 100), ticker("BTCUSDT", seconds, 100)])
    clock.now += timedelta(seconds=62, milliseconds=30)
    promotions = worker.promotions()
    assert len(promotions) == 1
    assert promotions[0]["bias"] == "SHORT"
    assert promotions[0]["native_instrument_verified"] is True


def test_history_bounded_and_past_mutable_snapshot_refused():
    worker, clock = build(UniverseLiveSettings(observation_capacity=65))
    for seconds in range(1, 300):
        worker.apply([ticker(seconds=seconds)])
    assert len(worker._observations["UNKNOWNUSDT"]) == 65
    clock.now += timedelta(seconds=300)
    assert worker.snapshot(NOW)["status"] == "UNAVAILABLE"
    assert worker.snapshot(NOW)["assets"] == []


def test_midrank_ties_and_activity_rank_not_expansion_probability():
    assert _percentiles({"A": 1, "B": 1, "C": 2}) == {"A": 25, "B": 25, "C": 100}
    worker, _ = build()
    row = worker.snapshot()["assets"][0]
    assert row["rank_kind"] == "LIVE_ACTIVITY_RANK"
    assert row["score_kind"] == "UNCALIBRATED_ACTIVITY_NOT_EXPANSION_PROBABILITY"
    assert "expansion_readiness" not in row


def test_snapshot_clients_cannot_mutate_current_cached_native_data():
    worker, _ = build()
    snapshot = worker.snapshot()
    snapshot["assets"][0]["price"] = -100
    snapshot["assets"][0]["activity_components"]["price_activity"] = -1
    snapshot["counts"]["live"] = 1000
    snapshot["limitations"].clear()
    again = worker.snapshot()
    assert again["assets"][0]["price"] > 0
    assert again["assets"][0]["activity_components"]["price_activity"] >= 0
    assert again["counts"]["live"] == 0
    assert again["limitations"]


def test_same_second_snapshot_never_reuses_cache_evaluated_after_decision_time():
    worker, clock = build()
    worker.apply([ticker(seconds=1, price=101)])
    clock.now += timedelta(seconds=1, milliseconds=30)
    assert any(row["price"] == 101 for row in worker.snapshot()["assets"])
    earlier = NOW + timedelta(seconds=1, milliseconds=10)
    snapshot = worker.snapshot(earlier)
    assert datetime.fromisoformat(snapshot["as_of"]) == earlier
    assert "UNKNOWNUSDT" not in {row["symbol"] for row in snapshot["assets"]}
    assert all(datetime.fromisoformat(row["received_timestamp"]) <= earlier for row in snapshot["assets"])


def test_stop_marks_retained_map_stale_without_deleting_it():
    worker, clock = build()
    worker.apply([ticker(seconds=1)])
    clock.now += timedelta(seconds=1, milliseconds=30)
    assert worker.snapshot()["counts"]["live"] == 1
    worker._running = False
    worker._version += 1
    assert worker.snapshot()["counts"]["live"] == 0
    assert worker.snapshot()["counts"]["stale"] == 2
    assert worker.asset("UNKNOWNUSDT") is not None


def test_minute_archive_is_immutable_compressed_and_replayable(tmp_path):
    store = IntelligenceStore(str(tmp_path / "universe.sqlite3"))
    worker, clock = build(store=store)
    snapshot = worker.snapshot()
    worker.archive(snapshot, [], NOW)
    record = store.object("universe_live_observation", NOW.isoformat(), NOW)
    assert decode_universe_archive(record) == snapshot
    assert store.object("universe_live_observation", NOW.isoformat(), NOW - timedelta(seconds=1)) is None
    assert store.states("universe_live_health")[0]["archive_resolution_seconds"] == 60
    assert worker.health()["archive_count"] == 1
    assert store.states("universe_promotion") == []
    store.close()


def test_future_rest_timestamp_is_not_accepted():
    bad = rest("BADUSDT")
    bad["closeTime"] += 60000
    worker, _ = build(http=HTTP([definition("BADUSDT"), definition("GOODUSDT")], [bad, rest("GOODUSDT")]))
    assert worker.asset("BADUSDT") is None
    assert worker.snapshot()["counts"]["exclusion_reasons"]["MALFORMED_OR_NONCAUSAL_TICKER"] == 1


def test_invalid_universe_settings_rejected():
    with pytest.raises(ValueError):
        UniverseLiveSettings(max_assets=1001)
    with pytest.raises(ValueError):
        UniverseLiveSettings(live_age_seconds=30, stale_age_seconds=15)


def test_native_promotion_enters_real_fast_membership_without_manual_symbol(tmp_path):
    store = IntelligenceStore(str(tmp_path / "promotion.sqlite3"))
    worker, clock = build(store=store)
    for seconds in range(1, 63):
        worker.apply([ticker(seconds=seconds, price=100 + seconds / 100), ticker("BTCUSDT", seconds, 100)])
    clock.now += timedelta(seconds=62, milliseconds=30)
    worker.archive(worker.snapshot(), worker.promotions(), clock.now)
    fast = LiveObservationWorker(Settings(db_path=str(tmp_path / "promotion.sqlite3")),
                                 feed_settings=LiveFeedSettings(max_symbols=5), clock=clock)
    assert set(fast.membership()) == {"UNKNOWNUSDT"}
    assert fast._analysis_bias("UNKNOWNUSDT", None) == "LONG"
    clock.now += timedelta(seconds=181)
    assert fast.membership() == {}
    fast.store.close()
    store.close()


def test_native_promotion_rejects_forged_or_future_or_wrong_identity(tmp_path):
    store = IntelligenceStore(str(tmp_path / "forged.sqlite3"))
    row = {"symbol": "WRONGUSDT", "created_at": NOW.isoformat(), "expires_at": (NOW + timedelta(seconds=120)).isoformat(),
           "source_timestamp": NOW.isoformat(), "native_instrument_verified": False,
           "source": "NATIVE_UNIVERSE_DISCOVERY", "bias": "LONG",
           "asset": Asset(asset_id="wrong", symbol="WRONG", name="Wrong", exchange_symbol="WRONGUSDT").model_dump()}
    store.put_state("universe_promotion", "WRONGUSDT", NOW, row)
    fast = LiveObservationWorker(Settings(db_path=str(tmp_path / "forged.sqlite3")), clock=lambda: NOW)
    assert fast.membership() == {}
    assert fast._native_universe_issues
    fast.store.close()
    store.close()


def test_native_confirmed_setup_retains_fresh_monitor_after_activity_rank_ttl(tmp_path):
    store = IntelligenceStore(str(tmp_path / "retention.sqlite3"))
    worker, clock = build(store=store)
    for seconds in range(1, 63):
        worker.apply([ticker(seconds=seconds, price=100 + seconds / 100), ticker("BTCUSDT", seconds, 100)])
    clock.now += timedelta(seconds=62, milliseconds=30)
    worker.archive(worker.snapshot(), worker.promotions(), clock.now)
    fast = LiveObservationWorker(Settings(db_path=str(tmp_path / "retention.sqlite3")), clock=clock)
    asset = worker.asset("UNKNOWNUSDT")
    discovered = clock.now
    boundary = FastResistance(price=100, lower=99.9, upper=100.1, event_timestamp=NOW,
        discovered_at=discovered, atr=1, kind="FIXTURE_PREDEFINED_RESISTANCE", tests=3, reason="Native protocol test only")
    state = FastState(asset=asset, stage=FastStage.BREAKOUT_CONFIRMED, resistance=boundary,
        promoted_at=discovered, confirmed_at=discovered, last_evaluation_at=discovered,
        stale=False, data_quality={"spot_candles": "AVAILABLE"})
    fast.monitor.states[asset.asset_id] = state
    clock.now += timedelta(seconds=181)
    state.last_evaluation_at = clock.now
    assert set(fast.membership()) == {"UNKNOWNUSDT"}
    assert fast._retained_symbols == {"UNKNOWNUSDT"}
    state.stale = True
    assert fast.membership() == {}
    state.stale = False
    clock.now = discovered + timedelta(seconds=fast.feed_settings.active_setup_retention_seconds)
    state.last_evaluation_at = clock.now
    assert fast.membership() == {}
    fast.store.close()
    store.close()


@pytest.mark.asyncio
async def test_native_array_runtime_yields_and_keeps_all_observations():
    class Stream:
        health = FeedHealth(provider="test-native-protocol")

        def stop(self):
            pass

        async def events(self, stop):
            for seconds in range(1, 101):
                yield [ticker(seconds=seconds)]

    worker = UniverseLiveWorker(Settings(), clock=lambda: NOW + timedelta(seconds=110), stream=Stream(), http=HTTP())
    progress = []

    async def observer():
        for _ in range(20):
            progress.append(worker._accepted)
            await asyncio.sleep(0)

    await asyncio.gather(worker.run(asyncio.Event()), observer())
    assert worker._accepted == 100
    assert len(set(progress)) > 1
    assert worker.health()["running"] is False
    assert worker.snapshot()["counts"]["live"] == 0


@pytest.mark.asyncio
async def test_bootstrap_failure_visible_and_stop_interrupts_retry():
    class FailingHTTP:
        async def get_json(self, *args, **kwargs):
            raise ValueError("provider blocked")

    worker = UniverseLiveWorker(Settings(), UniverseLiveSettings(bootstrap_retry_seconds=1), http=FailingHTTP())
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)
    assert worker.health()["error"] == "NATIVE_BOOTSTRAP:ValueError"
    stop.set()
    await asyncio.wait_for(task, 1)
    assert worker.snapshot()["assets"] == []
