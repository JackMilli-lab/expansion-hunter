"""Synthetic loader/PIT tests only; never market-data or predictive evidence."""
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from expansion_hunter.missed_move_forensics import (
    RECEIPT_ASSUMPTION,
    _derivatives,
    correct_pre_move_detection,
    feature_snapshot,
    intrinsic_five_minute_metrics,
    known_prefix,
    load_evidence,
    make_controls,
    match_controls,
    phase_detection_metrics,
    run_forensics,
    structural_price_sweeps,
)
from expansion_hunter.models import Candle
from expansion_hunter.top_mover_benchmark import ExpansionDefinition
from expansion_hunter.trade_alert_models import TradeAlertSettings

T = datetime(2026, 10, 9, tzinfo=UTC)


def native(at, price=100, *, frame_seconds=60):
    opening = int(at.timestamp()*1000)
    return [opening, str(price), str(price+1), str(price-1), str(price), "10",
            opening+frame_seconds*1000-1, str(price*10), 2, "6", str(price*6), "0"]


def archive(tmp_path, rows, *, symbol="UNKNOWNUSDT", as_of=None):
    (tmp_path / "exchange_info.json").write_text(json.dumps({
        "as_of_ms": int((as_of or T+timedelta(hours=4)).timestamp()*1000),
        "provenance": "EXPLICIT_TEST_FIXTURE", "retrieved_at": "2026-10-10T00:00:00Z",
        "records": [{"symbol": symbol, "data": {"symbols": [{"symbol": symbol,
            "baseAsset": "UNKNOWN", "quoteAsset": "USDT", "status": "TRADING"}]}}]}))
    envelope = {"request": {"symbol": symbol, "interval": "1m", "page": 0},
        "result": {"result": rows}, "source": "EXPLICIT_TEST_FIXTURE", "error": False,
        "retrieved_at": "2026-10-10T00:00:00Z"}
    (tmp_path / f"{symbol}_1m_0.json").write_text(json.dumps(envelope))
    return envelope


def minute(at, price=100, delay=0):
    return Candle(open_time=at, close_time=at+timedelta(minutes=1),
        available_at=at+timedelta(minutes=1, seconds=delay), open=price, high=price+1,
        low=price-1, close=price, volume=10, quote_volume=price*10, taker_buy_volume=6,
        source="binance/spot/test-fixture")


def test_real_exchange_boundary_and_distinct_download_receipt(tmp_path):
    archive(tmp_path, [native(T)])
    evidence = load_evidence(tmp_path)
    bar = evidence["histories"]["UNKNOWNUSDT"]["1m"][0]
    assert bar.close_time == T+timedelta(minutes=1)
    assert bar.available_at == bar.close_time
    assert evidence["receipt_assumption"] == RECEIPT_ASSUMPTION
    assert evidence["download_provenance"][0]["retrieved_at"] == "2026-10-10T00:00:00Z"
    assert len(evidence["download_provenance"][0]["sha256"]) == 64
    assert bar.taker_buy_volume == 6 and bar.quote_volume == 1000
    assert bar.trade_count == 2


def test_known_prefix_rejects_future_and_unreceived_candles():
    bars = [minute(T), minute(T+timedelta(minutes=1), delay=70), minute(T+timedelta(minutes=2))]
    assert known_prefix(bars, T+timedelta(minutes=2)) == bars[:1]
    assert known_prefix(bars, T+timedelta(minutes=3)) == [bars[0], bars[2]]


def test_explicit_receipt_delay_does_not_claim_observed_delivery(tmp_path):
    archive(tmp_path, [native(T), native(T+timedelta(minutes=1))], as_of=T+timedelta(minutes=2))
    evidence = load_evidence(tmp_path, receipt_delay_seconds=2)
    bars = evidence["histories"]["UNKNOWNUSDT"]["1m"]
    assert len(bars) == 1
    assert bars[0].available_at == T+timedelta(minutes=1, seconds=2)
    assert evidence["receipt_delay_seconds"] == 2


@pytest.mark.parametrize("delay", [-1, float("nan"), float("inf")])
def test_invalid_receipt_assumption_rejected(tmp_path, delay):
    with pytest.raises(ValueError, match="nonnegative"):
        load_evidence(tmp_path, receipt_delay_seconds=delay)


def test_identical_page_overlap_is_counted_and_deduplicated(tmp_path):
    envelope = archive(tmp_path, [native(T)])
    (tmp_path / "UNKNOWNUSDT_1m_1.json").write_text(json.dumps(envelope))
    evidence = load_evidence(tmp_path)
    assert len(evidence["histories"]["UNKNOWNUSDT"]["1m"]) == 1
    assert evidence["coverage"]["UNKNOWNUSDT"]["1m"]["identical_overlap_rows"] == 1


def test_conflicting_revision_is_rejected_without_future_selection(tmp_path):
    envelope = archive(tmp_path, [native(T)])
    envelope["result"]["result"] = [native(T, price=200)]
    (tmp_path / "UNKNOWNUSDT_1m_1.json").write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="revision"):
        load_evidence(tmp_path)


def test_invalid_native_boundary_is_rejected(tmp_path):
    row = native(T)
    row[6] += 1
    archive(tmp_path, [row])
    with pytest.raises(ValueError, match="boundary"):
        load_evidence(tmp_path)


def test_missing_market_intervals_are_recorded_never_filled(tmp_path):
    archive(tmp_path, [native(T), native(T+timedelta(minutes=2))])
    evidence = load_evidence(tmp_path)
    coverage = evidence["coverage"]["UNKNOWNUSDT"]["1m"]
    assert coverage["bars"] == 2 and coverage["gap_count"] == 1
    assert coverage["gaps"][0]["missing_seconds"] == 60


def test_preselected_clock_controls_keep_winners_and_nonwinners():
    bars = [minute(T-timedelta(minutes=31)+timedelta(minutes=i), price=100 if i < 31 else 104)
            for i in range(34)]
    evidence = {"histories": {"UNKNOWNUSDT": {"1m": bars}}, "as_of": T+timedelta(minutes=3)}
    controls = make_controls(evidence, T, T+timedelta(minutes=1), ExpansionDefinition(horizon_minutes=3))
    assert len(controls) == 1 and controls[0]["status"] == "MOVE"
    assert controls[0]["preselected_at"] == T.isoformat()
    assert controls[0]["features"]["latest_feature_available_at"] <= T.isoformat()
    assert "independently of outcomes" in controls[0]["selection_rule"]


def test_control_matching_never_prefers_success_or_failure_outcome():
    bars = [minute(T-timedelta(minutes=31)+timedelta(minutes=i)) for i in range(31)]
    evidence = {"histories": {"TARGETUSDT": {"1m": bars}, "CONTROLUSDT": {"1m": bars}}, "as_of": T}
    feature = {"volatility": 0, "quote_volume_60m": 31000, "btc_regime": "UNAVAILABLE",
        "computed_at": T.isoformat(), "latest_feature_available_at": T.isoformat()}
    controls = [{"symbol": "CONTROLUSDT", "start": T.isoformat(), "status": "MOVE", "features": feature}]
    label = {"event_id": "x", "symbol": "TARGETUSDT", "expansion_start": T.isoformat()}
    a = match_controls(evidence, [label], controls)
    controls[0]["status"] = "NON_MOVE"
    b = match_controls(evidence, [label], controls)
    assert a[0]["matches"][0]["feature_distance"] == b[0]["matches"][0]["feature_distance"]
    assert a[0]["matches"][0]["symbol"] == b[0]["matches"][0]["symbol"]


def test_control_matching_rejects_even_attractive_future_control_features():
    bars = [minute(T-timedelta(minutes=31)+timedelta(minutes=i)) for i in range(31)]
    evidence = {"histories": {"TARGETUSDT": {"1m": bars}}, "as_of": T}
    feature = {"volatility": 0, "quote_volume_60m": 31000, "btc_regime": "UNAVAILABLE"}
    controls = [{"symbol": "CONTROLUSDT", "start": (T+timedelta(minutes=1)).isoformat(), "status": "NON_MOVE", "features": feature}]
    label = {"event_id": "x", "symbol": "TARGETUSDT", "expansion_start": T.isoformat()}
    assert match_controls(evidence, [label], controls)[0]["matches"] == []


def test_wrong_direction_pre_alert_is_never_called_a_long_detection():
    label = {"symbol": "UNKNOWNUSDT", "bias": "LONG", "expansion_start": T.isoformat(),
        "definition": {"lookback_minutes": 60}, "baseline_price": 100, "peak_price": 105}
    signals = [{"symbol": "UNKNOWNUSDT", "bias": "SHORT", "as_of": (T-timedelta(minutes=5)).isoformat(),
        "state": "WATCH", "status": "AVAILABLE", "price": 100}]
    assert correct_pre_move_detection(label, signals)["earliest_detectable_pre_move_time"] is None
    signals.append({**signals[0], "bias": "LONG", "as_of": (T-timedelta(minutes=2)).isoformat()})
    result = correct_pre_move_detection(label, signals)
    assert result["earliest_detectable_pre_move_time"] == (T-timedelta(minutes=2)).isoformat()
    assert result["move_remaining_pct"] == pytest.approx(5)


def test_intrinsic_candle_metrics_available_without_resistance_or_alert():
    bars = [minute(T-timedelta(minutes=5*(40-index)), price=100+index*.01).model_copy(update={
        "close_time": T-timedelta(minutes=5*(39-index)), "available_at": T-timedelta(minutes=5*(39-index))})
        for index in range(40)]
    intrinsic = intrinsic_five_minute_metrics(bars, T)
    assert intrinsic["status"] == "DERIVED"
    assert intrinsic["metrics"]["relative_volume"] == 1
    assert intrinsic["metrics"]["volume_acceleration"] == 0
    assert intrinsic["metrics"]["taker_buy_ratio"] == .6
    assert intrinsic["trigger"] is None and intrinsic["resistance"] is None
    assert not any("distance" in key or "test_count" in key for key in intrinsic["metrics"])


def test_snapshot_preserves_candle_features_when_production_level_absent():
    bars = [minute(T-timedelta(minutes=5*(40-index)), price=100+index*.01).model_copy(update={
        "close_time": T-timedelta(minutes=5*(39-index)), "available_at": T-timedelta(minutes=5*(39-index))})
        for index in range(40)]
    one = [minute(T-timedelta(minutes=40-index), price=100+index*.01) for index in range(40)]
    evidence = {"histories": {"UNKNOWNUSDT": {"5m": bars, "1m": one}}}
    snapshot = feature_snapshot(evidence, "UNKNOWNUSDT", T, settings=TradeAlertSettings())
    assert snapshot["features"]["compression"]["value"] == 0
    assert snapshot["features"]["RVOL"]["value"] == 1
    assert snapshot["features"]["volume_acceleration"]["value"] == 0
    assert snapshot["features"]["repeated_level_pressure"]["status"] == "UNAVAILABLE"
    assert snapshot["long_state"] == "UNAVAILABLE"
    assert snapshot["long_pressure"] == {}


def test_price_pivot_sweep_uses_prior_confirmed_level_without_claiming_orders():
    values = [102, 101, 100, 99, 95, 99, 100, 101, 102, 103, 102, 101, 99]
    bars = [minute(T+timedelta(minutes=index), price=value) for index, value in enumerate(values)]
    bars[-1] = bars[-1].model_copy(update={"low": 93})
    result = structural_price_sweeps(bars, bars[-1].close_time)
    assert result["status"] == "DERIVED"
    event = next(row for row in result["value"]["events"] if row["side"] == "SELL_SIDE")
    assert event["price"] == 94
    assert event["observed_stop_orders"] == "UNAVAILABLE"
    assert _time_for_test(event["pivot_confirmed_at"]) <= bars[-1].open_time


def _time_for_test(value):
    return datetime.fromisoformat(value)


def test_derivatives_never_use_future_event_and_expose_staleness(tmp_path):
    rows = [{"timestamp": int((T-timedelta(minutes=30)).timestamp()*1000),
        "sumOpenInterest": "10", "sumOpenInterestValue": "100"},
        {"timestamp": int((T+timedelta(minutes=1)).timestamp()*1000),
        "sumOpenInterest": "100", "sumOpenInterestValue": "1000"}]
    (tmp_path / "UNKNOWNUSDT_oi.json").write_text(json.dumps({"result": {"result": rows},
        "error": False, "retrieved_at": "2026-10-10T00:00:00Z"}))
    values = _derivatives(tmp_path, "UNKNOWNUSDT", T)
    assert values["oi"]["value"]["open_interest"] == 10
    assert values["oi"]["status"] == "STALE"
    assert values["funding"]["status"] == "UNAVAILABLE"


def test_executable_runner_generates_honest_insufficient_report(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    archive(raw, [native(T-timedelta(minutes=1)), native(T)], as_of=T+timedelta(minutes=1))
    config = tmp_path / "frozen.json"
    config.write_text("{}")
    output = tmp_path / "report"
    report = run_forensics(raw, date(2026, 10, 9), output, config_path=config)
    assert report["execution_enabled"] is False
    assert report["production_changes"].startswith("NONE")
    assert all(row["status"] == "DATA_UNAVAILABLE" for row in report["cases"])
    assert report["today_metrics"]["direction_accuracy"] is None
    assert (output / "TOP_MOVER_EARLY_DETECTION_TEST.md").is_file()
    assert (output / "TOP_MOVER_EARLY_DETECTION_TEST.json").is_file()
    assert config.read_text() == "{}"


def test_warning_confirmation_and_lifecycle_control_rates_are_separate():
    control = {"symbol": "UNKNOWNUSDT", "start": T.isoformat(),
        "end": (T+timedelta(hours=1)).isoformat(), "status": "NON_MOVE"}
    base = {"symbol": "UNKNOWNUSDT", "bias": "LONG", "price": 100,
        "status": "ACTIVE", "detected_at": (T+timedelta(minutes=1)).isoformat()}
    alerts = [{**base, "phase": "STRUCTURE_WATCH"}, {**base, "phase": "BREAK_CONFIRMED"}]
    transitions = [{**base, "state": "WATCH"}]
    result = phase_detection_metrics([], alerts, transitions, controls=[control], as_of=T+timedelta(hours=2))
    assert result["STRUCTURE_WATCH_notifications"]["false_positive_control_windows"] == 1
    assert result["BREAK_CONFIRMED_notifications"]["false_positive_control_windows"] == 1
    assert result["TRADE_ALERT_notifications"]["false_positive_control_windows"] == 0
    assert result["PRE_IGNITION_or_LOADED_transitions"]["false_positive_control_windows"] == 0
    transitions.append({**base, "state": "PRE_IGNITION"})
    result = phase_detection_metrics([], alerts, transitions, controls=[control], as_of=T+timedelta(hours=2))
    assert result["PRE_IGNITION_or_LOADED_transitions"]["false_positive_control_windows"] == 1
