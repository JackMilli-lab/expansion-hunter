"""Additive SQLite migrations and immutable intelligence/event vintages.

The operational database is shared with legacy/discovery reports. No migrations
drop tables or rewrite prior evidence. Published research objects are immutable;
time-sensitive runtime state is separate and is restored as stale by consumers.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from hashlib import sha256

from .discovery_store import DiscoveryStore, _locked
from .models import aware, utcnow

_MIGRATION = """
CREATE TABLE IF NOT EXISTS intelligence_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intelligence_objects(
 kind TEXT NOT NULL, object_id TEXT NOT NULL, event_at TEXT NOT NULL,
 available_at TEXT NOT NULL, asset_id TEXT, payload TEXT NOT NULL,
 PRIMARY KEY(kind,object_id));
CREATE INDEX IF NOT EXISTS intelligence_asof ON intelligence_objects(kind,available_at);
CREATE INDEX IF NOT EXISTS intelligence_asset ON intelligence_objects(kind,asset_id,event_at);
CREATE TABLE IF NOT EXISTS signal_contexts(
 event_id TEXT PRIMARY KEY, available_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runtime_states(
 kind TEXT NOT NULL, object_id TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(kind,object_id));
CREATE TABLE IF NOT EXISTS personal_watchlist(
 asset_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, priority INTEGER NOT NULL,
 pinned INTEGER NOT NULL, notes TEXT NOT NULL, added_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 removed_at TEXT);
CREATE TABLE IF NOT EXISTS notification_outbox(
 event_id TEXT NOT NULL, channel TEXT NOT NULL, created_at TEXT NOT NULL,
 delivered_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
 PRIMARY KEY(event_id,channel));
"""


class IntelligenceStore(DiscoveryStore):
    @_locked
    def close(self):
        """Serialize shutdown with owned background SQLite work."""
        super().close()

    def __init__(self, path: str):
        super().__init__(path)
        with self._archive_lock:
            self._db.executescript("BEGIN IMMEDIATE;" + _MIGRATION)
            self._db.execute("INSERT OR IGNORE INTO intelligence_migrations VALUES(1,?)", (utcnow().isoformat(),))
            self._db.commit()
            # Existing outbox evidence is preserved; retry/delivery metadata is
            # additive so a restart cannot replay an already delivered event.
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                columns = {row[1] for row in self._db.execute("PRAGMA table_info(notification_outbox)")}
                for name, definition in {
                    "status": "TEXT NOT NULL DEFAULT 'QUEUED'", "last_attempt_at": "TEXT",
                    "next_attempt_at": "TEXT", "notification_latency_ms": "REAL",
                }.items():
                    if name not in columns:
                        self._db.execute(f"ALTER TABLE notification_outbox ADD COLUMN {name} {definition}")
                self._db.execute("UPDATE notification_outbox SET status='DELIVERED' "
                                 "WHERE delivered_at IS NOT NULL AND status!='DELIVERED'")
                self._db.execute("CREATE INDEX IF NOT EXISTS notification_pending "
                                 "ON notification_outbox(status,next_attempt_at,created_at)")
                self._db.execute("INSERT OR IGNORE INTO intelligence_migrations VALUES(2,?)", (utcnow().isoformat(),))

    @staticmethod
    def _json(payload) -> str:
        if hasattr(payload, "model_dump"):
            payload = payload.model_dump(mode="json")
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
                          default=lambda value: value.model_dump(mode="json"))

    @_locked
    def append(self, kind: str, object_id: str, event_at: datetime, available_at: datetime,
               payload, asset_id: str | None = None) -> bool:
        event_at, available_at = aware(event_at), aware(available_at)
        if available_at < event_at:
            raise ValueError("availability precedes event")
        row = (kind, object_id, event_at.isoformat(), available_at.isoformat(), asset_id, self._json(payload))
        with self._db:
            old = self._db.execute("SELECT * FROM intelligence_objects WHERE kind=? AND object_id=?",
                                   (kind, object_id)).fetchone()
            if old:
                if old != row:
                    raise ValueError("immutable intelligence object ID collision")
                return False
            self._db.execute("INSERT INTO intelligence_objects VALUES(?,?,?,?,?,?)", row)
        return True

    @_locked
    def object(self, kind: str, object_id: str, as_of: datetime | None = None) -> dict | None:
        """Indexed immutable lookup without truncating long-lived dedup archives."""
        cutoff = aware(as_of or utcnow()).isoformat()
        row = self._db.execute(
            "SELECT payload FROM intelligence_objects WHERE kind=? AND object_id=? "
            "AND event_at<=? AND available_at<=?", (kind, object_id, cutoff, cutoff),
        ).fetchone()
        return json.loads(row[0]) if row else None

    @_locked
    def objects(self, kind: str, as_of: datetime | None = None, *, asset_id: str | None = None,
                limit: int = 1000) -> list[dict]:
        cutoff = aware(as_of or utcnow()).isoformat()
        sql = "SELECT payload FROM intelligence_objects WHERE kind=? AND available_at<=?"
        args: list = [kind, cutoff]
        if asset_id is not None:
            sql += " AND asset_id=?"
            args.append(asset_id)
        sql += " ORDER BY available_at DESC,object_id DESC LIMIT ?"
        args.append(max(0, min(limit, 10000)))
        return [json.loads(row[0]) for row in reversed(self._db.execute(sql, args).fetchall())]

    @_locked
    def append_many(self, records: list[tuple]) -> list[bool]:
        """Atomic immutable batch, including collision checks and publication times."""
        rows = []
        keys = {}
        for kind, identity, event_at, available_at, payload, asset_id in records:
            event_at, available_at = aware(event_at), aware(available_at)
            if available_at < event_at:
                raise ValueError("availability precedes event")
            row = (kind, identity, event_at.isoformat(), available_at.isoformat(), asset_id, self._json(payload))
            if (kind, identity) in keys and keys[kind, identity] != row:
                raise ValueError("immutable batch object ID collision")
            keys[kind, identity] = row
            rows.append(row)
        inserted = []
        with self._db:
            for row in rows:
                old = self._db.execute("SELECT * FROM intelligence_objects WHERE kind=? AND object_id=?", row[:2]).fetchone()
                if old and old != row:
                    raise ValueError("immutable intelligence object ID collision")
                if not old:
                    self._db.execute("INSERT INTO intelligence_objects VALUES(?,?,?,?,?,?)", row)
                inserted.append(not bool(old))
        return inserted

    @_locked
    def record_context(self, event_id: str, available_at: datetime, context: dict):
        row = (event_id, aware(available_at).isoformat(), self._json(context))
        with self._db:
            existing = self._db.execute("SELECT * FROM signal_contexts WHERE event_id=?", (event_id,)).fetchone()
            if existing:
                if existing != row:
                    raise ValueError("immutable signal context ID collision")
            else:
                self._db.execute("INSERT INTO signal_contexts VALUES(?,?,?)", row)

    @_locked
    def signal_context(self, event_id: str, as_of: datetime | None = None):
        row = self._db.execute("SELECT payload FROM signal_contexts WHERE event_id=? AND available_at<=?",
                               (event_id, aware(as_of or utcnow()).isoformat())).fetchone()
        return json.loads(row[0]) if row else None

    @_locked
    def put_state(self, kind: str, object_id: str, updated_at: datetime, payload):
        """Operational caches may advance; archival features never use this table."""
        when = aware(updated_at).isoformat()
        with self._db:
            prior = self._db.execute("SELECT updated_at FROM runtime_states WHERE kind=? AND object_id=?",
                                     (kind, object_id)).fetchone()
            if prior and prior[0] > when:
                raise ValueError("runtime state cannot move backward")
            self._db.execute("INSERT INTO runtime_states VALUES(?,?,?,?) ON CONFLICT(kind,object_id) "
                             "DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload",
                             (kind, object_id, when, self._json(payload)))

    @_locked
    def states(self, kind: str) -> list[dict]:
        return [json.loads(row[0]) for row in self._db.execute(
            "SELECT payload FROM runtime_states WHERE kind=? ORDER BY object_id", (kind,))]

    @_locked
    def get_state(self, kind: str, object_id: str) -> dict | None:
        """Read one current cache via its primary key, without unrelated histories."""
        row = self._db.execute(
            "SELECT payload FROM runtime_states WHERE kind=? AND object_id=?",
            (kind, object_id),
        ).fetchone()
        return json.loads(row[0]) if row else None

    @_locked
    def states_projected(self, kind: str, fields: tuple[str, ...]) -> list[dict]:
        """Project bounded top-level fields before transferring/decoding JSON.

        Candle/chart caches remain available through ``get_state``. Missing
        projected fields are explicit nulls, never guessed values. Identifiers
        are restricted to plain field names and passed as bound SQL parameters.
        """
        if not fields or len(fields) > 50 or any(
            not isinstance(field, str) or not field.isidentifier() or not field.isascii()
            for field in fields
        ) or len(fields) != len(set(fields)):
            raise ValueError("projection requires 1-50 unique plain top-level field names")
        expressions = ",".join("?,json_extract(payload,?)" for _ in fields)
        args = [value for field in fields for value in (field, "$." + field)]
        args.append(kind)
        sql = f"SELECT json_object({expressions}) FROM runtime_states WHERE kind=? ORDER BY object_id"
        return [json.loads(row[0]) for row in self._db.execute(sql, args)]

    @_locked
    def live_overview_records(self) -> list[dict]:
        """Read small published summaries, with bounded legacy compatibility.

        Existing full chart caches are preserved. A legacy overview exposes at
        most eight recorded liquidity objects per asset, without transferring
        candles, overlays or every historical zone to each table refresh.
        """
        summaries = self.states("live_asset_summary")
        if summaries:
            return summaries
        fields = ("asset_id", "symbol", "price", "price_quality", "data_quality", "as_of",
                  "received_timestamp", "last_exchange_timestamp", "last_received_timestamp",
                  "price_source", "price_source_timestamp", "price_received_timestamp", "quote_asset")
        expressions = ",".join("?,json_extract(payload,?)" for _ in fields)
        args = [value for field in fields for value in (field, "$." + field)]
        sql = (
            f"SELECT json_object({expressions},'liquidity_map',json(("
            "SELECT json_group_array(json(value)) FROM "
            "(SELECT value FROM json_each(runtime_states.payload,'$.liquidity_map') LIMIT 8)))) "
            "FROM runtime_states WHERE kind='live_asset' ORDER BY object_id"
        )
        records = [json.loads(row[0]) for row in self._db.execute(sql, args)]
        for record in records:
            record["liquidity_map_scope"] = "FIRST_MAX8_LEGACY_OVERVIEW; full selected chart unchanged"
        return records

    @_locked
    def watchlist_add(self, asset_id: str, symbol: str, *, priority: int = 0, pinned: bool = False,
                      notes: str = "", at: datetime | None = None):
        if not asset_id or not symbol or not 0 <= priority <= 100 or len(notes) > 2000:
            raise ValueError("invalid watchlist fields")
        when = aware(at or utcnow()).isoformat()
        with self._db:
            self._db.execute("INSERT INTO personal_watchlist VALUES(?,?,?,?,?,?,?,NULL) ON CONFLICT(asset_id) "
                             "DO UPDATE SET symbol=excluded.symbol,priority=excluded.priority,pinned=excluded.pinned,"
                             "notes=excluded.notes,updated_at=excluded.updated_at,removed_at=NULL",
                             (asset_id, symbol, priority, int(pinned), notes, when, when))

    @_locked
    def watchlist_remove(self, asset_id: str, at: datetime | None = None):
        when = aware(at or utcnow()).isoformat()
        with self._db:
            self._db.execute("UPDATE personal_watchlist SET removed_at=?,updated_at=? WHERE asset_id=?",
                             (when, when, asset_id))

    @_locked
    def watchlist(self) -> list[dict]:
        columns = ("asset_id", "symbol", "priority", "pinned", "notes", "added_at", "updated_at")
        return [dict(zip(columns, row, strict=True)) for row in self._db.execute(
            "SELECT asset_id,symbol,priority,pinned,notes,added_at,updated_at FROM personal_watchlist "
            "WHERE removed_at IS NULL ORDER BY pinned DESC,priority DESC,added_at")]

    @_locked
    def enqueue_notification(self, event_id: str, channel: str, created_at: datetime):
        with self._db:
            self._db.execute("INSERT OR IGNORE INTO notification_outbox(event_id,channel,created_at) VALUES(?,?,?)",
                             (event_id, channel, aware(created_at).isoformat()))

    @_locked
    def notification_result(self, event_id: str, channel: str, at: datetime, error: str | None = None,
                            *, next_attempt_at: datetime | None = None, terminal: bool = False,
                            latency_ms: float | None = None):
        when = aware(at)
        retry = aware(next_attempt_at) if next_attempt_at else None
        if retry and retry < when:
            raise ValueError("notification retry precedes attempt")
        status = "FAILED" if error and terminal else "RETRYING" if error else "DELIVERED"
        with self._db:
            self._db.execute("UPDATE notification_outbox SET attempts=attempts+1,delivered_at=?,last_error=?,"
                             "status=?,last_attempt_at=?,next_attempt_at=?,notification_latency_ms=? "
                             "WHERE event_id=? AND channel=? AND delivered_at IS NULL",
                             (None if error else when.isoformat(), error, status, when.isoformat(),
                              retry.isoformat() if retry and error and not terminal else None,
                              latency_ms if not error else None, event_id, channel))

    @_locked
    def pending_notifications(self, as_of: datetime, *, limit: int = 5, max_attempts: int = 5) -> list[dict]:
        if not 1 <= limit <= 100 or not 1 <= max_attempts <= 100:
            raise ValueError("invalid notification bounds")
        cutoff = aware(as_of).isoformat()
        columns = ("event_id", "channel", "created_at", "attempts", "last_error", "next_attempt_at")
        return [dict(zip(columns, row, strict=True)) for row in self._db.execute(
            "SELECT event_id,channel,created_at,attempts,last_error,next_attempt_at FROM notification_outbox "
            "WHERE delivered_at IS NULL AND status IN ('QUEUED','RETRYING') AND created_at<=? "
            "AND attempts<? AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
            "ORDER BY created_at DESC,event_id,channel LIMIT ?", (cutoff, max_attempts, cutoff, limit))]

    @_locked
    def expire_notifications(self, as_of: datetime, *, max_age_seconds: float = 300, max_attempts: int = 5) -> dict:
        if max_age_seconds <= 0 or max_attempts < 1:
            raise ValueError("invalid notification expiry")
        cutoff = (aware(as_of) - timedelta(seconds=max_age_seconds)).isoformat()
        with self._db:
            stale = self._db.execute("UPDATE notification_outbox SET status='STALE',last_error='STALE_ALERT',"
                                     "next_attempt_at=NULL WHERE delivered_at IS NULL "
                                     "AND status IN ('QUEUED','RETRYING') AND created_at<?", (cutoff,)).rowcount
            exhausted = self._db.execute("UPDATE notification_outbox SET status='FAILED',"
                                         "last_error='RETRY_LIMIT',next_attempt_at=NULL WHERE delivered_at IS NULL "
                                         "AND status IN ('QUEUED','RETRYING') AND attempts>=?", (max_attempts,)).rowcount
        return {"stale": stale, "retry_exhausted": exhausted}

    @_locked
    def delivered_notification(self, event_id: str) -> str | None:
        row = self._db.execute("SELECT delivered_at FROM notification_outbox WHERE event_id=? "
                               "AND channel IN ('configured','telegram') AND delivered_at IS NOT NULL "
                               "ORDER BY delivered_at LIMIT 1", (event_id,)).fetchone()
        return row[0] if row else None

    @_locked
    def notification_counts(self) -> dict[str, int]:
        return {status: count for status, count in self._db.execute(
            "SELECT status,COUNT(*) FROM notification_outbox GROUP BY status")}

    def save_domain(self, kind: str, model, *, object_id: str | None = None,
                    event_at: datetime, available_at: datetime, asset_id: str | None = None):
        identity = object_id or sha256(self._json(model).encode()).hexdigest()
        return self.append(kind, identity, event_at, available_at, model, asset_id)
