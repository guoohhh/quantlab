from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from quantlab.domain.jobs import JobCancelled, JobStatus
from quantlab.persistence.migrations import record_component_migration
from quantlab.security import safe_error_detail, sanitize_for_export


RUNTIME_SCHEMA_VERSION = 6


class JobRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        record_component_migration(
            self.path,
            component="jobs",
            version=6,
            migration_identity="round11-scheduler-same-day-recovery-attempts-v1",
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
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS background_jobs (
            job_id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            queue_name TEXT NOT NULL DEFAULT 'default',
            status TEXT NOT NULL CHECK(status IN ('queued','running','cancelled','completed','failed')),
            priority INTEGER NOT NULL DEFAULT 100,
            payload TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            concurrency_key TEXT,
            progress REAL NOT NULL DEFAULT 0,
            progress_message TEXT,
            result_payload TEXT,
            result_ref TEXT,
            error_code TEXT,
            error_detail TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            timeout_seconds INTEGER NOT NULL DEFAULT 900,
            available_at TEXT NOT NULL,
            claimed_by TEXT,
            claimed_at TEXT,
            heartbeat_at TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            side_effect_state TEXT NOT NULL DEFAULT 'not_started',
            side_effect_result_payload TEXT,
            cost_budget_usd REAL NOT NULL DEFAULT 0,
            cost_used_usd REAL NOT NULL DEFAULT 0,
            parent_job_id TEXT,
            schedule_run_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(parent_job_id) REFERENCES background_jobs(job_id)
        );
        CREATE TABLE IF NOT EXISTS background_job_dependencies (
            job_id TEXT NOT NULL,
            depends_on_job_id TEXT NOT NULL,
            PRIMARY KEY(job_id,depends_on_job_id),
            FOREIGN KEY(job_id) REFERENCES background_jobs(job_id),
            FOREIGN KEY(depends_on_job_id) REFERENCES background_jobs(job_id)
        );
        CREATE TABLE IF NOT EXISTS background_job_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            progress REAL,
            message TEXT,
            payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES background_jobs(job_id)
        );
        CREATE TABLE IF NOT EXISTS runtime_schedules (
            schedule_id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            job_type TEXT NOT NULL,
            local_time TEXT NOT NULL,
            trading_days_only INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            dependency_names TEXT NOT NULL DEFAULT '[]',
            payload TEXT NOT NULL DEFAULT '{}',
            timeout_seconds INTEGER NOT NULL DEFAULT 900,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            cost_budget_usd REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runtime_schedule_runs (
            schedule_run_id TEXT PRIMARY KEY,
            schedule_id TEXT NOT NULL,
            run_date TEXT NOT NULL,
            attempt_number INTEGER NOT NULL DEFAULT 1,
            recovery_of_schedule_run_id TEXT,
            recovery_reason TEXT,
            job_id TEXT,
            status TEXT NOT NULL,
            is_backfill INTEGER NOT NULL DEFAULT 0,
            payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(schedule_id,run_date,attempt_number),
            FOREIGN KEY(schedule_id) REFERENCES runtime_schedules(schedule_id),
            FOREIGN KEY(job_id) REFERENCES background_jobs(job_id),
            FOREIGN KEY(recovery_of_schedule_run_id)
              REFERENCES runtime_schedule_runs(schedule_run_id)
        );
        CREATE TABLE IF NOT EXISTS trading_calendar (
            trade_date TEXT PRIMARY KEY,
            is_open INTEGER NOT NULL,
            source TEXT NOT NULL,
            available_at TEXT NOT NULL,
            quality TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runtime_failures (
            failure_id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            severity TEXT NOT NULL,
            error_detail TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            acknowledged_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_audit_log (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            client_fingerprint TEXT NOT NULL,
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            duration_ms REAL NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_rate_windows (
            client_fingerprint TEXT NOT NULL,
            window_started_at TEXT NOT NULL,
            request_count INTEGER NOT NULL,
            PRIMARY KEY(client_fingerprint,window_started_at)
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_claim
          ON background_jobs(status,available_at,priority,created_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_heartbeat
          ON background_jobs(status,heartbeat_at);
        CREATE INDEX IF NOT EXISTS idx_job_events
          ON background_job_events(job_id,event_id);
        CREATE INDEX IF NOT EXISTS idx_schedule_runs_date
          ON runtime_schedule_runs(run_date,status);
        CREATE INDEX IF NOT EXISTS idx_api_audit_created
          ON api_audit_log(created_at,path);
        """
        checksum = hashlib.sha256(schema.encode("utf-8")).hexdigest()
        with self.connect() as db:
            db.executescript(schema)
            self._migrate_schedule_runs_v6(db)
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(background_jobs)").fetchall()
            }
            if "side_effect_state" not in columns:
                db.execute(
                    """
                    ALTER TABLE background_jobs
                    ADD COLUMN side_effect_state TEXT NOT NULL DEFAULT 'not_started'
                    """
                )
            if "side_effect_result_payload" not in columns:
                db.execute(
                    "ALTER TABLE background_jobs ADD COLUMN side_effect_result_payload TEXT"
                )
            existing = db.execute(
                "SELECT checksum FROM schema_migrations WHERE version=?",
                (RUNTIME_SCHEMA_VERSION,),
            ).fetchone()
            if existing is not None and existing["checksum"] != checksum:
                raise RuntimeError("runtime schema migration checksum mismatch")
            db.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version,name,checksum,applied_at)
                VALUES(?,?,?,?)
                """,
                (RUNTIME_SCHEMA_VERSION, "round3_runtime", checksum, _now()),
            )

    def submit(
        self,
        *,
        job_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        queue_name: str = "default",
        priority: int = 100,
        concurrency_key: str | None = None,
        max_attempts: int = 3,
        timeout_seconds: int = 900,
        cost_budget_usd: float = 0.0,
        available_at: datetime | None = None,
        parent_job_id: str | None = None,
        schedule_run_id: str | None = None,
        dependency_job_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if not idempotency_key.strip():
            raise ValueError("job idempotency key is required")
        if max_attempts < 1 or timeout_seconds < 1 or cost_budget_usd < 0:
            raise ValueError("invalid job retry, timeout or cost budget")
        job_id = str(uuid.uuid4())
        now = _now()
        due = (available_at or datetime.now(UTC)).astimezone(UTC).isoformat()
        sanitized = json.dumps(sanitize_for_export(payload), ensure_ascii=False)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM background_jobs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                db.commit()
                return self._job_row(existing)
            db.execute(
                """
                INSERT INTO background_jobs(
                    job_id,job_type,queue_name,status,priority,payload,idempotency_key,
                    concurrency_key,max_attempts,timeout_seconds,available_at,
                    cost_budget_usd,parent_job_id,schedule_run_id,created_at,updated_at
                ) VALUES(?,?,?,'queued',?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    job_type,
                    queue_name,
                    priority,
                    sanitized,
                    idempotency_key,
                    concurrency_key,
                    max_attempts,
                    timeout_seconds,
                    due,
                    cost_budget_usd,
                    parent_job_id,
                    schedule_run_id,
                    now,
                    now,
                ),
            )
            for dependency in sorted(set(dependency_job_ids or [])):
                if dependency == job_id:
                    raise ValueError("a job cannot depend on itself")
                if db.execute(
                    "SELECT 1 FROM background_jobs WHERE job_id=?", (dependency,)
                ).fetchone() is None:
                    raise ValueError("job dependency not found")
                db.execute(
                    """
                    INSERT INTO background_job_dependencies(job_id,depends_on_job_id)
                    VALUES(?,?)
                    """,
                    (job_id, dependency),
                )
            self._event_in_tx(db, job_id, "submitted", 0.0, "job queued", {})
            row = db.execute(
                "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            db.commit()
        return self._job_row(row)

    def claim(
        self,
        *,
        worker_id: str,
        queue_name: str = "default",
        maximum_running: int = 4,
        per_type_limits: dict[str, int] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        per_type_limits = per_type_limits or {}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover_stale_in_tx(db, timestamp)
            self._propagate_failed_dependencies_in_tx(db)
            running = int(
                db.execute(
                    "SELECT COUNT(*) FROM background_jobs WHERE status='running' AND queue_name=?",
                    (queue_name,),
                ).fetchone()[0]
            )
            if running >= maximum_running:
                db.commit()
                return None
            candidates = db.execute(
                """
                SELECT j.* FROM background_jobs j
                WHERE j.status='queued' AND j.queue_name=? AND j.available_at<=?
                  AND j.cancel_requested=0
                  AND NOT EXISTS (
                    SELECT 1 FROM background_job_dependencies d
                    JOIN background_jobs dependency ON dependency.job_id=d.depends_on_job_id
                    WHERE d.job_id=j.job_id AND dependency.status!='completed'
                  )
                ORDER BY j.priority,j.created_at LIMIT 50
                """,
                (queue_name, timestamp.isoformat()),
            ).fetchall()
            selected = None
            for candidate in candidates:
                limit = per_type_limits.get(candidate["job_type"])
                if limit is not None:
                    type_running = int(
                        db.execute(
                            """
                            SELECT COUNT(*) FROM background_jobs
                            WHERE status='running' AND job_type=?
                            """,
                            (candidate["job_type"],),
                        ).fetchone()[0]
                    )
                    if type_running >= limit:
                        continue
                if candidate["concurrency_key"]:
                    conflict = db.execute(
                        """
                        SELECT 1 FROM background_jobs
                        WHERE status='running' AND concurrency_key=? LIMIT 1
                        """,
                        (candidate["concurrency_key"],),
                    ).fetchone()
                    if conflict is not None:
                        continue
                selected = candidate
                break
            if selected is None:
                db.commit()
                return None
            result = db.execute(
                """
                UPDATE background_jobs
                SET status='running',claimed_by=?,claimed_at=?,heartbeat_at=?,
                    attempts=attempts+1,updated_at=?
                WHERE job_id=? AND status='queued'
                """,
                (
                    worker_id,
                    timestamp.isoformat(),
                    timestamp.isoformat(),
                    timestamp.isoformat(),
                    selected["job_id"],
                ),
            )
            if result.rowcount != 1:
                db.rollback()
                return None
            self._event_in_tx(
                db,
                selected["job_id"],
                "claimed",
                float(selected["progress"]),
                f"claimed by {worker_id}",
                {"worker_id": worker_id},
            )
            row = db.execute(
                "SELECT * FROM background_jobs WHERE job_id=?", (selected["job_id"],)
            ).fetchone()
            db.commit()
        return self._job_row(row)

    def heartbeat(
        self,
        job_id: str,
        worker_id: str,
        *,
        progress: float | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        timestamp = _now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None or row["status"] != JobStatus.RUNNING.value:
                db.commit()
                return False
            if row["claimed_by"] != worker_id:
                db.rollback()
                raise PermissionError("job is owned by another worker")
            resolved_progress = (
                max(float(row["progress"]), min(1.0, max(0.0, float(progress))))
                if progress is not None
                else float(row["progress"])
            )
            db.execute(
                """
                UPDATE background_jobs
                SET heartbeat_at=?,progress=?,progress_message=COALESCE(?,progress_message),
                    updated_at=? WHERE job_id=?
                """,
                (timestamp, resolved_progress, message, timestamp, job_id),
            )
            if progress is not None or message is not None or payload:
                self._event_in_tx(
                    db,
                    job_id,
                    "progress",
                    resolved_progress,
                    message,
                    payload or {},
                )
            db.commit()
        return True

    def consume_cost(self, job_id: str, worker_id: str, amount_usd: float) -> bool:
        if amount_usd < 0:
            raise ValueError("job cost cannot be negative")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None or row["status"] != JobStatus.RUNNING.value:
                db.commit()
                return False
            if row["claimed_by"] != worker_id:
                db.rollback()
                raise PermissionError("job is owned by another worker")
            used = float(row["cost_used_usd"]) + amount_usd
            budget = float(row["cost_budget_usd"])
            if budget > 0 and used > budget + 1e-12:
                self._event_in_tx(
                    db,
                    job_id,
                    "cost_budget_rejected",
                    float(row["progress"]),
                    "cost budget would be exceeded",
                    {"attempted_cost_usd": amount_usd, "used_usd": used, "budget_usd": budget},
                )
                db.commit()
                return False
            db.execute(
                "UPDATE background_jobs SET cost_used_usd=?,updated_at=? WHERE job_id=?",
                (used, _now(), job_id),
            )
            db.commit()
        return True

    def complete(
        self,
        job_id: str,
        worker_id: str,
        *,
        result: dict[str, Any] | None = None,
        result_ref: str | None = None,
    ) -> dict[str, Any]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                db.rollback()
                raise ValueError("job not found")
            if row["status"] == JobStatus.COMPLETED.value:
                db.commit()
                return self._job_row(row)
            if row["status"] == JobStatus.CANCELLED.value:
                db.commit()
                return self._job_row(row)
            if row["cancel_requested"]:
                db.rollback()
                raise JobCancelled("job cancellation was requested before completion")
            if row["status"] != JobStatus.RUNNING.value or row["claimed_by"] != worker_id:
                db.rollback()
                raise PermissionError("job completion requires the current worker lease")
            now = _now()
            db.execute(
                """
                UPDATE background_jobs SET status='completed',progress=1,
                    progress_message='completed',result_payload=?,result_ref=?,
                    heartbeat_at=?,updated_at=?,completed_at=? WHERE job_id=?
                """,
                (
                    json.dumps(sanitize_for_export(result or {}), ensure_ascii=False),
                    result_ref,
                    now,
                    now,
                    now,
                    job_id,
                ),
            )
            self._event_in_tx(db, job_id, "completed", 1.0, "job completed", {})
            self._sync_schedule_run_in_tx(db, job_id, "completed")
            completed = db.execute(
                "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            db.commit()
        return self._job_row(completed)

    def fail(
        self,
        job_id: str,
        worker_id: str,
        error: Exception | str,
        *,
        retryable: bool = True,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        detail = safe_error_detail(error)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                db.rollback()
                raise ValueError("job not found")
            if row["status"] in {JobStatus.COMPLETED.value, JobStatus.CANCELLED.value}:
                db.commit()
                return self._job_row(row)
            if row["status"] == JobStatus.RUNNING.value and row["claimed_by"] != worker_id:
                db.rollback()
                raise PermissionError("job failure requires the current worker lease")
            can_retry = retryable and int(row["attempts"]) < int(row["max_attempts"])
            if can_retry:
                delay = min(3600, 2 ** max(0, int(row["attempts"]) - 1) * 5)
                available = timestamp + timedelta(seconds=delay)
                db.execute(
                    """
                    UPDATE background_jobs SET status='queued',available_at=?,claimed_by=NULL,
                        claimed_at=NULL,heartbeat_at=NULL,error_code='retryable_failure',
                        error_detail=?,updated_at=? WHERE job_id=?
                    """,
                    (available.isoformat(), detail, timestamp.isoformat(), job_id),
                )
                event_type = "retry_scheduled"
                message = f"retry scheduled after {delay} seconds"
            else:
                db.execute(
                    """
                    UPDATE background_jobs SET status='failed',error_code='job_failed',
                        error_detail=?,heartbeat_at=?,updated_at=?,completed_at=? WHERE job_id=?
                    """,
                    (
                        detail,
                        timestamp.isoformat(),
                        timestamp.isoformat(),
                        timestamp.isoformat(),
                        job_id,
                    ),
                )
                event_type = "failed"
                message = "job failed"
                self._sync_schedule_run_in_tx(db, job_id, "failed")
                self._failure_in_tx(db, "job", job_id, "warning", detail, {})
                self._propagate_failed_dependencies_in_tx(db)
            self._event_in_tx(
                db,
                job_id,
                event_type,
                float(row["progress"]),
                message,
                {"error": detail},
            )
            failed = db.execute(
                "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            db.commit()
        return self._job_row(failed)

    def cancel(self, job_id: str, reason: str = "cancelled_by_user") -> dict[str, Any]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                db.rollback()
                raise ValueError("job not found")
            if row["status"] in {
                JobStatus.COMPLETED.value,
                JobStatus.FAILED.value,
                JobStatus.CANCELLED.value,
            }:
                db.commit()
                return self._job_row(row)
            now = _now()
            if row["status"] == JobStatus.RUNNING.value:
                db.execute(
                    """
                    UPDATE background_jobs SET cancel_requested=1,
                        error_code='cancel_requested',error_detail=?,updated_at=?
                    WHERE job_id=?
                    """,
                    (reason[:500], now, job_id),
                )
                self._event_in_tx(
                    db,
                    job_id,
                    "cancel_requested",
                    float(row["progress"]),
                    reason[:500],
                    {"cooperative": True},
                )
            else:
                db.execute(
                    """
                    UPDATE background_jobs SET status='cancelled',cancel_requested=1,
                        error_code='cancelled',error_detail=?,updated_at=?,completed_at=?
                    WHERE job_id=?
                    """,
                    (reason[:500], now, now, job_id),
                )
                self._event_in_tx(
                    db,
                    job_id,
                    "cancelled",
                    float(row["progress"]),
                    reason[:500],
                    {"cooperative": False},
                )
                self._sync_schedule_run_in_tx(db, job_id, "cancelled")
                self._propagate_failed_dependencies_in_tx(db)
            cancelled = db.execute(
                "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            db.commit()
        return self._job_row(cancelled)

    def acknowledge_cancel(
        self,
        job_id: str,
        worker_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                db.rollback()
                raise ValueError("job not found")
            if row["status"] == JobStatus.CANCELLED.value:
                db.commit()
                return self._job_row(row)
            if row["status"] != JobStatus.RUNNING.value or row["claimed_by"] != worker_id:
                db.rollback()
                raise PermissionError("cancellation acknowledgement requires the worker lease")
            now = _now()
            code = (
                "cancelled_after_side_effect"
                if row["side_effect_state"] in {"started", "completed"}
                else "cancelled"
            )
            db.execute(
                """
                UPDATE background_jobs SET status='cancelled',cancel_requested=1,
                    error_code=?,error_detail=?,heartbeat_at=?,updated_at=?,completed_at=?
                WHERE job_id=?
                """,
                (code, reason[:500], now, now, now, job_id),
            )
            self._event_in_tx(
                db,
                job_id,
                "cancelled",
                float(row["progress"]),
                reason[:500],
                {"side_effect_state": row["side_effect_state"]},
            )
            self._sync_schedule_run_in_tx(db, job_id, "cancelled")
            self._propagate_failed_dependencies_in_tx(db)
            result = db.execute(
                "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            db.commit()
        return self._job_row(result)

    def mark_side_effect_state(
        self,
        job_id: str,
        worker_id: str,
        state: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        if state not in {"not_started", "started", "completed"}:
            raise ValueError("invalid side effect state")
        with self.connect() as db:
            result = db.execute(
                """
                UPDATE background_jobs SET side_effect_state=?,
                    side_effect_result_payload=COALESCE(?,side_effect_result_payload),
                    updated_at=?
                WHERE job_id=? AND status='running' AND claimed_by=?
                """,
                (
                    state,
                    json.dumps(sanitize_for_export(result), ensure_ascii=False)
                    if result is not None
                    else None,
                    _now(),
                    job_id,
                    worker_id,
                ),
            )
            if result.rowcount != 1:
                raise PermissionError("side effect state requires the worker lease")

    def block_uncertain_side_effect(
        self,
        job_id: str,
        worker_id: str,
    ) -> dict[str, Any]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                db.rollback()
                raise ValueError("job not found")
            if row["status"] != "running" or row["claimed_by"] != worker_id:
                db.rollback()
                raise PermissionError("uncertain side effect handling requires the worker lease")
            now = _now()
            detail = (
                "a prior worker stopped after entering a side-effect section; automatic "
                "re-execution was blocked to prevent duplicate external effects"
            )
            db.execute(
                """
                UPDATE background_jobs SET status='failed',
                    error_code='side_effect_outcome_unknown',error_detail=?,
                    updated_at=?,completed_at=? WHERE job_id=?
                """,
                (detail, now, now, job_id),
            )
            self._event_in_tx(
                db,
                job_id,
                "blocked",
                float(row["progress"]),
                detail,
                {"equivalent_state": "blocked", "requires_manual_reconciliation": True},
            )
            self._sync_schedule_run_in_tx(db, job_id, "failed")
            self._propagate_failed_dependencies_in_tx(db)
            output = db.execute(
                "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            db.commit()
        return self._job_row(output)

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return self._job_row(row) if row else None

    def jobs(
        self,
        *,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if job_type:
            clauses.append("job_type=?")
            params.append(job_type)
        query = "SELECT * FROM background_jobs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._job_row(row) for row in rows]

    def events(self, job_id: str, after_event_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM background_job_events
                WHERE job_id=? AND event_id>? ORDER BY event_id LIMIT ?
                """,
                (job_id, after_event_id, limit),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def is_cancelled(self, job_id: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT status,cancel_requested FROM background_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return row is None or row["status"] == JobStatus.CANCELLED.value or bool(
            row["cancel_requested"]
        )

    def recover_stale(self, now: datetime | None = None) -> dict[str, int]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = self._recover_stale_in_tx(
                db, (now or datetime.now(UTC)).astimezone(UTC)
            )
            db.commit()
        return result

    def schema_status(self) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM schema_migrations ORDER BY version"
            ).fetchall()
        return {
            "current_version": max((int(row["version"]) for row in rows), default=0),
            "expected_version": RUNTIME_SCHEMA_VERSION,
            "migrations": [dict(row) for row in rows],
        }

    def upsert_trading_day(
        self,
        *,
        trade_date: date,
        is_open: bool,
        source: str,
        available_at: datetime,
        quality: str = "available",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO trading_calendar(
                    trade_date,is_open,source,available_at,quality,created_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    is_open=excluded.is_open,source=excluded.source,
                    available_at=excluded.available_at,quality=excluded.quality
                """,
                (
                    trade_date.isoformat(),
                    int(is_open),
                    source,
                    available_at.astimezone(UTC).isoformat(),
                    quality,
                    _now(),
                ),
            )

    def trading_day(self, trade_date: date, cutoff_at: datetime | None = None) -> dict[str, Any]:
        cutoff = (cutoff_at or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connect() as db:
            row = db.execute(
                """
                SELECT * FROM trading_calendar WHERE trade_date=? AND available_at<=?
                """,
                (trade_date.isoformat(), cutoff),
            ).fetchone()
        if row:
            return {**dict(row), "is_open": bool(row["is_open"]), "estimated": False}
        return {
            "trade_date": trade_date.isoformat(),
            "is_open": trade_date.weekday() < 5,
            "source": "weekday_fallback",
            "available_at": cutoff,
            "quality": "degraded",
            "estimated": True,
        }

    def register_schedule(
        self,
        *,
        name: str,
        job_type: str,
        local_time: str,
        dependency_names: list[str] | None = None,
        trading_days_only: bool = True,
        enabled: bool = True,
        payload: dict[str, Any] | None = None,
        timeout_seconds: int = 900,
        max_attempts: int = 3,
        cost_budget_usd: float = 0.0,
    ) -> dict[str, Any]:
        datetime.strptime(local_time, "%H:%M")
        now = _now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO runtime_schedules(
                    schedule_id,name,job_type,local_time,trading_days_only,enabled,
                    dependency_names,payload,timeout_seconds,max_attempts,cost_budget_usd,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                    job_type=excluded.job_type,local_time=excluded.local_time,
                    trading_days_only=excluded.trading_days_only,enabled=excluded.enabled,
                    dependency_names=excluded.dependency_names,payload=excluded.payload,
                    timeout_seconds=excluded.timeout_seconds,max_attempts=excluded.max_attempts,
                    cost_budget_usd=excluded.cost_budget_usd,updated_at=excluded.updated_at
                """,
                (
                    str(uuid.uuid4()),
                    name,
                    job_type,
                    local_time,
                    int(trading_days_only),
                    int(enabled),
                    json.dumps(dependency_names or [], ensure_ascii=False),
                    json.dumps(sanitize_for_export(payload or {}), ensure_ascii=False),
                    timeout_seconds,
                    max_attempts,
                    cost_budget_usd,
                    now,
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM runtime_schedules WHERE name=?", (name,)
            ).fetchone()
        return self._schedule_row(row)

    def schedules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM runtime_schedules"
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY local_time,name"
        with self.connect() as db:
            rows = db.execute(query).fetchall()
        return [self._schedule_row(row) for row in rows]

    def create_schedule_run(
        self,
        *,
        schedule_id: str,
        run_date: date,
        is_backfill: bool,
        payload: dict[str, Any] | None = None,
        force_new_attempt: bool = False,
        recovery_of_schedule_run_id: str | None = None,
        recovery_reason: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self.connect() as db:
            existing = db.execute(
                """
                SELECT * FROM runtime_schedule_runs WHERE schedule_id=? AND run_date=?
                ORDER BY attempt_number DESC LIMIT 1
                """,
                (schedule_id, run_date.isoformat()),
            ).fetchone()
            if existing and not force_new_attempt:
                return self._schedule_run_row(existing)
            if force_new_attempt and existing is None:
                raise ValueError("a recovery schedule attempt requires an original schedule run")
            if force_new_attempt and not str(recovery_reason or "").strip():
                raise ValueError("a recovery schedule attempt requires an audit reason")
            attempt_number = int(existing["attempt_number"]) + 1 if existing else 1
            recovery_of = (
                recovery_of_schedule_run_id
                or (existing["schedule_run_id"] if force_new_attempt and existing else None)
            )
            run_id = str(uuid.uuid4())
            db.execute(
                """
                INSERT INTO runtime_schedule_runs(
                    schedule_run_id,schedule_id,run_date,attempt_number,
                    recovery_of_schedule_run_id,recovery_reason,status,is_backfill,payload,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,'planned',?,?,?,?)
                """,
                (
                    run_id,
                    schedule_id,
                    run_date.isoformat(),
                    attempt_number,
                    recovery_of,
                    str(recovery_reason).strip() if recovery_reason else None,
                    int(is_backfill),
                    json.dumps(sanitize_for_export(payload or {}), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM runtime_schedule_runs WHERE schedule_run_id=?", (run_id,)
            ).fetchone()
        return self._schedule_run_row(row)

    @staticmethod
    def _migrate_schedule_runs_v6(db: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(runtime_schedule_runs)")
        }
        if "attempt_number" in columns:
            return
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("ALTER TABLE runtime_schedule_runs RENAME TO runtime_schedule_runs_v5")
        db.execute(
            """CREATE TABLE runtime_schedule_runs (
                schedule_run_id TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL,
                run_date TEXT NOT NULL,
                attempt_number INTEGER NOT NULL DEFAULT 1,
                recovery_of_schedule_run_id TEXT,
                recovery_reason TEXT,
                job_id TEXT,
                status TEXT NOT NULL,
                is_backfill INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(schedule_id,run_date,attempt_number),
                FOREIGN KEY(schedule_id) REFERENCES runtime_schedules(schedule_id),
                FOREIGN KEY(job_id) REFERENCES background_jobs(job_id),
                FOREIGN KEY(recovery_of_schedule_run_id)
                  REFERENCES runtime_schedule_runs(schedule_run_id)
            )"""
        )
        db.execute(
            """INSERT INTO runtime_schedule_runs(
                schedule_run_id,schedule_id,run_date,attempt_number,
                recovery_of_schedule_run_id,recovery_reason,job_id,status,is_backfill,
                payload,created_at,updated_at
            )
            SELECT schedule_run_id,schedule_id,run_date,1,NULL,NULL,job_id,status,is_backfill,
                   payload,created_at,updated_at
            FROM runtime_schedule_runs_v5"""
        )
        db.execute("DROP TABLE runtime_schedule_runs_v5")
        db.execute(
            """CREATE INDEX IF NOT EXISTS idx_schedule_runs_date
               ON runtime_schedule_runs(run_date,status)"""
        )
        db.execute("PRAGMA foreign_keys=ON")

    def link_schedule_job(self, schedule_run_id: str, job_id: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE runtime_schedule_runs SET job_id=?,status='queued',updated_at=?
                WHERE schedule_run_id=?
                """,
                (job_id, _now(), schedule_run_id),
            )

    def schedule_runs(self, run_date: date | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if run_date:
            query = (
                "SELECT * FROM runtime_schedule_runs WHERE run_date=? "
                "ORDER BY created_at LIMIT ?"
            )
            params: tuple[Any, ...] = (run_date.isoformat(), limit)
        else:
            query = "SELECT * FROM runtime_schedule_runs ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._schedule_run_row(row) for row in rows]

    def schedule_run_for_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT r.*,s.name AS schedule_name,s.job_type AS schedule_job_type,
                       s.dependency_names AS schedule_dependency_names
                FROM background_jobs j
                JOIN runtime_schedule_runs r ON r.schedule_run_id=j.schedule_run_id
                JOIN runtime_schedules s ON s.schedule_id=r.schedule_id
                WHERE j.job_id=?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["schedule_dependency_names"] = json.loads(
            item["schedule_dependency_names"]
        )
        item["is_backfill"] = bool(item["is_backfill"])
        return item

    def check_rate_limit(
        self,
        *,
        client_fingerprint: str,
        maximum_requests: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        window = timestamp.replace(second=0, microsecond=0).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT INTO api_rate_windows(client_fingerprint,window_started_at,request_count)
                VALUES(?,?,1)
                ON CONFLICT(client_fingerprint,window_started_at) DO UPDATE SET
                    request_count=request_count+1
                """,
                (client_fingerprint, window),
            )
            count = int(
                db.execute(
                    """
                    SELECT request_count FROM api_rate_windows
                    WHERE client_fingerprint=? AND window_started_at=?
                    """,
                    (client_fingerprint, window),
                ).fetchone()[0]
            )
            db.commit()
        return {
            "allowed": count <= maximum_requests,
            "count": count,
            "limit": maximum_requests,
            "window_started_at": window,
        }

    def record_api_audit(
        self,
        *,
        request_id: str,
        client_fingerprint: str,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO api_audit_log(
                    request_id,client_fingerprint,method,path,status_code,duration_ms,created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    request_id,
                    client_fingerprint,
                    method[:16],
                    path[:500],
                    status_code,
                    max(0.0, duration_ms),
                    _now(),
                ),
            )

    def runtime_status(self) -> dict[str, Any]:
        with self.connect() as db:
            jobs = {
                row["status"]: int(row["count"])
                for row in db.execute(
                    "SELECT status,COUNT(*) count FROM background_jobs GROUP BY status"
                ).fetchall()
            }
            outbox = db.execute(
                """
                SELECT status,COUNT(*) count FROM notification_outbox GROUP BY status
                """
            ).fetchall() if _table_exists(db, "notification_outbox") else []
            failures = int(
                db.execute(
                    "SELECT COUNT(*) FROM runtime_failures WHERE acknowledged_at IS NULL"
                ).fetchone()[0]
            )
        return {
            "jobs": jobs,
            "notification_outbox": {row["status"]: int(row["count"]) for row in outbox},
            "unacknowledged_failures": failures,
            "schema": self.schema_status(),
        }

    def record_runtime_failure(
        self,
        *,
        source_type: str,
        source_id: str,
        severity: str,
        error_detail: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        failure_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute(
                """INSERT INTO runtime_failures(
                    failure_id,source_type,source_id,severity,error_detail,payload,created_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    failure_id,
                    source_type,
                    source_id,
                    severity,
                    error_detail,
                    json.dumps(sanitize_for_export(payload or {}), ensure_ascii=False),
                    _now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM runtime_failures WHERE failure_id=?", (failure_id,)
            ).fetchone()
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def purge_runtime_history(
        self,
        *,
        audit_retention_days: int = 365,
        job_retention_days: int = 365,
        as_of: datetime | None = None,
    ) -> dict[str, int]:
        resolved = (as_of or datetime.now(UTC)).astimezone(UTC)
        audit_cutoff = (resolved - timedelta(days=audit_retention_days)).isoformat()
        job_cutoff = (resolved - timedelta(days=job_retention_days)).isoformat()
        rate_cutoff = (resolved - timedelta(days=2)).isoformat()
        with self.connect() as db:
            api_audit = db.execute(
                "DELETE FROM api_audit_log WHERE created_at<?", (audit_cutoff,)
            ).rowcount
            rate_windows = db.execute(
                "DELETE FROM api_rate_windows WHERE window_started_at<?", (rate_cutoff,)
            ).rowcount
            terminal_jobs = [
                row[0]
                for row in db.execute(
                    """
                    SELECT job_id FROM background_jobs
                    WHERE completed_at<? AND status IN ('completed','failed','cancelled')
                      AND job_id NOT IN (
                        SELECT depends_on_job_id FROM background_job_dependencies
                      )
                      AND job_id NOT IN (
                        SELECT parent_job_id FROM background_jobs WHERE parent_job_id IS NOT NULL
                      )
                      AND job_id NOT IN (
                        SELECT job_id FROM runtime_schedule_runs WHERE job_id IS NOT NULL
                      )
                    """,
                    (job_cutoff,),
                ).fetchall()
            ]
            events = 0
            jobs = 0
            if terminal_jobs:
                placeholders = ",".join("?" for _ in terminal_jobs)
                events = db.execute(
                    f"DELETE FROM background_job_events WHERE job_id IN ({placeholders})",
                    terminal_jobs,
                ).rowcount
                db.execute(
                    f"DELETE FROM background_job_dependencies WHERE job_id IN ({placeholders})",
                    terminal_jobs,
                )
                jobs = db.execute(
                    f"DELETE FROM background_jobs WHERE job_id IN ({placeholders})",
                    terminal_jobs,
                ).rowcount
        return {
            "api_audit_deleted": api_audit,
            "rate_windows_deleted": rate_windows,
            "job_events_deleted": events,
            "jobs_deleted": jobs,
        }

    def _recover_stale_in_tx(
        self, db: sqlite3.Connection, now: datetime
    ) -> dict[str, int]:
        recovered = 0
        failed = 0
        rows = db.execute(
            "SELECT * FROM background_jobs WHERE status='running'"
        ).fetchall()
        for row in rows:
            heartbeat = datetime.fromisoformat(row["heartbeat_at"] or row["claimed_at"])
            if heartbeat + timedelta(seconds=int(row["timeout_seconds"])) >= now:
                continue
            if int(row["attempts"]) < int(row["max_attempts"]):
                db.execute(
                    """
                    UPDATE background_jobs SET status='queued',claimed_by=NULL,claimed_at=NULL,
                        heartbeat_at=NULL,available_at=?,error_code='worker_lease_expired',
                        error_detail='worker heartbeat expired; recovered for retry',updated_at=?
                    WHERE job_id=?
                    """,
                    (now.isoformat(), now.isoformat(), row["job_id"]),
                )
                self._event_in_tx(
                    db,
                    row["job_id"],
                    "recovered",
                    float(row["progress"]),
                    "expired worker lease recovered",
                    {},
                )
                recovered += 1
            else:
                db.execute(
                    """
                    UPDATE background_jobs SET status='failed',error_code='worker_lease_expired',
                        error_detail='worker heartbeat expired and retries exhausted',updated_at=?,
                        completed_at=? WHERE job_id=?
                    """,
                    (now.isoformat(), now.isoformat(), row["job_id"]),
                )
                self._event_in_tx(
                    db,
                    row["job_id"],
                    "failed",
                    float(row["progress"]),
                    "worker lease expired and retries exhausted",
                    {},
                )
                self._failure_in_tx(
                    db,
                    "job",
                    row["job_id"],
                    "warning",
                    "worker heartbeat expired and retries exhausted",
                    {},
                )
                self._sync_schedule_run_in_tx(db, row["job_id"], "failed")
                failed += 1
        self._propagate_failed_dependencies_in_tx(db)
        return {"recovered": recovered, "failed": failed}

    def _propagate_failed_dependencies_in_tx(self, db: sqlite3.Connection) -> int:
        propagated = 0
        while True:
            rows = db.execute(
                """
                SELECT DISTINCT j.* FROM background_jobs j
                JOIN background_job_dependencies d ON d.job_id=j.job_id
                JOIN background_jobs dependency ON dependency.job_id=d.depends_on_job_id
                WHERE j.status='queued' AND dependency.status IN ('failed','cancelled')
                """
            ).fetchall()
            if not rows:
                break
            now = _now()
            for row in rows:
                db.execute(
                    """
                    UPDATE background_jobs SET status='failed',
                        error_code='dependency_blocked',
                        error_detail='upstream dependency failed or was cancelled',
                        progress_message='blocked by upstream dependency',
                        updated_at=?,completed_at=? WHERE job_id=? AND status='queued'
                    """,
                    (now, now, row["job_id"]),
                )
                self._event_in_tx(
                    db,
                    row["job_id"],
                    "blocked",
                    float(row["progress"]),
                    "upstream dependency failed or was cancelled",
                    {"terminal_status": "failed", "equivalent_state": "blocked"},
                )
                self._sync_schedule_run_in_tx(db, row["job_id"], "failed")
                propagated += 1
        return propagated

    @staticmethod
    def _event_in_tx(
        db: sqlite3.Connection,
        job_id: str,
        event_type: str,
        progress: float | None,
        message: str | None,
        payload: dict[str, Any],
    ) -> None:
        db.execute(
            """
            INSERT INTO background_job_events(
                job_id,event_type,progress,message,payload,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                job_id,
                event_type,
                progress,
                message,
                json.dumps(sanitize_for_export(payload), ensure_ascii=False),
                _now(),
            ),
        )

    @staticmethod
    def _failure_in_tx(
        db: sqlite3.Connection,
        source_type: str,
        source_id: str,
        severity: str,
        detail: str,
        payload: dict[str, Any],
    ) -> None:
        db.execute(
            """
            INSERT INTO runtime_failures(
                failure_id,source_type,source_id,severity,error_detail,payload,created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                source_type,
                source_id,
                severity,
                detail,
                json.dumps(sanitize_for_export(payload), ensure_ascii=False),
                _now(),
            ),
        )

    @staticmethod
    def _sync_schedule_run_in_tx(
        db: sqlite3.Connection, job_id: str, status: str
    ) -> None:
        db.execute(
            """
            UPDATE runtime_schedule_runs SET status=?,updated_at=? WHERE job_id=?
            """,
            (status, _now(), job_id),
        )

    @staticmethod
    def _job_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["result_payload"] = (
            json.loads(item["result_payload"]) if item["result_payload"] else None
        )
        item["side_effect_result_payload"] = (
            json.loads(item["side_effect_result_payload"])
            if item.get("side_effect_result_payload")
            else None
        )
        item["cancel_requested"] = bool(item["cancel_requested"])
        return item

    @staticmethod
    def _schedule_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["trading_days_only"] = bool(item["trading_days_only"])
        item["enabled"] = bool(item["enabled"])
        item["dependency_names"] = json.loads(item["dependency_names"])
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _schedule_run_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["is_backfill"] = bool(item["is_backfill"])
        item["payload"] = json.loads(item["payload"])
        return item


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["JobRepository", "RUNTIME_SCHEMA_VERSION"]
