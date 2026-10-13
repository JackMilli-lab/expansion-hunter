"""Immutable, compressed point-in-time snapshots and atomic published scan runs.

Partial scans are retained for diagnosis but never returned as a completed scan.
No history is overwritten. SQLite WAL supports readers during ingestion.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import AssetSnapshot, ScanBatch, ScanResult


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                scan_id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                status TEXT NOT NULL, batch_json TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS observations (
                scan_id TEXT NOT NULL REFERENCES runs(scan_id), asset_id TEXT NOT NULL,
                as_of TEXT NOT NULL, snapshot BLOB NOT NULL, result_json TEXT NOT NULL,
                PRIMARY KEY(scan_id, asset_id)
            );
            CREATE INDEX IF NOT EXISTS observations_asset_time ON observations(asset_id, as_of);
            CREATE TABLE IF NOT EXISTS global_observations (
                scan_id TEXT PRIMARY KEY REFERENCES runs(scan_id), as_of TEXT NOT NULL, payload TEXT NOT NULL
            );
        """)
        self._db.commit()

    def close(self):
        self._db.close()

    def begin(self, scan_id: str, started_at: datetime):
        with self._db:
            self._db.execute(
                "INSERT INTO runs(scan_id, started_at, status) VALUES (?, ?, 'RUNNING')",
                (scan_id, started_at.isoformat()),
            )

    def record(self, scan_id: str, snapshot: AssetSnapshot, result: ScanResult):
        payload = gzip.compress(snapshot.model_dump_json().encode(), compresslevel=3)
        with self._db:
            self._ensure_running(scan_id)
            self._db.execute(
                "INSERT INTO observations VALUES (?, ?, ?, ?, ?)",
                (
                    scan_id,
                    snapshot.asset.asset_id,
                    snapshot.as_of.isoformat(),
                    payload,
                    result.model_dump_json(),
                ),
            )

    def record_global(self, scan_id: str, as_of: datetime, metrics: dict):
        payload = json.dumps({k: v.model_dump(mode="json") for k, v in metrics.items()}, allow_nan=False)
        with self._db:
            self._ensure_running(scan_id)
            self._db.execute(
                "INSERT INTO global_observations VALUES (?, ?, ?)", (scan_id, as_of.isoformat(), payload)
            )

    def _ensure_running(self, scan_id: str):
        row = self._db.execute("SELECT status FROM runs WHERE scan_id=?", (scan_id,)).fetchone()
        if not row or row[0] != "RUNNING":
            raise ValueError("Only a RUNNING scan may accept observations")

    def finish(self, batch: ScanBatch):
        with self._db:
            self._db.execute(
                "UPDATE runs SET status='COMPLETE', batch_json=? WHERE scan_id=? AND status='RUNNING'",
                (batch.model_dump_json(), batch.scan_id),
            )

    def fail(self, scan_id: str, error: str):
        with self._db:
            self._db.execute(
                "UPDATE runs SET status='FAILED', error=? WHERE scan_id=? AND status='RUNNING'",
                (error, scan_id),
            )

    def latest(self) -> ScanBatch | None:
        row = self._db.execute(
            "SELECT batch_json FROM runs WHERE status='COMPLETE' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return ScanBatch.model_validate_json(row[0]) if row else None

    def get_batch(self, scan_id: str) -> ScanBatch | None:
        row = self._db.execute(
            "SELECT batch_json FROM runs WHERE scan_id=? AND status='COMPLETE'", (scan_id,)
        ).fetchone()
        return ScanBatch.model_validate_json(row[0]) if row else None

    def snapshots(self, asset_id: str, before: datetime | None = None) -> list[AssetSnapshot]:
        sql = "SELECT snapshot FROM observations o JOIN runs r USING(scan_id) WHERE asset_id=? AND r.status='COMPLETE'"
        args: list = [asset_id]
        if before:
            sql += " AND as_of<=?"
            args.append(before.isoformat())
        sql += " ORDER BY as_of"
        return [
            AssetSnapshot.model_validate_json(gzip.decompress(row[0])) for row in self._db.execute(sql, args)
        ]

    def run_status(self) -> list[dict]:
        return [
            {"scan_id": row[0], "started_at": row[1], "status": row[2], "error": row[3]}
            for row in self._db.execute(
                "SELECT scan_id, started_at, status, error FROM runs ORDER BY started_at DESC LIMIT 20"
            )
        ]

    def latest_snapshot(self, asset_id: str, before: datetime) -> AssetSnapshot | None:
        row = self._db.execute(
            "SELECT snapshot FROM observations o JOIN runs r USING(scan_id) "
            "WHERE asset_id=? AND r.status='COMPLETE' AND as_of<=? "
            "ORDER BY as_of DESC LIMIT 1",
            (asset_id, before.isoformat()),
        ).fetchone()
        return AssetSnapshot.model_validate_json(gzip.decompress(row[0])) if row else None

    def globals_before(self, before: datetime) -> list[tuple[datetime, dict]]:
        rows = self._db.execute(
            "SELECT g.as_of, payload FROM global_observations g JOIN runs r USING(scan_id) "
            "WHERE r.status='COMPLETE' AND g.as_of<=? ORDER BY g.as_of",
            (before.isoformat(),),
        )
        return [(datetime.fromisoformat(row[0]), json.loads(row[1])) for row in rows]
