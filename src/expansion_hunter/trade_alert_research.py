"""Bounded, causal Trade Alert replay/outcomes; no model fitting or trading.

Completed one-minute candles cannot establish intraminute fill ordering. Every
outcome is explicitly a whole-candle proxy with hypothetical costs, never PnL.
Research labels are archived separately and never supplied to the live engine.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import TIMEFRAMES, Asset, Candle, aware, utcnow
from .trade_alert import TradeAlertEngine
from .trade_alert_models import TradeAlertRecord, TradeAlertSettings

VERSION = "trade-alert-research-v1"
MEANINGFUL = {"TRADE_ALERT", "BREAK_CONFIRMED"}


def _timestamp(value):
    return aware(value if isinstance(value, datetime) else datetime.fromisoformat(value))


def _identity(*values):
    return hashlib.sha256("|".join(map(str, values)).encode()).hexdigest()


def _native(bar):
    return bar.source == "binance-public-ws" or bar.source.startswith("binance/spot/")


def calculate_trade_alert_outcome(record: TradeAlertRecord, candles: list[Candle], horizon_seconds: int,
                                  as_of: datetime, config: TradeAlertSettings) -> dict | None:
    """Use only full post-detection intervals, with explicit partial-window loss."""
    as_of = aware(as_of)
    if horizon_seconds <= 0:
        raise ValueError("horizon must be positive")
    start = record.detected_at
    end = start + timedelta(seconds=horizon_seconds)
    if as_of < end:
        return None
    first = datetime.fromtimestamp(math.ceil(start.timestamp() / 60) * 60, UTC)
    last = datetime.fromtimestamp(math.floor(end.timestamp() / 60) * 60, UTC)
    expected = max(0, int((last - first).total_seconds() / 60))
    result = {"event_id": record.event_id, "asset_id": record.asset_id, "symbol": record.symbol,
        "phase": record.phase, "bias": record.bias, "setup_type": record.setup_type,
        "alert_status": record.status,
        "horizon_seconds": horizon_seconds, "decision_timestamp": start.isoformat(),
        "horizon_end": end.isoformat(), "evaluated_at": as_of.isoformat(), "version": VERSION,
        "status": "CENSORED", "measurement": "WHOLE_CANDLE_PROXY", "reference_price": record.price,
        "excluded_head_seconds": (first - start).total_seconds(),
        "excluded_tail_seconds": (end - last).total_seconds(), "observed_seconds": 0,
        "close_return_proxy": None, "mfe_proxy": None, "mae_proxy": None,
        "hypothetical_cost_bps": config.cost_bps, "hypothetical_cost_adjusted_close_return": None,
        "hypothetical_cost_adjusted_mfe": None, "hypothetical_cost_adjusted_mae": None,
        "expanded_proxy": None, "first_expansion_close_timestamp": None,
        "actual_trade_pnl": None, "reasons": [], "execution_enabled": False}
    if not expected:
        result["reasons"].append("No full post-alert one-minute interval exists inside this horizon")
        return result
    visible = sorted((bar for bar in candles if bar.open_time >= first and bar.close_time <= last
                      and bar.available_at <= as_of), key=lambda bar: bar.open_time)
    if (len(visible) != expected or len({bar.open_time for bar in visible}) != expected
            or len({bar.source for bar in visible}) != 1
            or any((bar.close_time - bar.open_time).total_seconds() != 60 for bar in visible)
            or not visible or visible[0].open_time != first or visible[-1].close_time != last
            or any(left.close_time != right.open_time for left, right in zip(visible[:-1], visible[1:], strict=True))):
        result["reasons"].append("Missing, delayed, duplicate, mixed-source or noncontiguous post-alert intervals")
        return result
    sign = -1 if record.bias == "SHORT" else 1
    favorable = max(bar.high for bar in visible) if sign == 1 else min(bar.low for bar in visible)
    adverse = min(bar.low for bar in visible) if sign == 1 else max(bar.high for bar in visible)
    close = sign * (visible[-1].close / record.price - 1)
    mfe, mae = max(0.0, sign * (favorable / record.price - 1)), min(0.0, sign * (adverse / record.price - 1))
    cost = config.cost_bps / 10000
    expansion = next((bar for bar in visible if sign * (bar.close / record.price - 1) * 100
                      >= config.expansion_pct), None)
    result.update(status="COMPLETE_PROXY", observed_seconds=expected * 60,
        source=visible[0].source, close_return_proxy=close, mfe_proxy=mfe, mae_proxy=mae,
        hypothetical_cost_adjusted_close_return=close-cost, hypothetical_cost_adjusted_mfe=mfe-cost,
        hypothetical_cost_adjusted_mae=mae-cost,
        expanded_proxy=mfe * 100 >= config.expansion_pct,
        first_expansion_close_timestamp=expansion.close_time.isoformat() if expansion else None)
    result["reasons"].append("Full candles only; excluded partial intervals and execution/fill order remain unknown")
    return result


def purged_chronological_validation(samples: list[dict], *, train_end: datetime,
                                    holdout_start: datetime, label_horizon_seconds: int,
                                    embargo_seconds: float = 0) -> dict:
    """Partition chronological metadata; never fit or choose factor weights."""
    train_end, holdout_start = aware(train_end), aware(holdout_start)
    if holdout_start < train_end or label_horizon_seconds <= 0 or embargo_seconds < 0:
        raise ValueError("invalid chronological validation boundaries")
    output = {"train": [], "purged": [], "holdout": [], "method": "PURGED_CHRONOLOGICAL_NO_MODEL_FITTING"}
    prior = None
    for sample in samples:
        when = _timestamp(sample["decision_time"])
        if prior is not None and when < prior:
            raise ValueError("out-of-order validation observations")
        prior = when
        label_end = _timestamp(sample["label_end"]) if sample.get("label_end") else when + timedelta(seconds=label_horizon_seconds)
        label_available = _timestamp(sample["label_available_at"]) if sample.get("label_available_at") else label_end
        if label_end < when or label_available < label_end:
            raise ValueError("invalid outcome availability")
        if when <= train_end and label_end <= train_end and label_available <= train_end:
            split = "train"
        elif when >= holdout_start + timedelta(seconds=embargo_seconds):
            split = "holdout"
        else:
            split = "purged"
        output[split].append(sample["event_id"])
    output["limitation"] = "Partitioning is not a predictive result, a fitted model or an independently blind holdout"
    return output


def replay_trade_alerts(path: Path, config: TradeAlertSettings) -> dict:
    """Replay JSON observations in supplied arrival order using production rules."""
    payload = json.loads(Path(path).read_text())
    observations = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(observations, list) or not observations:
        raise ValueError("replay requires a nonempty observations list")
    engine, prior, history, records, evaluations, issues = TradeAlertEngine(config), None, {}, [], [], []
    for observation in observations:
        if not isinstance(observation, dict):
            raise ValueError("invalid replay observation")
        if any(key.lower().startswith(("future_", "outcome_", "label_")) or key.lower() in {"mfe", "mae"}
               for key in observation):
            raise ValueError("future labels cannot enter replay features")
        now = _timestamp(observation["as_of"])
        received = _timestamp(observation.get("received_at", observation["as_of"]))
        if prior is not None and now < prior:
            raise ValueError("out-of-order replay observation timestamps")
        if received > now:
            raise ValueError("future replay receipt")
        prior = now
        asset = Asset.model_validate(observation["asset"])
        visible = {}
        for frame, values in observation.get("candles", {}).items():
            if frame not in TIMEFRAMES:
                raise ValueError("unsupported replay timeframe")
            cache = history.setdefault((asset.asset_id, frame), {})
            for value in values:
                bar = Candle.model_validate(value)
                if bar.close_time > now or bar.available_at > received:
                    continue
                original = cache.get(bar.open_time)
                if original is not None and original.model_dump(exclude={"available_at"}) != bar.model_dump(exclude={"available_at"}):
                    issues.append(f"{asset.asset_id}:{frame}: closed candle revision ignored")
                    continue
                if original is None:
                    cache[bar.open_time] = bar
            visible[frame] = sorted(cache.values(), key=lambda bar: bar.open_time)[-config.history_bars:]
        one = visible.get("1m", [])
        price = observation.get("price", one[-1].close if one else None)
        benchmark = [Candle.model_validate(bar) for bar in observation.get("btc_candles", [])]
        events = engine.evaluate(asset, visible, now, received, price, benchmark, observation.get("tick_size"))
        records.extend(events)
        evaluations.append({"as_of": now.isoformat(), "asset_id": asset.asset_id,
                            "events": [event.model_dump(mode="json") for event in events],
                            "observation": engine.observations.get(asset.asset_id)})
    outcomes = []
    for event in records:
        candles = list(history.get((event.asset_id, "1m"), {}).values())
        for horizon in config.horizon_seconds:
            outcome = calculate_trade_alert_outcome(event, candles, horizon, prior, config)
            if outcome:
                outcomes.append(outcome)
    validation = None
    if payload.get("validation"):
        options = payload["validation"]
        validation = purged_chronological_validation(
            sorted(({"event_id": event.event_id, "decision_time": event.detected_at.isoformat()} for event in records),
                   key=lambda sample: (sample["decision_time"], sample["event_id"])),
            train_end=_timestamp(options["train_end"]), holdout_start=_timestamp(options["holdout_start"]),
            label_horizon_seconds=max(config.horizon_seconds), embargo_seconds=options.get("embargo_seconds", 0))
    return {"mode": "RESEARCH_REPLAY", "version": VERSION,
        "evidence": payload.get("evidence", "USER_SUPPLIED_UNVERIFIED_REPLAY"),
        "evaluation_count": len(evaluations), "evaluations": evaluations,
        "alerts": [event.model_dump(mode="json") for event in records], "outcomes": outcomes,
        "validation": validation, "issues": sorted(set(issues)), "execution_enabled": False,
        "settings": config.model_dump(mode="json"),
        "limitations": ["No model fitting, threshold tuning or calibrated probability.",
                         "Outcome candles never enter an earlier decision; first received closed candles remain immutable.",
                         "Caller-supplied replay data are not independently verified historical evidence.",
                         "Whole-candle excursions and hypothetical costs are not actual trade PnL."]}


def _misses(store, asset_id, rows, alerts, config, as_of):
    duration = min(config.horizon_seconds)
    if duration % 60:
        return []
    count = duration // 60
    if len(rows) < count + 1:
        return []
    future = rows[-count:]
    known_baselines = [(index, bar) for index, bar in enumerate(rows[:-count])
                      if bar.available_at <= future[0].open_time]
    if not known_baselines:
        return []
    baseline_index, baseline = known_baselines[-1]
    # A native candle commonly arrives just after the next minute opens. That
    # partial minute is excluded from excursion measurement, but it must still
    # exist in the archive: otherwise the baseline-to-outcome path has a gap.
    path = rows[baseline_index:]
    if (len({bar.open_time for bar in path}) != len(path)
            or len({bar.source for bar in path}) != 1
            or any((bar.close_time-bar.open_time).total_seconds() != 60 for bar in path)
            or any(left.close_time != right.open_time
                   for left, right in zip(path[:-1], path[1:], strict=True))):
        return []
    snapshots = store.objects("trade_alert_observation", baseline.available_at,
                              asset_id=asset_id, limit=1)
    observation = snapshots[-1] if snapshots else None
    prior = [event for event in alerts if event.asset_id == asset_id and event.detected_at <= baseline.available_at
             and event.detected_at >= baseline.close_time - timedelta(seconds=config.episode_seconds)]
    cooldown = store.get_state("trade_alert_miss_cursor", asset_id) or {}
    end = future[-1].close_time
    if cooldown.get("last_end") and end < _timestamp(cooldown["last_end"]) + timedelta(seconds=config.cooldown_seconds):
        return []
    output = []
    for bias, extreme in (("LONG", max(bar.high for bar in future)), ("SHORT", min(bar.low for bar in future))):
        move = (1 if bias == "LONG" else -1) * (extreme / baseline.close - 1) * 100
        if move < config.expansion_pct or any(event.bias == bias and event.status == "ACTIVE"
                and event.phase in MEANINGFUL for event in prior):
            continue
        identity = _identity(VERSION, "MISS", asset_id, bias, baseline.close_time.isoformat(), config.expansion_pct)
        if store.object("trade_alert_miss", identity, as_of):
            continue
        features = max(prior, key=lambda event: event.detected_at).model_dump(mode="json") if prior else None
        record = {"event_id": identity, "asset_id": asset_id, "bias": bias,
            "status": "MISS", "classification": "NO_PRIOR_MEANINGFUL_SIGNAL_IN_OBSERVED_WINDOW",
            "move_start_timestamp": baseline.close_time.isoformat(), "observed_until": end.isoformat(),
            "baseline_available_at": baseline.available_at.isoformat(),
            "observed_window_start": future[0].open_time.isoformat(),
            "excluded_head_seconds": (future[0].open_time-baseline.available_at).total_seconds(),
            "excluded_prefix_candles": len(path)-count-1,
            "baseline_to_end_contiguous_seconds": (end-baseline.close_time).total_seconds(),
            "recorded_at": as_of.isoformat(), "expansion_pct_proxy": move,
            "measurement": "WHOLE_CANDLE_PROXY", "native_contiguous_seconds": duration,
            "pre_available_features": {"baseline_candle": baseline.model_dump(mode="json"),
                                       "latest_alert": features, "observation": observation},
            "missing": [] if features or observation else ["No archived pre-move setup feature snapshot"],
            "scope": "Observed native 1m archive only; no universe-wide recall claim", "execution_enabled": False}
        store.append("trade_alert_miss", identity, baseline.close_time, as_of, record, asset_id)
        output.append(record)
    if output:
        store.put_state("trade_alert_miss_cursor", asset_id, as_of, {"last_end": end.isoformat()})
    return output


def evaluate_trade_alert_research(store, config: TradeAlertSettings, as_of: datetime, *, maximum_alerts=200) -> dict:
    """Mature immutable native archive outcomes; bounded and outside ingestion."""
    as_of = aware(as_of)
    if not 1 <= maximum_alerts <= 10000:
        raise ValueError("invalid research work budget")
    alerts = [TradeAlertRecord.model_validate(row) for row in store.objects("trade_alert", as_of, limit=10000)]
    alerts.sort(key=lambda record: (record.detected_at, record.event_id))
    progress = store.get_state("trade_alert_research", "cursor") or {}
    if progress.get("last_event_id"):
        index = next((index+1 for index, alert in enumerate(alerts) if alert.event_id == progress["last_event_id"]), 0)
        alerts = alerts[index:] + alerts[:index]
    selected, all_alerts = alerts[:maximum_alerts], list(alerts)
    fingerprint = _identity(config.model_dump_json())
    histories, counts, misses = {}, {"complete_proxy": 0, "censored": 0, "pending": 0, "new_outcomes": 0}, []

    def history(asset_id):
        archived = store.objects("trade_alert_candle", as_of, asset_id=asset_id, limit=10000)
        rows = [Candle.model_validate(row["candle"]) for row in archived if row.get("timeframe") == "1m"]
        return sorted((bar for bar in rows if _native(bar) and bar.available_at <= as_of
                       and bar.close_time <= as_of and (bar.close_time-bar.open_time).total_seconds() == 60),
                      key=lambda bar: bar.open_time)

    # Miss accounting must include observed assets that never produced alerts.
    # Bounded recent identity selection uses archived column metadata, not a
    # current universe or a retrospective preferred-winner list.
    with store._archive_lock:
        identities = store._db.execute("SELECT asset_id FROM intelligence_objects "
            "WHERE kind='trade_alert_candle' AND available_at<=? AND event_at<=? "
            "ORDER BY available_at DESC,object_id DESC LIMIT 2000", (as_of.isoformat(), as_of.isoformat())).fetchall()
    candidates = list(dict.fromkeys(row[0] for row in identities if row[0]))
    if progress.get("last_asset_id") in candidates:
        index = candidates.index(progress["last_asset_id"]) + 1
        candidates = candidates[index:] + candidates[:index]
    for asset_id in candidates[:20]:
        histories[asset_id] = history(asset_id)
        misses.extend(_misses(store, asset_id, histories[asset_id], all_alerts, config, as_of))
    processed = []
    for alert in selected:
        if alert.asset_id not in histories:
            if len(histories) >= 20:
                continue
            rows = history(alert.asset_id)
            histories[alert.asset_id] = rows
            misses.extend(_misses(store, alert.asset_id, rows, all_alerts, config, as_of))
        for horizon in config.horizon_seconds:
            identity = _identity(VERSION, alert.event_id, horizon, fingerprint, "COMPLETE_PROXY")
            if store.object("trade_alert_outcome", identity, as_of):
                continue
            outcome = calculate_trade_alert_outcome(alert, histories[alert.asset_id], horizon, as_of, config)
            if outcome is None:
                counts["pending"] += 1
                continue
            counts["complete_proxy" if outcome["status"] == "COMPLETE_PROXY" else "censored"] += 1
            identity = _identity(VERSION, alert.event_id, horizon, fingerprint, outcome["status"])
            if not store.object("trade_alert_outcome", identity, as_of):
                counts["new_outcomes"] += store.append("trade_alert_outcome", identity,
                    _timestamp(outcome["horizon_end"]), as_of, outcome, alert.asset_id)
        processed.append(alert)
    if processed or histories:
        store.put_state("trade_alert_research", "cursor", as_of,
                        {"last_event_id": processed[-1].event_id if processed else progress.get("last_event_id"),
                         "last_asset_id": next(reversed(histories), progress.get("last_asset_id"))})
    return {"status": "AVAILABLE", "as_of": as_of.isoformat(), "version": VERSION,
            "processed_alerts": len(processed), "assets_evaluated": len(histories), **counts,
            "new_misses": len(misses), "archive_alerts_considered": len(all_alerts),
            "scope": "Bounded observed archive; COMPLETE_PROXY is not executed PnL", "execution_enabled": False}


async def run_trade_alert_research_worker(app, config: TradeAlertSettings, *, interval_seconds=60):
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("research interval must be positive")
    from .journal_runtime import _complete_thread_call
    try:
        while True:
            try:
                app.state.trade_alert_research_health = await _complete_thread_call(
                    evaluate_trade_alert_research, app.state.store, config, utcnow())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                app.state.trade_alert_research_health = {"status": "FAILED", "reason": type(exc).__name__,
                                                         "execution_enabled": False}
            await asyncio.sleep(interval_seconds)
    finally:
        app.state.trade_alert_research_health = {"status": "STOPPED", "execution_enabled": False}
