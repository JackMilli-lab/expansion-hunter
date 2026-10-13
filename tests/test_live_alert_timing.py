"""Measure calculation completion at the live boundary, not inside pure replay.

Synthetic native-format events and an injected clock make calculation and
writer delays deterministic; these tests do not measure external latency.
"""

from datetime import datetime, timedelta

import pytest
from test_live_runtime import ASSET, NOW, native_candle, seed_pressure

from expansion_hunter.config import Settings
from expansion_hunter.fast_models import FastEvent, FastSettings, FastStage
from expansion_hunter.live_runtime import LiveObservationWorker


@pytest.fixture
def timed_worker(tmp_path):
    current = [NOW]
    instance = LiveObservationWorker(
        Settings(db_path=str(tmp_path / "alert-timing.sqlite3")),
        fast_settings=FastSettings(pressure_score=50, loaded_score=45, pre_ignition_score=40),
        clock=lambda: current[0],
    )
    instance.assets = {"BTCUSDT": ASSET}
    instance.test_clock = current
    try:
        yield instance
    finally:
        instance.store.close()


def advancing_calculation(worker, monkeypatch, duration):
    original = worker.monitor.evaluate
    seen = []

    def evaluate(*args, **kwargs):
        result = original(*args, **kwargs)
        seen.extend(item.model_copy(deep=True) for item in result.events)
        worker.test_clock[0] += duration
        return result

    monkeypatch.setattr(worker.monitor, "evaluate", evaluate)
    return seen


def apply_close(worker):
    item = native_candle(NOW + timedelta(minutes=1))
    worker.test_clock[0] = item.processed_timestamp
    worker.apply(item)
    return item


def confirmed(worker):
    rows = worker.store.objects("fast_event", worker.test_clock[0])
    return FastEvent.model_validate(next(row for row in rows if row["event_type"] == str(FastStage.BREAKOUT_CONFIRMED)))


def test_live_alert_records_completed_calculation_and_keeps_known_inputs(timed_worker, monkeypatch):
    seed_pressure(timed_worker)
    duration = timedelta(milliseconds=17)
    emitted = advancing_calculation(timed_worker, monkeypatch, duration)
    received = apply_close(timed_worker)
    event = confirmed(timed_worker)
    before = next(item for item in emitted if item.event_type == str(FastStage.BREAKOUT_CONFIRMED))

    assert event.evaluation_timestamp == received.processed_timestamp + duration
    assert event.alert_created_timestamp == event.evaluation_timestamp
    assert event.processing_latency_ms == pytest.approx(19)
    assert event.detection_latency_ms == event.alert_latency_ms == pytest.approx(269)
    assert event.market_event_timestamp == before.market_event_timestamp
    assert event.candle_close_timestamp == before.candle_close_timestamp
    assert event.data_received_timestamp == before.data_received_timestamp
    assert event.resistance == before.resistance
    assert event.metrics == before.metrics
    assert event.event_id == before.event_id
    assert timed_worker.monitor.states[ASSET.asset_id].last_evaluation_at == received.processed_timestamp

    research = next(row for row in timed_worker.store.objects("research_event", event.alert_created_timestamp)
                    if row["event_id"] == event.event_id)
    assert datetime.fromisoformat(research["decision_timestamp"]) == event.evaluation_timestamp
    assert datetime.fromisoformat(research["available_at"]) == event.alert_created_timestamp
    earlier = timed_worker.store.objects("fast_event", received.processed_timestamp, asset_id=ASSET.asset_id)
    assert all(row["event_type"] != str(FastStage.BREAKOUT_CONFIRMED) for row in earlier)


def test_fixed_clock_retains_exact_existing_timestamp_semantics(timed_worker):
    seed_pressure(timed_worker)
    received = apply_close(timed_worker)
    event = confirmed(timed_worker)
    assert event.evaluation_timestamp == event.alert_created_timestamp == received.processed_timestamp
    assert event.detection_latency_ms == event.alert_latency_ms == pytest.approx(252)
    assert event.processing_latency_ms == pytest.approx(2)


def test_writer_queue_delay_is_excluded_from_calculation_creation_timing(timed_worker, monkeypatch):
    seed_pressure(timed_worker)
    duration = timedelta(milliseconds=31)
    emitted = advancing_calculation(timed_worker, monkeypatch, duration)
    timed_worker._writer_active = True
    received = apply_close(timed_worker)
    completed_at = received.processed_timestamp + duration
    queued = list(timed_worker._critical_writes)
    assert queued
    assert not any(row["event_type"] == str(FastStage.BREAKOUT_CONFIRMED)
                   for row in timed_worker.store.objects("fast_event", completed_at))

    timed_worker.test_clock[0] += timedelta(seconds=5)
    timed_worker._critical_writes.clear()
    timed_worker._writer_active = False
    for operation, arguments, _ in queued:
        operation(*arguments)

    event = confirmed(timed_worker)
    original = next(item for item in emitted if item.event_type == str(FastStage.BREAKOUT_CONFIRMED))
    assert event.evaluation_timestamp == event.alert_created_timestamp == completed_at
    assert event.processing_latency_ms == pytest.approx(33)
    assert event.detection_latency_ms == event.alert_latency_ms == pytest.approx(283)
    assert event.event_id == original.event_id
    row = timed_worker.store._db.execute(
        "SELECT created_at FROM notification_outbox WHERE event_id=?", (event.event_id,)
    ).fetchone()
    assert datetime.fromisoformat(row[0]) == completed_at


def test_completion_stamp_preserves_deduplication_across_a_duplicate_close(timed_worker, monkeypatch):
    seed_pressure(timed_worker)
    advancing_calculation(timed_worker, monkeypatch, timedelta(milliseconds=13))
    item = apply_close(timed_worker)
    before = timed_worker.store.objects("fast_event", timed_worker.test_clock[0])
    event = confirmed(timed_worker)
    timed_worker.test_clock[0] += timedelta(seconds=1)
    timed_worker.apply(item)
    after = timed_worker.store.objects("fast_event", timed_worker.test_clock[0])
    assert after == before
    assert sum(row["event_id"] == event.event_id for row in after) == 1
