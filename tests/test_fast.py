from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.fast import FastIgnitionMonitor, detect_resistance
from expansion_hunter.fast_models import FastResistance, FastSettings, FastStage, FastState
from expansion_hunter.models import Asset, AssetSnapshot, Candle

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
ASSET = Asset(asset_id="unknown", symbol="UNKNOWN", name="Unknown future asset")


def bar(close_at, frame="1m", *, close=99.9, high=None, low=99.5, volume=100, open_price=99.8, delay=0):
    seconds = {"1m": 60, "5m": 300, "15m": 900}[frame]
    return Candle(
        open_time=close_at - timedelta(seconds=seconds),
        close_time=close_at,
        available_at=close_at + timedelta(seconds=delay),
        open=open_price,
        high=high or max(open_price, close) + 0.05,
        low=min(low, close, open_price),
        close=close,
        volume=volume,
        source="TEST_ONLY",
    )


def snapshot(now, one=None, five=None, fifteen=None):
    return AssetSnapshot(
        asset=ASSET, as_of=now, candles={"1m": one or [], "5m": five or [], "15m": fifteen or []}
    )


def known_state(stage=FastStage.BREAKOUT_PRESSURE):
    return FastState(
        asset=ASSET,
        stage=stage,
        resistance=FastResistance(
            price=100,
            lower=99.95,
            upper=100.05,
            event_timestamp=NOW - timedelta(hours=1),
            discovered_at=NOW,
            atr=0.5,
            kind="PREDEFINED_TEST",
            tests=4,
            reason="Test fixture",
        ),
        candles={"1m": [bar(NOW - timedelta(minutes=20 - i)) for i in range(21)]},
        last_processed={"1m": NOW},
        stale=False,
    )


def monitor(stage=FastStage.BREAKOUT_PRESSURE, **settings):
    instance = FastIgnitionMonitor(FastSettings(**settings), [known_state(stage)])
    return instance


def next_minute(instance, *, close=100.12, high=None, low=99.5, volume=100, delay=0, tick_size=None):
    candle = bar(NOW + timedelta(minutes=1), close=close, high=high, low=low, volume=volume, delay=delay)
    return instance.evaluate(ASSET, snapshot(candle.available_at, [candle]), tick_size=tick_size)


def types(result):
    return [event.event_type for event in result.events]


def pressure_history(price=99.9):
    history = []
    for index in range(36):
        at = NOW - timedelta(minutes=(35 - index) * 5)
        close = price if index == 35 else price - 0.2
        high = 100 if index in (4, 12, 20, 28) else max(close + 0.02, 99.8)
        low = 98.9 + index * 0.018
        history.append(bar(at, "5m", close=close, high=high, low=low, open_price=close - 0.04))
    return history


def test_pressure_emitted_below_resistance_and_components_explained():
    instance = FastIgnitionMonitor(FastSettings(pressure_score=50, loaded_score=45, pre_ignition_score=40))
    result = instance.evaluate(ASSET, snapshot(NOW, [bar(NOW)], pressure_history()))
    assert "BREAKOUT_PRESSURE" in types(result)
    assert result.state.resistance.discovered_at == NOW
    assert result.state.price < result.state.resistance.price
    assert result.state.pressure_metrics["distance_to_resistance_atr"] >= 0
    assert result.state.pressure_metrics["contribution_tests"] is not None


def test_far_price_no_pressure():
    instance = FastIgnitionMonitor()
    history = pressure_history()
    history[-1] = bar(NOW, "5m", close=92, low=91.5, high=93, open_price=92)
    result = instance.evaluate(ASSET, snapshot(NOW, [bar(NOW, close=92, low=91.5, open_price=92)], history))
    assert "BREAKOUT_PRESSURE" not in types(result)


def test_wick_above_close_below_is_sweep_not_confirmed():
    result = next_minute(monitor(), close=99.92, high=100.12)
    assert types(result) == ["LIQUIDITY_SWEEP"]
    assert result.state.confirmed_at is None


def test_completed_1m_breakout_is_immediate_despite_missing_5m_15m_and_futures():
    result = next_minute(monitor())
    assert types(result) == ["1M_BREAKOUT_CONFIRMED"]
    assert result.events[0].quality == "WEAK"
    assert result.events[0].data_quality["order_book"] == "UNAVAILABLE"
    assert result.events[0].data_quality["futures"] == "UNAVAILABLE"


def test_volume_changes_quality_not_price_condition():
    weak = next_minute(monitor(), volume=10)
    strong = next_minute(monitor(), volume=500, low=99.75)
    assert weak.state.stage == strong.state.stage == FastStage.BREAKOUT_CONFIRMED
    assert weak.events[0].quality == "WEAK"
    assert strong.events[0].quality == "STRONG"


def test_atr_and_tick_buffer_both_respected():
    assert "1M_BREAKOUT_CONFIRMED" not in types(next_minute(monitor(), close=100.02))
    assert "1M_BREAKOUT_CONFIRMED" not in types(next_minute(monitor(), close=100.12, tick_size=0.2))
    assert "1M_BREAKOUT_CONFIRMED" in types(next_minute(monitor(), close=100.21, tick_size=0.2))


def test_intraminute_attempt_never_confirmed():
    instance = monitor()
    instance.evaluate(ASSET, snapshot(NOW, [bar(NOW)]))
    result = instance.observe_trade(
        "unknown",
        100.08,
        market_event_at=NOW + timedelta(seconds=10),
        received_at=NOW + timedelta(seconds=11),
    )
    assert types(result) == ["BREAKOUT_ATTEMPT"]
    assert result.state.confirmed_at is None


def test_failed_breakout_after_confirmed():
    instance = monitor()
    next_minute(instance)
    at = NOW + timedelta(minutes=2)
    result = instance.evaluate(ASSET, snapshot(at, [bar(at, close=99.9)]))
    assert types(result) == ["FAILED_BREAKOUT"]
    assert result.state.cooldown_until > at


def test_5m_acceptance_then_15m_structure_do_not_delay_1m():
    instance = monitor()
    assert types(next_minute(instance)) == ["1M_BREAKOUT_CONFIRMED"]
    at = NOW + timedelta(minutes=5)
    one = [
        bar(NOW + timedelta(minutes=i), close=100.2, high=100.3, low=100.1, open_price=100.15)
        for i in range(2, 6)
    ]
    result = instance.evaluate(ASSET, snapshot(at, one, [bar(at, "5m", close=100.2)]))
    assert types(result) == ["5M_ACCEPTED"]
    at = NOW + timedelta(minutes=15)
    one = [
        bar(NOW + timedelta(minutes=i), close=100.3, high=100.4, low=100.1, open_price=100.2)
        for i in range(6, 16)
    ]
    result = instance.evaluate(ASSET, snapshot(at, one, fifteen=[bar(at, "15m", close=100.3)]))
    assert types(result) == ["15M_EXPANSION_CONFIRMED"]


def test_alert_dedup_survives_serialized_restart():
    instance = monitor()
    result = next_minute(instance)
    restored = FastIgnitionMonitor(states=[FastState.model_validate_json(result.state.model_dump_json())])
    assert restored.states["unknown"].stale
    duplicate = next_minute(restored)
    assert duplicate.events == []
    assert not duplicate.state.stale


def test_stale_candles_never_confirm():
    instance = monitor()
    at = NOW + timedelta(minutes=5)
    candle = bar(NOW + timedelta(minutes=1), close=100.12)
    result = instance.evaluate(ASSET, snapshot(at, [candle]))
    assert "1M_BREAKOUT_CONFIRMED" not in types(result)
    assert result.state.stale
    assert "DATA_STALE" in types(result)


def test_duplicate_candle_no_event_and_revisions_do_not_repaint():
    instance = monitor()
    next_minute(instance)
    result = next_minute(instance, close=99.92, high=100.2)
    assert result.events == []
    assert any("revision ignored" in issue for issue in result.issues)
    assert result.state.stage == FastStage.BREAKOUT_CONFIRMED


def test_future_candle_and_unconfirmed_future_pivot_excluded():
    history = pressure_history()
    expected = detect_resistance(history, FastSettings(), NOW)
    history.append(bar(NOW + timedelta(minutes=5), "5m", high=120, close=110))
    actual = detect_resistance(history, FastSettings(), NOW)
    assert expected == actual
    result = monitor().evaluate(ASSET, snapshot(NOW, [bar(NOW + timedelta(minutes=1), close=100.12)]))
    assert "1M_BREAKOUT_CONFIRMED" not in types(result)
    assert any("future" in issue for issue in result.issues)


def test_exact_boundary_and_pre_discovery_candle_cannot_confirm():
    instance = monitor()
    candle = bar(NOW + timedelta(seconds=30), close=100.12)
    result = instance.evaluate(ASSET, snapshot(candle.close_time, [candle]))
    assert "1M_BREAKOUT_CONFIRMED" not in types(result)
    assert next_minute(monitor()).state.confirmed_at == NOW + timedelta(minutes=1)


def test_out_of_order_evaluation_ignored():
    instance = monitor()
    next_minute(instance)
    result = instance.evaluate(ASSET, snapshot(NOW, [bar(NOW)]))
    assert result.events == []
    assert "out-of-order evaluation ignored" in result.issues


def test_latency_and_timestamp_ordering():
    result = next_minute(monitor(), delay=2)
    event = result.events[0]
    assert event.detection_latency_ms == 2000
    assert event.processing_latency_ms == 0
    assert event.market_event_timestamp <= event.data_received_timestamp <= event.evaluation_timestamp
    with pytest.raises(ValueError):
        monitor().observe_trade("unknown", 100.1, market_event_at=NOW, received_at=NOW - timedelta(seconds=1))


def test_stale_trade_cannot_produce_attempt():
    instance = monitor()
    instance.evaluate(ASSET, snapshot(NOW, [bar(NOW)]))
    result = instance.observe_trade(
        "unknown", 100.08, market_event_at=NOW, received_at=NOW + timedelta(seconds=20)
    )
    assert not result.events
    assert result.state.stale


def test_fresh_trade_after_restart_does_not_rehabilitate_candle_history():
    result = monitor().observe_trade(
        "unknown",
        100.08,
        market_event_at=NOW + timedelta(seconds=10),
        received_at=NOW + timedelta(seconds=11),
    )
    assert not result.events
    assert result.state.stale


def test_closed_candle_not_available_until_receipt_boundary():
    instance = monitor()
    close_at = NOW + timedelta(minutes=1)
    candle = bar(close_at, close=100.12, delay=2)
    before = instance.evaluate(ASSET, snapshot(close_at, [candle]))
    assert "1M_BREAKOUT_CONFIRMED" not in types(before)
    received = instance.evaluate(ASSET, snapshot(candle.available_at, [candle]))
    assert types(received) == ["1M_BREAKOUT_CONFIRMED"]


def test_resistance_can_change_before_break_but_cannot_move_after_attempt():
    instance = monitor()
    higher_history = [
        item.model_copy(
            update={
                "open": item.open + 1,
                "high": item.high + 1,
                "low": item.low + 1,
                "close": item.close + 1,
            }
        )
        for item in pressure_history()
    ]
    result = instance.evaluate(ASSET, snapshot(NOW, [bar(NOW, close=100.9)], higher_history))
    assert result.state.resistance.price > 100.5
    second = monitor()
    second.evaluate(ASSET, snapshot(NOW, [bar(NOW)]))
    second.observe_trade(
        "unknown",
        100.08,
        market_event_at=NOW + timedelta(seconds=10),
        received_at=NOW + timedelta(seconds=10),
    )
    at = NOW + timedelta(seconds=20)
    result = second.evaluate(ASSET, snapshot(at, five=higher_history))
    assert result.state.resistance.price == 100
    assert result.state.stage == FastStage.BREAKOUT_ATTEMPT


def test_provider_failure_visible_without_fabricating_data():
    instance = monitor()
    source = snapshot(NOW + timedelta(minutes=10))
    source.issues.append("native spot provider unavailable: HTTP 451")
    result = instance.evaluate(ASSET, source)
    assert "native spot provider unavailable: HTTP 451" in result.issues
    assert result.state.data_quality["order_book"] == "UNAVAILABLE"
    assert result.state.stale
    assert "1M_BREAKOUT_CONFIRMED" not in types(result)
