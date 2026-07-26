from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantlab.domain.thesis import normalize_check_frequency
from quantlab.persistence.migrations import record_component_migration
from quantlab.persistence.round8 import Round8Repository
from quantlab.security import sanitize_for_export


ROUND9_SCHEMA_VERSION = 4


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            sanitize_for_export(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(sanitize_for_export(value), ensure_ascii=False, default=str)


def _unique_strings(values: Any) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


class Round9Repository:
    """Round-nine lifecycle, audit-export, memory-use and decision-task ledger."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        Round8Repository(self.path)
        self._init_schema()
        record_component_migration(
            self.path,
            component="round9",
            version=1,
            migration_identity=(
                "round9-thesis-revisions-decision-audit-memory-usage-task-center-v1"
            ),
        )
        record_component_migration(
            self.path,
            component="round9",
            version=2,
            migration_identity=(
                "round9-run-resume-events-idempotent-audit-export-v2"
            ),
        )
        record_component_migration(
            self.path,
            component="round9",
            version=3,
            migration_identity=(
                "round9-tail-thesis-freeze-provider-acceptance-scheduling-task-reconciliation-v3"
            ),
        )
        if not self._migration_applied(4):
            self._migrate_frozen_revision_reapply()
        record_component_migration(
            self.path,
            component="round9",
            version=4,
            migration_identity=(
                "round9-frozen-revision-payload-reapply-and-task-recovery-v4"
            ),
        )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _init_schema(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS investment_thesis_revisions (
                    revision_id TEXT PRIMARY KEY,
                    thesis_id TEXT NOT NULL,
                    revision_number INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('draft','frozen','superseded')),
                    source TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    supersedes_revision_id TEXT,
                    edited_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    frozen_at TEXT,
                    applied_at TEXT,
                    fingerprint TEXT NOT NULL,
                    UNIQUE(thesis_id,revision_number),
                    UNIQUE(thesis_id,fingerprint),
                    FOREIGN KEY(thesis_id) REFERENCES investment_theses(thesis_id)
                );
                CREATE INDEX IF NOT EXISTS idx_thesis_revision_latest
                  ON investment_thesis_revisions(thesis_id,revision_number DESC);

                CREATE TABLE IF NOT EXISTS decision_run_memory_usage (
                    run_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    applied_weight REAL NOT NULL,
                    ablation_arm TEXT NOT NULL,
                    used_at TEXT NOT NULL,
                    PRIMARY KEY(run_id,memory_id,ablation_arm),
                    FOREIGN KEY(run_id) REFERENCES unified_experiment_runs(run_id),
                    FOREIGN KEY(memory_id) REFERENCES controlled_research_memories(memory_id)
                );

                CREATE TABLE IF NOT EXISTS decision_tasks (
                    task_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('open','acknowledged','resolved','dismissed')),
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    user_summary TEXT NOT NULL,
                    diagnostic_detail TEXT,
                    account_id TEXT,
                    symbol TEXT,
                    decision_run_id TEXT,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    dedup_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    resolved_at TEXT,
                    management_source TEXT NOT NULL DEFAULT 'system_managed',
                    condition_fingerprint TEXT NOT NULL DEFAULT '',
                    resolved_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_decision_tasks_status_severity
                  ON decision_tasks(status,severity,updated_at DESC);

                CREATE TABLE IF NOT EXISTS decision_task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    previous_status TEXT,
                    new_status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    actor TEXT NOT NULL CHECK(actor IN ('system','user')),
                    evidence_fingerprint TEXT,
                    changed_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES decision_tasks(task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_decision_task_events_task
                  ON decision_task_events(task_id,changed_at);

                CREATE TABLE IF NOT EXISTS thesis_revision_assumptions (
                    revision_id TEXT NOT NULL,
                    assumption_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    PRIMARY KEY(revision_id,assumption_id),
                    FOREIGN KEY(revision_id) REFERENCES investment_thesis_revisions(revision_id)
                );

                CREATE TABLE IF NOT EXISTS decision_run_exports (
                    export_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    artifact_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id,artifact_fingerprint),
                    FOREIGN KEY(run_id) REFERENCES unified_experiment_runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS unified_run_resume_events (
                    resume_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    previous_status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    resumed_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES unified_experiment_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_run_resume_events
                  ON unified_run_resume_events(run_id,resumed_at);

                CREATE TABLE IF NOT EXISTS historical_pit_scorecards (
                    scorecard_id TEXT PRIMARY KEY,
                    scorecard_name TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    data_cutoff TEXT NOT NULL,
                    execution_date TEXT NOT NULL,
                    provider_manifest TEXT NOT NULL,
                    dataset_fingerprint TEXT NOT NULL,
                    candidate_universe TEXT NOT NULL,
                    metrics TEXT NOT NULL,
                    evidence_boundary TEXT NOT NULL CHECK(evidence_boundary='research_only'),
                    payload TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(db, "investment_thesis_revisions", "applied_at", "TEXT")
            self._ensure_column(
                db, "investment_theses", "current_frozen_revision_id", "TEXT"
            )
            self._ensure_column(db, "thesis_assumptions", "last_checked_at", "TEXT")
            self._ensure_column(db, "thesis_assumptions", "next_check_at", "TEXT")
            self._ensure_column(db, "thesis_assumptions", "active_revision_id", "TEXT")
            self._ensure_column(db, "thesis_checks", "frozen_revision_id", "TEXT")
            self._ensure_column(
                db, "thesis_checks", "frozen_revision_fingerprint", "TEXT"
            )
            self._ensure_column(
                db, "thesis_checks", "schedule_status", "TEXT NOT NULL DEFAULT 'not_applied'"
            )
            self._ensure_column(
                db,
                "thesis_checks",
                "schedule_update_applied",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(db, "thesis_checks", "next_check_at", "TEXT")
            for column, definition in (
                ("market_date", "TEXT"),
                ("status", "TEXT"),
                ("capability", "TEXT"),
                ("source_version", "TEXT"),
                ("manifest_id", "TEXT"),
                ("pool_snapshot_id", "TEXT"),
                ("pool_fingerprint", "TEXT"),
            ):
                self._ensure_column(db, "provider_refresh_selections", column, definition)
            self._ensure_column(db, "pit_pool_snapshots", "refresh_id", "TEXT")
            for column, definition in (
                ("management_source", "TEXT NOT NULL DEFAULT 'system_managed'"),
                ("condition_fingerprint", "TEXT NOT NULL DEFAULT ''"),
                ("resolved_reason", "TEXT"),
            ):
                self._ensure_column(db, "decision_tasks", column, definition)
            self._migrate_tail_closure(db)

    @staticmethod
    def _ensure_column(
        db: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            return
        columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        if column not in columns:
            db.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')

    def _migrate_tail_closure(self, db: sqlite3.Connection) -> None:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='investment_theses'"
        ).fetchone():
            return
        theses = db.execute("SELECT * FROM investment_theses ORDER BY created_at").fetchall()
        for thesis in theses:
            revisions = db.execute(
                """SELECT * FROM investment_thesis_revisions
                   WHERE thesis_id=? ORDER BY revision_number""",
                (thesis["thesis_id"],),
            ).fetchall()
            frozen = [row for row in revisions if row["status"] == "frozen"]
            if not revisions and thesis["status"] != "draft_pending_confirmation":
                assumptions = db.execute(
                    "SELECT * FROM thesis_assumptions WHERE thesis_id=? ORDER BY ordinal",
                    (thesis["thesis_id"],),
                ).fetchall()
                payload = {
                    "schema_version": "investment-thesis-legacy-frozen-v1",
                    "thesis_id": thesis["thesis_id"],
                    "symbol": thesis["symbol"],
                    "core_logic": thesis["core_thesis"],
                    "assumptions": [
                        {
                            "statement": row["statement"],
                            "verification_metric": row["verification_metric"],
                            "current_evidence": row["current_evidence"],
                            "supporting_evidence_refs": json.loads(row["evidence_refs"] or "[]"),
                            "opposing_evidence_refs": [],
                            "check_frequency": row["check_frequency"],
                            "next_check_at": thesis["next_check_at"],
                            "red_lines": [],
                            "invalidation_conditions": [],
                        }
                        for row in assumptions
                    ],
                    "valuation_anchor": thesis["valuation_anchor"] or "legacy unavailable",
                    "overall_red_lines": json.loads(thesis["red_lines"] or "[]"),
                    "overall_invalidation_conditions": json.loads(
                        thesis["invalidation_conditions"] or "[]"
                    ),
                    "data_provenance": json.loads(thesis["data_provenance"] or "{}"),
                    "legacy_migrated": True,
                }
                revision_id = str(uuid.uuid4())
                fingerprint = _fingerprint(payload)
                frozen_at = thesis["created_at"] or _now()
                db.execute(
                    """INSERT INTO investment_thesis_revisions(
                           revision_id,thesis_id,revision_number,status,source,payload,
                           supersedes_revision_id,edited_by,created_at,frozen_at,applied_at,fingerprint
                       ) VALUES(?,?,1,'frozen','legacy_migration',?,?,?,?,?,?,?)""",
                    (
                        revision_id,
                        thesis["thesis_id"],
                        json.dumps(payload, ensure_ascii=False),
                        None,
                        "system",
                        thesis["created_at"] or _now(),
                        frozen_at,
                        frozen_at,
                        fingerprint,
                    ),
                )
                frozen = [
                    db.execute(
                        "SELECT * FROM investment_thesis_revisions WHERE revision_id=?",
                        (revision_id,),
                    ).fetchone()
                ]
            if frozen:
                current = max(frozen, key=lambda row: int(row["revision_number"]))
                db.execute(
                    """UPDATE investment_thesis_revisions SET status='superseded'
                       WHERE thesis_id=? AND status='frozen' AND revision_id<>?""",
                    (thesis["thesis_id"], current["revision_id"]),
                )
                status = str(thesis["status"])
                if status == "draft_pending_confirmation":
                    status = "active"
                db.execute(
                    """UPDATE investment_theses
                       SET current_frozen_revision_id=?,status=? WHERE thesis_id=?""",
                    (current["revision_id"], status, thesis["thesis_id"]),
                )
                db.execute(
                    """UPDATE thesis_assumptions
                       SET active_revision_id=?,
                           next_check_at=CASE
                             WHEN LOWER(check_frequency) IN ('event_driven','manual')
                               THEN next_check_at
                             ELSE COALESCE(next_check_at,?)
                           END
                       WHERE thesis_id=? AND active_revision_id IS NULL""",
                    (current["revision_id"], thesis["next_check_at"], thesis["thesis_id"]),
                )
            elif revisions or thesis["status"] == "draft_pending_confirmation":
                db.execute(
                    """UPDATE investment_theses
                       SET current_frozen_revision_id=NULL,status='draft_pending_confirmation',
                           next_check_at=NULL WHERE thesis_id=?""",
                    (thesis["thesis_id"],),
                )

    def _migration_applied(self, version: int) -> bool:
        with self.connect() as db:
            table = db.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='quantlab_migration_registry'"""
            ).fetchone()
            if table is None:
                return False
            return bool(
                db.execute(
                    """SELECT 1 FROM quantlab_migration_registry
                       WHERE component='round9' AND version=?""",
                    (version,),
                ).fetchone()
            )

    def _migrate_frozen_revision_reapply(self) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            theses = db.execute(
                "SELECT * FROM investment_theses ORDER BY created_at,thesis_id"
            ).fetchall()
            for thesis in theses:
                revisions = db.execute(
                    """SELECT * FROM investment_thesis_revisions
                       WHERE thesis_id=? AND (status='frozen' OR frozen_at IS NOT NULL)
                       ORDER BY revision_number DESC""",
                    (thesis["thesis_id"],),
                ).fetchall()
                current: sqlite3.Row | None = None
                for revision in revisions:
                    try:
                        self._revision_payload_for_apply(revision)
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    current = revision
                    break
                if current is None:
                    db.execute(
                        """UPDATE investment_theses
                           SET current_frozen_revision_id=NULL,
                               status='draft_pending_confirmation',next_check_at=NULL
                           WHERE thesis_id=?""",
                        (thesis["thesis_id"],),
                    )
                    continue
                db.execute(
                    """UPDATE investment_thesis_revisions
                       SET status=CASE WHEN revision_id=? THEN 'frozen' ELSE 'superseded' END
                       WHERE thesis_id=? AND (status='frozen' OR frozen_at IS NOT NULL)""",
                    (current["revision_id"], thesis["thesis_id"]),
                )
                self._apply_frozen_revision_in_tx(db, current, thesis)

    def resume_decision_run(self, run_id: str, *, reason: str) -> dict[str, Any]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM unified_experiment_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise ValueError("decision run not found")
            if row["status"] == "running":
                return Round8Repository(self.path).run(run_id) or {}
            if row["status"] == "completed":
                raise ValueError("completed decision run cannot be resumed")
            if row["status"] not in {"failed", "cancelled", "blocked"}:
                raise ValueError("decision run status cannot be resumed")
            resumed_at = _now()
            db.execute(
                "INSERT INTO unified_run_resume_events VALUES(?,?,?,?,?)",
                (str(uuid.uuid4()), run_id, row["status"], reason, resumed_at),
            )
            db.execute(
                """UPDATE unified_experiment_runs
                   SET status='running',completed_at=NULL,updated_at=? WHERE run_id=?""",
                (resumed_at, run_id),
            )
        return Round8Repository(self.path).run(run_id) or {}

    def run_resume_events(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM unified_run_resume_events
                   WHERE run_id=? ORDER BY resumed_at""",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def link_pool_refresh(self, snapshot_id: str, refresh_id: str) -> dict[str, Any]:
        with self.connect() as db:
            self._ensure_column(db, "pit_pool_snapshots", "refresh_id", "TEXT")
            db.execute(
                "UPDATE pit_pool_snapshots SET refresh_id=? WHERE snapshot_id=?",
                (refresh_id, snapshot_id),
            )
            row = db.execute(
                "SELECT * FROM pit_pool_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise ValueError("point-in-time pool snapshot not found")
        item = dict(row)
        item["known_gaps"] = json.loads(item["known_gaps"] or "[]")
        return item

    def create_thesis_revision(
        self,
        thesis_id: str,
        *,
        payload: dict[str, Any],
        source: str,
        edited_by: str,
    ) -> dict[str, Any]:
        if Round8Repository(self.path).thesis(thesis_id) is None:
            raise ValueError("investment thesis not found")
        sanitized = self._normalize_revision_payload(sanitize_for_export(payload))
        fingerprint = _fingerprint(sanitized)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                """SELECT * FROM investment_thesis_revisions
                   WHERE thesis_id=? AND fingerprint=?""",
                (thesis_id, fingerprint),
            ).fetchone()
            if existing:
                return self._revision_row(existing, idempotent=True)
            latest = db.execute(
                """SELECT * FROM investment_thesis_revisions
                   WHERE thesis_id=? ORDER BY revision_number DESC LIMIT 1""",
                (thesis_id,),
            ).fetchone()
            number = int(latest["revision_number"] if latest else 0) + 1
            revision_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO investment_thesis_revisions(
                       revision_id,thesis_id,revision_number,status,source,payload,
                       supersedes_revision_id,edited_by,created_at,fingerprint
                   ) VALUES(?,?,?,'draft',?,?,?,?,?,?)""",
                (
                    revision_id,
                    thesis_id,
                    number,
                    source,
                    json.dumps(sanitized, ensure_ascii=False),
                    latest["revision_id"] if latest else None,
                    edited_by,
                    _now(),
                    fingerprint,
                ),
            )
            row = db.execute(
                "SELECT * FROM investment_thesis_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
        return self._revision_row(row, idempotent=False)

    def freeze_thesis_revision(
        self, revision_id: str, *, thesis_id: str | None = None
    ) -> dict[str, Any]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM investment_thesis_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
            if row is None:
                raise ValueError("investment thesis revision not found")
            if thesis_id is not None and row["thesis_id"] != thesis_id:
                raise ValueError("investment thesis revision does not belong to thesis")
            thesis = db.execute(
                "SELECT * FROM investment_theses WHERE thesis_id=?",
                (row["thesis_id"],),
            ).fetchone()
            if thesis is None:
                raise ValueError("investment thesis not found")
            if (
                row["status"] == "frozen"
                and thesis["current_frozen_revision_id"] == revision_id
            ):
                return self._revision_row(row, idempotent=True)
            if row["status"] != "draft":
                raise ValueError("only a draft thesis revision can be frozen")
            payload, assumptions = self._revision_payload_for_apply(row)
            now = _now()
            db.execute(
                """UPDATE investment_thesis_revisions
                   SET status='superseded'
                   WHERE thesis_id=? AND status='frozen' AND revision_id<>?""",
                (row["thesis_id"], revision_id),
            )
            db.execute(
                """UPDATE investment_thesis_revisions
                   SET status='frozen',frozen_at=?,applied_at=?
                   WHERE revision_id=? AND status='draft'""",
                (now, now, revision_id),
            )
            self._apply_frozen_revision_in_tx(db, row, thesis, payload=payload)
            saved = db.execute(
                "SELECT * FROM investment_thesis_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
        return self._revision_row(saved, idempotent=False)

    @staticmethod
    def _normalize_revision_payload(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        assumptions = []
        for assumption in list(normalized.get("assumptions") or []):
            item = dict(assumption)
            item["check_frequency"] = normalize_check_frequency(
                item.get("check_frequency")
            )
            if item["check_frequency"] in {"event_driven", "manual"}:
                item["next_check_at"] = None
            assumptions.append(item)
        normalized["assumptions"] = assumptions
        return normalized

    def _revision_payload_for_apply(
        self, revision: sqlite3.Row
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        raw = json.loads(revision["payload"])
        if not isinstance(raw, dict):
            raise ValueError("frozen thesis revision payload must be an object")
        payload = self._normalize_revision_payload(raw)
        assumptions = list(payload.get("assumptions") or [])
        if not 3 <= len(assumptions) <= 7:
            raise ValueError("frozen thesis revision requires 3 to 7 assumptions")
        if any(
            not str(item.get("statement") or "").strip()
            or not str(item.get("verification_metric") or "").strip()
            for item in assumptions
        ):
            raise ValueError("frozen thesis revision assumptions are incomplete")
        return payload, assumptions

    def _apply_frozen_revision_in_tx(
        self,
        db: sqlite3.Connection,
        revision: sqlite3.Row,
        thesis: sqlite3.Row,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        resolved_payload, assumptions = (
            (payload, list(payload.get("assumptions") or []))
            if payload is not None
            else self._revision_payload_for_apply(revision)
        )
        revision_id = str(revision["revision_id"])
        supporting = _unique_strings(
            value
            for assumption in assumptions
            for value in assumption.get("supporting_evidence_refs") or []
        )
        opposing = _unique_strings(
            value
            for assumption in assumptions
            for value in assumption.get("opposing_evidence_refs") or []
        )
        red_lines = _unique_strings(
            [
                *(resolved_payload.get("overall_red_lines") or []),
                *(
                    value
                    for assumption in assumptions
                    for value in assumption.get("red_lines") or []
                ),
            ]
        )
        invalidation = _unique_strings(
            [
                *(resolved_payload.get("overall_invalidation_conditions") or []),
                *(
                    value
                    for assumption in assumptions
                    for value in assumption.get("invalidation_conditions") or []
                ),
            ]
        )
        assumption_due_dates = sorted(
            str(item.get("next_check_at"))
            for item in assumptions
            if item.get("next_check_at")
            and item["check_frequency"] not in {"event_driven", "manual"}
        )
        next_check_at = assumption_due_dates[0] if assumption_due_dates else None
        applied = {
            "core_thesis": str(
                resolved_payload.get("core_logic") or thesis["core_thesis"]
            ),
            "supporting_evidence": supporting,
            "opposing_evidence": opposing,
            "red_lines": red_lines,
            "invalidation_conditions": invalidation,
            "valuation_anchor": resolved_payload.get("valuation_anchor"),
            "next_check_at": next_check_at,
            "data_provenance": resolved_payload.get("data_provenance") or {},
        }
        current_status = str(thesis["status"])
        next_status = (
            "active"
            if current_status == "draft_pending_confirmation"
            else current_status
            if current_status
            in {
                "active",
                "strengthened",
                "unchanged",
                "weakened",
                "damaged",
                "broken",
                "closed",
            }
            else "active"
        )
        thesis_fingerprint = _fingerprint(
            {
                "revision_id": revision_id,
                "revision_fingerprint": revision["fingerprint"],
                "applied": applied,
                "status": next_status,
            }
        )
        db.execute(
            """UPDATE investment_theses SET
                 current_frozen_revision_id=?,status=?,core_thesis=?,
                 supporting_evidence=?,opposing_evidence=?,red_lines=?,
                 invalidation_conditions=?,valuation_anchor=?,next_check_at=?,
                 data_provenance=?,thesis_fingerprint=?
               WHERE thesis_id=?""",
            (
                revision_id,
                next_status,
                applied["core_thesis"],
                json.dumps(supporting, ensure_ascii=False),
                json.dumps(opposing, ensure_ascii=False),
                json.dumps(red_lines, ensure_ascii=False),
                json.dumps(invalidation, ensure_ascii=False),
                applied["valuation_anchor"],
                next_check_at,
                json.dumps(applied["data_provenance"], ensure_ascii=False),
                thesis_fingerprint,
                thesis["thesis_id"],
            ),
        )
        existing_assumptions = {
            int(item["ordinal"]): item
            for item in db.execute(
                "SELECT * FROM thesis_assumptions WHERE thesis_id=?",
                (thesis["thesis_id"],),
            ).fetchall()
        }
        active_ids: list[str] = []
        for ordinal, assumption in enumerate(assumptions, start=1):
            item = sanitize_for_export(assumption)
            assumption_id = str(
                existing_assumptions[ordinal]["assumption_id"]
                if ordinal in existing_assumptions
                else uuid.uuid4()
            )
            active_ids.append(assumption_id)
            evidence_refs = _unique_strings(
                [
                    *(item.get("supporting_evidence_refs") or []),
                    *(item.get("opposing_evidence_refs") or []),
                ]
            )
            current_evidence = item.get("current_evidence") or "unavailable"
            values = (
                item["statement"],
                item["verification_metric"],
                _text_value(current_evidence),
                item["check_frequency"],
                json.dumps(evidence_refs, ensure_ascii=False),
                _fingerprint(item),
                item.get("next_check_at"),
                revision_id,
            )
            if ordinal in existing_assumptions:
                db.execute(
                    """UPDATE thesis_assumptions SET statement=?,verification_metric=?,
                         current_evidence=?,status='needs_review',check_frequency=?,
                         evidence_refs=?,fingerprint=?,last_checked_at=NULL,
                         next_check_at=?,active_revision_id=? WHERE assumption_id=?""",
                    (*values, assumption_id),
                )
            else:
                db.execute(
                    """INSERT INTO thesis_assumptions(
                         assumption_id,thesis_id,ordinal,statement,verification_metric,
                         current_evidence,status,check_frequency,evidence_refs,fingerprint,
                         last_checked_at,next_check_at,active_revision_id
                       ) VALUES(?,?,?,?,?,?,'needs_review',?,?,?,?,?,?)""",
                    (
                        assumption_id,
                        thesis["thesis_id"],
                        ordinal,
                        item["statement"],
                        item["verification_metric"],
                        _text_value(current_evidence),
                        item["check_frequency"],
                        json.dumps(evidence_refs, ensure_ascii=False),
                        _fingerprint(item),
                        None,
                        item.get("next_check_at"),
                        revision_id,
                    ),
                )
            db.execute(
                """INSERT OR REPLACE INTO thesis_revision_assumptions(
                     revision_id,assumption_id,ordinal,payload,fingerprint
                   ) VALUES(?,?,?,?,?)""",
                (
                    revision_id,
                    assumption_id,
                    ordinal,
                    json.dumps(item, ensure_ascii=False),
                    _fingerprint(item),
                ),
            )
        placeholders = ",".join("?" for _ in active_ids)
        db.execute(
            f"""UPDATE thesis_assumptions SET status='superseded'
                WHERE thesis_id=? AND assumption_id NOT IN ({placeholders})""",
            (thesis["thesis_id"], *active_ids),
        )
        db.execute(
            """UPDATE investment_thesis_revisions
               SET applied_at=COALESCE(applied_at,?) WHERE revision_id=?""",
            (_now(), revision_id),
        )

    def thesis_revisions(self, thesis_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM investment_thesis_revisions
                   WHERE thesis_id=? ORDER BY revision_number""",
                (thesis_id,),
            ).fetchall()
        return [self._revision_row(row) for row in rows]

    def record_memory_usage(
        self,
        run_id: str,
        memories: list[dict[str, Any]],
        *,
        ablation_arm: str,
    ) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in memories:
                db.execute(
                    """INSERT OR IGNORE INTO decision_run_memory_usage(
                           run_id,memory_id,symbol,scope,applied_weight,ablation_arm,used_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        item["memory_id"],
                        item["symbol"],
                        item["scope"],
                        float(item["weight"]),
                        ablation_arm,
                        _now(),
                    ),
                )

    def memory_usage(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM decision_run_memory_usage
                   WHERE run_id=? ORDER BY used_at,memory_id""",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_decision_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        sanitized = sanitize_for_export(payload)
        dedup_key = str(sanitized["dedup_key"])
        now = _now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM decision_tasks WHERE dedup_key=?",
                (dedup_key,),
            ).fetchone()
            task_id = str(existing["task_id"]) if existing else str(uuid.uuid4())
            management_source = str(
                sanitized.get("management_source") or "system_managed"
            )
            if (
                existing
                and existing["management_source"] != "system_managed"
                and management_source == "system_managed"
            ):
                return self._task_row(existing)
            condition_fingerprint = str(
                sanitized.get("condition_fingerprint")
                or _fingerprint(
                    {
                        "source_type": sanitized["source_type"],
                        "source_id": sanitized["source_id"],
                        "task_type": sanitized["task_type"],
                        "payload": sanitized.get("payload") or {},
                    }
                )
            )
            previous_status = str(existing["status"]) if existing else None
            status = previous_status or "open"
            reopen_reason: str | None = None
            if (
                existing
                and existing["management_source"] == "system_managed"
                and management_source == "system_managed"
            ):
                if previous_status == "resolved":
                    status = "open"
                    reopen_reason = "source_condition_recurred"
                elif (
                    previous_status == "dismissed"
                    and str(existing["condition_fingerprint"] or "")
                    != condition_fingerprint
                ):
                    status = "open"
                    reopen_reason = "source_condition_changed"
            db.execute(
                """INSERT INTO decision_tasks(
                       task_id,category,task_type,status,severity,title,user_summary,
                       diagnostic_detail,account_id,symbol,decision_run_id,source_type,
                       source_id,dedup_key,payload,created_at,updated_at,management_source,
                       condition_fingerprint
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(dedup_key) DO UPDATE SET
                     category=excluded.category,task_type=excluded.task_type,status=excluded.status,
                     severity=excluded.severity,title=excluded.title,
                     user_summary=excluded.user_summary,
                     diagnostic_detail=excluded.diagnostic_detail,
                     account_id=excluded.account_id,symbol=excluded.symbol,
                     decision_run_id=excluded.decision_run_id,payload=excluded.payload,
                     management_source=excluded.management_source,
                     condition_fingerprint=excluded.condition_fingerprint,
                     resolved_at=CASE WHEN excluded.status='open' THEN NULL ELSE decision_tasks.resolved_at END,
                     resolved_reason=CASE WHEN excluded.status='open' THEN NULL ELSE decision_tasks.resolved_reason END,
                     updated_at=excluded.updated_at""",
                (
                    task_id,
                    sanitized["category"],
                    sanitized["task_type"],
                    status,
                    sanitized.get("severity") or "info",
                    sanitized["title"],
                    sanitized["user_summary"],
                    sanitized.get("diagnostic_detail"),
                    sanitized.get("account_id"),
                    sanitized.get("symbol"),
                    sanitized.get("decision_run_id"),
                    sanitized["source_type"],
                    sanitized["source_id"],
                    dedup_key,
                    json.dumps(sanitized.get("payload") or {}, ensure_ascii=False),
                    now,
                    now,
                    management_source,
                    condition_fingerprint,
                ),
            )
            if existing is None:
                self._record_task_event_in_tx(
                    db,
                    task_id=task_id,
                    previous_status=None,
                    new_status="open",
                    reason="source_condition_detected",
                    actor="system" if management_source == "system_managed" else "user",
                    evidence_fingerprint=condition_fingerprint,
                )
            elif reopen_reason:
                self._record_task_event_in_tx(
                    db,
                    task_id=task_id,
                    previous_status=previous_status,
                    new_status="open",
                    reason=reopen_reason,
                    actor="system",
                    evidence_fingerprint=condition_fingerprint,
                )
            row = db.execute(
                "SELECT * FROM decision_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._task_row(row)

    def update_task_status(
        self,
        task_id: str,
        status: str,
        *,
        reason: str = "user_status_update",
        actor: str = "user",
        evidence_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"open", "acknowledged", "resolved", "dismissed"}:
            raise ValueError("invalid decision task status")
        now = _now()
        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM decision_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if existing is None:
                raise ValueError("decision task not found")
            db.execute(
                """UPDATE decision_tasks SET status=?,updated_at=?,
                     acknowledged_at=CASE WHEN ?='acknowledged' THEN ? ELSE acknowledged_at END,
                     resolved_at=CASE
                       WHEN ? IN ('resolved','dismissed') THEN ?
                       WHEN ?='open' THEN NULL ELSE resolved_at END,
                     resolved_reason=CASE
                       WHEN ? IN ('resolved','dismissed') THEN ?
                       WHEN ?='open' THEN NULL ELSE resolved_reason END
                   WHERE task_id=?""",
                (
                    status,
                    now,
                    status,
                    now,
                    status,
                    now,
                    status,
                    status,
                    reason,
                    status,
                    task_id,
                ),
            )
            if status != existing["status"]:
                self._record_task_event_in_tx(
                    db,
                    task_id=task_id,
                    previous_status=str(existing["status"]),
                    new_status=status,
                    reason=reason,
                    actor=actor,
                    evidence_fingerprint=evidence_fingerprint
                    or existing["condition_fingerprint"],
                )
            row = db.execute(
                "SELECT * FROM decision_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._task_row(row)

    def reconcile_system_tasks(
        self,
        *,
        active_dedup_keys: set[str],
        task_types: set[str],
    ) -> list[dict[str, Any]]:
        if not task_types:
            return []
        placeholders = ",".join("?" for _ in task_types)
        resolved: list[dict[str, Any]] = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                f"""SELECT * FROM decision_tasks
                    WHERE management_source='system_managed'
                      AND task_type IN ({placeholders})
                      AND status IN ('open','acknowledged')""",
                tuple(sorted(task_types)),
            ).fetchall()
            now = _now()
            for row in rows:
                if row["dedup_key"] in active_dedup_keys:
                    continue
                db.execute(
                    """UPDATE decision_tasks SET status='resolved',updated_at=?,resolved_at=?,
                         resolved_reason='source_condition_cleared' WHERE task_id=?""",
                    (now, now, row["task_id"]),
                )
                self._record_task_event_in_tx(
                    db,
                    task_id=row["task_id"],
                    previous_status=str(row["status"]),
                    new_status="resolved",
                    reason="source_condition_cleared",
                    actor="system",
                    evidence_fingerprint=row["condition_fingerprint"],
                )
                saved = db.execute(
                    "SELECT * FROM decision_tasks WHERE task_id=?", (row["task_id"],)
                ).fetchone()
                resolved.append(self._task_row(saved))
        return resolved

    def resolve_source_tasks(
        self,
        *,
        source_type: str,
        source_id: str,
        task_type: str | None = None,
        reason: str,
    ) -> list[dict[str, Any]]:
        clauses = [
            "management_source='system_managed'",
            "source_type=?",
            "source_id=?",
            "status IN ('open','acknowledged')",
        ]
        params: list[Any] = [source_type, source_id]
        if task_type:
            clauses.append("task_type=?")
            params.append(task_type)
        with self.connect() as db:
            rows = db.execute(
                "SELECT task_id FROM decision_tasks WHERE " + " AND ".join(clauses), params
            ).fetchall()
        return [
            self.update_task_status(
                str(row["task_id"]), status="resolved", reason=reason, actor="system"
            )
            for row in rows
        ]

    def decision_task_events(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM decision_task_events
                   WHERE task_id=? ORDER BY changed_at,event_id""",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _record_task_event_in_tx(
        db: sqlite3.Connection,
        *,
        task_id: str,
        previous_status: str | None,
        new_status: str,
        reason: str,
        actor: str,
        evidence_fingerprint: str | None,
    ) -> None:
        db.execute(
            """INSERT INTO decision_task_events(
                 event_id,task_id,previous_status,new_status,reason,actor,
                 evidence_fingerprint,changed_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()),
                task_id,
                previous_status,
                new_status,
                reason,
                actor,
                evidence_fingerprint,
                _now(),
            ),
        )

    def decision_tasks(
        self,
        *,
        status: str | None = None,
        account_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if account_id:
            clauses.append("account_id=?")
            params.append(account_id)
        query = "SELECT * FROM decision_tasks"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += (
            " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,"
            " updated_at DESC LIMIT ?"
        )
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._task_row(row) for row in rows]

    def export_decision_run(self, run_id: str) -> dict[str, Any]:
        round8 = Round8Repository(self.path)
        run = round8.run(run_id)
        if run is None:
            raise ValueError("decision run not found")
        child_runs = self._child_runs(run_id)
        linked = self._linked_entities([run_id, *[item["run_id"] for item in child_runs]])
        run_ids = [run_id, *[item["run_id"] for item in child_runs]]
        bundle = sanitize_for_export(
            {
                "schema_version": "decision-run-audit-bundle-v1",
                "decision_run": run,
                "child_runs": child_runs,
                "linked_entities": linked,
                "entity_snapshots": self._linked_entity_snapshots(linked),
                "memory_usage": self.memory_usage(run_id),
                "resume_events": {
                    item: self.run_resume_events(item) for item in run_ids
                },
                "checkpoints": self._checkpoints(run_ids),
                "secrets_redacted": True,
            }
        )
        artifact_fingerprint = _fingerprint(bundle)
        with self.connect() as db:
            existing = db.execute(
                """SELECT * FROM decision_run_exports
                   WHERE run_id=? AND artifact_fingerprint=?""",
                (run_id, artifact_fingerprint),
            ).fetchone()
            if existing is not None:
                return {"export_id": str(existing["export_id"]), **json.loads(existing["payload"])}
            export_id = str(uuid.uuid4())
            exported = {
                **bundle,
                "artifact_fingerprint": artifact_fingerprint,
                "exported_at": _now(),
            }
            db.execute(
                "INSERT INTO decision_run_exports VALUES(?,?,?,?,?)",
                (
                    export_id,
                    run_id,
                    json.dumps(exported, ensure_ascii=False),
                    artifact_fingerprint,
                    exported["exported_at"],
                ),
            )
        return {"export_id": export_id, **exported}

    def save_historical_scorecard(self, payload: dict[str, Any]) -> dict[str, Any]:
        sanitized = sanitize_for_export(payload)
        sanitized["evidence_boundary"] = "research_only"
        fingerprint = _fingerprint(sanitized)
        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM historical_pit_scorecards WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if existing:
                return self._scorecard_row(existing)
            scorecard_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO historical_pit_scorecards VALUES(
                       ?,?,?,?,?,?,?,?,?,?,?,?,?
                   )""",
                (
                    scorecard_id,
                    sanitized["scorecard_name"],
                    sanitized["signal_date"],
                    sanitized["data_cutoff"],
                    sanitized["execution_date"],
                    json.dumps(sanitized.get("provider_manifest") or {}, ensure_ascii=False),
                    sanitized["dataset_fingerprint"],
                    json.dumps(sanitized.get("candidate_universe") or [], ensure_ascii=False),
                    json.dumps(sanitized.get("metrics") or {}, ensure_ascii=False),
                    "research_only",
                    json.dumps(sanitized, ensure_ascii=False),
                    fingerprint,
                    _now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM historical_pit_scorecards WHERE scorecard_id=?",
                (scorecard_id,),
            ).fetchone()
        return self._scorecard_row(row)

    def historical_scorecards(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM historical_pit_scorecards
                   ORDER BY signal_date DESC,created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._scorecard_row(row) for row in rows]

    def _child_runs(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT run_id FROM unified_experiment_runs
                   WHERE parent_run_id=? ORDER BY started_at""",
                (run_id,),
            ).fetchall()
        round8 = Round8Repository(self.path)
        return [item for row in rows if (item := round8.run(str(row["run_id"]))) is not None]

    def _linked_entities(self, run_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        placeholders = ",".join("?" for _ in run_ids)
        with self.connect() as db:
            links = db.execute(
                f"SELECT * FROM unified_run_links WHERE run_id IN ({placeholders}) ORDER BY created_at",
                run_ids,
            ).fetchall()
            output: dict[str, list[dict[str, Any]]] = {}
            for link in links:
                output.setdefault(str(link["entity_type"]), []).append(dict(link))
        return output

    def _checkpoints(self, run_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        placeholders = ",".join("?" for _ in run_ids)
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT * FROM research_step_checkpoints
                    WHERE run_id IN ({placeholders}) ORDER BY run_id,created_at""",
                run_ids,
            ).fetchall()
        output: dict[str, list[dict[str, Any]]] = {run_id: [] for run_id in run_ids}
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            output[str(item["run_id"])].append(item)
        return output

    def _linked_entity_snapshots(
        self, linked: dict[str, list[dict[str, Any]]]
    ) -> dict[str, list[dict[str, Any]]]:
        table_map = {
            "decision_run": ("decision_runs", "run_id"),
            "investment_thesis_revision": ("investment_thesis_revisions", "revision_id"),
            "outcome_reflection": ("outcome_reflections", "reflection_id"),
            "user_paper_order": ("user_paper_orders", "order_id"),
            "pretrade_check": ("user_trade_decision_links", "check_id"),
            "investor_recommendation": ("investor_recommendations", "recommendation_id"),
            "forward_prediction": ("forward_ablation_predictions", "prediction_id"),
            "shadow_account": ("shadow_accounts", "account_id"),
            "chat_conversation": ("chat_conversations", "conversation_id"),
        }
        snapshots: dict[str, list[dict[str, Any]]] = {}
        round8 = Round8Repository(self.path)
        for entity_type, links in linked.items():
            for link in links:
                entity_id = str(link["entity_id"])
                snapshot: dict[str, Any] | None = None
                if entity_type in {"context", "context_pack", "analysis_context_pack"}:
                    from quantlab.persistence.evidence import EvidenceRepository

                    snapshot = EvidenceRepository(self.path).context(entity_id)
                elif entity_type == "investment_thesis":
                    snapshot = round8.thesis(entity_id)
                elif entity_type in table_map:
                    table, key = table_map[entity_type]
                    snapshot = self._safe_table_record(table, key, entity_id)
                snapshots.setdefault(entity_type, []).append(
                    {
                        "entity_id": entity_id,
                        "relation": link["relation"],
                        "status": "available" if snapshot is not None else "unavailable",
                        "record": snapshot,
                    }
                )
        return snapshots

    def _safe_table_record(
        self, table: str, key_column: str, entity_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone():
                return None
            row = db.execute(
                f'SELECT * FROM "{table}" WHERE "{key_column}"=?',  # noqa: S608
                (entity_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        for key, value in list(item.items()):
            if not isinstance(value, str) or not value or value[0] not in "[{":
                continue
            try:
                item[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
        return sanitize_for_export(item)

    @staticmethod
    def _revision_row(row: sqlite3.Row, *, idempotent: bool = False) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["idempotent"] = idempotent
        return item

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _scorecard_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for field in ("provider_manifest", "candidate_universe", "metrics", "payload"):
            item[field] = json.loads(item[field])
        return item


__all__ = ["ROUND9_SCHEMA_VERSION", "Round9Repository"]
