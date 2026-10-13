from datetime import timedelta

import pytest
from test_fast import ASSET, NOW, bar, pressure_history

from expansion_hunter.trade_alert import TradeAlertEngine, available_bars, score_pressure
from expansion_hunter.trade_alert_models import TradeAlertSettings


def histories(short=False):
    five = [b.model_copy(update={"taker_buy_volume": b.volume * 0.85}) for b in pressure_history()]
    one = [bar(NOW - timedelta(minutes=20 - i), close=99.9) for i in range(21)]
    if short:

        def mirror(b):
            return b.model_copy(
                update={
                    "open": 200 - b.open,
                    "close": 200 - b.close,
                    "high": 200 - b.low,
                    "low": 200 - b.high,
                    "taker_buy_volume": b.volume * 0.15,
                }
            )

        five, one = [mirror(b) for b in five], [mirror(b) for b in one]
    return {"1m": one, "5m": five}


def engine():
    return TradeAlertEngine(TradeAlertSettings(watch_score=20, alert_score=40))


def run(e, data, now=NOW, price=None):
    return e.evaluate(ASSET, data, now, now, price or data["1m"][-1].close, data["5m"])


@pytest.mark.parametrize("short", [False, True])
def test_unknown_asset_gets_directional_early_pressure_and_explained_components(short):
    e = engine()
    events = run(e, histories(short))
    wanted = "SHORT" if short else "LONG"
    alerts = [x for x in events if x.bias == wanted and x.phase == "TRADE_ALERT"]
    assert alerts and alerts[0].status == "ACTIVE"
    a = alerts[0]
    assert a.distance_pct >= 0 and a.invalidation is not None
    assert abs(sum(c["contribution"] or 0 for c in a.components.values()) - a.score) < 1e-9
    assert a.data_quality["CVD"] == "UNAVAILABLE"
    assert run(e, histories(short)) == []


@pytest.mark.parametrize("short", [False, True])
def test_completed_1m_confirms_predefined_trigger_without_waiting_for_5m(short):
    e = engine()
    data = histories(short)
    initial = run(e, data)
    side = "SHORT" if short else "LONG"
    a = next(x for x in initial if x.bias == side and x.phase == "TRADE_ALERT")
    at = NOW + timedelta(minutes=1)
    close = a.trigger_level - 0.12 if short else a.trigger_level + 0.12
    candle = bar(at, close=close, high=max(close + 0.05, 100.3), low=min(close - 0.05, 99.5), open_price=100)
    data["1m"].append(candle)
    events = run(e, data, at, close)
    confirms = [x for x in events if x.bias == side and x.phase == "BREAK_CONFIRMED"]
    assert confirms and confirms[0].trigger_level == a.trigger_level
    assert confirms[0].trigger_discovered_at == NOW
    assert confirms[0].detected_at == at


def test_wick_above_close_back_inside_is_sweep_not_confirmed():
    e = engine()
    data = histories()
    run(e, data)
    at = NOW + timedelta(minutes=1)
    data["1m"].append(bar(at, close=99.92, high=100.2))
    assert not any(x.phase == "BREAK_CONFIRMED" and x.bias == "LONG" for x in run(e, data, at))
    assert any(x.get("last_sweep") for x in e.episodes.values())


def test_native_flow_missing_never_fabricates_high_priority_pressure():
    data = histories()
    data["5m"] = [b.model_copy(update={"taker_buy_volume": None}) for b in data["5m"]]
    assert all(x.phase != "TRADE_ALERT" for x in run(engine(), data))


def test_stale_or_future_bars_cannot_alert():
    e = engine()
    data = histories()
    assert run(e, data, NOW + timedelta(minutes=8)) == []
    data["5m"][-1] = data["5m"][-1].model_copy(update={"available_at": NOW + timedelta(minutes=1)})
    assert not any(x.phase == "TRADE_ALERT" for x in run(engine(), data))
    assert len(available_bars(data["5m"], NOW, "5m")) == 35


def test_restore_is_stale_and_duplicate_episodes_survive_restart():
    e = engine()
    data = histories()
    run(e, data)
    saved = e.export_state()
    fresh = engine()
    fresh.restore_state(saved)
    assert all(x["restart_stale"] for x in fresh.episodes.values())
    assert run(fresh, data) == []


def test_completed_loss_invalidates_confirmed_break():
    e = engine()
    data = histories()
    run(e, data)
    at = NOW + timedelta(minutes=1)
    data["1m"].append(bar(at, close=100.12, high=100.2))
    run(e, data, at, 100.12)
    at += timedelta(minutes=1)
    data["1m"].append(bar(at, close=99.7, high=99.95))
    assert any(x.phase == "INVALIDATED" and x.bias == "LONG" for x in run(e, data, at, 99.7))


def test_giant_move_is_late_not_fresh_entry():
    e = engine()
    data = histories()
    run(e, data)
    at = NOW + timedelta(minutes=1)
    data["1m"].append(bar(at, close=105, high=105.1, open_price=99.9))
    events = run(e, data, at, 105)
    assert any(x.status == "LATE" for x in events)
    assert not any(x.phase == "TRADE_ALERT" and x.status == "ACTIVE" for x in events)


def test_pressure_weights_are_configuration_not_probabilities():
    with pytest.raises(ValueError):
        TradeAlertSettings(weights={"tests": 1})
    score, components = score_pressure({}, "LONG", {}, TradeAlertSettings())
    assert components["spot_volume"]["contribution"] is None
    assert score == 0
