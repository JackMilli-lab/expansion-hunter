"""Additive, immutable discovery archives in the operational SQLite database.

Each cycle is published atomically, including failed/excluded assets. Historical
queries use *completion* time: an earlier market timestamp does not make a scan
available before its actual retrieval and publication. Legacy evidence is copied
only through the explicit read-only import operation; source files are never
renamed, updated or removed.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import zlib
from datetime import datetime, timedelta
from functools import wraps
from hashlib import sha256
from pathlib import Path
from threading import RLock

from .discovery_models import DiscoveryCycle, DiscoveryEvent, DiscoveryObservation
from .models import aware
from .storage import Store


def _locked(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._archive_lock:
            return method(self, *args, **kwargs)
    return guarded


class DiscoveryStore(Store):
    def __init__(self, path: str):
        self._archive_lock = RLock()
        self._summary_cache: tuple[str, DiscoveryCycle] | None = None
        super().__init__(str(Path(path).expanduser().resolve()))
        self.database_path = self.path
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS discovery_cycles (
                cycle_id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                as_of TEXT NOT NULL, completed_at TEXT NOT NULL,
                status TEXT NOT NULL, config_hash TEXT NOT NULL,
                code_version TEXT NOT NULL, payload BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS discovery_cycle_completion
                ON discovery_cycles(completed_at);
            CREATE TABLE IF NOT EXISTS discovery_summaries (
                cycle_id TEXT PRIMARY KEY REFERENCES discovery_cycles(cycle_id),
                payload BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_observations (
                cycle_id TEXT NOT NULL REFERENCES discovery_cycles(cycle_id),
                asset_id TEXT NOT NULL, as_of TEXT NOT NULL,
                stage TEXT NOT NULL, disposition TEXT NOT NULL,
                rank REAL, payload BLOB NOT NULL,
                PRIMARY KEY(cycle_id, asset_id)
            );
            CREATE INDEX IF NOT EXISTS discovery_asset_time
                ON discovery_observations(asset_id, as_of);
            CREATE TABLE IF NOT EXISTS discovery_snapshot_objects (
                snapshot_id TEXT PRIMARY KEY, payload BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_snapshot_references (
                cycle_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL REFERENCES discovery_snapshot_objects(snapshot_id),
                PRIMARY KEY(cycle_id,asset_id),
                FOREIGN KEY(cycle_id,asset_id)
                    REFERENCES discovery_observations(cycle_id,asset_id)
            );
            CREATE TABLE IF NOT EXISTS discovery_transitions (
                cycle_id TEXT NOT NULL REFERENCES discovery_cycles(cycle_id),
                asset_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                previous_stage TEXT NOT NULL, stage TEXT NOT NULL,
                reason TEXT NOT NULL,
                PRIMARY KEY(cycle_id, asset_id)
            );
            CREATE TABLE IF NOT EXISTS discovery_events (
                event_id TEXT PRIMARY KEY,
                cycle_id TEXT NOT NULL REFERENCES discovery_cycles(cycle_id),
                asset_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS discovery_event_asset_time
                ON discovery_events(asset_id, timestamp);
            CREATE TABLE IF NOT EXISTS discovery_monitoring (
                cycle_id TEXT NOT NULL REFERENCES discovery_cycles(cycle_id),
                asset_id TEXT NOT NULL, stage TEXT NOT NULL,
                due_at TEXT, deep_scanned INTEGER NOT NULL,
                PRIMARY KEY(cycle_id, asset_id)
            );
        """)
        self._db.commit()

    @staticmethod
    def _encode(value) -> bytes:
        # Fixed gzip metadata makes identical observations stable across retries.
        return gzip.compress(value.model_dump_json().encode(), compresslevel=3, mtime=0)

    def _decode(self, payload: bytes, model):
        raw = gzip.decompress(payload)
        # Original archive blobs are never rewritten. Their existing fast
        # decoder remains valid alongside new content-addressed payloads.
        if b'"_archive_snapshot":' not in raw:
            return model.model_validate_json(raw)
        value = json.loads(raw)
        observations = value["observations"] if model is DiscoveryCycle else [value]
        for observation in observations:
            pointer = observation.get("snapshot")
            if isinstance(pointer, dict) and set(pointer) == {"_archive_snapshot"}:
                identity = pointer["_archive_snapshot"]
                row = self._db.execute(
                    "SELECT payload FROM discovery_snapshot_objects WHERE snapshot_id=?", (identity,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"archive snapshot object missing: {identity}")
                try:
                    snapshot = gzip.decompress(row[0])
                except (OSError, EOFError, zlib.error) as exc:
                    raise ValueError(f"archive snapshot object corrupt: {identity}") from exc
                if sha256(snapshot).hexdigest() != identity:
                    raise ValueError(f"archive snapshot object corrupt: {identity}")
                restored = json.loads(snapshot)
                if restored.get("asset", {}).get("asset_id") != observation["asset"]["asset_id"]:
                    raise ValueError(f"archive snapshot asset identity mismatch: {identity}")
                observation["snapshot"] = restored
        return model.model_validate(value)

    @staticmethod
    def _archive_payloads(cycle: DiscoveryCycle):
        """Normalize future writes; snapshots retain every original timestamp.

        SHA-256 identifies the exact serialized snapshot, not a price-only
        approximation. Changed receipt times, sources, unavailable metrics or
        candles therefore become separate immutable objects.
        """
        summary_raw = cycle.model_dump_json(
            exclude={"observations": {"__all__": {"snapshot"}}}
        ).encode()
        value = json.loads(summary_raw)
        objects, references, rows = {}, {}, {}
        for observation, payload in zip(cycle.observations, value["observations"], strict=True):
            asset_id = observation.asset.asset_id
            if observation.snapshot is not None:
                raw = observation.snapshot.model_dump_json().encode()
                identity = sha256(raw).hexdigest()
                objects[identity] = gzip.compress(raw, compresslevel=3, mtime=0)
                references[asset_id] = identity
                payload["snapshot"] = {"_archive_snapshot": identity}
            rows[asset_id] = gzip.compress(
                json.dumps(payload, separators=(",", ":"), allow_nan=False).encode(),
                compresslevel=3, mtime=0,
            )
        encoded = gzip.compress(
            json.dumps(value, separators=(",", ":"), allow_nan=False).encode(),
            compresslevel=3, mtime=0,
        )
        summary = gzip.compress(summary_raw, compresslevel=3, mtime=0)
        return encoded, summary, rows, objects, references

    @staticmethod
    def _encode_summary(cycle: DiscoveryCycle) -> bytes:
        """Keep decision evidence while excluding expensive archived candle histories."""
        payload = cycle.model_dump_json(
            exclude={"observations": {"__all__": {"snapshot"}}}
        ).encode()
        return gzip.compress(payload, compresslevel=3, mtime=0)

    def _read_summary(self, cycle_id: str, encoded: bytes | None = None) -> DiscoveryCycle:
        if encoded is not None:
            return self._decode(encoded, DiscoveryCycle)
        # Older immutable archives predate this additive table. Decode their JSON
        # once, before validating models, so thousands of candle models are never
        # constructed for membership/health requests. The source is not rewritten.
        row = self._db.execute(
            "SELECT payload FROM discovery_cycles WHERE cycle_id=?", (cycle_id,)
        ).fetchone()
        payload = json.loads(gzip.decompress(row[0]))
        for observation in payload["observations"]:
            observation["snapshot"] = None
        return DiscoveryCycle.model_validate(payload)

    @staticmethod
    def _validate_cycle(cycle: DiscoveryCycle):
        if not cycle.started_at <= cycle.as_of <= cycle.completed_at:
            raise ValueError("cycle requires started_at <= as_of <= completed_at")
        ids = [row.asset.asset_id for row in cycle.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("cycle contains duplicate asset observations")
        event_ids = [event.event_id for event in cycle.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("cycle contains duplicate event IDs")
        observations = {row.asset.asset_id: row for row in cycle.observations}
        for row in cycle.observations:
            if row.as_of > cycle.as_of:
                raise ValueError("observation is later than the cycle knowledge boundary")
            if any(value and value > row.as_of for value in (row.last_light_at, row.last_deep_at)):
                raise ValueError("refresh timestamp is later than its observation boundary")
            for feature in row.features.values():
                if feature.retrieved_at and feature.retrieved_at > row.as_of:
                    raise ValueError("feature retrieval is later than its observation boundary")
                if feature.source_timestamp and feature.source_timestamp > row.as_of:
                    raise ValueError("feature source timestamp is in the future")
            if row.snapshot and row.snapshot.as_of > row.as_of:
                raise ValueError("deep snapshot is later than its observation boundary")
        for event in cycle.events:
            if event.cycle_id != cycle.cycle_id or event.asset_id not in observations:
                raise ValueError("event must reference an observation in its own cycle")
            if event.timestamp > cycle.as_of:
                raise ValueError("event is later than the cycle knowledge boundary")

    @_locked
    def save_cycle(self, cycle: DiscoveryCycle):
        """Commit every disposition and event together, or leave no partial cycle.

        Repeating the exact same cycle is idempotent. Reusing an ID for changed
        evidence is an error, including after a deployment/retry.
        """
        self._validate_cycle(cycle)
        if str(Path(cycle.database_path).expanduser().resolve()) != self.database_path:
            raise ValueError("cycle database_path does not match the operational database")
        encoded, summary, rows, objects, references = self._archive_payloads(cycle)
        with self._db:
            existing = self._db.execute(
                "SELECT payload FROM discovery_cycles WHERE cycle_id=?", (cycle.cycle_id,)
            ).fetchone()
            if existing:
                if existing[0] != encoded and existing[0] != self._encode(cycle):
                    raise ValueError("immutable discovery cycle ID collision")
                if existing[0] == encoded:
                    for identity, payload in objects.items():
                        obj = self._db.execute(
                            "SELECT payload FROM discovery_snapshot_objects WHERE snapshot_id=?", (identity,)
                        ).fetchone()
                        if obj is None:
                            raise ValueError(f"archive snapshot object missing: {identity}")
                        if obj[0] != payload:
                            raise ValueError(f"archive snapshot object corrupt: {identity}")
                self._db.execute(
                    "INSERT OR IGNORE INTO discovery_summaries(cycle_id,payload) VALUES (?,?)",
                    (cycle.cycle_id, summary),
                )
                return
            self._db.execute(
                "INSERT INTO discovery_cycles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cycle.cycle_id, cycle.started_at.isoformat(), cycle.as_of.isoformat(),
                 cycle.completed_at.isoformat(), cycle.status, cycle.config_hash,
                 cycle.code_version, encoded),
            )
            self._db.execute(
                "INSERT INTO discovery_summaries(cycle_id,payload) VALUES (?,?)",
                (cycle.cycle_id, summary),
            )
            for identity, payload in objects.items():
                old = self._db.execute(
                    "SELECT payload FROM discovery_snapshot_objects WHERE snapshot_id=?", (identity,)
                ).fetchone()
                if old and old[0] != payload:
                    raise ValueError("immutable archive snapshot object collision")
                if not old:
                    self._db.execute(
                        "INSERT INTO discovery_snapshot_objects(snapshot_id,payload) VALUES (?,?)",
                        (identity, payload),
                    )
            for row in cycle.observations:
                asset_id = row.asset.asset_id
                self._db.execute(
                    "INSERT INTO discovery_observations VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (cycle.cycle_id, asset_id, row.as_of.isoformat(), row.stage,
                     row.disposition, row.rank.current_rank, rows[asset_id]),
                )
                if asset_id in references:
                    self._db.execute(
                        "INSERT INTO discovery_snapshot_references(cycle_id,asset_id,snapshot_id) VALUES (?,?,?)",
                        (cycle.cycle_id, asset_id, references[asset_id]),
                    )
                if row.previous_stage != row.stage:
                    self._db.execute(
                        "INSERT INTO discovery_transitions VALUES (?, ?, ?, ?, ?, ?)",
                        (cycle.cycle_id, asset_id, row.as_of.isoformat(), row.previous_stage,
                         row.stage, "; ".join(row.reasons) or "explicit stage change"),
                    )
                self._db.execute(
                    "INSERT INTO discovery_monitoring VALUES (?, ?, ?, ?, ?)",
                    (cycle.cycle_id, asset_id, row.stage,
                     row.monitoring_due_at.isoformat() if row.monitoring_due_at else None,
                     int(row.deep_scanned)),
                )
            for event in cycle.events:
                self._db.execute(
                    "INSERT INTO discovery_events VALUES (?, ?, ?, ?, ?, ?)",
                    (event.event_id, cycle.cycle_id, event.asset_id, event.timestamp.isoformat(),
                    event.event_type, event.model_dump_json()),
                )

    @_locked
    def latest_cycle_id(self) -> str | None:
        """Light publication key for consumers whose cached projection is unchanged."""
        row = self._db.execute(
            "SELECT cycle_id FROM discovery_cycles "
            "ORDER BY completed_at DESC,cycle_id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    @_locked
    def latest_summary(self) -> DiscoveryCycle | None:
        """Cached current decision evidence, without any OHLCV snapshots.

        The current immutable cycle ID is checked on every call, including when
        another process publishes a scan. Returned models are independent copies;
        a consumer cannot alter the cache or another consumer's evidence. Full
        histories remain available through ``latest_cycle`` and ``get_cycle``.
        """
        cycle_id = self.latest_cycle_id()
        if cycle_id is None:
            self._summary_cache = None
            return None
        if self._summary_cache is None or self._summary_cache[0] != cycle_id:
            summary = self._db.execute(
                "SELECT payload FROM discovery_summaries WHERE cycle_id=?", (cycle_id,)
            ).fetchone()
            self._summary_cache = (
                cycle_id, self._read_summary(cycle_id, summary[0] if summary else None)
            )
        return self._summary_cache[1].model_copy(deep=True)

    @_locked
    def latest_cycle(self) -> DiscoveryCycle | None:
        row = self._db.execute(
            "SELECT payload FROM discovery_cycles ORDER BY completed_at DESC, cycle_id DESC LIMIT 1"
        ).fetchone()
        return self._decode(row[0], DiscoveryCycle) if row else None

    @_locked
    def get_cycle(self, cycle_id: str) -> DiscoveryCycle | None:
        row = self._db.execute(
            "SELECT payload FROM discovery_cycles WHERE cycle_id=?", (cycle_id,)
        ).fetchone()
        return self._decode(row[0], DiscoveryCycle) if row else None

    @_locked
    def history(self, before: datetime, limit: int = 100) -> list[DiscoveryCycle]:
        if limit < 1:
            return []
        rows = self._db.execute(
            "SELECT payload FROM discovery_cycles WHERE completed_at<=? "
            "ORDER BY completed_at DESC, cycle_id DESC LIMIT ?", (aware(before).isoformat(), limit),
        ).fetchall()
        return [self._decode(row[0], DiscoveryCycle) for row in reversed(rows)]

    @_locked
    def ranking_history(self, before: datetime, settings=None) -> list[DiscoveryCycle]:
        """At most eight thin cycles for previous/acceleration and horizon ranks.

        Select the last two known cycles and the nearest *earlier* publication at
        15m/30m/1h/3h/6h/24h. Only one full archived payload is decoded at a time;
        deep candle histories are discarded before model validation. Full source
        evidence remains unchanged and available through ``get_cycle``/history.
        ``settings`` is accepted for integration compatibility; these horizons
        are the Phase 1 rank reporting contract, not tunable outcome labels.
        """
        cutoff = aware(before)
        candidates = {
            row[0]: row[1] for row in self._db.execute(
                "SELECT cycle_id, completed_at FROM discovery_cycles WHERE completed_at<=? "
                "ORDER BY completed_at DESC, cycle_id DESC LIMIT 2", (cutoff.isoformat(),)
            )
        }
        for seconds in (900, 1800, 3600, 10800, 21600, 86400):
            row = self._db.execute(
                "SELECT cycle_id, completed_at FROM discovery_cycles WHERE completed_at<=? "
                "ORDER BY completed_at DESC, cycle_id DESC LIMIT 1",
                ((cutoff - timedelta(seconds=seconds)).isoformat(),),
            ).fetchone()
            if row:
                candidates[row[0]] = row[1]
        result = []
        for identity, _ in sorted(candidates.items(), key=lambda item: (item[1], item[0])):
            row = self._db.execute(
                "SELECT payload FROM discovery_summaries WHERE cycle_id=?", (identity,)
            ).fetchone()
            result.append(self._read_summary(identity, row[0] if row else None))
        return result

    @_locked
    def asset_history(
        self, asset_id: str, before: datetime | None = None, limit: int = 100
    ) -> list[DiscoveryObservation]:
        if limit < 1:
            return []
        sql = (
            "SELECT o.payload FROM discovery_observations o "
            "JOIN discovery_cycles c USING(cycle_id) WHERE asset_id=?"
        )
        args: list = [asset_id]
        if before is not None:
            cutoff = aware(before).isoformat()
            sql += " AND o.as_of<=? AND c.completed_at<=?"
            args.extend([cutoff, cutoff])
        sql += " ORDER BY c.completed_at DESC, o.as_of DESC LIMIT ?"
        args.append(limit)
        rows = self._db.execute(sql, args).fetchall()
        return [self._decode(row[0], DiscoveryObservation) for row in reversed(rows)]

    @_locked
    def events(
        self, asset_id: str | None = None, limit: int = 100, before: datetime | None = None
    ) -> list[DiscoveryEvent]:
        if limit < 1:
            return []
        sql = "SELECT e.payload FROM discovery_events e JOIN discovery_cycles c USING(cycle_id) WHERE 1=1"
        args: list = []
        if asset_id is not None:
            sql += " AND e.asset_id=?"
            args.append(asset_id)
        if before is not None:
            sql += " AND c.completed_at<=?"
            args.append(aware(before).isoformat())
        sql += " ORDER BY e.timestamp DESC, e.event_id DESC LIMIT ?"
        args.append(limit)
        return [DiscoveryEvent.model_validate_json(row[0]) for row in self._db.execute(sql, args)]

    @_locked
    def import_legacy(self, source_path: str) -> dict[str, int]:
        """Copy old scan evidence without modifying or overwriting either archive.

        All matching IDs must have byte-for-byte matching rows. Any discrepancy
        rolls back the complete import, rather than silently replacing evidence.
        Source access is read-only and includes committed WAL data.
        """
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        counts = {"runs": 0, "observations": 0, "global_observations": 0, "already_present": 0}
        if str(source) == self.database_path:
            return counts
        specs = {
            "runs": ("scan_id, started_at, status, batch_json, error", (0,)),
            "observations": ("scan_id, asset_id, as_of, snapshot, result_json", (0, 1)),
            "global_observations": ("scan_id, as_of, payload", (0,)),
        }
        connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        try:
            connection.execute("BEGIN")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"runs", "observations"}.issubset(tables):
                raise ValueError("source is not a supported legacy scanner database")
            with self._db:
                for table, (columns, key_indices) in specs.items():
                    if table not in tables:
                        continue
                    names = columns.split(", ")
                    predicate = " AND ".join(f"{names[index]}=?" for index in key_indices)
                    for row in connection.execute(f"SELECT {columns} FROM {table}"):
                        key = tuple(row[index] for index in key_indices)
                        existing = self._db.execute(
                            f"SELECT {columns} FROM {table} WHERE {predicate}", key
                        ).fetchone()
                        if existing is not None:
                            if existing != row:
                                raise ValueError(f"legacy {table} immutable ID collision: {key}")
                            counts["already_present"] += 1
                            continue
                        self._db.execute(
                            f"INSERT INTO {table} ({columns}) VALUES ({', '.join('?' for _ in row)})", row
                        )
                        counts[table] += 1
        finally:
            connection.close()
        return counts
