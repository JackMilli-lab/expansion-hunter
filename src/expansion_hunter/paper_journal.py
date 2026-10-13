"""Persistent, explicit manual paper positions; never an exchange order client.

Entry and fills are declared/model assumptions in one quote currency. Native
completed 1m intervals provide causal progress, not an executable tick path.
Gaps censor automatic exits. A bar crossing both stop and target is ambiguous.
Exit-bar excursions are conservative lower bounds because intrabar order is
unknown. None of these records are real fills, calibrated forecasts or live PnL.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timedelta
from statistics import mean, median
from typing import Literal

from pydantic import Field, model_validator

from .intelligence_store import IntelligenceStore
from .macro_models import MacroMarketObservation, MacroQuality
from .models import TIMEFRAMES, Model, aware, utcnow
from .trade_calculator import (
    PortfolioInput,
    PortfolioPosition,
    PortfolioResult,
    TradeInput,
    calculate_portfolio,
    calculate_trade,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_journal_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_positions(
 position_id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,asset_id TEXT NOT NULL,
 symbol TEXT NOT NULL,opened_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 status TEXT NOT NULL,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS paper_position_work ON paper_positions(status,updated_at);
CREATE INDEX IF NOT EXISTS paper_position_asset ON paper_positions(asset_id,opened_at);
CREATE TABLE IF NOT EXISTS paper_journal_events(
 event_id TEXT PRIMARY KEY,position_id TEXT NOT NULL REFERENCES paper_positions(position_id),
 event_at TEXT NOT NULL,available_at TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS paper_journal_history ON paper_journal_events(position_id,available_at);
"""
PAPER_ASSUMPTIONS = [
    "PAPER ONLY: entries are user-declared prices; no exchange orders or actual fills.",
    "Linear quote-currency settlement; no EUR/USD conversion or inverse-contract accounting.",
    "Full position exits at the primary target; additional targets are annotations, not partial fills.",
    "Stop/target fills assume the declared level; slippage, gaps and liquidation execution are not modeled.",
    "Fees are user inputs; funding is unknown unless explicitly supplied as a signed cash assumption.",
    "1m bars cannot determine intrabar ordering; simultaneous stop/target hits remain AMBIGUOUS.",
]


class PaperTradeInput(TradeInput):
    # Missing funding must not silently become an assumed neutral market input.
    funding_cost: float | None = None


class PaperOpenRequest(Model):
    request_id: str = Field(min_length=1, max_length=128)
    asset_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9_.-]+$")
    quote_currency: str = Field(min_length=2, max_length=16, pattern=r"^[A-Z0-9]+$")
    trade: PaperTradeInput
    setup: str = Field(default="MANUAL", min_length=1, max_length=128)
    timeframe: str = "1m"
    notes: str = Field(default="", max_length=2000)
    additional_targets: list[float] = Field(default_factory=list, max_length=10)
    sector: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def coherent(self):
        if self.timeframe not in TIMEFRAMES:
            raise ValueError("unsupported journal timeframe")
        direction = 1 if self.trade.side == "LONG" else -1
        if len(self.additional_targets) != len(set(self.additional_targets)) or any(
            target <= 0 or direction * (target - self.trade.target) <= 0
            for target in self.additional_targets
        ):
            raise ValueError("additional targets must be distinct and beyond the primary target")
        return self


class PaperCloseRequest(Model):
    exit_price: float = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1000)
    funding_cost: float | None = None
    funding_source: Literal["USER_ASSUMPTION", "USER_INPUT"] = "USER_INPUT"


class PaperFinancialResult(Model):
    exit_price: float = Field(gt=0)
    gross_pnl: float
    entry_fee: float
    exit_fee: float
    fees: float
    funding_cost: float | None
    net_pnl: float | None
    net_status: Literal["USER_ASSUMPTION", "USER_INPUT", "UNAVAILABLE"]
    roi_on_margin_percent: float | None
    initial_net_risk: float | None
    r_multiple: float | None
    gross_r_multiple: float


class PaperPosition(Model):
    position_id: str
    request: PaperOpenRequest
    opened_at: datetime
    updated_at: datetime
    status: Literal["OPEN", "CENSORED", "AMBIGUOUS", "CLOSED"] = "OPEN"
    mode: Literal["PAPER_ONLY"] = "PAPER_ONLY"
    entry_source: Literal["USER_DECLARED_NOT_EXECUTED"] = "USER_DECLARED_NOT_EXECUTED"
    position_size: float = Field(gt=0)
    notional: float = Field(gt=0)
    initial_net_risk: float | None = None
    feature_snapshot: dict = Field(default_factory=dict)
    regime: str | None = None
    last_market_timestamp: datetime | None = None
    last_evaluation_at: datetime | None = None
    closed_at: datetime | None = None
    exit_interval_start: datetime | None = None
    exit_interval_end: datetime | None = None
    exit_kind: Literal["STOP", "TARGET", "USER_DECLARED"] | None = None
    exit_source: Literal["PAPER_MODEL_LEVEL_FILL", "USER_DECLARED_NOT_EXECUTED"] | None = None
    financial_result: PaperFinancialResult | None = None
    mfe_return: float | None = None
    mae_return: float | None = None
    excursion_status: str = "UNAVAILABLE"
    coverage_status: str = "PENDING"
    samples: int = 0
    source: str | None = None
    reasons: list[str] = Field(default_factory=list)
    risk_warnings: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=lambda: list(PAPER_ASSUMPTIONS))
    version: str = "paper-journal-v1"


def _timestamp(value) -> datetime | None:
    try:
        return aware(value if isinstance(value, datetime) else datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


def _reject_outcome_features(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower().startswith(("future_", "outcome_", "label_")) or key.lower() in {
                "mfe", "mae", "winrate", "profit_factor", "realized_pnl",
            }:
                raise ValueError("future outcomes cannot enter entry feature snapshots")
            _reject_outcome_features(item)
    elif isinstance(value, list):
        for item in value:
            _reject_outcome_features(item)


def entry_snapshot(store: IntelligenceStore, symbol: str, at: datetime) -> dict:
    """Preserve exactly an available runtime report, never retrofit newer states."""
    at = aware(at)
    with store._archive_lock:
        row = store._db.execute("SELECT updated_at,payload FROM runtime_states WHERE kind=? AND object_id=?",
                                ("war_room_state", symbol)).fetchone()
    state = json.loads(row[1]) if row else None
    published = _timestamp(row[0]) if row else None
    stamp = _timestamp(state.get("as_of")) if state else None
    if stamp is None or published is None or not stamp <= published <= at:
        return {"status": "UNAVAILABLE", "source": "war_room_state", "reason": "No causal entry-time state snapshot."}
    _reject_outcome_features(state)
    age = (at - stamp).total_seconds()
    return {
        "status": "STALE" if age > 90 else "AVAILABLE",
        "source": "war_room_state:derived_public_market_data",
        "as_of": stamp.isoformat(),
        "available_at": published.isoformat(),
        "age_seconds": age,
        "payload": copy.deepcopy(state),
    }


def _financial(position: PaperPosition, exit_price: float, funding: float | None,
               funding_source: str) -> PaperFinancialResult:
    trade = position.request.trade
    direction = 1 if trade.side == "LONG" else -1
    quantity = position.position_size
    gross = direction * quantity * (exit_price - trade.entry)
    entry_fee = quantity * trade.entry * trade.entry_fee_rate
    exit_fee = quantity * exit_price * trade.exit_fee_rate
    net = gross - entry_fee - exit_fee - funding if funding is not None else None
    # A later funding estimate may describe the final cash cost. It does not
    # rewrite the original risk budget that was unknown at entry.
    risk = position.initial_net_risk
    return PaperFinancialResult(
        exit_price=exit_price,
        gross_pnl=gross,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        fees=entry_fee + exit_fee,
        funding_cost=funding,
        net_pnl=net,
        net_status=funding_source if funding is not None else "UNAVAILABLE",
        roi_on_margin_percent=100 * net / trade.margin if net is not None else None,
        initial_net_risk=risk,
        r_multiple=net / risk if net is not None and risk is not None and risk > 0 else None,
        gross_r_multiple=gross / (quantity * abs(trade.entry - trade.stop)),
    )


def evaluate_position(position: PaperPosition, observations: list[MacroMarketObservation], *,
                      as_of: datetime) -> PaperPosition:
    """Completed native intervals only; no future timestamps or guessed fills."""
    as_of = aware(as_of)
    if as_of < position.opened_at or (position.last_evaluation_at and as_of < position.last_evaluation_at):
        raise ValueError("paper evaluation cannot move backward")
    if position.status in {"CLOSED", "AMBIGUOUS"}:
        return position.model_copy(deep=True)
    result = position.model_copy(deep=True)
    result.updated_at = result.last_evaluation_at = as_of
    result.reasons = []
    first_start = position.opened_at.replace(second=0, microsecond=0)
    if first_start < position.opened_at:
        first_start += timedelta(minutes=1)
        result.reasons.append("The entry's partial minute is excluded; excursions/exits in that minute are unknowable.")
        partial = [row for row in observations if row.asset_id == position.request.asset_id
            and row.interval_start_timestamp == first_start - timedelta(minutes=1)
            and row.resolution_seconds == 60 and row.market_event_timestamp <= as_of
            and row.data_received_timestamp <= as_of]
        trade = position.request.trade
        safe_partial = bool(len(partial) == 1 and partial[0].data_quality == MacroQuality.AVAILABLE
            and partial[0].high is not None and partial[0].low is not None
            and partial[0].low > min(trade.stop, trade.target)
            and partial[0].high < max(trade.stop, trade.target))
        if not safe_partial:
            result.status = "CENSORED" if as_of >= first_start else "OPEN"
            result.coverage_status = "PARTIAL_ENTRY_INTERVAL_UNKNOWN"
            result.excursion_status = "UNAVAILABLE"
            result.reasons.append("Entry was intraminute: missing or level-crossing entry bar cannot establish that no earlier exit occurred.")
            return result
        partial_source = partial[0].source
    else:
        partial_source = None
    rows = sorted([
        row for row in observations
        if row.asset_id == position.request.asset_id
        and row.resolution_seconds == 60
        and row.interval_start_timestamp is not None
        and row.interval_start_timestamp >= first_start
        and row.market_event_timestamp <= as_of
        and row.data_received_timestamp <= as_of
    ], key=lambda row: (row.interval_start_timestamp, row.data_received_timestamp))
    result.status = "OPEN"
    result.mfe_return = result.mae_return = None
    result.samples = 0
    result.source = None
    result.last_market_timestamp = None
    result.coverage_status = "PENDING"
    result.excursion_status = "UNAVAILABLE"
    if not rows:
        if as_of >= first_start + timedelta(minutes=1):
            result.status = "CENSORED"
            result.coverage_status = "MISSING_POST_ENTRY_HISTORY"
            result.reasons.append("No fully received completed post-entry 1m candles.")
        return result
    expected = first_start
    source = rows[0].source
    if partial_source is not None and partial_source != source:
        result.status = "CENSORED"
        result.coverage_status = "MIXED_ENTRY_SOURCE"
        result.reasons.append("Entry partial minute and subsequent intervals have different sources.")
        return result
    direction = 1 if position.request.trade.side == "LONG" else -1
    trade = position.request.trade
    mfe = mae = 0.0
    for index, row in enumerate(rows):
        if row.interval_start_timestamp != expected or row.source != source or row.data_quality != MacroQuality.AVAILABLE or row.high is None or row.low is None:
            result.status = "CENSORED"
            result.coverage_status = "INCOMPLETE_PATH"
            result.reasons.append("Gap, duplicate, mixed-source, unavailable or missing-extrema interval; automatic exit is censored.")
            break
        # Reject duplicate intervals before any automatic exit decision.
        if index + 1 < len(rows) and rows[index + 1].interval_start_timestamp == row.interval_start_timestamp:
            result.status = "CENSORED"
            result.coverage_status = "DUPLICATE_INTERVAL"
            result.reasons.append("Duplicate 1m interval; no preferred revision is invented.")
            break
        stop_hit = row.low <= trade.stop if direction == 1 else row.high >= trade.stop
        target_hit = row.high >= trade.target if direction == 1 else row.low <= trade.target
        result.samples += 1
        result.source = source
        result.last_market_timestamp = row.market_event_timestamp
        if stop_hit and target_hit:
            result.status = "AMBIGUOUS"
            result.coverage_status = "STOP_TARGET_ORDER_UNKNOWN"
            result.exit_interval_start = row.interval_start_timestamp
            result.exit_interval_end = row.market_event_timestamp
            result.reasons.append("Both stop and primary target were hit in the same 1m bar. No winner or loser is assigned.")
            break
        if stop_hit or target_hit:
            exit_price = trade.stop if stop_hit else trade.target
            excursion = direction * (exit_price / trade.entry - 1)
            mfe, mae = max(mfe, excursion), min(mae, excursion)
            result.status = "CLOSED"
            result.closed_at = row.market_event_timestamp
            result.exit_interval_start = row.interval_start_timestamp
            result.exit_interval_end = row.market_event_timestamp
            result.exit_kind = "STOP" if stop_hit else "TARGET"
            result.exit_source = "PAPER_MODEL_LEVEL_FILL"
            result.financial_result = _financial(result, exit_price, trade.funding_cost, trade.funding_source)
            result.coverage_status = "COMPLETE_TO_MODELED_EXIT"
            result.excursion_status = "LOWER_BOUND_EXIT_BAR_ORDER_UNKNOWN"
            result.reasons.append("Exit time is a 1m interval, not an observed tick; excursions after the modeled exit are excluded.")
            break
        favorable = row.high if direction == 1 else row.low
        adverse = row.low if direction == 1 else row.high
        mfe = max(mfe, direction * (favorable / trade.entry - 1))
        mae = min(mae, direction * (adverse / trade.entry - 1))
        expected = row.market_event_timestamp
    if result.samples:
        result.mfe_return, result.mae_return = mfe, mae
        if result.excursion_status == "UNAVAILABLE":
            result.excursion_status = "KNOWN_COMPLETED_PREFIX_ONLY" if result.status in {"CENSORED", "AMBIGUOUS"} else "COMPLETED_INTERVAL_EXTREMA_EXCLUDES_PARTIAL_ENTRY" if partial_source else "COMPLETED_INTERVAL_EXTREMA"
    if result.status == "OPEN":
        result.coverage_status = "COMPLETE_TO_LAST_RECEIVED_INTERVAL"
        # Do not call a truncated archive a complete live path.
        if as_of - rows[-1].market_event_timestamp >= timedelta(minutes=1):
            result.status = "CENSORED"
            result.coverage_status = "MISSING_TAIL"
            result.reasons.append("Completed post-entry intervals are missing at the tail.")
    return PaperPosition.model_validate(result.model_dump())


class PaperJournal:
    """Uses the existing operational SQLite connection and additive tables."""

    def __init__(self, store: IntelligenceStore):
        self.store = store
        with store._archive_lock:
            if not getattr(store, "_paper_journal_schema_installed", False):
                store._db.executescript("BEGIN IMMEDIATE;" + _SCHEMA)
                store._db.execute("INSERT OR IGNORE INTO paper_journal_migrations VALUES(1,?)", (utcnow().isoformat(),))
                store._db.commit()
                store._paper_journal_schema_installed = True

    def get(self, position_id: str) -> PaperPosition | None:
        with self.store._archive_lock:
            row = self.store._db.execute("SELECT payload FROM paper_positions WHERE position_id=?", (position_id,)).fetchone()
            return PaperPosition.model_validate_json(row[0]) if row else None

    def list(self, *, status: str | None = None, limit: int = 1000) -> list[PaperPosition]:
        with self.store._archive_lock:
            query = "SELECT payload FROM paper_positions"
            args: list = []
            if status is not None:
                query += " WHERE status=?"
                args.append(status)
            query += " ORDER BY opened_at DESC,position_id LIMIT ?"
            args.append(max(0, min(limit, 10000)))
            return [PaperPosition.model_validate_json(row[0]) for row in self.store._db.execute(query, args)]

    def _event(self, position: PaperPosition, kind: str, at: datetime, detail: dict):
        # Entry evidence is immutable and stored once in OPENED. Subsequent
        # progress records are deltas, not repeated large chart/state snapshots.
        excluded = {"request", "feature_snapshot", "assumptions"} if kind == "EVALUATED" else set()
        payload = {"position": position.model_dump(mode="json", exclude=excluded), "detail": detail}
        if excluded:
            payload["immutable_entry_reference"] = position.position_id
        body = self.store._json(payload)
        event_id = hashlib.sha256((position.position_id + kind + at.isoformat() + body).encode()).hexdigest()
        self.store._db.execute("INSERT OR IGNORE INTO paper_journal_events VALUES(?,?,?,?,?,?)",
                               (event_id, position.position_id, at.isoformat(), position.updated_at.isoformat(), kind, body))

    def _save(self, position: PaperPosition, kind: str, at: datetime, detail: dict):
        prior = self.get(position.position_id)
        self.store._db.execute("UPDATE paper_positions SET updated_at=?,status=?,payload=? WHERE position_id=?",
                               (position.updated_at.isoformat(), position.status, position.model_dump_json(), position.position_id))
        if kind != "EVALUATED" or prior is None or prior.model_dump(exclude={"updated_at", "last_evaluation_at"}) != position.model_dump(exclude={"updated_at", "last_evaluation_at"}):
            self._event(position, kind, at, detail)

    def open(self, request: PaperOpenRequest, *, at: datetime | None = None) -> PaperPosition:
        request = PaperOpenRequest.model_validate(request.model_dump())
        at = aware(at or utcnow())
        with self.store._archive_lock, self.store._db:
            prior = self.store._db.execute("SELECT payload FROM paper_positions WHERE request_id=?", (request.request_id,)).fetchone()
            if prior:
                position = PaperPosition.model_validate_json(prior[0])
                if position.request != request:
                    raise ValueError("paper request ID collision; original entry is immutable")
                return position
            known_fast = self.store._db.execute("SELECT payload FROM runtime_states WHERE kind=? AND object_id=? AND updated_at<=?",
                ("fast_state", request.asset_id, at.isoformat())).fetchone()
            fast = json.loads(known_fast[0]) if known_fast else None
            native_symbol = fast.get("asset", {}).get("exchange_symbol") if fast else None
            if native_symbol and native_symbol != request.symbol:
                raise ValueError("paper asset ID and symbol disagree with the known public-market instrument")
            calculation = calculate_trade(request.trade)
            snapshot = entry_snapshot(self.store, request.symbol, at)
            regime = snapshot.get("payload", {}).get("market_regime") if snapshot["status"] == "AVAILABLE" else None
            if not isinstance(regime, str) or regime in {"UNAVAILABLE", "UNKNOWN"}:
                regime = None
            position = PaperPosition(
                position_id=hashlib.sha256(("paper-journal-v1:" + request.request_id).encode()).hexdigest()[:32],
                request=request,
                opened_at=at,
                updated_at=at,
                position_size=calculation.position_size,
                notional=calculation.notional,
                initial_net_risk=calculation.loss_at_stop,
                feature_snapshot=snapshot,
                regime=regime,
                risk_warnings=list(calculation.warnings),
            )
            self.store._db.execute("INSERT INTO paper_positions VALUES(?,?,?,?,?,?,?,?)", (
                position.position_id, request.request_id, request.asset_id, request.symbol,
                at.isoformat(), at.isoformat(), position.status, position.model_dump_json()))
            self._event(position, "OPENED", at, {"entry_source": position.entry_source})
            return position

    def close(self, position_id: str, request: PaperCloseRequest, *, at: datetime | None = None) -> PaperPosition:
        at = aware(at or utcnow())
        with self.store._archive_lock, self.store._db:
            position = self.get(position_id)
            if position is None:
                raise KeyError("paper position not found")
            if position.status in {"CLOSED", "AMBIGUOUS"}:
                raise ValueError("closed/ambiguous paper position cannot be rewritten")
            if at < position.updated_at:
                raise ValueError("manual close cannot precede current position state")
            funding = request.funding_cost if "funding_cost" in request.model_fields_set else position.request.trade.funding_cost
            funding_source = request.funding_source if "funding_cost" in request.model_fields_set else position.request.trade.funding_source
            position.status = "CLOSED"
            position.updated_at = position.closed_at = at
            position.exit_kind = "USER_DECLARED"
            position.exit_source = "USER_DECLARED_NOT_EXECUTED"
            position.financial_result = _financial(position, request.exit_price, funding, funding_source)
            position.reasons.append("Manual close price and reason are user declarations, not observed exchange fills: " + request.reason)
            if position.coverage_status != "COMPLETE_TO_LAST_RECEIVED_INTERVAL":
                position.reasons.append("MFE/MAE remain unavailable or partial; manual closing does not repair missing market history.")
            self._save(position, "MANUALLY_CLOSED", at, request.model_dump(mode="json"))
            return position

    def history(self, position_id: str, *, as_of: datetime | None = None) -> list[dict]:
        cutoff = aware(as_of or utcnow()).isoformat()
        with self.store._archive_lock:
            rows = self.store._db.execute("SELECT kind,event_at,available_at,payload FROM paper_journal_events "
                "WHERE position_id=? AND available_at<=? ORDER BY available_at,event_id", (position_id, cutoff))
            return [{"kind": row[0], "event_at": row[1], "available_at": row[2], **json.loads(row[3])} for row in rows]

    def evaluate(self, as_of: datetime, *, maximum_positions: int = 100) -> dict:
        """Bounded background work over archived bars; never called per tick."""
        as_of = aware(as_of)
        counts = {"evaluated": 0, "closed": 0, "ambiguous": 0, "censored": 0, "concurrent_updates_skipped": 0}
        with self.store._archive_lock:
            positions = [PaperPosition.model_validate_json(row[0]) for row in self.store._db.execute(
                "SELECT payload FROM paper_positions WHERE status IN ('OPEN','CENSORED') AND opened_at<=? "
                "ORDER BY updated_at,position_id LIMIT ?", (as_of.isoformat(), max(0, min(maximum_positions, 1000))))]
        # Historical reads/computation occur before the short writer transaction.
        # Do not hold SQLite's single-writer lock while processing thousands of
        # bars; native market/event ingestion must retain access to the database.
        history = {}
        prepared = []
        for position in positions:
            if position.updated_at > as_of:
                continue
            asset_id = position.request.asset_id
            if asset_id not in history:
                history[asset_id] = [MacroMarketObservation.model_validate(row) for row in self.store.objects(
                    "market_observation", as_of, asset_id=asset_id, limit=10000)]
            prepared.append((position, evaluate_position(position, history[asset_id], as_of=as_of)))
        with self.store._archive_lock, self.store._db:
            for position, updated in prepared:
                # A manual close or another evaluator may have advanced while
                # history was being read. Never overwrite its newer result.
                current = self.get(position.position_id)
                if current != position:
                    counts["concurrent_updates_skipped"] += 1
                    continue
                self._save(updated, "EVALUATED", as_of, {"coverage_status": updated.coverage_status})
                counts["evaluated"] += 1
                if updated.status in {"CLOSED", "AMBIGUOUS", "CENSORED"}:
                    counts[updated.status.lower()] += 1
        return {**counts, "as_of": as_of.isoformat(), "coverage_scope": "ARCHIVED_NATIVE_COMPLETED_1M_ONLY", "live_orders": False}

    def portfolio(self, *, quote_currency: str | None = None) -> PortfolioResult:
        positions = self.list(limit=10000)
        positions = [position for position in positions if position.status != "CLOSED"
            and (quote_currency is None or position.request.quote_currency == quote_currency)]
        if len({position.request.quote_currency for position in positions}) > 1:
            raise ValueError("MIXED_QUOTE_CURRENCIES: select one quote_currency; exposure cannot be summed across currencies")
        inputs = []
        for position in positions:
            if position.status != "CLOSED":
                trade = position.request.trade
                inputs.append(PortfolioPosition(symbol=position.request.symbol, side=trade.side,
                    entry=trade.entry, quantity=position.position_size, stop=trade.stop,
                    sector=position.request.sector, entry_fee_rate=trade.entry_fee_rate,
                    exit_fee_rate=trade.exit_fee_rate, funding_cost=trade.funding_cost))
        if len(inputs) > 500:
            raise ValueError("portfolio exceeds the supported 500-position calculation budget")
        result = calculate_portfolio(PortfolioInput(positions=inputs))
        result.assumptions.append("Declared paper-entry exposure; not marked-to-market exchange exposure. CENSORED/AMBIGUOUS positions remain unresolved.")
        return result

    def analytics(self, *, initial_balance: float | None = None, quote_currency: str | None = None) -> dict:
        if initial_balance is not None and (initial_balance <= 0 or not math.isfinite(initial_balance)):
            raise ValueError("initial paper balance must be positive")
        positions = self.list(limit=10000)
        if quote_currency is not None:
            positions = [position for position in positions if position.request.quote_currency == quote_currency]
        currencies = sorted({position.request.quote_currency for position in positions})
        closed = [position for position in positions if position.status == "CLOSED"]
        known = [position for position in closed if position.financial_result and position.financial_result.net_pnl is not None]
        mixed = len(currencies) > 1
        totals = _statistics(known, initial_balance) if not mixed else _statistics([], None)
        groups = {}
        for label, key in (("setup", lambda p: p.request.setup), ("regime", lambda p: p.regime or "UNAVAILABLE"),
                           ("timeframe", lambda p: p.request.timeframe), ("quote_currency", lambda p: p.request.quote_currency)):
            grouped = {}
            for value in sorted({key(position) for position in positions}):
                group = [position for position in known if key(position) == value]
                if len({position.request.quote_currency for position in group}) > 1:
                    grouped[value] = {"sample_size": len(group), "status": "MIXED_QUOTE_CURRENCIES"}
                else:
                    grouped[value] = _statistics(group, None)
            groups[label] = grouped
        return {
            "mode": "PAPER_ONLY", "status": "MIXED_QUOTE_CURRENCIES" if mixed else "DESCRIPTIVE_PAPER_SAMPLE" if known else "INSUFFICIENT_DATA",
            "positions": len(positions), "closed_positions": len(closed), "known_cost_results": len(known),
            "unknown_cost_results": len(closed) - len(known),
            "open_positions": sum(position.status == "OPEN" for position in positions),
            "censored_positions": sum(position.status == "CENSORED" for position in positions),
            "ambiguous_positions": sum(position.status == "AMBIGUOUS" for position in positions),
            "quote_currencies": currencies, "initial_balance": initial_balance,
            **totals, "groups": groups, "assumptions": list(PAPER_ASSUMPTIONS) + [
                "Descriptive recorded paper results; sample size is not evidence of an independent statistical edge.",
                "Pending, ambiguous and unknown-net results are excluded from winrate/expectancy.",
                "Drawdown is realized closed-trade drawdown; intratrade/unrealized portfolio drawdown is unavailable.",
            ],
        }


def _statistics(positions: list[PaperPosition], initial_balance: float | None) -> dict:
    positions = sorted(positions, key=lambda position: (position.closed_at, position.position_id))
    values = [position.financial_result.net_pnl for position in positions]
    rs = [position.financial_result.r_multiple for position in positions if position.financial_result.r_multiple is not None]
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    equity = peak = initial_balance or 0.0
    max_drawdown = 0.0
    max_drawdown_percent = 0.0 if initial_balance else None
    curve = []
    simultaneous = {}
    for position, value in zip(positions, values, strict=True):
        simultaneous.setdefault(position.closed_at, []).append((position, value))
    for timestamp, results in simultaneous.items():
        equity += sum(value for _, value in results)
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)
        if initial_balance is not None and peak > 0:
            max_drawdown_percent = max(max_drawdown_percent, 100 * drawdown / peak)
        curve.append({"closed_at": timestamp.isoformat(), "position_count": len(results),
                      "cumulative_net_pnl": equity - (initial_balance or 0),
                      "equity": equity if initial_balance is not None else None})
    mfes = [position.mfe_return for position in positions if position.mfe_return is not None]
    maes = [position.mae_return for position in positions if position.mae_return is not None]
    return {
        "sample_size": len(values), "winrate": sum(value > 0 for value in values) / len(values) if values else None,
        "expectancy_net_pnl": mean(values) if values else None,
        "realized_net_pnl": sum(values) if values else None,
        "profit_factor": gains / losses if losses else None,
        "profit_factor_status": "CALCULATED" if losses else "UNAVAILABLE_NO_RECORDED_LOSSES" if values else "INSUFFICIENT_DATA",
        "r_sample_size": len(rs), "average_r": mean(rs) if rs else None, "median_r": median(rs) if rs else None,
        "mfe_sample_size": len(mfes), "mae_sample_size": len(maes),
        "mean_mfe_return": mean(mfes) if mfes else None, "mean_mae_return": mean(maes) if maes else None,
        "excursion_qualities": dict(Counter(position.excursion_status for position in positions)),
        "excursion_definition": "Recorded interval bounds; not exact whole-trade tick MFE/MAE.",
        "excursion_note": "Exit-bar extrema are lower bounds; partial/censored manual histories remain labeled on each position.",
        "maximum_realized_drawdown": max_drawdown if values else None,
        "maximum_realized_drawdown_percent": max_drawdown_percent if values else None,
        "realized_curve": curve,
    }
