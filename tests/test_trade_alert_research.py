"""Explicit synthetic replay/measurement tests; no historical edge evidence."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_fast import NOW, bar
from test_trade_alert import histories

from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.models import Asset, Candle
from expansion_hunter.trade_alert_models import TradeAlertRecord, TradeAlertSettings
from expansion_hunter.trade_alert_research import (
    calculate_trade_alert_outcome,
    evaluate_trade_alert_research,
    purged_chronological_validation,
    replay_trade_alerts,
)

T = datetime(2026, 10, 8, 12, tzinfo=UTC)


def config(**changes):
    return TradeAlertSettings(horizon_seconds=(300,), **changes)


def record(**changes):
    fields = dict(event_id="synthetic-alert", episode_id="synthetic-episode", asset_id="unknown",
        symbol="UNKNOWNUSDT", phase="TRADE_ALERT", bias="LONG", event_timestamp=T, received_at=T,
        detected_at=T, expires_at=T+timedelta(seconds=120), price=100, trigger_level=100,
        trigger_discovered_at=T-timedelta(minutes=1), distance_pct=0, invalidation=98, score=70,
        reason="Explicit synthetic test only", detection_latency_ms=0, alert_latency_ms=0,
        data_quality={"1m": "AVAILABLE", "5m": "AVAILABLE"})
    fields.update(changes)
    return TradeAlertRecord(**fields)


def candle(open_time, close=100, high=None, low=None, *, available_at=None, source="binance-public-ws"):
    return Candle(open_time=open_time, close_time=open_time+timedelta(minutes=1),
        available_at=available_at or open_time+timedelta(minutes=1), open=100, close=close,
        high=high if high is not None else max(100, close)+1,
        low=low if low is not None else min(100, close)-1, volume=100,
        source=source)


def future():
    return [candle(T+timedelta(minutes=index), close=101+index, high=102+index, low=99)
            for index in range(5)]


def test_outcome_ignores_prealert_wicks_and_costs_are_explicit_proxies():
    rows = [candle(T-timedelta(minutes=1), high=9999, low=1), *future()]
    result = calculate_trade_alert_outcome(record(), rows, 300, T+timedelta(minutes=5), config())
    assert result["status"] == "COMPLETE_PROXY"
    assert result["measurement"] == "WHOLE_CANDLE_PROXY"
    assert result["close_return_proxy"] == pytest.approx(.05)
    assert result["mfe_proxy"] == pytest.approx(.06)
    assert result["mae_proxy"] == pytest.approx(-.01)
    assert result["hypothetical_cost_adjusted_close_return"] == pytest.approx(.048)
    assert result["actual_trade_pnl"] is None


def test_short_outcome_uses_directional_excursions():
    result = calculate_trade_alert_outcome(record(bias="SHORT"), future(), 300,
                                          T+timedelta(minutes=5), config())
    assert result["close_return_proxy"] == pytest.approx(-.05)
    assert result["mfe_proxy"] == pytest.approx(.01)
    assert result["mae_proxy"] == pytest.approx(-.06)


def test_partial_alert_minute_is_excluded_instead_of_using_predecision_high():
    rows = future()
    rows[0] = candle(T, close=101, high=9999, low=1)
    result = calculate_trade_alert_outcome(record(detected_at=T+timedelta(seconds=5),
        received_at=T+timedelta(seconds=5)), rows, 300, T+timedelta(minutes=6), config())
    assert result["status"] == "COMPLETE_PROXY"
    assert result["mfe_proxy"] == pytest.approx(.06)
    assert result["observed_seconds"] == 240
    assert result["excluded_head_seconds"] == 55 and result["excluded_tail_seconds"] == 5


@pytest.mark.parametrize("problem", ["gap", "duplicate", "source", "late"])
def test_bad_future_window_is_censored_not_a_negative_example(problem):
    rows = future()
    if problem == "gap":
        rows.pop(2)
    elif problem == "duplicate":
        rows.append(rows[0])
    elif problem == "source":
        rows[0] = rows[0].model_copy(update={"source": "another-venue"})
    else:
        rows[-1] = rows[-1].model_copy(update={"available_at": T+timedelta(days=1)})
    result = calculate_trade_alert_outcome(record(), rows, 300, T+timedelta(minutes=5), config())
    assert result["status"] == "CENSORED"
    assert result["expanded_proxy"] is None and result["mfe_proxy"] is None


def test_horizon_must_mature_before_outcome_is_created():
    assert calculate_trade_alert_outcome(record(), future(), 300, T+timedelta(seconds=299), config()) is None


def replay_payload(symbol="METUSDT"):
    asset = Asset(asset_id=f"synthetic:{symbol}", symbol=symbol[:-4], name="Explicit synthetic test",
                  exchange_symbol=symbol)
    data = histories()
    observations = [{"as_of": NOW.isoformat(), "asset": asset.model_dump(mode="json"),
                     "candles": {frame: [b.model_dump(mode="json") for b in bars] for frame, bars in data.items()}}]
    at = NOW+timedelta(minutes=1)
    data["1m"].append(bar(at, close=100.12, high=100.2))
    observations.append({"as_of": at.isoformat(), "asset": asset.model_dump(mode="json"),
                     "candles": {frame: [b.model_dump(mode="json") for b in bars] for frame, bars in data.items()}})
    return {"evidence": "SYNTHETIC_TEST_ONLY", "observations": observations}


def execute(tmp_path, payload, name="replay.json"):
    path = tmp_path/name
    path.write_text(json.dumps(payload))
    return replay_trade_alerts(path, config(watch_score=20, alert_score=40))


def test_replay_is_deterministic_and_met_identifier_is_not_preferred(tmp_path):
    met = execute(tmp_path, replay_payload())
    repeat = execute(tmp_path, replay_payload(), "repeat.json")
    unknown = execute(tmp_path, replay_payload("UNKNOWNUSDT"), "unknown.json")
    assert met == repeat
    def measured(report):
        return [(row["phase"], row["bias"], row["trigger_level"], row["score"], row["components"])
                for row in report["alerts"]]
    assert measured(met) == measured(unknown)
    assert any(row["phase"] == "BREAK_CONFIRMED" for row in met["alerts"])
    assert met["evidence"] == "SYNTHETIC_TEST_ONLY" and met["execution_enabled"] is False


def test_future_values_cannot_change_prior_replay_decision(tmp_path):
    payload = replay_payload()
    before = execute(tmp_path, payload)
    future_bar = payload["observations"][1]["candles"]["1m"][-1].copy()
    future_bar.update(high=10000, close=9999)
    payload["observations"][0]["candles"]["1m"].append(future_bar)
    after = execute(tmp_path, payload, "future.json")
    assert after["evaluations"][0] == before["evaluations"][0]


@pytest.mark.parametrize("problem", ["backwards", "future_receipt", "future_label"])
def test_replay_rejects_bad_arrival_order_or_future_feature_columns(tmp_path, problem):
    payload = replay_payload()
    if problem == "backwards":
        payload["observations"].reverse()
    elif problem == "future_receipt":
        payload["observations"][0]["received_at"] = (NOW+timedelta(minutes=1)).isoformat()
    else:
        payload["observations"][0]["future_return"] = .2
    with pytest.raises(ValueError):
        execute(tmp_path, payload)


def archive(store, asset_id, bars):
    for value in bars:
        store.append("trade_alert_candle", f"{asset_id}:{value.open_time.isoformat()}", value.close_time,
            value.available_at, {"symbol": "UNKNOWNUSDT", "timeframe": "1m",
                                 "candle": value.model_dump(mode="json")}, asset_id)


def test_censored_and_later_complete_outcomes_are_separate_immutable_objects(tmp_path):
    store = IntelligenceStore(str(tmp_path/"research.sqlite3"))
    try:
        event = record()
        store.append("trade_alert", event.event_id, event.event_timestamp, event.detected_at, event, event.asset_id)
        now = T+timedelta(minutes=5)
        first = evaluate_trade_alert_research(store, config(), now)
        assert first["censored"] == 1
        first_object = store.objects("trade_alert_outcome", now)[0]
        archive(store, event.asset_id, future())
        second = evaluate_trade_alert_research(store, config(), now+timedelta(seconds=1))
        assert second["complete_proxy"] == 1
        objects = store.objects("trade_alert_outcome", now+timedelta(seconds=1))
        assert len(objects) == 2 and first_object in objects
        assert {value["status"] for value in objects} == {"CENSORED", "COMPLETE_PROXY"}
        assert evaluate_trade_alert_research(store, config(), now+timedelta(seconds=2))["new_outcomes"] == 0
    finally:
        store.close()


def test_online_miss_detects_observed_unknown_asset_even_without_any_alert(tmp_path):
    store = IntelligenceStore(str(tmp_path/"miss.sqlite3"))
    try:
        # Native-shaped fixtures explicitly remain artificial test prices.
        bars = [candle(T-timedelta(minutes=2)), candle(T-timedelta(minutes=1)), *future()]
        archive(store, "unknown", bars)
        report = evaluate_trade_alert_research(store, config(), T+timedelta(minutes=5))
        assert report["processed_alerts"] == 0 and report["new_misses"] == 1
        miss = store.objects("trade_alert_miss", T+timedelta(minutes=5))[0]
        assert miss["asset_id"] == "unknown" and miss["bias"] == "LONG"
        assert miss["native_contiguous_seconds"] == 300
        assert miss["pre_available_features"]["latest_alert"] is None
        assert "No archived pre-move" in miss["missing"][0]
        assert evaluate_trade_alert_research(store, config(), T+timedelta(minutes=5, seconds=1))["new_misses"] == 0
    finally:
        store.close()


def test_prior_meaningful_signal_prevents_miss_and_gaps_do_not_create_misses(tmp_path):
    store = IntelligenceStore(str(tmp_path/"prior.sqlite3"))
    try:
        event = record(detected_at=T-timedelta(minutes=1), received_at=T-timedelta(minutes=1),
                       event_timestamp=T-timedelta(minutes=1), expires_at=T+timedelta(minutes=1),
                       trigger_discovered_at=T-timedelta(minutes=2))
        store.append("trade_alert", event.event_id, event.event_timestamp, event.detected_at, event, event.asset_id)
        archive(store, "unknown", [candle(T-timedelta(minutes=1)), *future()])
        assert evaluate_trade_alert_research(store, config(), T+timedelta(minutes=5))["new_misses"] == 0
        missing = future()
        missing.pop(2)
        archive(store, "gapped", [candle(T-timedelta(minutes=1)), *missing])
        assert evaluate_trade_alert_research(store, config(), T+timedelta(minutes=5, seconds=1))["new_misses"] == 0
    finally:
        store.close()


@pytest.mark.parametrize("has_known_snapshot", [True, False])
def test_miss_features_exclude_late_snapshot_and_mutable_current_state(tmp_path, has_known_snapshot):
    store = IntelligenceStore(str(tmp_path/"snapshot.sqlite3"))
    try:
        known = {"symbol": "UNKNOWNUSDT", "status": "EVALUATED", "as_of": T.isoformat(),
                 "directions": {"LONG": {"reason": "Pressure below alert threshold", "score": 30}}}
        late = {"symbol": "UNKNOWNUSDT", "status": "EVALUATED", "as_of": T.isoformat(),
                "directions": {"LONG": {"reason": "Unavailable at baseline", "score": 99}}}
        if has_known_snapshot:
            store.append("trade_alert_observation", "known", T, T, known, "unknown")
        store.append("trade_alert_observation", "late", T, T+timedelta(minutes=1), late, "unknown")
        store.put_state("trade_alert_engine", "default", T+timedelta(minutes=1),
                        {"observations": {"unknown": late}})
        archive(store, "unknown", [candle(T-timedelta(minutes=1)), *future()])
        as_of = T+timedelta(minutes=5)
        assert evaluate_trade_alert_research(store, config(), as_of)["new_misses"] == 1
        miss = store.objects("trade_alert_miss", as_of)[0]
        assert miss["pre_available_features"]["observation"] == (known if has_known_snapshot else None)
        assert miss["missing"] == ([] if has_known_snapshot else ["No archived pre-move setup feature snapshot"])
    finally:
        store.close()


@pytest.mark.parametrize("missing_prefix", [False, True])
def test_miss_checks_baseline_to_future_continuity_but_excludes_partial_receipt_minute(tmp_path, missing_prefix):
    store = IntelligenceStore(str(tmp_path/"receipt.sqlite3"))
    try:
        baseline = candle(T-timedelta(minutes=2), available_at=T-timedelta(minutes=1)+timedelta(seconds=.5))
        partial = candle(T-timedelta(minutes=1), high=9999, low=1,
                         available_at=T+timedelta(seconds=.5))
        measured = [bar.model_copy(update={"available_at": bar.close_time+timedelta(seconds=.5)})
                    for bar in future()]
        archive(store, "unknown", [baseline, *([] if missing_prefix else [partial]), *measured])
        as_of = T+timedelta(minutes=5, seconds=1)
        report = evaluate_trade_alert_research(store, config(), as_of)
        assert report["new_misses"] == (0 if missing_prefix else 1)
        if not missing_prefix:
            miss = store.objects("trade_alert_miss", as_of)[0]
            assert miss["bias"] == "LONG" and miss["expansion_pct_proxy"] == pytest.approx(6)
            assert miss["excluded_head_seconds"] == 59.5
            assert miss["excluded_prefix_candles"] == 1
            assert miss["observed_window_start"] == T.isoformat()
            assert miss["baseline_to_end_contiguous_seconds"] == 360
            assert miss["native_contiguous_seconds"] == 300
    finally:
        store.close()


def test_purged_validation_removes_overlapping_and_late_labels():
    samples = [
        {"event_id": "train", "decision_time": T-timedelta(minutes=10)},
        {"event_id": "late", "decision_time": T-timedelta(minutes=6), "label_available_at": T+timedelta(seconds=1)},
        {"event_id": "overlap", "decision_time": T-timedelta(minutes=1)},
        {"event_id": "embargo", "decision_time": T+timedelta(minutes=2)},
        {"event_id": "holdout", "decision_time": T+timedelta(minutes=5)},
    ]
    result = purged_chronological_validation(samples, train_end=T, holdout_start=T+timedelta(minutes=1),
                                            label_horizon_seconds=300, embargo_seconds=120)
    assert result["train"] == ["train"]
    assert result["purged"] == ["late", "overlap", "embargo"]
    assert result["holdout"] == ["holdout"]
    with pytest.raises(ValueError, match="out-of-order"):
        purged_chronological_validation(list(reversed(samples)), train_end=T, holdout_start=T,
                                        label_horizon_seconds=300)
