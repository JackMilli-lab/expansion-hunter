"""Synthetic daily-audit invariants; no evidence of predictive edge."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_top_mover_benchmark import T, alert, definition, expansion, prices

from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.pre_ignition_research import (
    SNAPSHOT_MINUTES,
    _coverage,
    _snapshot,
    cached_daily_report,
    evaluate_cached_store,
    evaluate_daily,
    run_pre_ignition_research_worker,
)
from expansion_hunter.top_mover_benchmark import consumed_grade, label_control_window


def emitted(at, price, *, bias="LONG", phase="TRADE_ALERT", **changes):
    row = alert(at, price, bias=bias, status="ACTIVE", state="READY")
    row.update(phase=phase, **changes)
    return row


def covered():
    return {"UNKNOWNUSDT": {"status": "MONITORED", "source": "SYNTHETIC_ARCHIVE"}}


@pytest.mark.parametrize("fraction,grade", [(0, "A+"), (.049999, "A+"), (.05, "A"),
    (.09999, "A"), (.10, "B"), (.20, "B"), (.20001, "LATE"), (1, "LATE"), (None, None)])
def test_consumed_grading_has_declared_exact_boundaries(fraction, grade):
    assert consumed_grade(fraction) == grade


@pytest.mark.parametrize("value", [-.01, 1.01, float("nan"), float("inf")])
def test_consumed_grading_rejects_invalid_finite_ranges(value):
    with pytest.raises(ValueError):
        consumed_grade(value)


@pytest.mark.parametrize("bias,values,first_price", [
    ("LONG", [100, 100.2, 100.3, 101, 103, 105], 100.2),
    ("SHORT", [100, 99.8, 99.7, 99, 97, 95], 99.8)])
def test_long_short_same_consumption_and_actual_warning_filter(bias, values, first_price):
    label = expansion(values)
    report = evaluate_daily([label], [
        alert(T-timedelta(minutes=5), 100, bias=bias),  # WATCH cannot count.
        emitted(T-timedelta(minutes=2), 100, bias=bias, phase="BREAK_CONFIRMED"),
        emitted(T+timedelta(minutes=1), first_price, bias=bias)], coverage=covered())
    row = report["cases"][0]
    assert row["move_consumed_pct"] == pytest.approx(4)
    assert row["consumed_grade"] == "A+"
    assert row["kpi_bucket"] == "detected_lt_5_pct"
    assert row["lead_time_seconds"] == -60  # Honest post-onset low-consumption call.
    assert not row["early_detection_pass"]
    assert report["kpi"]["detected_before_10_pct"] == 1
    assert report["kpi"]["rate"] == 1


def test_confirmations_and_watches_do_not_hide_a_complete_early_alert_miss():
    report = evaluate_daily([expansion()], [
        alert(T-timedelta(minutes=4), 100), emitted(T-timedelta(minutes=3), 100, phase="BREAK_CONFIRMED")],
        coverage=covered())
    assert report["kpi"]["buckets"]["missed_completely"] == 1
    assert report["cases"][0]["first_alert"] is None


def test_first_wrong_direction_cannot_be_replaced_by_a_later_winning_call():
    report = evaluate_daily([expansion()], [emitted(T-timedelta(minutes=5), 100, bias="SHORT"),
        emitted(T-timedelta(minutes=1), 100)], coverage=covered())
    assert report["kpi"]["buckets"]["wrong_direction"] == 1
    assert report["kpi"]["detected_before_10_pct"] == 0


def test_retracement_cannot_wash_away_consumed_move_and_peak_is_not_early():
    label = expansion([100, 100.2, 103, 100.4, 105, 104])
    report = evaluate_daily([label], [emitted(T+timedelta(minutes=3), 100.4)], coverage=covered())
    assert report["cases"][0]["move_consumed_pct"] == pytest.approx(60)
    assert report["cases"][0]["consumed_grade"] == "LATE"
    assert report["kpi"]["buckets"]["detected_gt_20_pct"] == 1


@pytest.mark.parametrize("status,bucket", [("NOT_IN_UNIVERSE", "not_in_monitored_universe"),
    ("SCANNER_OFFLINE", "scanner_offline"), ("DATA_UNAVAILABLE", "data_unavailable"),
    ("COVERAGE_UNKNOWN", "coverage_unknown"), ("IN_UNIVERSE", "coverage_unknown")])
def test_coverage_failure_is_not_claimed_as_model_failure(status, bucket):
    report = evaluate_daily([expansion()], [], coverage={"UNKNOWNUSDT": {"status": status}})
    assert report["kpi"]["buckets"][bucket] == 1
    assert report["kpi"]["eligible_monitored_moves"] == 0
    assert report["kpi"]["rate"] is None
    assert not report["cases"][0]["detection_failure"]


def test_no_coverage_does_not_infer_scanner_offline_and_censored_labels_do_not_grade():
    report = evaluate_daily([expansion()], [emitted(T, 100)])
    assert report["kpi"]["buckets"]["coverage_unknown"] == 1
    censored = evaluate_daily([expansion()], [emitted(T, 100)], coverage=covered(),
        as_of=T+timedelta(minutes=4))
    assert censored["kpi"]["buckets"]["censored"] == 1
    assert censored["cases"][0]["move_consumed_pct"] is None


def test_only_actual_early_alerts_count_as_control_false_alarms():
    rows = prices([100, 100, 100, 100, 100, 100])
    control = label_control_window("UNKNOWNUSDT", rows[0], rows[1:], T+timedelta(minutes=5), definition())
    watch = evaluate_daily([], [alert(T+timedelta(minutes=1), 100)], controls=[control])
    actual = evaluate_daily([], [emitted(T+timedelta(minutes=1), 100)], controls=[control])
    assert watch["control_stats"]["false_positive_rate"] == 0
    assert actual["control_stats"]["false_positive_rate"] == 1
    assert actual["profitability"]["win_rate"] is None
    assert actual["probability"] is None and not actual["execution_enabled"]


@pytest.fixture
def store(tmp_path):
    archive = IntelligenceStore(str(tmp_path / "daily.sqlite"))
    yield archive
    archive.close()


def archive(store, *, observations=True, late_feature=False, symbol="UNKNOWNUSDT", identity="unknown"):
    rows = prices([100, 100.2, 100.3, 101, 103, 105])
    for index, bar in enumerate(rows):
        store.append("trade_alert_candle", f"{symbol}:{index}", bar.close_time, bar.available_at,
            {"symbol": symbol, "timeframe": "1m", "candle": bar.model_dump(mode="json")}, identity)
    if observations:
        store.append("trade_alert_observation", f"{identity}:evaluation", T-timedelta(minutes=1),
            T+timedelta(minutes=2) if late_feature else T-timedelta(minutes=1),
            {"as_of": (T-timedelta(minutes=1)).isoformat(), "pre_ignition_score": 77, "status": "EVALUATED",
             "source_candle": rows[0].model_copy(update={"open_time": T-timedelta(minutes=2),
                 "close_time": T-timedelta(minutes=1), "available_at": T-timedelta(minutes=1)}).model_dump(mode="json")}, identity)
    row = emitted(T, 100)
    row["symbol"] = symbol
    store.append("trade_alert", f"{identity}:alert", T, T, row, identity)
    return rows


def test_empty_archive_is_unavailable_not_an_invented_offline_miss(store):
    report = evaluate_cached_store(store, T+timedelta(days=1), date=T.date(), definition=definition())
    assert report["status"] == "UNAVAILABLE"
    assert report["coverage"]["evaluated_assets"] == 0
    assert report["kpi"]["rate"] is None
    assert report["kpi"]["buckets"]["scanner_offline"] == 0
    assert cached_daily_report(store, T+timedelta(days=1))["report_id"] == report["report_id"]


def test_daily_report_is_immutable_and_has_seven_causal_snapshots(store):
    archive(store)
    now = T+timedelta(days=1)
    report = evaluate_cached_store(store, now, date=T.date(), definition=definition())
    assert report["kpi"]["detected_before_10_pct"] == 1
    assert report["cases"][0]["label_source"] == "COMPLETED_1M_CLOSE_PROXY"
    snapshots = report["cases"][0]["snapshots"]
    assert tuple(row["minutes_before"] for row in snapshots) == SNAPSHOT_MINUTES
    assert all(not row["future_outcome_in_features"] for row in snapshots)
    assert snapshots[0]["archived_features"] is None
    assert snapshots[-1]["archived_features"]["pre_ignition_score"] == 77
    published = store.object("pre_ignition_daily_report", report["report_id"], now)
    assert published == report
    assert cached_daily_report(store, T)["status"] == "UNAVAILABLE"  # Future cache is unavailable.
    repeat = evaluate_cached_store(store, now+timedelta(minutes=1), date=T.date(), definition=definition())
    assert repeat["report_id"] != report["report_id"]
    assert store.object("pre_ignition_daily_report", report["report_id"], now) == published


def test_late_receipt_is_excluded_from_pre_move_snapshot_and_monitoring_proof(store):
    archive(store, late_feature=True)
    report = evaluate_cached_store(store, T+timedelta(days=1), date=T.date(), definition=definition())
    row = report["cases"][0]
    assert row["kpi_bucket"] == "coverage_unknown"
    assert all(snapshot["archived_features"] is None for snapshot in row["snapshots"])


def test_candle_archive_alone_is_not_proof_the_engine_was_running(store):
    archive(store, observations=False)
    report = evaluate_cached_store(store, T+timedelta(days=1), date=T.date(), definition=definition())
    assert report["kpi"]["buckets"]["coverage_unknown"] == 1
    assert report["kpi"]["buckets"]["missed_completely"] == 0


@pytest.mark.parametrize("status,quality", [("INSUFFICIENT_DATA", {}),
    ("EVALUATED", {"1m": "AVAILABLE", "5m": "STALE"})])
def test_recorded_missing_required_data_is_data_failure_not_model_miss(status, quality):
    record = {"event_at": T.isoformat(), "available_at": T.isoformat(),
        "payload": {"status": status, "data_quality": quality}}
    assert _coverage(expansion(), [record])["status"] == "DATA_UNAVAILABLE"


def test_snapshot_rejects_payload_future_calculation_even_with_backdated_archive_metadata():
    record = {"event_at": T.isoformat(), "available_at": T.isoformat(),
        "payload": {"status": "EVALUATED", "evaluated_at": (T+timedelta(minutes=1)).isoformat()}}
    snapshot = _snapshot("UNKNOWNUSDT", {}, [record], T)
    assert snapshot["archived_features"] is None
    assert _coverage(expansion(), [record])["status"] == "COVERAGE_UNKNOWN"


def test_indexed_archive_read_uses_existing_kind_asset_event_index(store):
    plan = store._db.execute("EXPLAIN QUERY PLAN SELECT event_at,available_at,payload FROM intelligence_objects "
        "WHERE kind=? AND asset_id=? AND event_at>=? AND event_at<=? AND available_at<=? "
        "ORDER BY event_at DESC LIMIT ?", ("trade_alert_candle", "any", T.isoformat(),
        T.isoformat(), T.isoformat(), 5001)).fetchall()
    assert any("intelligence_asset" in row[3] for row in plan)


def test_bounded_batches_rotate_and_accumulate_two_assets_without_one_favored_coin(store):
    archive(store, symbol="ONEUSDT", identity="one")
    archive(store, symbol="TWOUSDT", identity="two")
    now = T+timedelta(days=1)
    first = evaluate_cached_store(store, now, date=T.date(), definition=definition(), maximum_assets=1)
    second = evaluate_cached_store(store, now+timedelta(minutes=1), date=T.date(),
        definition=definition(), maximum_assets=1)
    assert first["coverage"]["evaluated_assets"] == 1 and first["status"] == "PARTIAL"
    assert second["coverage"]["evaluated_assets"] == 2
    assert {row["symbol"] for row in second["cases"]} == {"ONEUSDT", "TWOUSDT"}


def test_stale_published_report_is_flagged_but_never_recalculated(store):
    archive(store)
    now = T+timedelta(days=1)
    report = evaluate_cached_store(store, now, date=T.date(), definition=definition())
    cached = cached_daily_report(store, now+timedelta(hours=3))
    assert cached["status"] == "STALE" and cached["age_seconds"] == 10800
    assert cached["report_id"] == report["report_id"]


@pytest.mark.parametrize("changes", [{"maximum_assets": 0}, {"maximum_assets": 101},
    {"top_per_direction": 0}, {"date": "20261008"}, {"date": "2026-10-20"}])
def test_invalid_work_budget_or_future_day_is_rejected(store, changes):
    with pytest.raises(ValueError):
        evaluate_cached_store(store, T, **changes)


@pytest.mark.asyncio
async def test_daily_worker_is_cancellable_and_reports_real_empty_archives(store):
    import asyncio
    app = SimpleNamespace(state=SimpleNamespace(store=store))
    task = asyncio.create_task(run_pre_ignition_research_worker(app, interval_seconds=60))
    for _ in range(100):
        if getattr(app.state, "pre_ignition_research_health", {}).get("status") == "UNAVAILABLE":
            break
        await asyncio.sleep(.001)
    assert app.state.pre_ignition_research_health["status"] == "UNAVAILABLE"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert app.state.pre_ignition_research_health["status"] == "STOPPED"
