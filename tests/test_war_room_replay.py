"""Synthetic causal replay integration; no historical profit or edge claims."""

import copy
import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from expansion_hunter.fast_models import FastSettings
from expansion_hunter.models import TIMEFRAMES, Asset, AssetSnapshot, Candle, Metric, Status
from expansion_hunter.war_room_replay import WarRoomReplaySettings, replay_war_room

START = datetime(2025, 1, 1, 12, tzinfo=UTC)


def bar(at, frame="1m", *, close=99.9, high=None, low=99.75, opened=99.8, volume=100, delay=0):
    return Candle(open_time=at - timedelta(seconds=TIMEFRAMES[frame]), close_time=at,
                  available_at=at + timedelta(seconds=delay), open=opened,
                  high=high if high is not None else max(opened, close) + 0.05,
                  low=min(low, opened, close), close=close, volume=volume,
                  source="SYNTHETIC_REPLAY_TEST_ONLY")


def source(*, wick=False, fail=False):
    asset = Asset(asset_id="synthetic-replay-only", symbol="SYN", name="Synthetic replay only",
                  exchange_symbol="SYNUSDT", rank=17)
    one = [bar(START - timedelta(minutes=20 - index)) for index in range(21)]
    for minute in range(1, 16):
        close = 99.92 if wick and minute == 1 else 99.9 if fail and minute == 2 else 100.12 if minute == 1 else 100.2
        one.append(bar(START + timedelta(minutes=minute), close=close,
                       high=100.13 if minute == 1 else 100.3, opened=99.9 if minute == 1 else 100.15,
                       low=99.85 if minute == 1 else 99.75, volume=500 if minute == 1 else 100))
    five = []
    for index in range(36):
        point = START - timedelta(minutes=(35 - index) * 5)
        close = 99.9 if index == 35 else 99.7
        high = 100 if index in (4, 12, 20, 28) else max(close + 0.02, 99.8)
        five.append(bar(point, "5m", close=close, high=high, low=98.9 + index * 0.018, opened=close - 0.04))
    five += [bar(START + timedelta(minutes=minute), "5m", close=100.2, high=100.3, low=99.9, opened=100.1)
             for minute in (5, 10, 15)]
    fifteen = [bar(START - timedelta(minutes=(19 - index) * 15), "15m") for index in range(20)]
    fifteen.append(bar(START + timedelta(minutes=15), "15m", close=100.2, high=100.3, low=99.9, opened=100.1))
    return AssetSnapshot(asset=asset, as_of=START + timedelta(minutes=15),
                         candles={"1m": one, "5m": five, "15m": fifteen})


def config(**kwargs):
    # Explicit deterministic fixture configuration, not a production adjustment.
    return WarRoomReplaySettings(fast=FastSettings(pressure_score=50, loaded_score=45, pre_ignition_score=40), **kwargs)


def boundaries():
    return [START + timedelta(minutes=offset) for offset in (0, 1, 5, 15)]


def test_integrated_pressure_1m_break_5m_acceptance_15m_confirmation():
    result = replay_war_room(source(), boundaries(), settings=config())
    rows = result["evaluations"]
    assert result["mode"] == "HISTORICAL_REPLAY"
    assert not result["execution_enabled"]
    assert result["research_weight"] == 0
    assert result["evaluation_count"] == 4
    assert [row["fast_state"]["stage"] for row in rows] == ["BREAKOUT_PRESSURE", "1M_BREAKOUT_CONFIRMED", "5M_ACCEPTED", "15M_EXPANSION_CONFIRMED"]
    assert rows[0]["fast_state"]["resistance"]["price"] == 100
    assert datetime.fromisoformat(rows[0]["fast_state"]["resistance"]["discovered_at"]) == START
    assert rows[1]["events"][0]["event_type"] == "1M_BREAKOUT_CONFIRMED"
    assert datetime.fromisoformat(rows[1]["events"][0]["candle_close_timestamp"]) == START + timedelta(minutes=1)
    assert rows[1]["fast_state"]["accepted_at"] is None
    assert rows[2]["fast_state"]["structural_at"] is None
    assert rows[3]["war_room"]["mode"] == "DECISION_SUPPORT"
    assert "setup_library" in rows[3]["war_room"]
    assert "expected_path" in rows[3]["war_room"]
    assert "fibonacci" in rows[3]
    assert "relative_context" in rows[3]
    assert set(rows[3]["scores"]) >= {"stored_energy", "absorption", "trigger_sensitivity", "expansion_readiness"}
    json.dumps(result, allow_nan=False)


def test_wick_without_closed_break_is_sweep_not_confirmation():
    result = replay_war_room(source(wick=True), boundaries()[:2], settings=config())
    assert result["evaluations"][1]["fast_state"]["stage"] == "LIQUIDITY_SWEEP"
    assert result["evaluations"][1]["fast_state"]["confirmed_at"] is None
    assert all(event["event_type"] != "1M_BREAKOUT_CONFIRMED" for row in result["evaluations"] for event in row["events"])


def test_exact_candle_close_boundary_cannot_confirm_one_microsecond_early():
    closed = START + timedelta(minutes=1)
    result = replay_war_room(source(), [START, closed - timedelta(microseconds=1), closed], settings=config())
    assert result["evaluations"][1]["fast_state"]["confirmed_at"] is None
    assert result["evaluations"][2]["fast_state"]["stage"] == "1M_BREAKOUT_CONFIRMED"
    assert datetime.fromisoformat(result["evaluations"][2]["events"][0]["market_event_timestamp"]) == closed


def test_actual_delayed_receipt_controls_confirmation_and_replay_latency():
    snapshot = source().model_copy(deep=True)
    closed = START + timedelta(minutes=1)
    received = closed + timedelta(seconds=2)
    next(candle for candle in snapshot.candles["1m"] if candle.close_time == closed).available_at = received
    result = replay_war_room(snapshot, [START, closed, received], settings=config())
    assert result["evaluations"][1]["fast_state"]["confirmed_at"] is None
    event = result["evaluations"][2]["events"][0]
    assert event["event_type"] == "1M_BREAKOUT_CONFIRMED"
    assert datetime.fromisoformat(event["data_received_timestamp"]) == received
    assert datetime.fromisoformat(event["evaluation_timestamp"]) == received
    assert event["detection_latency_ms"] == event["alert_latency_ms"] == 2000
    assert event["processing_latency_ms"] == 0


def test_breakout_that_loses_frozen_level_records_failure():
    points = [START + timedelta(minutes=offset) for offset in (0, 1, 2)]
    result = replay_war_room(source(fail=True), points, settings=config())
    assert result["evaluations"][-1]["fast_state"]["stage"] == "FAILED_BREAKOUT"
    assert result["evaluations"][-1]["events"][0]["event_type"] == "FAILED_BREAKOUT"
    assert result["evaluations"][-1]["fast_state"]["resistance"]["price"] == 100


def test_replay_is_deterministic_and_inputs_are_not_mutated():
    snapshot = source()
    original = snapshot.model_dump(mode="json")
    first = replay_war_room(snapshot, boundaries(), settings=config())
    second = replay_war_room(snapshot, reversed(boundaries()), settings=config())
    assert first == second
    assert snapshot.model_dump(mode="json") == original


def test_duplicate_and_out_of_order_boundaries_are_sorted_and_evaluated_once():
    result = replay_war_room(source(), [boundaries()[2], boundaries()[0], boundaries()[2], boundaries()[1]], settings=config())
    assert result["evaluation_count"] == 3
    assert [row["as_of"] for row in result["evaluations"]] == [point.isoformat() for point in boundaries()[:3]]
    assert "duplicates evaluated once" in result["boundary_policy"]
    events = [event["event_id"] for row in result["evaluations"] for event in row["events"]]
    assert len(events) == len(set(events))


def test_empty_boundaries_do_not_invent_evaluations():
    result = replay_war_room(source(), [])
    assert result["evaluation_count"] == 0
    assert result["evaluations"] == []


def test_naive_timestamps_and_excessive_boundaries_fail_explicitly():
    with pytest.raises(ValueError, match="timezone"):
        replay_war_room(source(), [START.replace(tzinfo=None)])
    with pytest.raises(ValueError, match="maximum"):
        replay_war_room(source(), boundaries(), settings=config(maximum_boundaries=2))


def test_future_suffix_prices_volumes_and_sources_cannot_change_earlier_rows():
    original = source()
    changed = original.model_copy(deep=True)
    cutoff = START + timedelta(minutes=5)
    for frame, candles in changed.candles.items():
        changed.candles[frame] = [candle.model_copy(update={"open": 500, "high": 600, "low": 400,
                                                         "close": 550, "volume": 10000000, "source": "FUTURE_ONLY_MUTATED_SOURCE"})
                                if candle.close_time > cutoff else candle for candle in candles]
    expected = replay_war_room(original, boundaries(), settings=config())
    actual = replay_war_room(changed, boundaries(), settings=config())
    assert actual["evaluations"][:3] == expected["evaluations"][:3]


def test_future_only_timeframe_and_metadata_do_not_leak_their_presence():
    original = source()
    changed = original.model_copy(deep=True)
    changed.candles["1h"] = [bar(START + timedelta(hours=1), "1h", close=500, high=600)]
    changed.market["market_cap"] = Metric(value=999999, observed_at=START, available_at=START + timedelta(hours=1), source="FUTURE_CAP", unit="USD", status=Status.AVAILABLE)
    changed.derivatives["funding"] = Metric(value=0.123, observed_at=START, available_at=START + timedelta(hours=1), source="FUTURE_FUNDING", status=Status.AVAILABLE)
    changed.issues.append("Future provider incident is not a historical diagnosis")
    changed.asset.rank = 1
    changed.asset.name = "Future renamed project"
    expected = replay_war_room(original, boundaries()[:2], settings=config())
    assert replay_war_room(changed, boundaries()[:2], settings=config()) == expected


def test_late_downloaded_history_is_unavailable_until_actual_receipt():
    snapshot = source().model_copy(deep=True)
    for candles in snapshot.candles.values():
        for candle in candles:
            candle.available_at = START + timedelta(days=1)
    result = replay_war_room(snapshot, boundaries(), settings=config())
    assert all(row["war_room"]["status"] == "INSUFFICIENT_DATA" for row in result["evaluations"])
    assert all(row["events"] == [] for row in result["evaluations"])
    assert all(row["fast_state"]["resistance"] is None for row in result["evaluations"])


@pytest.mark.parametrize("bad", ["gap", "duplicate", "mixed_source", "wrong_native_instrument"])
def test_invalid_candle_lineage_never_confirms_breakout(bad):
    snapshot = source().model_copy(deep=True)
    if bad == "gap":
        del snapshot.candles["1m"][5]
    elif bad == "duplicate":
        snapshot.candles["1m"].append(snapshot.candles["1m"][5])
    elif bad == "mixed_source":
        snapshot.candles["1m"][5].source = "OTHER_VENUE"
    else:
        for candle in snapshot.candles["1m"]:
            candle.source = "binance/spot/OTHERUSDT/1m"
    result = replay_war_room(snapshot, boundaries()[:2], settings=config())
    assert all(event["event_type"] != "1M_BREAKOUT_CONFIRMED" for row in result["evaluations"] for event in row["events"])
    assert result["evaluations"][0]["war_room"]["timeframes"]["1m"]["quality"] == "UNAVAILABLE"
    assert result["evaluations"][0]["issues"]


def test_regime_orderbook_cvd_and_intraminute_trades_remain_explicitly_unavailable():
    snapshot = source()
    snapshot.market["observed_depth_quote"] = Metric(value=100000, observed_at=START, available_at=START,
                                                     source="SYNTHETIC_TEST_ONLY_DEPTH_METRIC", status=Status.AVAILABLE)
    result = replay_war_room(snapshot, boundaries()[:2], settings=config())
    for row in result["evaluations"]:
        assert row["war_room"]["market_regime"] == "UNAVAILABLE"
        assert row["fast_state"]["regime"] == "UNAVAILABLE"
        assert row["fast_state"]["data_quality"]["order_book"] == "UNAVAILABLE"
        for field in ("order_book_snapshots", "spot_cvd", "intraminute_trades", "macro_context", "market_regime", "sector_basket"):
            assert row["data_quality"][field] == "UNAVAILABLE"
    assert not any(event["event_type"] == "BREAKOUT_ATTEMPT" for row in result["evaluations"] for event in row["events"])
    assert "not measured" in result["latency_interpretation"]


def test_known_tick_buffer_is_used_without_future_instrument_metadata():
    snapshot = source()
    snapshot.market["tick_size"] = Metric(value=0.2, observed_at=START, available_at=START,
                                          source="SYNTHETIC_TEST_ONLY_TICK", status=Status.AVAILABLE)
    result = replay_war_room(snapshot, boundaries()[:2], settings=config())
    last = result["evaluations"][-1]
    assert last["fast_state"]["stage"] == "BREAKOUT_PRESSURE"
    assert last["fast_state"]["trigger_metrics"]["confirmation_buffer"] == 0.2
    assert last["data_quality"]["tick_size"] == "AVAILABLE"
    future = snapshot.model_copy(deep=True)
    future.market["tick_size"].available_at = START + timedelta(hours=1)
    assert replay_war_room(future, boundaries()[:2], settings=config())["evaluations"][-1]["fast_state"]["stage"] == "1M_BREAKOUT_CONFIRMED"


def test_structure_history_is_carried_forward_without_rewriting_previous_reports():
    points = [START + timedelta(minutes=offset) for offset in range(0, 7)]
    full = replay_war_room(source(), points, settings=config())
    prefix = replay_war_room(source(), points[:2], settings=config())
    assert full["evaluations"][:2] == prefix["evaluations"]
    histories = [row["war_room"]["timeframes"]["1m"]["history"] for row in full["evaluations"]]
    assert len(histories[-1]) > len(histories[0])
    for row in full["evaluations"]:
        decision = datetime.fromisoformat(row["as_of"])
        for frame in row["war_room"]["timeframes"].values():
            for pivot in frame.get("pivots", []):
                assert datetime.fromisoformat(pivot["discovered_at"]) <= decision
            for history in frame.get("history", []):
                assert datetime.fromisoformat(history["discovered_at"]) <= decision


def test_input_hash_uses_visible_data_and_settings_without_future_suffixes():
    result = replay_war_room(source(), boundaries()[:2], settings=config())
    for row in result["evaluations"]:
        assert len(row["visible_snapshot_sha256"]) == len(row["visible_inputs_sha256"]) == 64
    altered = replay_war_room(source(), boundaries()[:2], settings=config(maximum_analysis_bars=64))
    assert result["evaluations"][0]["visible_inputs_sha256"] != altered["evaluations"][0]["visible_inputs_sha256"]


def test_wrong_benchmark_identity_is_not_mislabeled_btc():
    wrong = source().model_copy(deep=True)
    wrong.asset = wrong.asset.model_copy(update={"symbol": "ETH", "exchange_symbol": "ETHUSDT"})
    with pytest.raises(ValueError, match="BTC benchmark instrument"):
        replay_war_room(source(), [START], btc=wrong)


def test_benchmark_future_suffix_is_cut_and_its_known_hash_is_reproducible():
    benchmark = source().model_copy(deep=True)
    benchmark.asset = Asset(asset_id="bitcoin", symbol="BTC", name="Bitcoin", exchange_symbol="BTCUSDT")
    expected = replay_war_room(source(), boundaries()[:2], btc=benchmark, settings=config())
    changed = benchmark.model_copy(deep=True)
    for candles in changed.candles.values():
        for index, candle in enumerate(candles):
            if candle.close_time > START + timedelta(minutes=1):
                candles[index] = candle.model_copy(update={"open": 500, "high": 600, "low": 400, "close": 500})
    assert replay_war_room(source(), boundaries()[:2], btc=changed, settings=config()) == expected
    assert expected["evaluations"][-1]["relative_context"]["windows"]["15m"]["vs_btc"]["value"] == pytest.approx(0)


def test_later_stale_boundary_does_not_claim_a_fresh_breakout():
    result = replay_war_room(source(), [START, START + timedelta(hours=1)], settings=config())
    later = result["evaluations"][-1]
    assert later["fast_state"]["stale"]
    assert later["war_room"]["timeframes"]["1m"]["quality"] == "STALE"
    assert all(event["event_type"] != "1M_BREAKOUT_CONFIRMED" for event in later["events"])


def test_conditional_swing_steps_and_setup_reports_are_integrated_when_history_permits():
    snapshot = source()
    patterned = []
    for index in range(50):
        point = START - timedelta(minutes=(49 - index) * 5)
        price = 100 + 0.3 * index + 3 * math.sin(index * 0.9)
        patterned.append(bar(point, "5m", close=price, opened=price, low=price - 0.4, high=price + 0.4))
    snapshot.candles["5m"] = patterned
    report = replay_war_room(snapshot, [START], settings=config())["evaluations"][0]["war_room"]
    phase = report["swing_phases"]["5m"]
    assert phase["status"] == "CONDITIONAL_RESEARCH"
    assert len(phase["conditional_next_steps"]) == 3
    assert report["patterns"]
    assert report["setup_library"]["research_weight"] == 0
    assert report["expected_path"]["probability"] is None


def test_empty_history_has_no_targets_prices_or_retroactive_alerts():
    snapshot = source().model_copy(update={"candles": {}})
    report = replay_war_room(snapshot, [START], settings=config())["evaluations"][0]
    assert report["events"] == []
    assert report["fast_state"]["price"] is None
    assert report["war_room"]["price"] is None
    assert report["war_room"]["scenarios"]["bullish"]["targets"] == []
    assert report["war_room"]["scenarios"]["bearish"]["targets"] == []


def test_changing_a_future_receipt_to_an_even_later_time_does_not_change_known_outputs():
    original = source()
    changed = copy.deepcopy(original)
    for candles in changed.candles.values():
        for candle in candles:
            if candle.close_time > START + timedelta(minutes=1):
                candle.available_at += timedelta(days=1)
    expected = replay_war_room(original, boundaries()[:2], settings=config())
    assert replay_war_room(changed, boundaries()[:2], settings=config()) == expected
