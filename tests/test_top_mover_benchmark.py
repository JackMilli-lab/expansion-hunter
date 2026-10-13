"""Synthetic deterministic benchmark tests; never evidence of trading edge."""
from datetime import UTC, datetime, timedelta

import pytest
from test_fast import ASSET, NOW, bar
from test_trade_alert import histories

from expansion_hunter.fast_models import FastStage
from expansion_hunter.models import Candle
from expansion_hunter.top_mover_benchmark import (
    ExpansionDefinition,
    evaluate_early_detection,
    label_control_window,
    label_expansions,
    replay_fast_monitor,
    replay_pre_move,
    walk_forward_splits,
)
from expansion_hunter.trade_alert import TradeAlertEngine
from expansion_hunter.trade_alert_models import TradeAlertSettings

T = datetime(2026, 10, 8, 12, tzinfo=UTC)


def minute(at, price, *, available_at=None):
    return Candle(open_time=at, close_time=at+timedelta(minutes=1),
        available_at=available_at or at+timedelta(minutes=1), open=price, close=price,
        high=price+.01, low=price-.01, volume=100, taker_buy_volume=60,
        source="binance/spot/historical-public")


def prices(values):
    return [minute(T+timedelta(minutes=index-1), price) for index, price in enumerate(values)]


def definition(**changes):
    return ExpansionDefinition(horizon_minutes=5, **changes)


def expansion(values=None):
    bars = prices(values or [100, 100.2, 100.3, 101, 103, 105])
    labels = label_expansions("UNKNOWNUSDT", bars, T+timedelta(minutes=5), definition())
    assert len(labels) == 1
    return labels[0]


def alert(at, price, *, bias="LONG", status="AVAILABLE", state="WATCH"):
    return {"symbol": "UNKNOWNUSDT", "bias": bias, "as_of": at.isoformat(),
            "price": price, "status": status, "state": state}


@pytest.mark.parametrize("bad", [{"threshold_pct": 0}, {"initiation_pct": 3},
                                  {"max_completed_fraction": 1.01}, {"threshold_pct": float("nan")},
                                  {"horizon_minutes": 0}, {"lookback_minutes": -1},
                                  {"horizon_minutes": 1.5}, {"lookback_minutes": True}])
def test_expansion_definition_must_be_predeclared_valid_numbers(bad):
    with pytest.raises(ValueError):
        ExpansionDefinition(**bad)


def test_outcome_only_onset_is_explicit_close_proxy_and_not_threshold_crossing():
    label = expansion()
    assert label["expansion_start"] == T.isoformat()
    assert label["threshold_crossing"] == (T+timedelta(minutes=4)).isoformat()
    assert label["total_move_pct"] == pytest.approx(5)
    assert label["measurement"] == "COMPLETED_1M_CLOSE_PROXY"
    assert label["label_available_at"] == (T+timedelta(minutes=5)).isoformat()


def test_long_short_labels_are_symmetric_and_overlap_does_not_multiply_samples():
    up = prices([100, 100.2, 100.3, 101, 103, 105, 106, 107])
    down = prices([200-value.close for value in up])
    long = label_expansions("ANY", up, T+timedelta(minutes=7), definition())
    short = label_expansions("ANY", down, T+timedelta(minutes=7), definition())
    assert len(long) == len(short) == 1
    assert long[0]["bias"] == "LONG" and short[0]["bias"] == "SHORT"
    assert long[0]["total_move_pct"] == pytest.approx(short[0]["total_move_pct"])


def test_insufficient_future_horizon_is_not_labeled_a_miss_or_nonmove():
    rows = prices([100, 100.2, 100.3, 101, 103, 105])
    assert not label_expansions("ANY", rows, T+timedelta(minutes=4, seconds=59), definition())
    result = label_control_window("ANY", rows[0], rows[1:], T+timedelta(minutes=4), definition())
    assert result["status"] == "CENSORED" and result["maximum_move_pct"] is None


@pytest.mark.parametrize("problem", ["gap", "source", "late"])
def test_gaps_mixed_sources_and_unreceived_future_do_not_create_labels(problem):
    rows = prices([100, 100.2, 100.3, 101, 103, 105])
    if problem == "gap":
        rows.pop(2)
    elif problem == "source":
        rows[2] = rows[2].model_copy(update={"source": "different-venue"})
    else:
        rows[-1] = rows[-1].model_copy(update={"available_at": T+timedelta(days=1)})
    assert not label_expansions("ANY", rows, T+timedelta(minutes=5), definition())


def test_duplicates_and_revisions_are_not_silently_selected():
    rows = prices([100, 100.2, 100.3, 101, 103, 105])
    with pytest.raises(ValueError, match="duplicate"):
        label_expansions("ANY", [*rows, rows[0]], T+timedelta(minutes=5), definition())


def test_close_proxy_does_not_invent_intraminute_expansion_or_excursion_order():
    quiet = prices([100, 100.1, 100.05, 100, 100.05, 100.1])
    quiet[2] = quiet[2].model_copy(update={"high": 150})
    assert label_expansions("ANY", quiet, T+timedelta(minutes=5), definition()) == []
    moving = prices([100, 100.2, 100.3, 101, 103, 105])
    moving[2] = moving[2].model_copy(update={"high": 150})
    label = label_expansions("UNKNOWNUSDT", moving, T+timedelta(minutes=5), definition())[0]
    case = evaluate_early_detection([label], [alert(T+timedelta(minutes=2), 100.3)])["cases"][0]
    assert case["move_completed_fraction"] == pytest.approx(.06)
    assert case["not_late_detection_pass"] and not case["early_detection_pass"]


def test_early_watch_measures_lead_time_remaining_move_and_pass():
    report = evaluate_early_detection([expansion()], [alert(T-timedelta(minutes=5), 99.9)])
    case = report["cases"][0]
    assert case["status"] == "PASS"
    assert case["lead_time_seconds"] == 300
    assert case["move_completed_fraction"] == 0
    assert case["move_remaining_pct"] == pytest.approx((105/99.9-1)*100)
    assert report["direction_accuracy"] == 1 and report["false_positive_rate"] is None


@pytest.mark.parametrize("bias,pressure,confirmation", [
    ("LONG", FastStage.BREAKOUT_PRESSURE, FastStage.BREAKOUT_CONFIRMED),
    ("SHORT", FastStage.BREAKDOWN_PRESSURE, FastStage.BREAKDOWN_CONFIRMED)])
def test_actual_legacy_pressure_and_confirmation_are_not_silently_dropped(bias, pressure, confirmation):
    label = expansion() if bias == "LONG" else expansion([100, 99.8, 99.7, 99, 97, 95])
    before = evaluate_early_detection([label], [
        alert(T-timedelta(minutes=3), 100, bias=bias, status="ACTIVE", state=pressure.value)])
    assert before["cases"][0]["status"] == "PASS"
    assert before["cases"][0]["lead_time_seconds"] == 180
    after_price = 100.2 if bias == "LONG" else 99.8
    after = evaluate_early_detection([label], [
        alert(T+timedelta(minutes=1), after_price, bias=bias, status="ACTIVE", state=confirmation.value)])
    assert after["cases"][0]["status"] == "POST_ONSET_FAILURE"
    assert after["cases"][0]["lead_time_seconds"] == -60
    assert after["cases"][0]["not_late_detection_pass"]
    assert not after["cases"][0]["early_detection_pass"]


@pytest.mark.parametrize("state", [FastStage.BREAKOUT_ATTEMPT, FastStage.BREAKDOWN_ATTEMPT,
                                  FastStage.ACCEPTED, FastStage.ACCEPTED_DOWN,
                                  FastStage.EXPANSION_CONFIRMED, FastStage.EXPANSION_CONFIRMED_DOWN])
def test_legacy_followup_states_are_recognized_but_never_pre_move_after_onset(state):
    after = evaluate_early_detection([expansion()], [
        alert(T+timedelta(minutes=1), 100.2, status="ACTIVE", state=state.value)])
    assert after["alerted_expansion_count"] == 1
    assert after["cases"][0]["status"] == "POST_ONSET_FAILURE"
    assert not after["cases"][0]["early_detection_pass"]


def test_twenty_percent_boundary_and_direction_are_independent():
    at_twenty = evaluate_early_detection([expansion()], [alert(T+timedelta(minutes=3), 101)])
    assert not at_twenty["cases"][0]["early_detection_pass"]
    assert at_twenty["cases"][0]["not_late_detection_pass"]
    assert at_twenty["cases"][0]["status"] == "POST_ONSET_FAILURE"
    assert at_twenty["early_detection_rate"] == 0 and at_twenty["not_late_detection_rate"] == 1
    late = evaluate_early_detection([expansion()], [alert(T+timedelta(minutes=4), 103)])
    assert late["cases"][0]["status"] == "LATE_FAILURE" and late["direction_accuracy"] == 1
    wrong = evaluate_early_detection([expansion()], [alert(T-timedelta(minutes=1), 100, bias="SHORT")])
    assert wrong["cases"][0]["status"] == "WRONG_DIRECTION" and wrong["direction_accuracy"] == 0


def test_alert_exactly_at_onset_is_not_pre_move_and_wrong_first_call_is_not_replaced():
    at_onset = evaluate_early_detection([expansion()], [alert(T, 100)])
    assert at_onset["cases"][0]["status"] == "POST_ONSET_FAILURE"
    assert at_onset["cases"][0]["lead_time_seconds"] == 0
    wrong_first = evaluate_early_detection([expansion()], [
        alert(T-timedelta(minutes=5), 100, bias="SHORT"),
        alert(T-timedelta(minutes=1), 100, bias="LONG")])
    assert wrong_first["cases"][0]["status"] == "WRONG_DIRECTION"
    assert wrong_first["cases"][0]["first_alert"]["bias"] == "SHORT"


def test_late_flag_cannot_pass_even_before_outcome_onset():
    case = evaluate_early_detection([expansion()], [
        alert(T-timedelta(minutes=1), 100, status="LATE")])["cases"][0]
    assert case["pre_move"] and not case["early_detection_pass"]
    assert case["status"] == "LATE_FAILURE"


def test_late_retracement_cannot_be_counted_as_early_entry():
    label = expansion([100, 100.2, 103, 100.4, 105, 104])
    report = evaluate_early_detection([label], [alert(T+timedelta(minutes=3), 100.4)])
    case = report["cases"][0]
    assert case["move_completed_fraction"] == pytest.approx(.6)
    assert case["status"] == "LATE_FAILURE"


def test_alert_after_peak_has_no_remaining_move_even_if_price_returns_to_origin():
    label = expansion([100, 100.2, 103, 105, 100.5, 100])
    case = evaluate_early_detection([label], [alert(T+timedelta(minutes=5), 100)])["cases"][0]
    assert case["move_completed_fraction"] == 1
    assert case["move_remaining_pct"] == 0 and case["status"] == "LATE_FAILURE"


def test_missing_stale_or_normal_alerts_cannot_be_called_success():
    calls = [alert(T-timedelta(minutes=1), 100, status="STALE"),
             alert(T-timedelta(minutes=1), 100, state="NORMAL")]
    report = evaluate_early_detection([expansion()], calls)
    assert report["cases"][0]["status"] == "MISSED"
    assert report["early_detection_rate"] == 0 and report["direction_accuracy"] is None


def test_matched_controls_are_labeled_after_selection_and_winners_remain():
    control = prices([100, 100.1, 100.05, 100, 100.05, 100.1])
    quiet = label_control_window("UNKNOWNUSDT", control[0], control[1:], T+timedelta(minutes=5), definition())
    assert quiet["status"] == "NON_MOVE"
    mover = prices([100, 100.2, 100.3, 101, 103, 105])
    winner = label_control_window("OTHER", mover[0], mover[1:], T+timedelta(minutes=5), definition())
    assert winner["status"] == "MOVE"
    report = evaluate_early_detection([], [alert(T+timedelta(minutes=1), 100.1)], controls=[quiet, winner])
    assert report["non_move_control_count"] == 1
    assert report["false_positive_rate"] == 1


def test_directional_control_matches_outcome_and_false_positive_direction():
    down = prices([100, 99.9, 99, 98, 97, 96])
    long = label_control_window("UNKNOWNUSDT", down[0], down[1:],
                                T+timedelta(minutes=5), definition(), bias="LONG")
    short = label_control_window("UNKNOWNUSDT", down[0], down[1:],
                                 T+timedelta(minutes=5), definition(), bias="SHORT")
    assert long["status"] == "NON_MOVE" and long["maximum_move_pct"] == 0
    assert short["status"] == "MOVE"
    opposite = evaluate_early_detection([], [alert(T+timedelta(minutes=1), 99.9, bias="SHORT")],
                                       controls=[long])
    assert opposite["false_positive_rate"] == 0
    matched = evaluate_early_detection([], [alert(T+timedelta(minutes=1), 99.9)], controls=[long])
    assert matched["false_positive_rate"] == 1
    with pytest.raises(ValueError, match="control bias"):
        label_control_window("UNKNOWN", down[0], down[1:], T, definition(), bias="NEUTRAL")


def test_outcomes_cannot_be_scored_before_horizon_or_late_label_receipt():
    label = expansion()
    quiet = prices([100, 100.1, 100.05, 100, 100.05, 100.1])
    control = label_control_window("UNKNOWNUSDT", quiet[0], quiet[1:],
                                  T+timedelta(minutes=5), definition())
    too_soon = evaluate_early_detection([label], [alert(T-timedelta(minutes=1), 100)],
                                       controls=[control], as_of=T+timedelta(minutes=4))
    assert too_soon["cases"][0]["status"] == "CENSORED"
    assert too_soon["early_detection_rate"] is None and too_soon["false_positive_rate"] is None
    assert too_soon["matured_expansion_count"] == 0 and too_soon["censored_expansion_count"] == 1
    label["label_available_at"] = (T+timedelta(hours=1)).isoformat()
    delayed = evaluate_early_detection([label], [], as_of=T+timedelta(minutes=5))
    assert delayed["cases"][0]["status"] == "CENSORED"


def test_future_alert_cannot_enter_as_of_metrics():
    report = evaluate_early_detection([expansion()], [alert(T+timedelta(minutes=3), 101)], as_of=T)
    assert report["cases"][0]["first_alert"] is None


def test_late_baseline_receipt_shifts_control_alert_window_without_inventing_knowledge():
    quiet = prices([100, 100.1, 100.05, 100, 100.05, 100.1])
    baseline = quiet[0].model_copy(update={"available_at": T+timedelta(seconds=2)})
    control = label_control_window("UNKNOWNUSDT", baseline, quiet[1:],
                                  T+timedelta(minutes=5), definition())
    assert control["status"] == "NON_MOVE"
    assert control["start"] == (T+timedelta(seconds=2)).isoformat()
    report = evaluate_early_detection([], [alert(T, 100)], controls=[control])
    assert report["false_positive_rate"] == 0
    after_outcome = baseline.model_copy(update={"available_at": T+timedelta(hours=1)})
    unavailable = label_control_window("UNKNOWNUSDT", after_outcome, quiet[1:],
                                      T+timedelta(hours=2), definition())
    assert unavailable["status"] == "CENSORED"


def test_replay_uses_unchanged_production_engine_and_never_downloads_data():
    frames = histories()
    config = TradeAlertSettings(watch_score=20, alert_score=40)
    direct = TradeAlertEngine(config).evaluate(ASSET, frames, NOW, NOW, frames["1m"][-1].close, [])
    replayed = replay_pre_move({ASSET.asset_id: ASSET}, {ASSET.asset_id: frames}, NOW,
                              decision_start=NOW, settings=config)
    assert replayed["alerts"] == [value.model_dump(mode="json") for value in direct]
    assert replayed["rank_scope"] == "REPLAYED_SUPPLIED_COHORT_NOT_ARCHIVED_HISTORICAL_UNIVERSE"
    assert replayed["snapshots"][-1]["observations"][ASSET.asset_id]["data_quality"]["futures"] == "UNAVAILABLE"
    assert all(row["rank"]["rank_velocity"] is None for row in replayed["snapshots"][-1]["rows"])


def test_legacy_fast_replay_uses_actual_monitor_and_only_counts_emitted_events():
    from expansion_hunter.fast import FastIgnitionMonitor
    from expansion_hunter.fast_models import FastSettings
    from expansion_hunter.models import AssetSnapshot

    frames = histories()
    config = FastSettings()
    direct = FastIgnitionMonitor(config).evaluate(ASSET, AssetSnapshot(asset=ASSET, as_of=NOW,
                                                candles=frames), evaluation_at=NOW, regime="UNAVAILABLE")
    result = replay_fast_monitor({ASSET.asset_id: ASSET}, {ASSET.asset_id: frames}, NOW,
                                 decision_start=NOW, settings=config)
    assert result["engine"] == "FastIgnitionMonitor"
    assert result["events"] == [event.model_dump(mode="json") for event in direct.events]
    assert len(result["alerts"]) == len(result["events"])
    assert "ARCHIVED_PROMOTIONS_UNKNOWN" in result["membership_assumption"]
    assert "candles" not in result["snapshots"][-1]["states"][ASSET.asset_id]
    partial = {"1m": frames["1m"][-1:]}
    cold = replay_fast_monitor({ASSET.asset_id: ASSET}, {ASSET.asset_id: partial}, NOW)
    assert cold["alerts"] == []


def test_legacy_replay_excludes_unreceived_future_candles_and_is_deterministic():
    frames = histories()
    before = replay_fast_monitor({ASSET.asset_id: ASSET}, {ASSET.asset_id: frames}, NOW, decision_start=NOW)
    frames["1m"].append(bar(NOW+timedelta(minutes=1), close=999, high=1000))
    after = replay_fast_monitor({ASSET.asset_id: ASSET}, {ASSET.asset_id: frames}, NOW, decision_start=NOW)
    assert before == after


def test_unreceived_future_candle_cannot_change_prior_snapshot():
    frames = histories()
    original = replay_pre_move({ASSET.asset_id: ASSET}, {ASSET.asset_id: frames}, NOW, decision_start=NOW)
    frames["1m"].append(bar(NOW+timedelta(minutes=1), close=999, high=1000))
    changed = replay_pre_move({ASSET.asset_id: ASSET}, {ASSET.asset_id: frames}, NOW, decision_start=NOW)
    assert changed == original
    late = {frame: [value.model_copy(update={"available_at": NOW+timedelta(days=1)})
                    for value in values] for frame, values in frames.items()}
    absent = replay_pre_move({ASSET.asset_id: ASSET}, {ASSET.asset_id: late}, NOW)
    assert absent["alerts"] == absent["snapshots"] == []


def test_walk_forward_reuses_purge_and_late_label_availability():
    samples = [{"event_id": "train", "decision_time": (T-timedelta(hours=2)).isoformat()},
               {"event_id": "late", "decision_time": (T-timedelta(minutes=20)).isoformat()},
               {"event_id": "holdout", "decision_time": (T+timedelta(hours=2)).isoformat()}]
    result = walk_forward_splits(samples, [(T, T+timedelta(hours=1))], horizon_seconds=3600)
    assert result[0]["train"] == ["train"]
    assert result[0]["purged"] == ["late"] and result[0]["holdout"] == ["holdout"]
    with pytest.raises(ValueError, match="increase"):
        walk_forward_splits(samples, [(T, T), (T, T)], horizon_seconds=3600)
