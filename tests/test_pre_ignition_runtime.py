"""Runtime wiring with explicit native-shaped fixtures; no live/edge claims."""

from __future__ import annotations

import asyncio
import copy
from datetime import timedelta

import pytest
from test_trade_alert_runtime import Clock, Engine, Provider, Stream, drain_writes

from expansion_hunter.config import Settings
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.live_models import FeedConnection
from expansion_hunter.pre_ignition import PreIgnitionSettings
from expansion_hunter.pre_move_runtime import candidate_fast_state, select_deep_candidates
from expansion_hunter.trade_alert_models import TradeAlertSettings
from expansion_hunter.trade_alert_runtime import TradeAlertWorker


class Ignition:
    """Isolate delivery behaviour from the separately tested scoring model."""

    def __init__(self):
        self.settings = PreIgnitionSettings()
        self.rows = []
        self.restored = None
        self.calls = []

    def restore_state(self, state):
        self.restored = state

    def checkpoint_state(self):
        return {"version": 1, "fixture": "runtime-delivery-only"}

    def update(self, observations, episodes, as_of):
        self.calls.append((copy.deepcopy(observations), copy.deepcopy(episodes), as_of))
        return copy.deepcopy(self.rows)


@pytest.fixture
def runtime(tmp_path):
    clock, ignition = Clock(), Ignition()
    store = IntelligenceStore(str(tmp_path / "ignition.sqlite3"))
    worker = TradeAlertWorker(Settings(db_path=store.database_path),
        TradeAlertSettings(max_assets=2, history_bars=36, stream_mode="CANDLES", legacy_pre_move_alerts=False),
        store=store, provider=Provider(clock), engine=Engine(), stream_factory=Stream,
        pre_ignition_engine=ignition, clock=clock)
    yield worker, ignition, clock, store
    store.close()


async def prepare(worker, ignition, clock, bias="LONG"):
    await worker.bootstrap()
    await worker._warmup()
    asset_id = worker.assets["ABCUSDT"].asset_id
    price = 99.8 if bias == "LONG" else 100.2
    worker._prices["ABCUSDT"] = (price, clock())
    worker._price_meta["ABCUSDT"] = {"type": "NATIVE_TRADE", "source": "fixture-native-trade",
                                     "exchange_timestamp": clock().isoformat()}
    worker._observations[asset_id] = {"symbol": "ABCUSDT", "price": price,
        "as_of": clock().isoformat(), "status": "EVALUATED",
        "data_quality": {"1m": "AVAILABLE", "5m": "AVAILABLE", "order_book": "UNAVAILABLE"},
        "btc_context": {"status": "UNAVAILABLE"}, "directions": {}}
    row = {"symbol": "ABCUSDT", "asset_id": asset_id, "bias": bias, "state": "READY",
        "state_changed": True, "state_changed_at": clock().isoformat(), "status": "AVAILABLE",
        "as_of": clock().isoformat(), "observation_timestamp": clock().isoformat(),
        "trigger": 100., "trigger_discovered_at": (clock()-timedelta(minutes=5)).isoformat(),
        "price": price, "score": 72., "pre_ignition_score": 72., "early_detection_priority": 89.,
        "invalidation": 99. if bias == "LONG" else 101.,
        "targets": [102.] if bias == "LONG" else [98.], "components": {"compression": {"weight": .2}},
        "metrics": {"resistance_test_count": 3} if bias == "LONG" else {"support_test_count": 3},
        "move_consumed_pct": None, "move_consumed_estimate_pct": None, "move_remaining": "UNKNOWN",
        "reason": "Native-shaped fixture only", "data_quality": {"order_book": "UNAVAILABLE"}}
    ignition.rows = [row]
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
async def test_ready_is_frozen_persisted_notified_and_restart_deduplicated(runtime, monkeypatch, bias):
    worker, ignition, clock, store = runtime
    monkeypatch.setenv("COCKPIT_TELEGRAM_BOT_TOKEN", "test-token-not-a-real-secret")
    monkeypatch.setenv("COCKPIT_TELEGRAM_CHAT_ID", "123456")
    row = await prepare(worker, ignition, clock, bias)
    native_episode_before = copy.deepcopy(worker._episode_state)
    record = worker._emit_pre_ignition_ready(row, clock())
    assert record is not None and record.phase == "TRADE_ALERT" and record.signal_state == "READY"
    assert record.metrics["move_consumed_pct"] is None
    assert record.metrics["pre_ignition"]["move_remaining"] == "UNKNOWN"
    assert record.data_quality["order_book"] == "UNAVAILABLE"
    assert record.metrics["pre_ignition"]["components"]["compression"]["weight"] == .2
    row["components"]["compression"]["weight"] = .99
    assert record.metrics["pre_ignition"]["components"]["compression"]["weight"] == .2
    assert worker._episode_state == native_episode_before
    drain_writes(worker)
    saved = store.objects("trade_alert", clock())
    assert len(saved) == 1 and saved[0]["signal_state"] == "READY"
    assert store.notification_counts()["PENDING"] == 1
    assert worker._emit_pre_ignition_ready(row, clock()) is None
    restored_ignition = Ignition()
    restarted = TradeAlertWorker(worker.settings, worker.config, store=store, provider=worker.provider,
        engine=Engine(), stream_factory=Stream, pre_ignition_engine=restored_ignition, clock=clock)
    assert restored_ignition.restored["fixture"] == "runtime-delivery-only"
    await restarted.bootstrap()
    await restarted._warmup()
    restarted._prices = copy.deepcopy(worker._prices)
    restarted._price_meta = copy.deepcopy(worker._price_meta)
    restarted._observations = copy.deepcopy(worker._observations)
    assert restarted._emit_pre_ignition_ready(row, clock()) is None
    assert len(store.objects("trade_alert", clock())) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
async def test_ready_cannot_be_made_after_quote_crosses_or_extension_is_late(runtime, bias):
    worker, ignition, clock, store = runtime
    row = await prepare(worker, ignition, clock, bias)
    worker._prices["ABCUSDT"] = (100.1 if bias == "LONG" else 99.9, clock())
    assert worker._emit_pre_ignition_ready(row, clock()) is None
    worker._prices["ABCUSDT"] = (row["price"], clock())
    row["move_consumed_estimate_pct"] = 20.01
    assert worker._emit_pre_ignition_ready(row, clock()) is None
    drain_writes(worker)
    assert store.objects("trade_alert", clock()) == []


@pytest.mark.asyncio
async def test_stale_or_future_quote_cannot_trigger_ready(runtime):
    worker, ignition, clock, _ = runtime
    row = await prepare(worker, ignition, clock)
    worker._prices["ABCUSDT"] = (row["price"], clock()+timedelta(seconds=1))
    assert worker._emit_pre_ignition_ready(row, clock()) is None
    worker._prices["ABCUSDT"] = (row["price"], clock())
    clock.now += timedelta(seconds=16)
    assert worker._emit_pre_ignition_ready(row, clock()) is None


@pytest.mark.asyncio
async def test_publish_integrates_new_rows_while_retaining_legacy_snapshot(runtime):
    worker, ignition, clock, store = runtime
    row = await prepare(worker, ignition, clock)
    publisher = asyncio.create_task(worker._publish_loop())
    await asyncio.sleep(.02)
    worker.stop.set()
    await publisher
    drain_writes(worker)
    published = worker.snapshot()
    assert published["pre_ignition"][0]["state"] == "READY"
    assert "pre_move" in published and "pre_move_statealerts" in published
    asset = next(item for item in published["assets"] if item["symbol"] == "ABCUSDT")
    assert asset["pre_ignition"]["LONG"]["early_detection_priority"] == 89
    assert store.objects("trade_alert_pre_ignition_transition", clock())
    assert store.get_state("trade_alert_pre_ignition_rows", "default")["rows"][0]["state"] == "READY"
    assert ignition.calls[0][0][row["asset_id"]]["status"] == "EVALUATED"
    restarted = TradeAlertWorker(worker.settings, worker.config, store=store, provider=worker.provider,
        engine=Engine(), stream_factory=Stream, pre_ignition_engine=Ignition(), clock=clock)
    assert restarted.snapshot()["pre_ignition"][0]["status"] == "STALE"


@pytest.mark.asyncio
async def test_candle_features_are_cached_but_native_snapshot_is_not_fabricated(runtime):
    worker, ignition, clock, _ = runtime
    await prepare(worker, ignition, clock)
    history = {frame: worker._history("ABCUSDT", frame, clock()) for frame in ("1m", "5m")}
    first = worker._native_fast_features("ABCUSDT", history, clock())
    clock.now += timedelta(seconds=1)
    again = worker._native_fast_features("ABCUSDT", history, clock())
    assert first == again and worker._counts["fast_feature_revisions"] == 1
    assert first["trade_count"] is None
    first["quality"] = "CORRUPTED_TEST_COPY"
    assert worker._native_fast_features("ABCUSDT", history, clock())["quality"] == "AVAILABLE"
    snapshot = worker._state("ABCUSDT").snapshot("ABCUSDT", clock(), include_history=False)
    assert snapshot["data_quality"]["order_book"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_configured_capacity_does_not_claim_live_scanner_coverage(runtime):
    worker, ignition, clock, _ = runtime
    await prepare(worker, ignition, clock)
    snapshot = worker.snapshot()
    assert snapshot["coverage"]["configured"] == 2
    assert snapshot["coverage"]["selected"] == 2
    assert snapshot["coverage"]["live"] == 0
    assert snapshot["coverage"]["scanner_status"] == "SCANNER_OFFLINE"
    worker._running = True
    assert worker.snapshot()["coverage"]["live"] == 1
    assert worker.snapshot()["coverage"]["scanner_status"] == "DATA_GAP"
    worker._streams[0].health.connection = FeedConnection.DISCONNECTED
    assert worker.snapshot()["coverage"]["live"] == 0


def test_deep_worker_prefers_change_over_absolute_score_and_bounds_candidates():
    rows = [{"symbol": "STABLE", "asset_id": "a", "bias": "LONG", "score": 73,
             "early_detection_priority": 22, "state": "WATCH", "status": "AVAILABLE"},
            {"symbol": "CHANGING", "asset_id": "b", "bias": "SHORT", "score": 58,
             "early_detection_priority": 88, "state": "RADAR", "status": "AVAILABLE"},
            {"symbol": "STALE", "asset_id": "c", "bias": "LONG", "score": 100,
             "early_detection_priority": 100, "state": "READY", "status": "STALE"}]
    assert list(select_deep_candidates({"pre_ignition_enabled": True, "pre_ignition": rows}, 1)) == ["CHANGING"]
    assert list(select_deep_candidates({"pre_move": rows[:2]}, 1)) == ["STABLE"]
    assert select_deep_candidates({"pre_ignition_enabled": True, "pre_ignition": rows}, 0) == {}
    many = [dict(rows[0], symbol=f"COIN{i}") for i in range(50)]
    assert len(select_deep_candidates({"pre_ignition_enabled": True, "pre_ignition": many}, 50)) == 40


@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
def test_deep_analysis_uses_frozen_ready_boundary_without_inventing_confirmation(bias):
    row = {"asset_id": "a", "bias": bias, "state": "READY", "as_of": "2026-10-08T12:00:00+00:00",
           "trigger": 100., "trigger_discovered_at": "2026-10-08T11:55:00+00:00",
           "metrics": {"trigger_atr": 1.}}
    fast = candidate_fast_state(row, {})
    assert fast["stage"] == "LOADED"
    assert fast["support" if bias == "SHORT" else "resistance"]["price"] == 100
    assert "confirmed_at" not in fast


def test_pre_ignition_settings_are_validated_and_can_explicitly_be_disabled():
    settings = TradeAlertSettings(pre_ignition={"enabled": False})
    assert not PreIgnitionSettings.model_validate(settings.pre_ignition).enabled
    with pytest.raises(ValueError, match="thresholds must be ordered"):
        TradeAlertSettings(pre_ignition={"radar_score": 80, "ready_score": 60})
