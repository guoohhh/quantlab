from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from quantlab.domain.thesis import normalize_check_frequency
from quantlab.persistence.migrations import record_component_migration
from quantlab.security import sanitize_for_export


ROUND8_SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            sanitize_for_export(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(sanitize_for_export(value), ensure_ascii=False, sort_keys=True)


class Round8Repository:
    """Unified run ledger, theses, reflections, checkpoints and provider selections."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        record_component_migration(
            self.path,
            component="round8",
            version=ROUND8_SCHEMA_VERSION,
            migration_identity="round8-run-ledger-thesis-reflection-checkpoint-provider-selection-v1",
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
                CREATE TABLE IF NOT EXISTS provider_refresh_selections (
                    refresh_id TEXT NOT NULL, observed_at TEXT NOT NULL,
                    component TEXT NOT NULL, selected_provider TEXT,
                    selection_reason TEXT NOT NULL, related_failures TEXT NOT NULL,
                    attempts TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    PRIMARY KEY(refresh_id,component)
                );
                CREATE INDEX IF NOT EXISTS idx_provider_selections_observed
                    ON provider_refresh_selections(observed_at,component);
                CREATE TABLE IF NOT EXISTS unified_experiments (
                    experiment_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    experiment_type TEXT NOT NULL, evidence_boundary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(name,experiment_type,evidence_boundary)
                );
                CREATE TABLE IF NOT EXISTS unified_experiment_runs (
                    run_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE, run_type TEXT NOT NULL,
                    evidence_boundary TEXT NOT NULL, status TEXT NOT NULL,
                    code_fingerprint TEXT NOT NULL, config_fingerprint TEXT NOT NULL,
                    prompt_version TEXT, dataset_fingerprint TEXT,
                    universe_fingerprint TEXT, context_fingerprint TEXT,
                    quote_fingerprint TEXT, model_routing TEXT NOT NULL,
                    parameters TEXT NOT NULL, cost_budget TEXT NOT NULL,
                    result_summary TEXT NOT NULL DEFAULT '{}', error_detail TEXT,
                    started_at TEXT NOT NULL, completed_at TEXT, updated_at TEXT NOT NULL,
                    quality_gate_fingerprint TEXT, prompt_fingerprint TEXT,
                    build_state TEXT NOT NULL DEFAULT '{}', parent_run_id TEXT,
                    workflow_version TEXT,
                    FOREIGN KEY(experiment_id) REFERENCES unified_experiments(experiment_id)
                );
                CREATE INDEX IF NOT EXISTS idx_unified_runs_experiment
                    ON unified_experiment_runs(experiment_id,started_at);
                CREATE TABLE IF NOT EXISTS unified_run_links (
                    run_id TEXT NOT NULL, entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL, relation TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id,entity_type,entity_id,relation),
                    FOREIGN KEY(run_id) REFERENCES unified_experiment_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS unified_run_artifacts (
                    artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL, name TEXT NOT NULL, uri TEXT,
                    payload TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(run_id,artifact_type,name,fingerprint),
                    FOREIGN KEY(run_id) REFERENCES unified_experiment_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS research_step_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    step_name TEXT NOT NULL, checkpoint_signature TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(run_id,step_name),
                    FOREIGN KEY(run_id) REFERENCES unified_experiment_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS investment_theses (
                    thesis_id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL,
                    symbol TEXT NOT NULL, recommendation_id TEXT UNIQUE,
                    research_id TEXT, context_id TEXT, run_id TEXT, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, initial_price REAL NOT NULL,
                    core_thesis TEXT NOT NULL, supporting_evidence TEXT NOT NULL,
                    opposing_evidence TEXT NOT NULL, red_lines TEXT NOT NULL,
                    invalidation_conditions TEXT NOT NULL, valuation_anchor TEXT,
                    next_check_at TEXT, user_decision TEXT NOT NULL,
                    linked_order_id TEXT, linked_external_trade_id TEXT,
                    data_provenance TEXT NOT NULL, thesis_fingerprint TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_theses_portfolio_status
                    ON investment_theses(portfolio_id,status,next_check_at);
                CREATE TABLE IF NOT EXISTS thesis_assumptions (
                    assumption_id TEXT PRIMARY KEY, thesis_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, statement TEXT NOT NULL,
                    verification_metric TEXT NOT NULL, current_evidence TEXT NOT NULL,
                    status TEXT NOT NULL, check_frequency TEXT NOT NULL,
                    evidence_refs TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    UNIQUE(thesis_id,ordinal),
                    FOREIGN KEY(thesis_id) REFERENCES investment_theses(thesis_id)
                );
                CREATE TABLE IF NOT EXISTS thesis_checks (
                    check_id TEXT PRIMARY KEY, thesis_id TEXT NOT NULL,
                    checked_at TEXT NOT NULL, context_id TEXT, context_fingerprint TEXT,
                    trigger_type TEXT NOT NULL, price_change_pct REAL,
                    facts_changed INTEGER NOT NULL, red_line_triggered INTEGER NOT NULL,
                    proposed_status TEXT NOT NULL, final_status TEXT NOT NULL,
                    user_resolution TEXT NOT NULL, assumption_results TEXT NOT NULL,
                    evidence_refs TEXT NOT NULL, unavailable_reasons TEXT NOT NULL,
                    report_fingerprint TEXT NOT NULL,
                    verified_evidence_snapshot TEXT NOT NULL DEFAULT '{}',
                    verification_fingerprint TEXT,
                    frozen_revision_id TEXT,
                    frozen_revision_fingerprint TEXT,
                    schedule_status TEXT NOT NULL DEFAULT 'not_applied',
                    schedule_update_applied INTEGER NOT NULL DEFAULT 0,
                    next_check_at TEXT,
                    UNIQUE(thesis_id,report_fingerprint),
                    FOREIGN KEY(thesis_id) REFERENCES investment_theses(thesis_id)
                );
                CREATE TABLE IF NOT EXISTS outcome_reflections (
                    reflection_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    source_type TEXT NOT NULL, source_id TEXT NOT NULL,
                    evidence_boundary TEXT NOT NULL, horizon_days INTEGER NOT NULL,
                    due_at TEXT NOT NULL, settled_at TEXT NOT NULL,
                    raw_return_pct REAL NOT NULL, benchmark_return_pct REAL NOT NULL,
                    alpha_pct REAL NOT NULL, transaction_cost REAL NOT NULL,
                    maximum_adverse_excursion REAL, maximum_favorable_excursion REAL,
                    direction_correct INTEGER, reflection TEXT NOT NULL,
                    candidate_lessons TEXT NOT NULL, evidence_refs TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, authority_fingerprint TEXT,
                    UNIQUE(source_type,source_id,horizon_days),
                    FOREIGN KEY(run_id) REFERENCES unified_experiment_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS controlled_research_memories (
                    memory_id TEXT PRIMARY KEY, reflection_id TEXT NOT NULL,
                    symbol TEXT NOT NULL, scope TEXT NOT NULL, lesson TEXT NOT NULL,
                    weight REAL NOT NULL, status TEXT NOT NULL,
                    mature_evidence INTEGER NOT NULL, challenge_eligible INTEGER NOT NULL,
                    created_at TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    UNIQUE(reflection_id,lesson),
                    FOREIGN KEY(reflection_id) REFERENCES outcome_reflections(reflection_id)
                );
                CREATE TABLE IF NOT EXISTS next_trading_day_acceptance_reports (
                    report_id TEXT PRIMARY KEY, trade_date TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, checks TEXT NOT NULL, blockers TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(db, "investor_adoption_revisions", "trade_side", "TEXT")
            self._ensure_column(
                db, "investor_adoption_revisions", "decision_relation", "TEXT NOT NULL DEFAULT 'aligned'"
            )
            self._ensure_column(
                db, "investor_recommendation_adoptions", "actual_trade_side", "TEXT"
            )
            self._ensure_column(
                db, "investor_recommendation_adoptions", "actual_trade_date", "TEXT"
            )
            self._ensure_column(
                db, "outcome_reflections", "authority_fingerprint", "TEXT"
            )
            self._ensure_column(
                db,
                "thesis_checks",
                "verified_evidence_snapshot",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                db, "thesis_checks", "verification_fingerprint", "TEXT"
            )
            self._ensure_column(
                db, "unified_experiment_runs", "quality_gate_fingerprint", "TEXT"
            )
            self._ensure_column(
                db, "unified_experiment_runs", "prompt_fingerprint", "TEXT"
            )
            self._ensure_column(
                db,
                "unified_experiment_runs",
                "build_state",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                db, "unified_experiment_runs", "parent_run_id", "TEXT"
            )
            self._ensure_column(
                db, "unified_experiment_runs", "workflow_version", "TEXT"
            )
            self._ensure_column(
                db, "investor_recommendation_adoptions", "transaction_cost", "REAL NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                db, "investor_recommendation_adoptions", "decision_relation", "TEXT NOT NULL DEFAULT 'aligned'"
            )
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

    def record_provider_selections(
        self,
        refresh_id: str,
        selections: dict[str, dict[str, Any]],
        *,
        observed_at: datetime | None = None,
        market_date: date | str | None = None,
    ) -> None:
        timestamp = (observed_at or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connect() as db:
            for component, selection in selections.items():
                payload = sanitize_for_export(selection)
                db.execute(
                    """INSERT OR REPLACE INTO provider_refresh_selections(
                         refresh_id,observed_at,component,selected_provider,selection_reason,
                         related_failures,attempts,fingerprint,market_date,status,capability,
                         source_version,manifest_id,pool_snapshot_id,pool_fingerprint
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        refresh_id,
                        timestamp,
                        component,
                        payload.get("selected_provider"),
                        str(payload.get("reason") or "unavailable"),
                        json.dumps(payload.get("related_failures") or [], ensure_ascii=False),
                        json.dumps(payload.get("attempts") or [], ensure_ascii=False),
                        _fingerprint({"component": component, **payload}),
                        str(payload.get("market_date") or market_date or "")[:10] or None,
                        payload.get("status"),
                        payload.get("capability"),
                        payload.get("source_version") or payload.get("provider_version"),
                        payload.get("manifest_id"),
                        payload.get("pool_snapshot_id"),
                        payload.get("pool_fingerprint"),
                    ),
                )

    def provider_selections(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM provider_refresh_selections ORDER BY observed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._json_row(row, "related_failures", "attempts") for row in rows]

    def start_run(
        self,
        *,
        experiment_name: str,
        experiment_type: str,
        run_type: str,
        evidence_boundary: str,
        idempotency_key: str,
        code_fingerprint: str,
        config_fingerprint: str,
        prompt_version: str | None = None,
        dataset_fingerprint: str | None = None,
        universe_fingerprint: str | None = None,
        context_fingerprint: str | None = None,
        quote_fingerprint: str | None = None,
        model_routing: dict[str, Any] | None = None,
        parameters: dict[str, Any] | None = None,
        cost_budget: dict[str, Any] | None = None,
        quality_gate_fingerprint: str | None = None,
        prompt_fingerprint: str | None = None,
        build_state: dict[str, Any] | None = None,
        parent_run_id: str | None = None,
        workflow_version: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM unified_experiment_runs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                expected = {
                    "experiment_name": experiment_name,
                    "experiment_type": experiment_type,
                    "run_type": run_type,
                    "evidence_boundary": evidence_boundary,
                    "code_fingerprint": code_fingerprint,
                    "config_fingerprint": config_fingerprint,
                    "prompt_version": prompt_version,
                    "dataset_fingerprint": dataset_fingerprint,
                    "universe_fingerprint": universe_fingerprint,
                    "context_fingerprint": context_fingerprint,
                    "quote_fingerprint": quote_fingerprint,
                    "model_routing": sanitize_for_export(model_routing or {}),
                    "parameters": sanitize_for_export(parameters or {}),
                    "cost_budget": sanitize_for_export(cost_budget or {}),
                    "quality_gate_fingerprint": quality_gate_fingerprint,
                    "prompt_fingerprint": prompt_fingerprint,
                    "build_state": sanitize_for_export(build_state or {}),
                    "parent_run_id": parent_run_id,
                    "workflow_version": workflow_version,
                }
                experiment_row = db.execute(
                    "SELECT name,experiment_type FROM unified_experiments WHERE experiment_id=?",
                    (existing["experiment_id"],),
                ).fetchone()
                actual = {
                    "experiment_name": experiment_row[0],
                    "experiment_type": experiment_row[1],
                    "run_type": existing["run_type"],
                    "evidence_boundary": existing["evidence_boundary"],
                    "code_fingerprint": existing["code_fingerprint"],
                    "config_fingerprint": existing["config_fingerprint"],
                    "prompt_version": existing["prompt_version"],
                    "dataset_fingerprint": existing["dataset_fingerprint"],
                    "universe_fingerprint": existing["universe_fingerprint"],
                    "context_fingerprint": existing["context_fingerprint"],
                    "quote_fingerprint": existing["quote_fingerprint"],
                    "model_routing": json.loads(existing["model_routing"]),
                    "parameters": json.loads(existing["parameters"]),
                    "cost_budget": json.loads(existing["cost_budget"]),
                    "quality_gate_fingerprint": existing["quality_gate_fingerprint"],
                    "prompt_fingerprint": existing["prompt_fingerprint"],
                    "build_state": json.loads(existing["build_state"] or "{}"),
                    "parent_run_id": existing["parent_run_id"],
                    "workflow_version": existing["workflow_version"],
                }
                if actual != expected:
                    raise ValueError(
                        "experiment idempotency key is already bound to different frozen inputs"
                    )
                return self._run_row(existing, idempotent=True)
            experiment = db.execute(
                """SELECT * FROM unified_experiments
                   WHERE name=? AND experiment_type=? AND evidence_boundary=?""",
                (experiment_name, experiment_type, evidence_boundary),
            ).fetchone()
            experiment_id = str(experiment["experiment_id"]) if experiment else str(uuid.uuid4())
            if experiment is None:
                db.execute(
                    "INSERT INTO unified_experiments VALUES(?,?,?,?,?)",
                    (experiment_id, experiment_name, experiment_type, evidence_boundary, now),
                )
            run_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO unified_experiment_runs(
                       run_id,experiment_id,idempotency_key,run_type,evidence_boundary,status,
                       code_fingerprint,config_fingerprint,prompt_version,dataset_fingerprint,
                       universe_fingerprint,context_fingerprint,quote_fingerprint,model_routing,
                       parameters,cost_budget,started_at,updated_at,quality_gate_fingerprint,
                       prompt_fingerprint,build_state,parent_run_id,workflow_version
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    experiment_id,
                    idempotency_key,
                    run_type,
                    evidence_boundary,
                    "running",
                    code_fingerprint,
                    config_fingerprint,
                    prompt_version,
                    dataset_fingerprint,
                    universe_fingerprint,
                    context_fingerprint,
                    quote_fingerprint,
                    json.dumps(sanitize_for_export(model_routing or {}), ensure_ascii=False),
                    json.dumps(sanitize_for_export(parameters or {}), ensure_ascii=False),
                    json.dumps(sanitize_for_export(cost_budget or {}), ensure_ascii=False),
                    now,
                    now,
                    quality_gate_fingerprint,
                    prompt_fingerprint,
                    json.dumps(sanitize_for_export(build_state or {}), ensure_ascii=False),
                    parent_run_id,
                    workflow_version,
                ),
            )
            row = db.execute(
                "SELECT * FROM unified_experiment_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._run_row(row, idempotent=False)

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        result_summary: dict[str, Any] | None = None,
        error_detail: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled", "blocked"}:
            raise ValueError("invalid run terminal status")
        now = _now()
        with self.connect() as db:
            db.execute(
                """UPDATE unified_experiment_runs
                   SET status=?,result_summary=?,error_detail=?,completed_at=?,updated_at=?
                   WHERE run_id=? AND status='running'""",
                (
                    status,
                    json.dumps(sanitize_for_export(result_summary or {}), ensure_ascii=False),
                    error_detail,
                    now,
                    now,
                    run_id,
                ),
            )
        result = self.run(run_id)
        if result is None:
            raise ValueError("experiment run not found")
        return result

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM unified_experiment_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._run_row(row) if row else None

    def link_entity(
        self, run_id: str, *, entity_type: str, entity_id: str, relation: str
    ) -> None:
        if self.run(run_id) is None:
            raise ValueError("experiment run not found")
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO unified_run_links VALUES(?,?,?,?,?)",
                (run_id, entity_type, entity_id, relation, _now()),
            )

    def run_for_link(
        self, *, entity_type: str, entity_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT run_id FROM unified_run_links
                   WHERE entity_type=? AND entity_id=? ORDER BY created_at LIMIT 1""",
                (entity_type, entity_id),
            ).fetchone()
        return self.run(str(row["run_id"])) if row else None

    def save_artifact(
        self,
        run_id: str,
        *,
        artifact_type: str,
        name: str,
        payload: dict[str, Any],
        uri: str | None = None,
    ) -> dict[str, Any]:
        if self.run(run_id) is None:
            raise ValueError("experiment run not found")
        sanitized = sanitize_for_export(payload)
        fingerprint = _fingerprint(sanitized)
        with self.connect() as db:
            existing = db.execute(
                """SELECT * FROM unified_run_artifacts
                   WHERE run_id=? AND artifact_type=? AND name=? AND fingerprint=?""",
                (run_id, artifact_type, name, fingerprint),
            ).fetchone()
            if existing:
                return self._artifact_row(existing)
            artifact_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO unified_run_artifacts VALUES(?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    run_id,
                    artifact_type,
                    name,
                    uri,
                    json.dumps(sanitized, ensure_ascii=False),
                    fingerprint,
                    _now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM unified_run_artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        return self._artifact_row(row)

    def save_checkpoint(
        self,
        run_id: str,
        *,
        step_name: str,
        checkpoint_signature: str,
        payload: dict[str, Any],
        status: str = "completed",
    ) -> dict[str, Any]:
        if self.run(run_id) is None:
            raise ValueError("experiment run not found")
        now = _now()
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM research_step_checkpoints WHERE run_id=? AND step_name=?",
                (run_id, step_name),
            ).fetchone()
            if row and row["checkpoint_signature"] != checkpoint_signature:
                raise ValueError("checkpoint signature changed; old step cannot be reused")
            checkpoint_id = str(row["checkpoint_id"]) if row else str(uuid.uuid4())
            db.execute(
                """INSERT INTO research_step_checkpoints VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id,step_name) DO UPDATE SET
                     status=excluded.status,payload=excluded.payload,
                     fingerprint=excluded.fingerprint,updated_at=excluded.updated_at""",
                (
                    checkpoint_id,
                    run_id,
                    step_name,
                    checkpoint_signature,
                    status,
                    json.dumps(sanitize_for_export(payload), ensure_ascii=False),
                    _fingerprint(payload),
                    row["created_at"] if row else now,
                    now,
                ),
            )
            saved = db.execute(
                "SELECT * FROM research_step_checkpoints WHERE checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
        return self._checkpoint_row(saved)

    def load_checkpoint(
        self, run_id: str, step_name: str, checkpoint_signature: str
    ) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM research_step_checkpoints WHERE run_id=? AND step_name=?",
                (run_id, step_name),
            ).fetchone()
        if not row or row["checkpoint_signature"] != checkpoint_signature:
            return None
        return self._checkpoint_row(row)

    def checkpoint_for_step(self, run_id: str, step_name: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM research_step_checkpoints WHERE run_id=? AND step_name=?",
                (run_id, step_name),
            ).fetchone()
        return self._checkpoint_row(row) if row else None

    def claim_checkpoint_step(
        self,
        run_id: str,
        *,
        step_name: str,
        checkpoint_signature: str,
        stale_after_seconds: int = 1_800,
    ) -> dict[str, Any]:
        """Atomically claim an expensive step before invoking an LLM or network call."""
        if self.run(run_id) is None:
            raise ValueError("experiment run not found")
        now = datetime.now(UTC)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM research_step_checkpoints WHERE run_id=? AND step_name=?",
                (run_id, step_name),
            ).fetchone()
            if row is not None and row["checkpoint_signature"] != checkpoint_signature:
                raise ValueError(
                    "checkpoint signature changed; callback was not executed and old step cannot be reused"
                )
            if row is not None and row["status"] == "completed":
                return {"state": "completed", "checkpoint": self._checkpoint_row(row)}
            if row is not None and row["status"] == "running":
                updated_at = datetime.fromisoformat(str(row["updated_at"]))
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=UTC)
                if updated_at + timedelta(seconds=max(1, stale_after_seconds)) > now:
                    return {"state": "in_progress", "checkpoint": self._checkpoint_row(row)}
            checkpoint_id = str(row["checkpoint_id"]) if row else str(uuid.uuid4())
            created_at = str(row["created_at"]) if row else now.isoformat()
            db.execute(
                """INSERT INTO research_step_checkpoints VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id,step_name) DO UPDATE SET
                     checkpoint_signature=excluded.checkpoint_signature,
                     status='running',payload='{}',fingerprint=excluded.fingerprint,
                     updated_at=excluded.updated_at""",
                (
                    checkpoint_id,
                    run_id,
                    step_name,
                    checkpoint_signature,
                    "running",
                    "{}",
                    _fingerprint({}),
                    created_at,
                    now.isoformat(),
                ),
            )
            claimed = db.execute(
                "SELECT * FROM research_step_checkpoints WHERE checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
        return {"state": "acquired", "checkpoint": self._checkpoint_row(claimed)}

    def fail_checkpoint_step(
        self,
        run_id: str,
        *,
        step_name: str,
        checkpoint_signature: str,
        error_type: str,
    ) -> dict[str, Any]:
        return self.save_checkpoint(
            run_id,
            step_name=step_name,
            checkpoint_signature=checkpoint_signature,
            payload={"error_type": error_type},
            status="failed",
        )

    def create_thesis(self, payload: dict[str, Any]) -> dict[str, Any]:
        assumptions = list(payload.get("assumptions") or [])
        if not 3 <= len(assumptions) <= 7:
            raise ValueError("investment thesis requires 3 to 7 assumptions")
        sanitized = sanitize_for_export(payload)
        recommendation_id = sanitized.get("recommendation_id")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if recommendation_id:
                existing = db.execute(
                    "SELECT thesis_id FROM investment_theses WHERE recommendation_id=?",
                    (recommendation_id,),
                ).fetchone()
                if existing:
                    return self.thesis(existing["thesis_id"]) or {}
            thesis_id = str(sanitized.get("thesis_id") or uuid.uuid4())
            created_at = str(sanitized.get("created_at") or _now())
            db.execute(
                """INSERT INTO investment_theses(
                       thesis_id,portfolio_id,symbol,recommendation_id,research_id,context_id,
                       run_id,status,created_at,initial_price,core_thesis,supporting_evidence,
                       opposing_evidence,red_lines,invalidation_conditions,valuation_anchor,
                       next_check_at,user_decision,linked_order_id,linked_external_trade_id,
                       data_provenance,thesis_fingerprint
                   ) VALUES(?,?,?,?,?,?,?,'draft_pending_confirmation',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    thesis_id,
                    sanitized["portfolio_id"],
                    sanitized["symbol"],
                    recommendation_id,
                    sanitized.get("research_id"),
                    sanitized.get("context_id"),
                    sanitized.get("run_id"),
                    created_at,
                    float(sanitized["initial_price"]),
                    sanitized["core_thesis"],
                    json.dumps(sanitized.get("supporting_evidence") or [], ensure_ascii=False),
                    json.dumps(sanitized.get("opposing_evidence") or [], ensure_ascii=False),
                    json.dumps(sanitized.get("red_lines") or [], ensure_ascii=False),
                    json.dumps(sanitized.get("invalidation_conditions") or [], ensure_ascii=False),
                    sanitized.get("valuation_anchor"),
                    sanitized.get("next_check_at"),
                    sanitized["user_decision"],
                    sanitized.get("linked_order_id"),
                    sanitized.get("linked_external_trade_id"),
                    json.dumps(sanitized.get("data_provenance") or {}, ensure_ascii=False),
                    _fingerprint(sanitized),
                ),
            )
            for ordinal, assumption in enumerate(assumptions, start=1):
                item = sanitize_for_export(assumption)
                frequency = normalize_check_frequency(
                    item.get("check_frequency") or "quarterly"
                )
                db.execute(
                    """INSERT INTO thesis_assumptions(
                         assumption_id,thesis_id,ordinal,statement,verification_metric,
                         current_evidence,status,check_frequency,evidence_refs,fingerprint,
                         last_checked_at,next_check_at,active_revision_id
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()),
                        thesis_id,
                        ordinal,
                        item["statement"],
                        item["verification_metric"],
                        _text_value(item.get("current_evidence") or "unavailable"),
                        item.get("status") or "needs_review",
                        frequency,
                        json.dumps(item.get("evidence_refs") or [], ensure_ascii=False),
                        _fingerprint(item),
                        None,
                        None
                        if frequency in {"event_driven", "manual"}
                        else item.get("next_check_at"),
                        None,
                    ),
                )
        return self.thesis(thesis_id) or {}

    def thesis(self, thesis_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM investment_theses WHERE thesis_id=?", (thesis_id,)
            ).fetchone()
            current_revision_id = (
                row["current_frozen_revision_id"]
                if row and "current_frozen_revision_id" in row.keys()
                else None
            )
            if current_revision_id:
                assumptions = db.execute(
                    """SELECT * FROM thesis_assumptions
                       WHERE thesis_id=? AND active_revision_id=? ORDER BY ordinal""",
                    (thesis_id, current_revision_id),
                ).fetchall()
            else:
                assumptions = db.execute(
                    "SELECT * FROM thesis_assumptions WHERE thesis_id=? ORDER BY ordinal",
                    (thesis_id,),
                ).fetchall()
            checks = db.execute(
                "SELECT * FROM thesis_checks WHERE thesis_id=? ORDER BY checked_at DESC",
                (thesis_id,),
            ).fetchall()
            revision_table = db.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='investment_thesis_revisions'"""
            ).fetchone()
            revisions = (
                db.execute(
                    """SELECT * FROM investment_thesis_revisions
                       WHERE thesis_id=? ORDER BY revision_number""",
                    (thesis_id,),
                ).fetchall()
                if revision_table
                else []
            )
        if not row:
            return None
        revision_history = []
        for revision in revisions:
            item = dict(revision)
            item["payload"] = json.loads(item["payload"])
            revision_history.append(item)
        current_frozen = next(
            (
                item
                for item in revision_history
                if item["revision_id"] == current_revision_id
            ),
            None,
        )
        return {
            **self._json_row(
                row,
                "supporting_evidence",
                "opposing_evidence",
                "red_lines",
                "invalidation_conditions",
                "data_provenance",
            ),
            "assumptions": [self._json_row(item, "evidence_refs") for item in assumptions],
            "checks": [
                self._json_row(
                    item,
                    "assumption_results",
                    "evidence_refs",
                    "unavailable_reasons",
                    "verified_evidence_snapshot",
                )
                for item in checks
            ],
            "lifecycle_status": row["status"],
            "current_frozen_revision": current_frozen,
            "draft_revisions": [
                item for item in revision_history if item["status"] == "draft"
            ],
            "revision_history": revision_history,
            "revisions": revision_history,
        }

    def thesis_for_recommendation(self, recommendation_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT thesis_id FROM investment_theses WHERE recommendation_id=?",
                (recommendation_id,),
            ).fetchone()
        return self.thesis(str(row[0])) if row else None

    def revise_thesis_decision(
        self,
        recommendation_id: str,
        *,
        user_decision: str,
        external_trade_id: str | None,
    ) -> dict[str, Any] | None:
        existing = self.thesis_for_recommendation(recommendation_id)
        if existing is None:
            return None
        old_decision = str(existing.get("user_decision") or "")
        status = str(existing["status"])
        closed_at = existing.get("closed_at")
        if user_decision in {"rejected", "user_override"}:
            status = "closed"
            closed_at = _now()
        elif status == "closed" and old_decision in {"rejected", "user_override"}:
            status = (
                "active"
                if existing.get("current_frozen_revision_id")
                else "draft_pending_confirmation"
            )
            closed_at = None
        revised_fingerprint = _fingerprint(
            {
                "previous_thesis_fingerprint": existing["thesis_fingerprint"],
                "user_decision": user_decision,
                "external_trade_id": external_trade_id,
                "status": status,
                "closed_at": closed_at,
            }
        )
        with self.connect() as db:
            db.execute(
                """UPDATE investment_theses
                   SET user_decision=?,linked_external_trade_id=?,status=?,closed_at=?,
                       thesis_fingerprint=?
                   WHERE recommendation_id=?""",
                (
                    user_decision,
                    external_trade_id,
                    status,
                    closed_at,
                    revised_fingerprint,
                    recommendation_id,
                ),
            )
        return self.thesis(existing["thesis_id"])

    def theses(
        self,
        *,
        portfolio_id: str | None = None,
        statuses: tuple[str, ...] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if portfolio_id:
            clauses.append("portfolio_id=?")
            params.append(portfolio_id)
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        query = "SELECT thesis_id FROM investment_theses"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as db:
            ids = [row[0] for row in db.execute(query, params).fetchall()]
        return [item for thesis_id in ids if (item := self.thesis(thesis_id))]

    def save_thesis_check(self, thesis_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        thesis = self.thesis(thesis_id)
        if not thesis:
            raise ValueError("investment thesis not found")
        current_revision = thesis.get("current_frozen_revision")
        if not current_revision:
            raise ValueError("waiting_for_user_confirmation")
        sanitized = sanitize_for_export(payload)
        supplied_revision_id = sanitized.get("frozen_revision_id")
        if supplied_revision_id and supplied_revision_id != current_revision["revision_id"]:
            raise ValueError("thesis check frozen revision does not match current thesis")
        sanitized["frozen_revision_id"] = current_revision["revision_id"]
        sanitized["frozen_revision_fingerprint"] = current_revision["fingerprint"]
        final_status = str(sanitized["final_status"])
        allowed = {"active", "strengthened", "unchanged", "weakened", "damaged", "broken", "closed"}
        if final_status not in allowed:
            raise ValueError("invalid thesis status")
        report_fp = _fingerprint(sanitized)
        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM thesis_checks WHERE thesis_id=? AND report_fingerprint=?",
                (thesis_id, report_fp),
            ).fetchone()
            if existing:
                return self._json_row(
                    existing,
                    "assumption_results",
                    "evidence_refs",
                    "unavailable_reasons",
                    "verified_evidence_snapshot",
                )
            check_id = str(uuid.uuid4())
            checked_at = str(sanitized.get("checked_at") or _now())
            db.execute(
                """INSERT INTO thesis_checks(
                       check_id,thesis_id,checked_at,context_id,context_fingerprint,
                       trigger_type,price_change_pct,facts_changed,red_line_triggered,
                       proposed_status,final_status,user_resolution,assumption_results,
                       evidence_refs,unavailable_reasons,report_fingerprint,
                       verified_evidence_snapshot,verification_fingerprint,
                       frozen_revision_id,frozen_revision_fingerprint,schedule_status,
                       schedule_update_applied,next_check_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    check_id,
                    thesis_id,
                    checked_at,
                    sanitized.get("context_id"),
                    sanitized.get("context_fingerprint"),
                    sanitized.get("trigger_type") or "manual_review",
                    sanitized.get("price_change_pct"),
                    int(bool(sanitized.get("facts_changed"))),
                    int(bool(sanitized.get("red_line_triggered"))),
                    sanitized.get("proposed_status") or final_status,
                    final_status,
                    sanitized.get("user_resolution") or "confirmed",
                    json.dumps(sanitized.get("assumption_results") or [], ensure_ascii=False),
                    json.dumps(sanitized.get("evidence_refs") or [], ensure_ascii=False),
                    json.dumps(sanitized.get("unavailable_reasons") or [], ensure_ascii=False),
                    report_fp,
                    json.dumps(
                        sanitized.get("verified_evidence_snapshot") or {}, ensure_ascii=False
                    ),
                    _fingerprint(sanitized.get("verified_evidence_snapshot") or {}),
                    current_revision["revision_id"],
                    current_revision["fingerprint"],
                    sanitized.get("schedule_status") or "not_applied",
                    int(bool(sanitized.get("schedule_update_applied"))),
                    sanitized.get("next_check_at"),
                ),
            )
            db.execute(
                """UPDATE investment_theses SET status=?,closed_at=?,
                     next_check_at=CASE
                       WHEN ?='closed' THEN NULL
                       WHEN ? THEN ?
                       ELSE next_check_at
                     END
                   WHERE thesis_id=?""",
                (
                    final_status,
                    checked_at if final_status == "closed" else None,
                    final_status,
                    int(bool(sanitized.get("schedule_update_applied"))),
                    sanitized.get("next_check_at"),
                    thesis_id,
                ),
            )
            if final_status == "closed":
                db.execute(
                    "UPDATE thesis_assumptions SET next_check_at=NULL WHERE thesis_id=?",
                    (thesis_id,),
                )
            if sanitized.get("user_resolution") != "ignored":
                for result in sanitized.get("assumption_results") or []:
                    assumption_id = result.get("assumption_id")
                    if not assumption_id:
                        continue
                    evidence = result.get("evidence_refs") or []
                    db.execute(
                        """UPDATE thesis_assumptions
                           SET status=?,current_evidence=?,
                               last_checked_at=CASE WHEN ? THEN ? ELSE last_checked_at END,
                               next_check_at=CASE WHEN ? THEN ? ELSE next_check_at END
                           WHERE assumption_id=? AND thesis_id=?""",
                        (
                            str(result.get("status") or "needs_review"),
                            _text_value(evidence or "unavailable"),
                            int(bool(result.get("schedule_update_applied"))),
                            checked_at,
                            int(bool(result.get("schedule_update_applied"))),
                            result.get("next_check_at"),
                            assumption_id,
                            thesis_id,
                        ),
                    )
            elif sanitized.get("schedule_update_applied"):
                for result in sanitized.get("assumption_results") or []:
                    if not result.get("assumption_id"):
                        continue
                    db.execute(
                        """UPDATE thesis_assumptions SET last_checked_at=?,next_check_at=?
                           WHERE assumption_id=? AND thesis_id=?""",
                        (
                            checked_at,
                            result.get("next_check_at"),
                            result["assumption_id"],
                            thesis_id,
                        ),
                    )
            row = db.execute("SELECT * FROM thesis_checks WHERE check_id=?", (check_id,)).fetchone()
        return self._json_row(
            row,
            "assumption_results",
            "evidence_refs",
            "unavailable_reasons",
            "verified_evidence_snapshot",
        )

    def authoritative_outcome(
        self,
        *,
        run_id: str,
        source_type: str,
        source_id: str,
        horizon_days: int,
    ) -> dict[str, Any]:
        """Resolve immutable outcome facts from server-owned settlement ledgers.

        This deliberately lives in the repository rather than only in a workflow so a
        direct repository call cannot turn caller supplied returns into formal memory.
        """
        if horizon_days not in {5, 20}:
            raise ValueError("reflection horizon must be 5 or 20 trading days")
        run = self.run(run_id)
        if run is None:
            raise ValueError("reflection requires a unified experiment run")
        if run["status"] != "completed":
            raise ValueError("reflection requires a completed unified experiment run")
        if source_type == "forward_sample":
            outcome = self._forward_authoritative_outcome(
                run=run,
                source_id=source_id,
                horizon_days=horizon_days,
            )
        elif source_type == "shadow_account_cycle":
            outcome = self._shadow_authoritative_outcome(
                run=run,
                source_id=source_id,
                horizon_days=horizon_days,
            )
        elif source_type == "user_recommendation_outcome":
            outcome = self._user_authoritative_outcome(
                run=run,
                source_id=source_id,
                horizon_days=horizon_days,
            )
        else:
            raise ValueError("unsupported authoritative reflection source")
        if outcome["evidence_boundary"] != run["evidence_boundary"]:
            raise ValueError("authoritative outcome boundary does not match the frozen run")
        if not outcome.get("run_linked"):
            raise ValueError("authoritative outcome is not linked to the supplied run")
        if outcome.get("status") != "settled":
            raise ValueError("authoritative outcome has not settled")
        due_at = datetime.fromisoformat(str(outcome["due_at"]))
        settled_at = datetime.fromisoformat(str(outcome["settled_at"]))
        now = datetime.now(UTC)
        if settled_at > now:
            raise ValueError("authoritative settlement time cannot be in the future")
        if settled_at < due_at:
            raise ValueError("authoritative outcome has not reached its frozen due time")
        missing = [
            field
            for field in (
                "signal_date",
                "due_at",
                "settled_at",
                "start_price",
                "end_price",
                "raw_return_pct",
                "benchmark_return_pct",
                "transaction_cost",
                "evidence_boundary",
            )
            if outcome.get(field) is None
        ]
        if missing:
            raise ValueError(
                "authoritative outcome unavailable: missing " + ",".join(missing)
            )
        outcome["authority_fingerprint"] = _fingerprint(
            {key: outcome.get(key) for key in sorted(outcome) if key != "run_linked"}
        )
        return outcome

    def save_reflection(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self.run(str(payload.get("run_id") or ""))
        if run is None:
            raise ValueError("reflection requires a unified experiment run")
        if run["status"] != "completed":
            raise ValueError("reflection requires a completed unified experiment run")
        if payload.get("evidence_boundary") not in {None, "production", "forward_shadow", "user"}:
            raise ValueError(
                "only matured production or forward_shadow results can create reflection"
            )
        prohibited = {
            "due_at",
            "settled_at",
            "raw_return_pct",
            "benchmark_return_pct",
            "transaction_cost",
            "maximum_adverse_excursion",
            "maximum_favorable_excursion",
            "direction_correct",
            "evidence_boundary",
        }
        supplied = sorted(prohibited.intersection(payload))
        if supplied:
            raise ValueError(
                "reflection outcome facts are server authoritative; caller supplied: "
                + ",".join(supplied)
            )
        sanitized = sanitize_for_export(payload)
        authority = self.authoritative_outcome(
            run_id=str(sanitized["run_id"]),
            source_type=str(sanitized["source_type"]),
            source_id=str(sanitized["source_id"]),
            horizon_days=int(sanitized["horizon_days"]),
        )
        evidence_boundary = str(authority["evidence_boundary"])
        if evidence_boundary not in {"production", "forward_shadow", "user"}:
            raise ValueError("demo, test and research-only outcomes cannot create reflection")
        with self.connect() as db:
            existing = db.execute(
                """SELECT * FROM outcome_reflections
                   WHERE source_type=? AND source_id=? AND horizon_days=?""",
                (sanitized["source_type"], sanitized["source_id"], sanitized["horizon_days"]),
            ).fetchone()
            if existing:
                return self._reflection_row(existing)
            reflection_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO outcome_reflections(
                       reflection_id,run_id,source_type,source_id,evidence_boundary,
                       horizon_days,due_at,settled_at,raw_return_pct,
                       benchmark_return_pct,alpha_pct,transaction_cost,
                       maximum_adverse_excursion,maximum_favorable_excursion,
                       direction_correct,reflection,candidate_lessons,evidence_refs,
                       fingerprint,authority_fingerprint
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    reflection_id,
                    sanitized["run_id"],
                    sanitized["source_type"],
                    sanitized["source_id"],
                    evidence_boundary,
                    int(sanitized["horizon_days"]),
                    authority["due_at"],
                    authority["settled_at"],
                    float(authority["raw_return_pct"]),
                    float(authority["benchmark_return_pct"]),
                    float(authority["raw_return_pct"])
                    - float(authority["benchmark_return_pct"]),
                    float(authority["transaction_cost"]),
                    authority.get("maximum_adverse_excursion"),
                    authority.get("maximum_favorable_excursion"),
                    None
                    if authority.get("direction_correct") is None
                    else int(bool(authority["direction_correct"])),
                    json.dumps(sanitized.get("reflection") or {}, ensure_ascii=False),
                    json.dumps(sanitized.get("candidate_lessons") or [], ensure_ascii=False),
                    json.dumps(sanitized.get("evidence_refs") or [], ensure_ascii=False),
                    _fingerprint({"authority": authority, "reflection": sanitized}),
                    authority["authority_fingerprint"],
                ),
            )
            row = db.execute(
                "SELECT * FROM outcome_reflections WHERE reflection_id=?", (reflection_id,)
            ).fetchone()
        return self._reflection_row(row)

    def add_memory_candidates(
        self,
        reflection_id: str,
        *,
        symbol: str,
        lessons: list[str],
        minimum_mature_samples: int = 30,
    ) -> list[dict[str, Any]]:
        with self.connect() as db:
            reflection = db.execute(
                """SELECT evidence_boundary FROM outcome_reflections
                   WHERE reflection_id=?""",
                (reflection_id,),
            ).fetchone()
            if not reflection:
                raise ValueError("reflection not found")
            if reflection["evidence_boundary"] not in {"production", "forward_shadow"}:
                raise ValueError("user, demo and test reflections cannot enter research memory")
            matured = int(
                db.execute(
                    """SELECT COUNT(*) FROM outcome_reflections
                       WHERE evidence_boundary IN ('production','forward_shadow')"""
                ).fetchone()[0]
            )
            for lesson in lessons:
                db.execute(
                    "INSERT OR IGNORE INTO controlled_research_memories VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        reflection_id,
                        symbol,
                        "same_symbol",
                        lesson,
                        1.0,
                        "candidate",
                        1,
                        int(matured >= minimum_mature_samples),
                        _now(),
                        _fingerprint({"reflection_id": reflection_id, "lesson": lesson}),
                    ),
                )
            rows = db.execute(
                "SELECT * FROM controlled_research_memories WHERE reflection_id=?",
                (reflection_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def memories(self, symbol: str, *, include_cross_symbol: bool = True) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT m.* FROM controlled_research_memories m
                   JOIN outcome_reflections r ON r.reflection_id=m.reflection_id
                   WHERE m.mature_evidence=1
                     AND r.evidence_boundary IN ('production','forward_shadow')
                     AND (m.symbol=? OR ?=1)
                   ORDER BY CASE WHEN m.symbol=? THEN 0 ELSE 1 END,m.created_at DESC LIMIT 100""",
                (symbol, int(include_cross_symbol), symbol),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            if item["symbol"] != symbol:
                item["weight"] = min(float(item["weight"]), 0.25)
                item["scope"] = "cross_symbol_low_weight"
            output.append(item)
        return output

    def _run_has_link(
        self, run_id: str, *, entity_type: str, entity_id: str
    ) -> bool:
        with self.connect() as db:
            return bool(
                db.execute(
                    """SELECT 1 FROM unified_run_links
                       WHERE run_id=? AND entity_type=? AND entity_id=? LIMIT 1""",
                    (run_id, entity_type, entity_id),
                ).fetchone()
            )

    def _forward_authoritative_outcome(
        self, *, run: dict[str, Any], source_id: str, horizon_days: int
    ) -> dict[str, Any]:
        self._require_tables(
            "forward_ablation_predictions",
            "forward_ablation_outcomes",
            purpose="authoritative forward outcome",
        )
        with self.connect() as db:
            row = db.execute(
                """SELECT p.*,o.realized_direction,o.realized_return_pct,
                          o.portfolio_return_pct,o.turnover,o.drawdown,
                          o.transaction_cost_pct,o.outcome_source,o.observed_at,
                          o.payload AS outcome_payload
                   FROM forward_ablation_predictions p
                   JOIN forward_ablation_outcomes o ON o.prediction_id=p.prediction_id
                   WHERE p.prediction_id=? AND p.horizon_days=?
                     AND p.evidence_stage='forward_shadow'
                     AND p.registration_origin='automatic_primary'""",
                (source_id, horizon_days),
            ).fetchone()
            if row is None:
                raise ValueError("authoritative forward outcome not found")
            benchmark = db.execute(
                """SELECT o.portfolio_return_pct
                   FROM forward_ablation_predictions p
                   JOIN forward_ablation_outcomes o ON o.prediction_id=p.prediction_id
                   WHERE p.cohort_id=? AND p.sample_key=? AND p.horizon_days=?
                     AND p.variant='simple_baseline'
                     AND p.registration_origin='automatic_primary'""",
                (row["cohort_id"], row["sample_key"], horizon_days),
            ).fetchone()
        outcome_payload = json.loads(row["outcome_payload"] or "{}")
        quote = outcome_payload.get("quote") or {}
        if (
            quote.get("authoritative") is not True
            or quote.get("evidence_stage") != "production"
            or quote.get("trust_level")
            not in {
                "server_observed",
                "trusted_licensed",
                "exchange_or_broker_confirmed",
            }
        ):
            raise ValueError(
                "authoritative forward outcome unavailable: settlement quote is non-authoritative"
            )
        probabilities = json.loads(row["probabilities"] or "{}")
        predicted = max(probabilities, key=probabilities.get) if probabilities else None
        realized = str(row["realized_direction"])
        linked = self._run_has_link(
            run["run_id"], entity_type="forward_prediction", entity_id=source_id
        ) or (
            run.get("context_fingerprint") == row["context_fingerprint"]
            and run["evidence_boundary"] == "forward_shadow"
        )
        return {
            "source_type": "forward_sample",
            "source_id": source_id,
            "symbol": row["symbol"],
            "signal_date": row["as_of"],
            "due_at": row["due_at"],
            "settled_at": row["observed_at"],
            "start_price": float(row["start_price"]),
            "end_price": quote.get("raw_price") or outcome_payload.get("end_price"),
            "raw_return_pct": float(row["realized_return_pct"]),
            "benchmark_return_pct": float(benchmark[0]) if benchmark else None,
            "transaction_cost": float(row["transaction_cost_pct"]),
            "maximum_adverse_excursion": float(row["drawdown"]),
            "maximum_favorable_excursion": outcome_payload.get(
                "maximum_favorable_excursion"
            ),
            "predicted_direction": predicted,
            "direction_correct": predicted == realized if predicted else None,
            "evidence_boundary": "forward_shadow",
            "status": "settled",
            "run_linked": linked,
            "context_fingerprint": row["context_fingerprint"],
            "outcome_source": row["outcome_source"],
        }

    def _user_authoritative_outcome(
        self, *, run: dict[str, Any], source_id: str, horizon_days: int
    ) -> dict[str, Any]:
        self._require_tables(
            "investor_recommendation_outcomes",
            "investor_recommendations",
            purpose="authoritative user recommendation outcome",
        )
        with self.connect() as db:
            row = db.execute(
                """SELECT o.*,r.symbol,r.as_of,r.context_fingerprint,
                          r.research_run_id,r.payload AS recommendation_payload
                   FROM investor_recommendation_outcomes o
                   JOIN investor_recommendations r
                     ON r.recommendation_id=o.recommendation_id
                   WHERE o.outcome_id=? AND o.horizon_days=?""",
                (source_id, horizon_days),
            ).fetchone()
        if row is None:
            raise ValueError("authoritative user recommendation outcome not found")
        payload = json.loads(row["payload"] or "{}")
        linked = self._run_has_link(
            run["run_id"],
            entity_type="investor_recommendation",
            entity_id=row["recommendation_id"],
        ) or (
            run.get("context_fingerprint") == row["context_fingerprint"]
            and run["evidence_boundary"] == "user"
        )
        return {
            "source_type": "user_recommendation_outcome",
            "source_id": source_id,
            "symbol": row["symbol"],
            "signal_date": row["as_of"],
            "due_at": row["due_date"],
            "settled_at": row["observed_at"],
            "start_price": float(row["start_price"]),
            "end_price": float(row["end_price"]),
            "raw_return_pct": float(row["realized_return_pct"]),
            "benchmark_return_pct": payload.get("benchmark_return_pct"),
            "transaction_cost": payload.get("transaction_cost"),
            "maximum_adverse_excursion": payload.get("maximum_adverse_excursion"),
            "maximum_favorable_excursion": payload.get("maximum_favorable_excursion"),
            "predicted_direction": payload.get("predicted_direction"),
            "direction_correct": payload.get("direction_correct"),
            "evidence_boundary": "user",
            "status": "settled",
            "run_linked": linked,
            "context_fingerprint": row["context_fingerprint"],
            "outcome_source": row["source"],
        }

    def _shadow_authoritative_outcome(
        self, *, run: dict[str, Any], source_id: str, horizon_days: int
    ) -> dict[str, Any]:
        self._require_tables(
            "shadow_accounts",
            "shadow_nav",
            "trusted_calendar_days",
            purpose="authoritative shadow account cycle",
        )
        parts = source_id.split("|")
        if len(parts) != 3:
            raise ValueError(
                "shadow source_id must be account_id|signal_date|settled_date"
            )
        account_id, signal_date, settled_date = parts
        with self.connect() as db:
            account = db.execute(
                "SELECT * FROM shadow_accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            start = db.execute(
                "SELECT * FROM shadow_nav WHERE account_id=? AND nav_date=?",
                (account_id, signal_date),
            ).fetchone()
            end = db.execute(
                "SELECT * FROM shadow_nav WHERE account_id=? AND nav_date=?",
                (account_id, settled_date),
            ).fetchone()
            elapsed_sessions = int(
                db.execute(
                    """SELECT COUNT(DISTINCT trade_date) FROM trusted_calendar_days
                       WHERE namespace='production' AND is_open=1
                         AND trade_date>? AND trade_date<=?""",
                    (signal_date, settled_date),
                ).fetchone()[0]
            )
        if account is None or start is None or end is None:
            raise ValueError("authoritative shadow account cycle not found")
        if elapsed_sessions != horizon_days:
            raise ValueError("shadow account cycle horizon does not match trading calendar")
        if str(end["data_status"]) != "available":
            raise ValueError("authoritative shadow account cycle is unavailable")
        start_equity = float(start["equity"])
        end_equity = float(end["equity"])
        linked = self._run_has_link(
            run["run_id"], entity_type="shadow_account", entity_id=account_id
        ) or (
            run["evidence_boundary"] == "forward_shadow"
            and str((run.get("parameters") or {}).get("cohort_id") or "")
            == str(account["cohort_id"])
        )
        return {
            "source_type": "shadow_account_cycle",
            "source_id": source_id,
            "symbol": "__portfolio__",
            "signal_date": signal_date,
            "due_at": datetime.combine(
                date.fromisoformat(settled_date), datetime.min.time(), tzinfo=UTC
            ).isoformat(),
            "settled_at": end["created_at"],
            "start_price": start_equity,
            "end_price": end_equity,
            "raw_return_pct": (end_equity / start_equity - 1.0) * 100.0,
            "benchmark_return_pct": json.loads(end["payload"] or "{}").get(
                "benchmark_return_pct"
            ),
            "transaction_cost": float(end["cumulative_cost"])
            - float(start["cumulative_cost"]),
            "maximum_adverse_excursion": float(end["maximum_drawdown"]),
            "maximum_favorable_excursion": json.loads(end["payload"] or "{}").get(
                "maximum_favorable_excursion"
            ),
            "predicted_direction": "up"
            if str(account["variant"]) not in {"simple_benchmark"}
            else None,
            "direction_correct": None,
            "evidence_boundary": "forward_shadow",
            "status": "settled",
            "run_linked": linked,
            "cohort_id": account["cohort_id"],
        }

    def _require_tables(self, *tables: str, purpose: str) -> None:
        with self.connect() as db:
            existing = {
                str(row[0])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        missing = [table for table in tables if table not in existing]
        if missing:
            raise ValueError(
                f"{purpose} unavailable: required tables missing: {','.join(missing)}"
            )

    def save_acceptance_report(
        self, trade_date: str, *, status: str, checks: dict[str, Any], blockers: list[str]
    ) -> dict[str, Any]:
        payload = {"trade_date": trade_date, "status": status, "checks": checks, "blockers": blockers}
        with self.connect() as db:
            existing = db.execute(
                "SELECT report_id FROM next_trading_day_acceptance_reports WHERE trade_date=?",
                (trade_date,),
            ).fetchone()
            report_id = str(existing[0]) if existing else str(uuid.uuid4())
            db.execute(
                """INSERT INTO next_trading_day_acceptance_reports VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status,
                     checks=excluded.checks,blockers=excluded.blockers,
                     fingerprint=excluded.fingerprint,created_at=excluded.created_at""",
                (
                    report_id,
                    trade_date,
                    status,
                    json.dumps(sanitize_for_export(checks), ensure_ascii=False),
                    json.dumps(blockers, ensure_ascii=False),
                    _fingerprint(payload),
                    _now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM next_trading_day_acceptance_reports WHERE trade_date=?",
                (trade_date,),
            ).fetchone()
        return self._json_row(row, "checks", "blockers")

    @staticmethod
    def _json_row(row: sqlite3.Row, *fields: str) -> dict[str, Any]:
        item = dict(row)
        for field in fields:
            item[field] = json.loads(item[field])
        return item

    def _run_row(self, row: sqlite3.Row, *, idempotent: bool = False) -> dict[str, Any]:
        item = self._json_row(
            row,
            "model_routing",
            "parameters",
            "cost_budget",
            "result_summary",
            "build_state",
        )
        item["idempotent"] = idempotent
        with self.connect() as db:
            links = db.execute("SELECT * FROM unified_run_links WHERE run_id=?", (row["run_id"],)).fetchall()
            artifacts = db.execute("SELECT * FROM unified_run_artifacts WHERE run_id=?", (row["run_id"],)).fetchall()
        item["links"] = [dict(link) for link in links]
        item["artifacts"] = [self._artifact_row(artifact) for artifact in artifacts]
        return item

    def _artifact_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return self._json_row(row, "payload")

    def _checkpoint_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return self._json_row(row, "payload")

    def _reflection_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return self._json_row(row, "reflection", "candidate_lessons", "evidence_refs")


__all__ = ["ROUND8_SCHEMA_VERSION", "Round8Repository"]
