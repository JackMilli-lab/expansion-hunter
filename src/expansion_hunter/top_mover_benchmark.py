"""TOP MOVER EARLY DETECTION TEST: outcome labels never enter live decisions.

Onsets and excursions are completed-one-minute-close proxies, not tick-level
facts. Archived receipt times are honoured. A historical REST download cannot
prove what this installation received or ranked in the past. Production engines
are reused unchanged; this module neither fits models nor adjusts thresholds.
"""
from __future__ import annotations

import copy
import hashlib
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from statistics import mean

from .fast import FastIgnitionMonitor
from .fast_models import FastSettings, FastStage
from .models import Asset, AssetSnapshot, Candle, aware
from .pre_move import PreMoveRanker, PreMoveSettings
from .trade_alert import TradeAlertEngine
from .trade_alert_models import TradeAlertSettings
from .trade_alert_research import purged_chronological_validation

NAME = "TOP MOVER EARLY DETECTION TEST"
LEGACY_DETECTION_STATES = frozenset(stage.value for stage in (
    FastStage.WATCH, FastStage.PRE_IGNITION, FastStage.LOADED,
    FastStage.BREAKOUT_PRESSURE, FastStage.BREAKDOWN_PRESSURE,
    FastStage.BREAKOUT_ATTEMPT, FastStage.BREAKDOWN_ATTEMPT,
    FastStage.BREAKOUT_CONFIRMED, FastStage.BREAKDOWN_CONFIRMED,
    FastStage.ACCEPTED, FastStage.ACCEPTED_DOWN,
    FastStage.EXPANSION_CONFIRMED, FastStage.EXPANSION_CONFIRMED_DOWN))
EARLY_STATES = LEGACY_DETECTION_STATES.union({
    "RADAR", "READY", "TRIGGER_IMMINENT", "TRIGGER", "STRUCTURE_WATCH", "TRADE_ALERT",
    "BREAK_CONFIRMED", "ACTIVE"})


def consumed_grade(fraction: float | None) -> str | None:
    """Outcome-only grade, mirrored for both directions; never a live forecast.

    Round floating-point ratio noise at the exact 5/10/20 percent boundaries.
    The consumption numerator remains maximum favourable progress, so a late
    retracement cannot wash away an already consumed move.
    """
    if fraction is None:
        return None
    if not math.isfinite(fraction) or not 0 <= fraction <= 1:
        raise ValueError("consumed fraction must be between zero and one")
    value = round(fraction, 12)
    return "A+" if value < .05 else "A" if value < .10 else "B" if value <= .20 else "LATE"


@dataclass(frozen=True)
class ExpansionDefinition:
    threshold_pct: float = 3.0
    horizon_minutes: int = 60
    initiation_pct: float = .15
    max_completed_fraction: float = .20
    lookback_minutes: int = 60

    def __post_init__(self):
        values = (self.threshold_pct, self.initiation_pct, self.max_completed_fraction)
        if (any(not math.isfinite(value) for value in values) or self.threshold_pct <= 0
                or not 0 < self.initiation_pct < self.threshold_pct
                or not 0 <= self.max_completed_fraction <= 1
                or not isinstance(self.horizon_minutes, int) or isinstance(self.horizon_minutes, bool)
                or not isinstance(self.lookback_minutes, int) or isinstance(self.lookback_minutes, bool)
                or self.horizon_minutes < 1 or self.lookback_minutes < 0):
            raise ValueError("invalid predeclared expansion definition")


def _time(value):
    return aware(value if isinstance(value, datetime) else datetime.fromisoformat(value))


def _minutes(candles, as_of):
    rows = sorted((bar for bar in candles if bar.close_time <= as_of and bar.available_at <= as_of),
                  key=lambda bar: bar.open_time)
    if any((bar.close_time-bar.open_time).total_seconds() != 60 for bar in rows):
        raise ValueError("benchmark labels require completed one-minute candles")
    if len({bar.open_time for bar in rows}) != len(rows):
        raise ValueError("duplicate/revised candle intervals must be resolved before benchmarking")
    return rows


def _complete(rows):
    return bool(rows) and len({bar.source for bar in rows}) == 1 and all(
        left.close_time == right.open_time for left, right in zip(rows[:-1], rows[1:], strict=True))


def label_expansions(symbol: str, candles: list[Candle], as_of: datetime,
                     definition: ExpansionDefinition | None = None) -> list[dict]:
    """Outcome-only onset labels with fixed initiation and forward thresholds.

    A qualifying episode begins at the open of a completed minute whose close
    changes by initiation_pct from the previous close. Its fixed, fully observed
    horizon must subsequently reach threshold_pct on a close. The earliest
    qualifying onset is kept, then overlapping horizons are excluded. This
    declared definition may miss slow ramps and is not an objective truth about
    the precise start of every move. Do not tune it using selected winners.
    """
    config, now = definition or ExpansionDefinition(), aware(as_of)
    rows, output, cursor = _minutes(candles, now), [], 0
    while cursor + config.horizon_minutes < len(rows):
        baseline = rows[cursor]
        path = rows[cursor+1:cursor+1+config.horizon_minutes]
        if not _complete([baseline, *path]):
            cursor += 1
            continue
        first_return = (path[0].close/baseline.close-1)*100
        bias = "LONG" if first_return >= config.initiation_pct else (
            "SHORT" if first_return <= -config.initiation_pct else None)
        if bias is None:
            cursor += 1
            continue
        sign = 1 if bias == "LONG" else -1
        progress = [sign*(bar.close/baseline.close-1)*100 for bar in path]
        if max(progress) < config.threshold_pct:
            cursor += 1
            continue
        peak_index = progress.index(max(progress))
        crossing = next(index for index, move in enumerate(progress) if move >= config.threshold_pct)
        identity = hashlib.sha256(f"{symbol}|{bias}|{baseline.close_time.isoformat()}|{asdict(config)}".encode()).hexdigest()
        output.append({"event_id": identity, "symbol": symbol, "bias": bias,
            "expansion_start": baseline.close_time.isoformat(), "baseline_price": baseline.close,
            "threshold_crossing": path[crossing].close_time.isoformat(),
            "peak_time": path[peak_index].close_time.isoformat(), "peak_price": path[peak_index].close,
            "total_move_pct": max(progress), "horizon_end": path[-1].close_time.isoformat(),
            "label_available_at": max(bar.available_at for bar in path).isoformat(),
            "source": baseline.source, "measurement": "COMPLETED_1M_CLOSE_PROXY",
            "definition": asdict(config),
            "path": [{"at": bar.close_time.isoformat(), "price": bar.close,
                      "move_pct": move} for bar, move in zip(path, progress, strict=True)]})
        cursor += config.horizon_minutes
    return output


def label_control_window(symbol: str, baseline: Candle, future: list[Candle], as_of: datetime,
                         definition: ExpansionDefinition | None = None, *, bias: str | None = None) -> dict:
    """Label preselected controls after maturation; never select them by outcome.

    Other winners remain in the selected control cohort. NON_MOVE is only set
    after a full contiguous horizon has been observed. Optional bias conditions
    both the outcome and subsequent false-positive matching on that direction.
    Alert eligibility starts when the baseline was actually available; a delayed
    baseline does not retroactively grant knowledge at its candle close.
    """
    config, now = definition or ExpansionDefinition(), aware(as_of)
    if bias not in {None, "LONG", "SHORT"}:
        raise ValueError("control bias must be LONG, SHORT or unspecified")
    end = baseline.close_time + timedelta(minutes=config.horizon_minutes)
    start = max(baseline.close_time, baseline.available_at)
    result = {"symbol": symbol, "start": start.isoformat(), "baseline_timestamp": baseline.close_time.isoformat(),
              "end": end.isoformat(),
              "status": "CENSORED", "measurement": "COMPLETED_1M_CLOSE_PROXY",
              "maximum_move_pct": None, "bias": bias, "definition": asdict(config)}
    if baseline.close_time > now or start > now or start >= end or now < end:
        return result
    rows = _minutes(future, now)
    rows = [bar for bar in rows if baseline.close_time <= bar.open_time and bar.close_time <= end]
    if (len(rows) != config.horizon_minutes or not _complete([baseline, *rows])
            or rows[-1].close_time != end):
        return result
    moves = [(bar.close/baseline.close-1)*100 for bar in rows]
    maximum = max(abs(move) for move in moves) if bias is None else max(
        0.0, *(move if bias == "LONG" else -move for move in moves))
    result.update(status="NON_MOVE" if maximum < config.threshold_pct else "MOVE",
                  maximum_move_pct=maximum, label_available_at=max(bar.available_at for bar in rows).isoformat())
    return result


def replay_pre_move(assets: dict[str, Asset], histories: dict[str, dict[str, list[Candle]]],
                    as_of: datetime, *, decision_start: datetime | None = None,
                    settings: TradeAlertSettings | None = None,
                    snapshot_times: set[datetime] | None = None) -> dict:
    """Replay actual production engines in archived candle-availability order.

    Closed candles with late receipts remain unavailable until receipt. No
    intraminute trades/books/derivatives are reconstructed. Rank is a replayed
    fixed supplied cohort, never a claimed historical top-500 rank. All symbols
    received at the same boundary are evaluated before ranking that cohort.
    """
    config, now = settings or TradeAlertSettings(), aware(as_of)
    start = aware(decision_start) if decision_start else None
    keep = {aware(value) for value in snapshot_times} if snapshot_times is not None else None
    if set(assets) != set(histories):
        raise ValueError("histories must match supplied asset identifiers")
    engine, ranker = TradeAlertEngine(config), PreMoveRanker(PreMoveSettings.model_validate(config.pre_move))
    arrivals = []
    for asset_id, frames in histories.items():
        for frame, candles in frames.items():
            seen = set()
            for bar in candles:
                if bar.open_time in seen:
                    raise ValueError("duplicate/revised historical candle")
                seen.add(bar.open_time)
                if bar.available_at <= now and bar.close_time <= now:
                    arrivals.append((bar.available_at, asset_id, frame, bar))
    arrivals.sort(key=lambda row: (row[0], row[1], row[2], row[3].open_time))
    cache = {key: {} for key in assets}
    benchmark_id = next((key for key, asset in assets.items() if asset.exchange_symbol == "BTCUSDT"), None)
    alerts, snapshots, transitions, cursor = [], [], [], 0
    while cursor < len(arrivals):
        boundary, changed = arrivals[cursor][0], set()
        while cursor < len(arrivals) and arrivals[cursor][0] == boundary:
            _, identity, frame, bar = arrivals[cursor]
            bars = cache[identity].setdefault(frame, [])
            bars.append(bar)
            bars.sort(key=lambda value: value.open_time)
            cache[identity][frame] = bars[-config.history_bars:]
            changed.add(identity)
            cursor += 1
        if start and boundary < start:
            continue
        benchmark = cache[benchmark_id].get("5m", []) if benchmark_id else []
        for identity in sorted(changed):
            one = cache[identity].get("1m", [])
            records = engine.evaluate(assets[identity], cache[identity], boundary, boundary,
                                      one[-1].close if one else None, benchmark)
            alerts.extend(record.model_dump(mode="json") for record in records)
        rows = ranker.update(engine.observations, engine.episodes, boundary)
        transitions.extend(copy.deepcopy(row) for row in rows if row["state_changed"])
        if keep is None or boundary in keep:
            snapshots.append({"as_of": boundary.isoformat(), "rows": copy.deepcopy(rows),
                              "observations": copy.deepcopy(engine.observations)})
    return {"name": NAME, "mode": "RESEARCH_REPLAY", "alerts": alerts,
            "engine": "TradeAlertEngine+PreMoveRanker",
            "state_transitions": transitions, "snapshots": snapshots,
            "settings": config.model_dump(mode="json"), "execution_enabled": False,
            "rank_scope": "REPLAYED_SUPPLIED_COHORT_NOT_ARCHIVED_HISTORICAL_UNIVERSE",
            "limitations": ["Historical receipt timestamps must be independently justified.",
                "This dedicated engine is not the default Docker app's legacy FastIgnitionMonitor worker.",
                "Minute closes cannot establish intraminute order flow, sub-minute onset or live installation misses.",
                "No archived order book, OI, funding or trade tape is inferred.",
                "Replay starts with a cold production episode state; earlier live alerts are unknown."]}


def replay_fast_monitor(assets: dict[str, Asset], histories: dict[str, dict[str, list[Candle]]],
                        as_of: datetime, *, decision_start: datetime | None = None,
                        settings: FastSettings | None = None,
                        snapshot_times: set[datetime] | None = None) -> dict:
    """Replay the existing app's monitor under explicit eligibility assumptions.

    Every supplied asset is assumed already deep-watched; historical broad-scan
    eligibility/promotion is unknown. Only actual emitted events are alerts:
    the initial WATCH membership alone is never counted as detection evidence.
    Archived tick sizes and intraminute trades are not reconstructed.
    """
    config, now = settings or FastSettings(), aware(as_of)
    start = aware(decision_start) if decision_start else None
    keep = {aware(value) for value in snapshot_times} if snapshot_times is not None else None
    if set(assets) != set(histories):
        raise ValueError("histories must match supplied asset identifiers")
    arrivals = []
    for identity, frames in histories.items():
        for frame in ("1m", "5m", "15m"):
            seen = set()
            for bar in frames.get(frame, []):
                if bar.open_time in seen:
                    raise ValueError("duplicate/revised historical candle")
                seen.add(bar.open_time)
                if bar.available_at <= now and bar.close_time <= now:
                    arrivals.append((bar.available_at, identity, frame, bar))
    arrivals.sort(key=lambda row: (row[0], row[1], row[2], row[3].open_time))
    cache = {identity: {} for identity in assets}
    monitor = FastIgnitionMonitor(config)
    events, alerts, snapshots, issues, cursor = [], [], [], [], 0
    initialized = set()
    while cursor < len(arrivals):
        boundary, changes = arrivals[cursor][0], {}
        while cursor < len(arrivals) and arrivals[cursor][0] == boundary:
            _, identity, frame, bar = arrivals[cursor]
            bars = cache[identity].setdefault(frame, [])
            bars.append(bar)
            bars.sort(key=lambda value: value.open_time)
            cache[identity][frame] = bars[-config.history_bars:]
            changes.setdefault(identity, {}).setdefault(frame, []).append(bar)
            cursor += 1
        if start and boundary < start:
            continue
        for identity, changed in sorted(changes.items()):
            visible = changed if identity in initialized else cache[identity]
            snapshot = AssetSnapshot(asset=assets[identity], as_of=boundary, candles=visible)
            evaluated = monitor.evaluate(assets[identity], snapshot, evaluation_at=boundary,
                                         regime="UNAVAILABLE")
            initialized.add(identity)
            issues.extend({"asset_id": identity, "as_of": boundary.isoformat(), "reason": issue}
                          for issue in evaluated.issues)
            for event in evaluated.events:
                raw = event.model_dump(mode="json")
                events.append(raw)
                alerts.append({**raw, "symbol": assets[identity].exchange_symbol or assets[identity].symbol,
                               "detected_at": boundary.isoformat(), "state": raw["event_type"],
                               "status": "ACTIVE" if evaluated.state.data_quality.get("spot_candles") ==
                               "AVAILABLE" else "STALE"})
        if keep is None or boundary in keep:
            snapshots.append({"as_of": boundary.isoformat(), "states": {
                identity: state.model_dump(mode="json", exclude={"candles"})
                for identity, state in sorted(monitor.states.items())}})
    return {"name": NAME, "mode": "RESEARCH_REPLAY", "engine": "FastIgnitionMonitor",
            "events": events, "alerts": alerts, "snapshots": snapshots, "issues": issues,
            "settings": config.model_dump(mode="json"), "execution_enabled": False,
            "membership_assumption": "ALL_SUPPLIED_ASSETS_ALREADY_DEEP_WATCHED; ARCHIVED_PROMOTIONS_UNKNOWN",
            "limitations": ["This is a detector counterfactual, not proof of historical installation alerts or misses.",
                "Every supplied asset is assumed eligible; actual broad-scan promotion history is unknown.",
                "Initial WATCH membership is not counted as an emitted alert.",
                "Minute closes cannot establish intraminute trades or sub-minute onset.",
                "Historical receipt timestamps must be independently justified.",
                "Order book, funding, OI, historical regime and tick size remain unavailable.",
                "Replay begins with cold episode state; earlier live alerts are unknown."]}


def _alert_time(row):
    return _time(row.get("detected_at", row.get("as_of")))


def _meaningful(row):
    return (row.get("state", row.get("phase")) in EARLY_STATES
            and row.get("status") in {"AVAILABLE", "ACTIVE", "LATE", "MISSED"}
            and row.get("bias") in {"LONG", "SHORT"}
            and isinstance(row.get("price"), (int, float)) and row["price"] > 0
            and math.isfinite(row["price"]))


def evaluate_early_detection(labels: list[dict], alerts: list[dict], *,
                              controls: list[dict] | None = None, as_of: datetime | None = None) -> dict:
    """Measure early detection, not hindsight profitability or calibrated odds.

    Completed fraction uses the maximum favourable progress already observed,
    preventing a late retracement from being relabelled as an early entry.
    Controls must have been preselected before their outcome was known.
    PRE-MOVE passes require an alert strictly BEFORE the declared onset and
    no more than the configured fraction completed. A separate not-late metric
    permits alerts after onset with substantial move remaining. Neither
    receives a pass merely because the eventual direction was right.
    If supplied, as_of censors outcome labels not yet fully received.
    """
    eligible = sorted((row for row in alerts if _meaningful(row)), key=_alert_time)
    now = aware(as_of) if as_of else None
    if now:
        eligible = [row for row in eligible if _alert_time(row) <= now]
    cases = []
    for label in labels:
        start, end = _time(label["expansion_start"]), _time(label["horizon_end"])
        config = ExpansionDefinition(**label["definition"])
        window_start = start-timedelta(minutes=config.lookback_minutes)
        prior = [row for row in eligible if row.get("symbol") == label["symbol"]
                 and window_start <= _alert_time(row) <= end]
        alert = prior[0] if prior else None
        measured = {"event_id": label["event_id"], "symbol": label["symbol"], "bias": label["bias"],
                    "status": "MISSED", "first_alert": alert, "direction_correct": None,
                    "lead_time_seconds": None, "move_completed_fraction": None,
                    "move_already_completed_pct": None, "move_remaining_pct": None,
                    "pre_move": False, "early_detection_pass": False, "not_late_detection_pass": False,
                    "move_consumed_pct": None, "move_remaining_fraction_pct": None,
                    "consumed_grade": None, "detected_before_10_pct": False,
                    "consumed_measurement": label.get("measurement", "COMPLETED_1M_CLOSE_PROXY")}
        if now and max(end, _time(label.get("label_available_at", label["horizon_end"]))) > now:
            measured.update(status="CENSORED", first_alert=None)
            cases.append(measured)
            continue
        if alert:
            when, sign = _alert_time(alert), 1 if label["bias"] == "LONG" else -1
            known = [part["move_pct"] for part in label["path"] if _time(part["at"]) <= when]
            price_progress = sign*(alert["price"]/label["baseline_price"]-1)*100
            completed = max(0.0, price_progress, *known)
            fraction = min(1.0, completed/label["total_move_pct"])
            correct = alert["bias"] == label["bias"]
            timely = round(fraction, 12) <= round(config.max_completed_fraction, 12) and when < _time(label["peak_time"])
            not_late = correct and timely and alert.get("status") not in {"LATE", "MISSED"}
            pre_move = when < start
            passed = not_late and pre_move
            measured.update(status="PASS" if passed else "WRONG_DIRECTION" if not correct else
                "POST_ONSET_FAILURE" if not_late else "LATE_FAILURE",
                direction_correct=correct, lead_time_seconds=(start-when).total_seconds(),
                move_completed_fraction=fraction, move_already_completed_pct=completed,
                move_consumed_pct=fraction*100, move_remaining_fraction_pct=(1-fraction)*100,
                consumed_grade=consumed_grade(fraction),
                detected_before_10_pct=not_late and round(fraction, 12) < .10,
                move_remaining_pct=max(0.0, sign*(label["peak_price"]/alert["price"]-1)*100)
                    if when < _time(label["peak_time"]) else 0.0,
                pre_move=pre_move, early_detection_pass=passed, not_late_detection_pass=not_late)
        cases.append(measured)
    nonmoves = [row for row in controls or [] if row["status"] == "NON_MOVE" and
        (not now or max(_time(row["end"]), _time(row.get("label_available_at", row["end"]))) <= now)]
    false_positive_windows = sum(any(row.get("symbol") == control["symbol"]
        and (control.get("bias") is None or row.get("bias") == control["bias"])
        and _time(control["start"]) <= _alert_time(row) < _time(control["end"]) for row in eligible)
        for control in nonmoves)
    matured = [row for row in cases if row["status"] != "CENSORED"]
    called = [row for row in matured if row["first_alert"]]
    return {"name": NAME, "cases": cases, "expansion_count": len(cases), "alerted_expansion_count": len(called),
            "early_detection_count": sum(row["early_detection_pass"] for row in cases),
            "matured_expansion_count": len(matured), "censored_expansion_count": len(cases)-len(matured),
            "early_detection_rate": mean(row["early_detection_pass"] for row in matured) if matured else None,
            "not_late_detection_count": sum(row["not_late_detection_pass"] for row in matured),
            "not_late_detection_rate": mean(row["not_late_detection_pass"] for row in matured) if matured else None,
            "detected_before_10_pct_count": sum(row["detected_before_10_pct"] for row in matured),
            "direction_accuracy": mean(row["direction_correct"] for row in called) if called else None,
            "mean_alert_lead_time_seconds": mean(row["lead_time_seconds"] for row in called) if called else None,
            "non_move_control_count": len(nonmoves), "false_positive_control_windows": false_positive_windows,
            "false_positive_rate": false_positive_windows/len(nonmoves) if nonmoves else None,
            "early_detection_definition": "Correct first alert strictly before onset and within configured completed-fraction limit",
            "not_late_detection_definition": "Correct first alert within completed-fraction limit before peak, including post-onset",
            "false_positive_definition": "Fully observed preselected NON_MOVE windows with a meaningful alert matching optional control bias",
            "execution_enabled": False, "probability": None}


def walk_forward_splits(samples: list[dict], folds: list[tuple[datetime, datetime]], *,
                        horizon_seconds: int, embargo_seconds: float = 0) -> list[dict]:
    """Reuse existing purged chronological validation; never fit on holdout."""
    if any(right[0] <= left[0] for left, right in zip(folds[:-1], folds[1:], strict=True)):
        raise ValueError("walk-forward training boundaries must increase")
    return [purged_chronological_validation(samples, train_end=train_end, holdout_start=holdout,
        label_horizon_seconds=horizon_seconds, embargo_seconds=embargo_seconds)
        for train_end, holdout in folds]
