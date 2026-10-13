"""No external requests: reuse genuine analysis code with explicit fixtures."""
from datetime import timedelta

import pytest
from test_fast import ASSET, NOW
from test_trade_alert import histories

from expansion_hunter.live_runtime import _calculate_analysis
from expansion_hunter.models import AssetSnapshot
from expansion_hunter.pre_move_runtime import candidate_fast_state
from expansion_hunter.trade_alert import TradeAlertEngine
from expansion_hunter.trade_alert_models import TradeAlertSettings


@pytest.mark.parametrize("short", [False, True])
def test_live_micro_flow_updates_volume_family_without_double_counting(short):
    settings = TradeAlertSettings(watch_score=20, alert_score=40)
    engine = TradeAlertEngine(settings)
    data = histories(short)
    price = data["1m"][-1].close
    market = {"as_of": NOW.isoformat(), "data_quality": {"spot_trades": "AVAILABLE", "order_book": "UNAVAILABLE"},
              "order_flow_windows": {"1m": {"quality": "AVAILABLE", "taker_buy_volume": 10 if short else 90,
                  "taker_sell_volume": 90 if short else 10, "delta": -80 if short else 80,
                  "aggregate_trade_count": 40, "aggregation": "OBSERVED_PARTIAL_WINDOW"}}}
    engine.evaluate(ASSET, data, NOW, NOW, price, data["5m"], market_snapshot=market)
    evidence = engine.observations[ASSET.asset_id]["directions"]["SHORT" if short else "LONG"]
    assert len(evidence["components"]) == 9
    assert evidence["metrics"]["live_taker_buy_ratio"] == (.1 if short else .9)
    assert evidence["metrics"]["observed_trade_delta_1m"] == (-80 if short else 80)
    assert evidence["components"]["spot_volume"]["weight"] == .15


def test_future_native_flow_snapshot_rejected():
    engine = TradeAlertEngine()
    with pytest.raises(ValueError, match="Future microstructure"):
        engine.evaluate(ASSET, histories(), NOW, NOW, 99.9, [],
                        market_snapshot={"as_of": (NOW + timedelta(seconds=1)).isoformat()})


@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
def test_existing_war_room_produces_conditional_forecast_from_frozen_native_level(bias):
    data = histories(bias == "SHORT")
    episode = {"trigger": 100, "atr": 1, "discovered_at": (NOW-timedelta(minutes=2)).isoformat(),
               "phase": "STRUCTURE_WATCH"}
    row = {"asset_id": ASSET.asset_id, "symbol": ASSET.exchange_symbol, "bias": bias,
           "state": "PRE_IGNITION", "as_of": NOW.isoformat()}
    fast = candidate_fast_state(row, {f"{ASSET.asset_id}|{bias}|CONTINUATION": episode})
    boundary = fast["resistance" if bias == "LONG" else "support"]
    assert boundary["price"] == 100
    assert fast["stage"] == "PRE_IGNITION"
    snapshot = AssetSnapshot(asset=ASSET, as_of=NOW, candles=data)
    result = _calculate_analysis(ASSET.exchange_symbol or ASSET.symbol, snapshot, None, None, None, {}, NOW, fast, {}, {}, bias)
    assert "war_room" in result
    assert result["war_room"]["decisions"]
    assert result["war_room"]["probability"] is None if "probability" in result["war_room"] else True
