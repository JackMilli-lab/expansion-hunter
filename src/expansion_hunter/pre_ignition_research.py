"""Cached daily early-warning audit; outcomes never feed live candidate scores.

Only actual TRADE_ALERT/READY alerts count, not broad WATCH membership or a
confirmation of an earlier local move. Consumption uses maximum favourable
close progress, not a retracement's current price. Minute data cannot establish
intraminute onset, fills or the start of a complete daily rally.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from datetime import date as Date
from statistics import mean

from .models import Candle, aware, utcnow
from .top_mover_benchmark import (
    NAME,
    ExpansionDefinition,
    _alert_time,
    _time,
    evaluate_early_detection,
    label_control_window,
    label_expansions,
)
from .trade_alert_models import TradeAlertRecord, TradeAlertSettings
from .trade_alert_research import calculate_trade_alert_outcome

VERSION = "pre-ignition-daily-v1"
SNAPSHOT_MINUTES = (60, 30, 15, 10, 5, 3, 1)
KPI_NAME = "TOP MOVERS DETECTED BEFORE 10% OF MOVE"
BUCKETS = ("detected_lt_5_pct", "detected_5_to_10_pct", "detected_10_to_20_pct",
           "detected_gt_20_pct", "missed_completely", "not_in_monitored_universe",
           "scanner_offline", "data_unavailable", "coverage_unknown", "wrong_direction", "censored")
LIMITATIONS = [
    "Completed 1m close onset/excursion proxies; no tick onset, intraminute fill order or whole-rally guarantee.",
    "Actual emitted early alerts only; WATCH, RADAR and breakout confirmations do not count as TRADE ALERT.",
    "No archive is UNKNOWN coverage, not proof that a remote scanner was offline or that an asset was excluded.",
    "Bounded archived cohort, not a verified complete market-wide or historical top-500 universe.",
    "Fixed-clock control windows overlap event labels; observations are not independent trials.",
    "Labels and eventual consumption are research outcomes and never supplied to live features.",
    "No model fitting, automatic challenger promotion, calibrated probabilities or actual trading profitability.",
]


def _early_alert(row):
    phase = row.get("phase")
    # Phase takes precedence: a confirmed event must not masquerade as READY
    # just because a stale presentation field still contains that name.
    if phase is not None:
        return phase == "TRADE_ALERT"
    return row.get("state", row.get("signal_state")) == "READY" and row.get("emitted", True)


def evaluate_daily(labels: list[dict], alerts: list[dict], *, controls: list[dict] | None = None,
                   coverage: dict[str, dict] | None = None, as_of: datetime | None = None) -> dict:
    """Pure mature-label evaluation; the first wrong-direction early call fails.

    Coverage evidence is supplied independently of alerts. An omitted symbol
    remains UNKNOWN rather than an inferred detector failure. Counterfactual
    replay membership must be explicitly marked REPLAY_ASSUMED_MONITORED.
    """
    early = [row for row in alerts if _early_alert(row)]
    report = evaluate_early_detection(labels, early, controls=controls, as_of=as_of)
    evidence = coverage or {}
    buckets = dict.fromkeys(BUCKETS, 0)
    cases, eligible = [], []
    by_id = {label["event_id"]: label for label in labels}
    for result in report["cases"]:
        row = dict(result)
        label = by_id[row["event_id"]]
        supplied = evidence.get(row["event_id"], evidence.get(row["symbol"], {}))
        state = supplied.get("status", "COVERAGE_UNKNOWN")
        row.update(coverage=supplied, label_source=label.get("measurement"),
                   earliest_detectable_time=_alert_time(row["first_alert"]).isoformat() if row["first_alert"] else None,
                   price_at_detection=row["first_alert"]["price"] if row["first_alert"] else None,
                   expected_alert_state=row["first_alert"].get("signal_state", row["first_alert"].get(
                       "state", row["first_alert"].get("phase"))) if row["first_alert"] else None,
                   outcome_available_at=label.get("label_available_at", label["horizon_end"]))
        if row["status"] == "CENSORED":
            bucket = "censored"
        elif state == "NOT_IN_UNIVERSE":
            bucket = "not_in_monitored_universe"
        elif state == "SCANNER_OFFLINE":
            bucket = "scanner_offline"
        elif state == "DATA_UNAVAILABLE":
            bucket = "data_unavailable"
        elif state not in {"MONITORED", "REPLAY_ASSUMED_MONITORED"}:
            bucket = "coverage_unknown"
        else:
            eligible.append(row)
            if row["first_alert"] is None:
                bucket = "missed_completely"
            elif not row["direction_correct"]:
                bucket = "wrong_direction"
            elif (row["not_late_detection_pass"] and row["consumed_grade"] == "A+"):
                bucket = "detected_lt_5_pct"
            elif (row["not_late_detection_pass"] and row["consumed_grade"] == "A"):
                bucket = "detected_5_to_10_pct"
            elif (row["not_late_detection_pass"] and row["consumed_grade"] == "B"):
                bucket = "detected_10_to_20_pct"
            else:
                bucket = "detected_gt_20_pct"
        row["kpi_bucket"] = bucket
        row["detection_failure"] = bucket in {"missed_completely", "wrong_direction", "detected_gt_20_pct"}
        row["move_remaining"] = ("UNKNOWN" if row["move_remaining_fraction_pct"] is None else
                                 "HIGH" if row["move_remaining_fraction_pct"] >= 80 else
                                 "MEDIUM" if row["move_remaining_fraction_pct"] >= 50 else "LOW")
        buckets[bucket] += 1
        cases.append(row)
    good = buckets["detected_lt_5_pct"] + buckets["detected_5_to_10_pct"]
    called = [row for row in eligible if row["first_alert"]]
    return {"name": NAME, "version": VERSION, "status": "AVAILABLE" if eligible else "UNAVAILABLE",
            "as_of": aware(as_of).isoformat() if as_of else None, "cases": cases,
            "kpi": {"name": KPI_NAME, "detected_before_10_pct": good,
                    "eligible_monitored_moves": len(eligible), "rate": good/len(eligible) if eligible else None,
                    "buckets": buckets, "grade_boundaries": "A+ <5%; A 5-<10%; B 10-20%; LATE >20%",
                    "alert_eligibility": "Actual TRADE_ALERT or emitted READY; first wrong direction fails"},
            "control_stats": {key: report[key] for key in (
                "non_move_control_count", "false_positive_control_windows", "false_positive_rate",
                "false_positive_definition")},
            "direction_accuracy": mean(row["direction_correct"] for row in called) if called else None,
            "mean_alert_lead_time_seconds": mean(row["lead_time_seconds"] for row in called) if called else None,
            "profitability": {"status": "UNAVAILABLE", "win_rate": None, "expectancy": None,
                              "profit_factor": None, "reason": "No executed entries/exits with verified costs"},
            "execution_enabled": False, "probability": None, "limitations": list(LIMITATIONS)}


def _day(value, as_of):
    if value is None:
        return (aware(as_of).date()-timedelta(days=1)).isoformat()
    text = value.isoformat() if isinstance(value, Date) else value
    parsed = Date.fromisoformat(text)
    if parsed.isoformat() != text:
        raise ValueError("date must be canonical YYYY-MM-DD")
    return text


def _records(store, kind, asset_id, start, end, *, limit=5000):
    """Use the existing kind/asset/event index; decode after releasing its lock."""
    with store._archive_lock:
        rows = store._db.execute(
            "SELECT event_at,available_at,payload FROM intelligence_objects "
            "WHERE kind=? AND asset_id=? AND event_at>=? AND event_at<=? AND available_at<=? "
            "ORDER BY event_at DESC LIMIT ?",
            (kind, asset_id, start.isoformat(), end.isoformat(), end.isoformat(), limit+1)).fetchall()
    truncated = len(rows) > limit
    return [{"event_at": event, "available_at": available, "payload": json.loads(payload)}
            for event, available, payload in reversed(rows[:limit])], truncated


def _qualified_observation(record, at):
    if _time(record["available_at"]) > at or _time(record["event_at"]) > at:
        return False
    payload = record["payload"]
    if any(_time(payload[key]) > at for key in ("as_of", "evaluated_at", "received_at") if payload.get(key)):
        return False
    candle = payload.get("source_candle")
    return not candle or (_time(candle["close_time"]) <= at and _time(candle["available_at"]) <= at)


def _snapshot(symbol, frames, observations, at):
    """Use receipt-qualified feature vintages; no outcome fields are features."""
    candles = {}
    for frame, history in frames.items():
        known = [bar for bar in history if bar.close_time <= at and bar.available_at <= at]
        candles[frame] = {"count": len(known), "latest": known[-1].model_dump(mode="json") if known else None,
                          "status": "AVAILABLE" if known else "UNAVAILABLE"}
    prior = [row for row in observations if _qualified_observation(row, at)]
    payload = prior[-1]["payload"] if prior else None
    return {"symbol": symbol, "decision_time": at.isoformat(), "candles": candles,
            "archived_features": payload, "feature_status": "AVAILABLE" if payload else "UNAVAILABLE",
            "archive_receipt_time": prior[-1]["available_at"] if prior else None,
            "future_outcome_in_features": False}


def _coverage(label, observations):
    start = _time(label["expansion_start"])
    known = [row for row in observations if start-timedelta(minutes=60) <= _time(row["event_at"])
             and _qualified_observation(row, start)]
    # An actual evaluation in the minute immediately preceding onset proves
    # activity at that time. A warmup candle download alone does not.
    fresh = [row for row in known if (start-_time(row["event_at"])).total_seconds() <= 120]
    if fresh:
        payload = fresh[-1]["payload"]
        quality = payload.get("data_quality", {})
        if payload.get("status") == "INSUFFICIENT_DATA" or payload.get("observation_status") == "UNAVAILABLE" or any(
                quality.get(frame) in {"UNAVAILABLE", "STALE", "MISSING"} for frame in ("1m", "5m")):
            return {"status": "DATA_UNAVAILABLE", "source": "ARCHIVED_TRADE_ALERT_OBSERVATION",
                    "last_evaluation": fresh[-1]["event_at"], "reason": "Recorded required-candle quality failure"}
        if payload.get("status") not in {"EVALUATED", "LATE/MISSED"}:
            return {"status": "COVERAGE_UNKNOWN", "reason": "Archive does not demonstrate a completed engine evaluation"}
        return {"status": "MONITORED", "source": "ARCHIVED_TRADE_ALERT_OBSERVATION",
                "last_evaluation": fresh[-1]["event_at"],
                "scope": "Fresh actual evaluation near onset, not proof of gap-free 60m monitoring"}
    return {"status": "COVERAGE_UNKNOWN", "reason": "No fresh archived evaluation at onset",
            "latest_known_evaluation": known[-1]["event_at"] if known else None}


def _asset_day(store, asset_id, symbol, start, end, now, definition, config):
    cutoff = min(now, end+timedelta(minutes=definition.horizon_minutes))
    beginning = start-timedelta(minutes=definition.lookback_minutes)
    archived, truncated = _records(store, "trade_alert_candle", asset_id, beginning, cutoff)
    frames = {}
    for record in archived:
        payload = record["payload"]
        bar = Candle.model_validate(payload["candle"])
        # Respect both the native first receipt and this archive's publication.
        available = max(bar.available_at, _time(record["available_at"]))
        bar = bar.model_copy(update={"available_at": available})
        if bar.source != "binance-public-ws" and not bar.source.startswith("binance/spot/"):
            continue
        frames.setdefault(payload["timeframe"], []).append(bar)
    for rows in frames.values():
        rows.sort(key=lambda bar: bar.open_time)
    one = frames.get("1m", [])
    observations, observations_truncated = _records(store, "trade_alert_observation", asset_id, beginning, cutoff)
    raw_alerts, alerts_truncated = _records(store, "trade_alert", asset_id, beginning, cutoff, limit=1000)
    alerts = [record["payload"] for record in raw_alerts]
    labels = [label for label in label_expansions(symbol, one, cutoff, definition)
              if start <= _time(label["expansion_start"]) < end]
    controls = []
    for hour in range(24):
        clock = start+timedelta(hours=hour)
        baseline = next((bar for bar in one if bar.close_time == clock and bar.available_at <= clock), None)
        if baseline is None:
            # Native receipts normally lag close. Use the first known baseline
            # vintage and shift eligibility to its real receipt timestamp.
            baseline = next((bar for bar in one if bar.close_time == clock), None)
        if baseline is not None:
            controls.append(label_control_window(symbol, baseline, one, cutoff, definition))
    snapshots, coverage = {}, {}
    for label in labels:
        when = _time(label["expansion_start"])
        snapshots[label["event_id"]] = [dict(_snapshot(symbol, frames, observations,
            when-timedelta(minutes=minutes)), minutes_before=minutes) for minutes in SNAPSHOT_MINUTES]
        coverage[label["event_id"]] = _coverage(label, observations)
    # Reuse the existing, explicitly hypothetical whole-candle evaluator.
    # Larger horizons remain censored if the bounded daily archive ends sooner.
    outcomes = []
    for row in [item for item in alerts if _early_alert(item)][:100]:
        try:
            record = TradeAlertRecord.model_validate(row)
        except ValueError:
            continue
        for horizon in cfg_horizons(config, definition):
            outcome = calculate_trade_alert_outcome(record, one, horizon, now, config)
            if outcome is not None:
                outcomes.append({key: outcome.get(key) for key in (
                    "event_id", "horizon_seconds", "status", "close_return_proxy", "mfe_proxy", "mae_proxy",
                    "hypothetical_cost_bps", "hypothetical_cost_adjusted_close_return",
                    "hypothetical_cost_adjusted_mfe", "hypothetical_cost_adjusted_mae")})
    return {"asset_id": asset_id, "symbol": symbol, "labels": labels, "controls": controls,
            "alerts": alerts, "coverage": coverage, "snapshots": snapshots,
            "evaluated_at": now.isoformat(), "archive_1m_count": len(one),
            "archive_truncated": truncated or observations_truncated or alerts_truncated,
            "hypothetical_cost_bps": config.cost_bps, "hypothetical_outcomes": outcomes,
            "outcome_status": "AVAILABLE" if labels or controls else "DATA_UNAVAILABLE"}


def cfg_horizons(config, definition):
    return [horizon for horizon in config.horizon_seconds if horizon <= definition.horizon_minutes*60]


def evaluate_cached_store(store, as_of: datetime, date=None, config: TradeAlertSettings | None = None,
                          *, maximum_assets=40, top_per_direction=10,
                          definition: ExpansionDefinition | None = None) -> dict:
    """Advance a bounded daily audit using archived data only, never REST.

    Rotate at most forty asset histories per invocation. Persist completed asset
    batches so a 500-asset archive can be evaluated without monopolising the
    live worker. Discovery itself is capped at 10k recent archive records and
    cannot prove complete universe coverage. Current-day results remain partial.
    """
    if not 1 <= maximum_assets <= 100 or not 1 <= top_per_direction <= 100:
        raise ValueError("invalid bounded daily work budget")
    now, cfg = aware(as_of), config or TradeAlertSettings()
    day = _day(date, now)
    start = datetime.combine(Date.fromisoformat(day), datetime.min.time(), UTC)
    end = start+timedelta(days=1)
    if start > now:
        raise ValueError("daily audit cannot query a future date")
    spec = definition or ExpansionDefinition()
    fingerprint = hashlib.sha256(json.dumps(asdict(spec), sort_keys=True).encode()).hexdigest()[:16]
    work_id = f"{day}:{fingerprint}"
    previous = store.get_state("pre_ignition_research_work", work_id) or {}
    accumulated = dict(previous.get("assets", {}))
    with store._archive_lock:
        records = store._db.execute(
            "SELECT asset_id,json_extract(payload,'$.symbol') FROM intelligence_objects "
            "WHERE kind='trade_alert_candle' AND available_at>=? AND available_at<=? AND event_at<=? "
            "ORDER BY available_at DESC,object_id DESC LIMIT 10001",
            (start.isoformat(), min(now, end+timedelta(minutes=spec.horizon_minutes)).isoformat(),
             min(now, end+timedelta(minutes=spec.horizon_minutes)).isoformat())).fetchall()
    discovery_truncated = len(records) > 10000
    assets = dict((identity, symbol) for identity, symbol in reversed(records[:10000]) if identity and symbol)
    identities = sorted(assets)
    cursor = previous.get("last_asset_id")
    if cursor in identities:
        index = identities.index(cursor)+1
        identities = identities[index:]+identities[:index]
    selected = identities[:maximum_assets]
    failures = []
    for identity in selected:
        try:
            accumulated[identity] = _asset_day(store, identity, assets[identity], start, end, now, spec, cfg)
        except (KeyError, ValueError) as exc:
            failures.append({"asset_id": identity, "status": "DATA_UNAVAILABLE", "reason": str(exc)})
    if selected:
        store.put_state("pre_ignition_research_work", work_id, now,
                        {"assets": accumulated, "last_asset_id": selected[-1], "date": day})
    labels = [label for asset in accumulated.values() for label in asset["labels"]]
    ranked = sorted(labels, key=lambda label: (-label["total_move_pct"], label["event_id"]))
    chosen = [label for bias in ("LONG", "SHORT")
              for label in [row for row in ranked if row["bias"] == bias][:top_per_direction]]
    coverage = {key: value for asset in accumulated.values() for key, value in asset["coverage"].items()}
    alerts = [row for asset in accumulated.values() for row in asset["alerts"]]
    controls = [row for asset in accumulated.values() for row in asset["controls"]]
    report = evaluate_daily(chosen, alerts, controls=controls, coverage=coverage, as_of=now)
    snapshots = {key: value for asset in accumulated.values() for key, value in asset["snapshots"].items()}
    for case in report["cases"]:
        case["snapshots"] = snapshots.get(case["event_id"], [])
        case["why_missed"] = ("No actual early alert recorded in the declared window" if case["kpi_bucket"] ==
            "missed_completely" else "First early call had the wrong direction" if case["kpi_bucket"] ==
            "wrong_direction" else "First early call was late or explicitly ineligible" if case["kpi_bucket"] ==
            "detected_gt_20_pct" else "UNPROVEN" if case["kpi_bucket"] == "coverage_unknown" else None)
    fully_mature = now >= end+timedelta(minutes=spec.horizon_minutes)
    partial = (not fully_mature or len(accumulated) < len(assets) or discovery_truncated or failures
               or any(asset["archive_truncated"] for asset in accumulated.values()))
    if partial and report["status"] == "AVAILABLE":
        report["status"] = "PARTIAL"
    report.update(date=day, as_of=now.isoformat(), definition=asdict(spec),
                  coverage={"discovered_archived_assets": len(assets), "evaluated_assets": len(accumulated),
                            "batch_assets": len(selected), "configured_target": cfg.max_assets,
                            "discovery_truncated": discovery_truncated,
                            "claim": "Bounded archived evaluation cohort; actual complete universe UNKNOWN"},
                  top_selection={"per_direction": top_per_direction, "all_labeled_episodes": len(labels),
                                 "method": "Strongest matured LONG and SHORT episodes in observed cohort"},
                  scope="ARCHIVED_RECEIPT_QUALIFIED_DAILY_RESEARCH; not a live prediction or trade PnL",
                  failures=failures, full_day_mature=fully_mature,
                  reason="No usable archived daily market/evaluation evidence" if not accumulated else None)
    outcomes = [outcome for asset in accumulated.values() for outcome in asset.get("hypothetical_outcomes", [])]
    cost_groups = {}
    for horizon in cfg_horizons(cfg, spec):
        samples = [row for row in outcomes if row["horizon_seconds"] == horizon and row["status"] == "COMPLETE_PROXY"]
        cost_groups[str(horizon)] = {"sample_size": len(samples),
            "mean_cost_adjusted_close_return": mean(row["hypothetical_cost_adjusted_close_return"] for row in samples) if samples else None,
            "mean_mfe_proxy": mean(row["mfe_proxy"] for row in samples) if samples else None,
            "mean_mae_proxy": mean(row["mae_proxy"] for row in samples) if samples else None}
    report["hypothetical_outcomes"] = {"status": "AVAILABLE" if any(group["sample_size"] for group in cost_groups.values()) else "UNAVAILABLE",
        "assumed_round_trip_cost_bps": cfg.cost_bps, "horizons": cost_groups,
        "funding": "UNAVAILABLE", "actual_trade_pnl": None,
        "measurement": "Existing whole-candle return/excursion proxies; fees/spread/slippage are explicit assumed costs"}
    if not assets:
        report["status"] = "UNAVAILABLE"
    identity = hashlib.sha256(f"{VERSION}|{work_id}|{now.isoformat()}|{json.dumps(report, sort_keys=True)}".encode()).hexdigest()
    report["report_id"] = identity
    store.append("pre_ignition_daily_report", identity, min(end, now), now, report)
    store.put_state("pre_ignition_research", f"daily:{day}", now, report)
    store.put_state("pre_ignition_research", "current", now, report)
    return report


def cached_daily_report(store, as_of: datetime | None = None, date=None) -> dict:
    """Read the published cache only; no archive scan in the API hot path."""
    now = aware(as_of or utcnow())
    identity = f"daily:{_day(date, now)}" if date is not None else "current"
    row = store.get_state("pre_ignition_research", identity)
    if not row or _time(row["as_of"]) > now:
        return {"status": "UNAVAILABLE", "name": NAME, "version": VERSION, "as_of": None,
                "date": _day(date, now) if date is not None else None,
                "kpi": {"name": KPI_NAME, "rate": None, "buckets": None},
                "reason": "No published daily audit is available", "execution_enabled": False}
    row = dict(row)
    age = max(0, (now-_time(row["as_of"])).total_seconds())
    row["age_seconds"] = age
    row["cache_freshness"] = "STALE" if age > 7200 else "AVAILABLE"
    # An unavailable dataset stays UNAVAILABLE; freshness is a separate fact.
    if age > 7200 and row["status"] in {"AVAILABLE", "PARTIAL"}:
        row["status"] = "STALE"
    return row


async def run_pre_ignition_research_worker(app, config: TradeAlertSettings | None = None, *, interval_seconds=3600):
    """Separate cancellable thread worker; no computation during ingestion."""
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("research interval must be positive")
    from functools import partial

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="early-daily-audit")
    try:
        while True:
            try:
                operation = partial(evaluate_cached_store, app.state.store, utcnow(), config=config)
                # The owned executor does not compete with live ingestion's
                # default pool. Cancellation drains SQLite work before the
                # lifespan may close the shared store.
                pending = asyncio.get_running_loop().run_in_executor(executor, operation)
                try:
                    result = await asyncio.shield(pending)
                except asyncio.CancelledError:
                    await pending
                    raise
                app.state.pre_ignition_research_health = {"status": result["status"], "as_of": result["as_of"],
                                                         "date": result["date"], "coverage": result["coverage"],
                                                         "execution_enabled": False}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                app.state.pre_ignition_research_health = {"status": "FAILED", "reason": type(exc).__name__,
                                                         "execution_enabled": False}
            await asyncio.sleep(interval_seconds)
    finally:
        # Every submitted operation has completed above, so joining the idle
        # owned thread does not wait for database computation on the event loop.
        executor.shutdown(wait=True)
        app.state.pre_ignition_research_health = {"status": "STOPPED", "execution_enabled": False}
