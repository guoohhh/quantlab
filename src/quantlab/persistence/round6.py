from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from quantlab.persistence.migrations import record_component_migration
from quantlab.security import sanitize_for_export


ROUND6_SCHEMA_VERSION = 1


class Round6Repository:
    """Operational state and privacy-minimal product telemetry for the single-host runtime."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        record_component_migration(
            self.path,
            component="round6",
            version=ROUND6_SCHEMA_VERSION,
            migration_identity="round6-runtime-readiness-product-events-v1",
        )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                db.close()
                raise
        return db

    def _init_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS runtime_processes (
            process_type TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            pid INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running','stopping','stopped','failed')),
            started_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            stop_requested INTEGER NOT NULL DEFAULT 0,
            stopped_at TEXT,
            detail TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trusted_data_source_state (
            batch_type TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT NOT NULL,
            last_success_at TEXT,
            date_start TEXT,
            date_end TEXT,
            symbol_count INTEGER NOT NULL DEFAULT 0,
            field_coverage REAL NOT NULL DEFAULT 0,
            minimum_ready INTEGER NOT NULL DEFAULT 0,
            manifest_id TEXT,
            detail TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS product_usage_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            entrypoint TEXT,
            account_id TEXT,
            portfolio_id TEXT,
            symbol TEXT,
            reference_id TEXT,
            payload TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            training_eligible INTEGER NOT NULL DEFAULT 0,
            forward_scorecard_eligible INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_round6_product_events_time
          ON product_usage_events(occurred_at,event_type);
        CREATE INDEX IF NOT EXISTS idx_round6_product_events_reference
          ON product_usage_events(reference_id,event_type);
        """
        with self.connect() as db:
            db.executescript(schema)

    def claim_process(
        self,
        process_type: str,
        *,
        instance_id: str,
        pid: int | None = None,
        stale_after_seconds: int = 90,
        detail: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        observed = (now or datetime.now(UTC)).astimezone(UTC)
        stale_before = observed - timedelta(seconds=max(1, stale_after_seconds))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM runtime_processes WHERE process_type=?",
                (process_type,),
            ).fetchone()
            if (
                existing is not None
                and existing["status"] in {"running", "stopping"}
                and datetime.fromisoformat(existing["heartbeat_at"]) > stale_before
                and existing["instance_id"] != instance_id
            ):
                db.commit()
                return {**_process_row(existing), "claimed": False, "reason": "already_running"}
            timestamp = observed.isoformat()
            db.execute(
                """
                INSERT INTO runtime_processes(
                    process_type,instance_id,pid,status,started_at,heartbeat_at,
                    stop_requested,stopped_at,detail,updated_at
                ) VALUES(?,?,?,'running',?,?,0,NULL,?,?)
                ON CONFLICT(process_type) DO UPDATE SET
                    instance_id=excluded.instance_id,pid=excluded.pid,status='running',
                    started_at=excluded.started_at,heartbeat_at=excluded.heartbeat_at,
                    stop_requested=0,stopped_at=NULL,detail=excluded.detail,
                    updated_at=excluded.updated_at
                """,
                (
                    process_type,
                    instance_id,
                    int(pid or os.getpid()),
                    timestamp,
                    timestamp,
                    json.dumps(sanitize_for_export(detail or {}), ensure_ascii=False),
                    timestamp,
                ),
            )
            row = db.execute(
                "SELECT * FROM runtime_processes WHERE process_type=?",
                (process_type,),
            ).fetchone()
            db.commit()
        return {**_process_row(row), "claimed": True}

    def heartbeat_process(
        self,
        process_type: str,
        instance_id: str,
        *,
        detail: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> bool:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        assignments = ["heartbeat_at=?", "updated_at=?"]
        params: list[Any] = [timestamp, timestamp]
        if detail is not None:
            assignments.append("detail=?")
            params.append(json.dumps(sanitize_for_export(detail), ensure_ascii=False))
        params.extend([process_type, instance_id])
        with self.connect() as db:
            result = db.execute(
                f"""UPDATE runtime_processes SET {','.join(assignments)}
                    WHERE process_type=? AND instance_id=? AND status IN ('running','stopping')""",
                params,
            )
        return bool(result.rowcount)

    def request_stop(self, process_type: str | None = None) -> int:
        query = (
            "UPDATE runtime_processes SET stop_requested=1,status='stopping',updated_at=? "
            "WHERE status='running'"
        )
        params: list[Any] = [_now()]
        if process_type is not None:
            query += " AND process_type=?"
            params.append(process_type)
        with self.connect() as db:
            result = db.execute(query, params)
        return int(result.rowcount)

    def stop_requested(self, process_type: str, instance_id: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                """SELECT stop_requested FROM runtime_processes
                   WHERE process_type=? AND instance_id=?""",
                (process_type, instance_id),
            ).fetchone()
        return row is None or bool(row["stop_requested"])

    def finish_process(
        self,
        process_type: str,
        instance_id: str,
        *,
        status: str = "stopped",
        detail: dict[str, Any] | None = None,
    ) -> bool:
        if status not in {"stopped", "failed"}:
            raise ValueError("runtime process terminal status must be stopped or failed")
        timestamp = _now()
        with self.connect() as db:
            result = db.execute(
                """UPDATE runtime_processes SET status=?,heartbeat_at=?,stopped_at=?,
                   detail=?,updated_at=? WHERE process_type=? AND instance_id=?""",
                (
                    status,
                    timestamp,
                    timestamp,
                    json.dumps(sanitize_for_export(detail or {}), ensure_ascii=False),
                    timestamp,
                    process_type,
                    instance_id,
                ),
            )
        return bool(result.rowcount)

    def processes(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM runtime_processes ORDER BY process_type"
            ).fetchall()
        return [_process_row(row) for row in rows]

    def update_data_source_state(
        self,
        batch_type: str,
        *,
        status: str,
        manifest_id: str | None,
        date_start: str | None,
        date_end: str | None,
        symbol_count: int,
        field_coverage: float,
        minimum_ready: bool,
        detail: dict[str, Any] | None = None,
        attempted_at: datetime | None = None,
    ) -> dict[str, Any]:
        observed = (attempted_at or datetime.now(UTC)).astimezone(UTC).isoformat()
        success = status in {"completed", "partial"} and symbol_count > 0
        neutral_skip = status in {"skipped", "skipped_non_trading_day", "not_applicable"}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM trusted_data_source_state WHERE batch_type=?",
                (batch_type,),
            ).fetchone()
            failures = (
                0
                if success
                else int(existing["consecutive_failures"] if existing else 0)
                if neutral_skip
                else int(existing["consecutive_failures"] if existing else 0) + 1
            )
            last_success = observed if success else existing["last_success_at"] if existing else None
            db.execute(
                """
                INSERT INTO trusted_data_source_state(
                    batch_type,status,consecutive_failures,last_attempt_at,last_success_at,
                    date_start,date_end,symbol_count,field_coverage,minimum_ready,
                    manifest_id,detail,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(batch_type) DO UPDATE SET
                    status=excluded.status,consecutive_failures=excluded.consecutive_failures,
                    last_attempt_at=excluded.last_attempt_at,last_success_at=excluded.last_success_at,
                    date_start=excluded.date_start,date_end=excluded.date_end,
                    symbol_count=excluded.symbol_count,field_coverage=excluded.field_coverage,
                    minimum_ready=excluded.minimum_ready,manifest_id=excluded.manifest_id,
                    detail=excluded.detail,updated_at=excluded.updated_at
                """,
                (
                    batch_type,
                    status,
                    failures,
                    observed,
                    last_success,
                    date_start,
                    date_end,
                    max(0, int(symbol_count)),
                    min(1.0, max(0.0, float(field_coverage))),
                    int(minimum_ready),
                    manifest_id,
                    json.dumps(sanitize_for_export(detail or {}), ensure_ascii=False),
                    observed,
                ),
            )
            row = db.execute(
                "SELECT * FROM trusted_data_source_state WHERE batch_type=?",
                (batch_type,),
            ).fetchone()
            db.commit()
        return _data_source_row(row)

    def data_source_states(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM trusted_data_source_state ORDER BY batch_type"
            ).fetchall()
        return [_data_source_row(row) for row in rows]

    def record_product_event(
        self,
        *,
        event_type: str,
        entrypoint: str | None = None,
        account_id: str | None = None,
        portfolio_id: str | None = None,
        symbol: str | None = None,
        reference_id: str | None = None,
        payload: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> dict[str, Any]:
        event_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute(
                """INSERT INTO product_usage_events(
                    event_id,event_type,entrypoint,account_id,portfolio_id,symbol,
                    reference_id,payload,occurred_at,training_eligible,
                    forward_scorecard_eligible
                ) VALUES(?,?,?,?,?,?,?,?,?,0,0)""",
                (
                    event_id,
                    event_type,
                    entrypoint,
                    account_id,
                    portfolio_id,
                    symbol,
                    reference_id,
                    json.dumps(sanitize_for_export(payload or {}), ensure_ascii=False),
                    (occurred_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
                ),
            )
            row = db.execute(
                "SELECT * FROM product_usage_events WHERE event_id=?", (event_id,)
            ).fetchone()
        return _product_event_row(row)

    def product_events(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM product_usage_events
                   ORDER BY occurred_at DESC LIMIT ?""",
                (max(1, min(5000, int(limit))),),
            ).fetchall()
        return [_product_event_row(row) for row in rows]


def _process_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["stop_requested"] = bool(item["stop_requested"])
    item["detail"] = json.loads(item["detail"])
    return item


def _data_source_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["minimum_ready"] = bool(item["minimum_ready"])
    item["detail"] = json.loads(item["detail"])
    return item


def _product_event_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    item["training_eligible"] = bool(item["training_eligible"])
    item["forward_scorecard_eligible"] = bool(item["forward_scorecard_eligible"])
    return item


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["ROUND6_SCHEMA_VERSION", "Round6Repository"]
