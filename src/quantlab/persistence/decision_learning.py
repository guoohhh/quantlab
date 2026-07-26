from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


DECISION_LEARNING_SCHEMA_VERSION = 1
DECISION_LEARNING_MIGRATION_IDENTITY = "decision-learning-provenance-quarantine-v1"


def apply_decision_learning_schema(path: str | Path) -> None:
    """Install the provenance schema and conservatively quarantine ambiguous rows."""

    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(resolved, timeout=30)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        _create_base_tables(db)
        _add_columns(db)
        ensure_decision_research_indexes(db)
        _quarantine_legacy_rows(db)
        _record_migration(db)
        violations = db.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("foreign key violations after decision-learning migration")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def decision_learning_schema_ready(path: str | Path) -> bool:
    resolved = Path(path)
    if not resolved.is_file():
        return False
    required = {
        "decision_runs": {
            "requested_as_of",
            "effective_as_of",
            "origin",
            "evidence_stage",
            "settlement_eligible",
            "training_eligible",
            "registration_id",
            "context_id",
            "context_fingerprint",
            "quarantine_reason",
        },
        "forecast_predictions": {
            "origin",
            "evidence_stage",
            "settlement_eligible",
            "training_eligible",
            "registration_id",
            "quarantine_reason",
        },
        "learning_samples": {
            "origin",
            "evidence_stage",
            "settlement_eligible",
            "training_eligible",
            "registration_id",
            "quarantine_reason",
        },
    }
    with sqlite3.connect(resolved, timeout=30) as db:
        for table, columns in required.items():
            present = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
            if not columns.issubset(present):
                return False
    return True


def _create_base_tables(db: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS decision_runs (
            run_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            as_of TEXT NOT NULL,
            action TEXT NOT NULL,
            confidence REAL NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS forecast_predictions (
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            as_of TEXT NOT NULL,
            horizon_days INTEGER NOT NULL,
            model TEXT NOT NULL,
            up_probability REAL NOT NULL,
            flat_probability REAL NOT NULL,
            down_probability REAL NOT NULL,
            confidence REAL NOT NULL,
            PRIMARY KEY (run_id, horizon_days)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS forecast_outcomes (
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            as_of TEXT,
            horizon_days INTEGER NOT NULL,
            realized_return_pct REAL,
            outcome TEXT,
            evaluated_at TEXT,
            PRIMARY KEY (run_id, horizon_days)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS learning_samples (
            sample_key TEXT PRIMARY KEY,
            run_id TEXT,
            source TEXT NOT NULL,
            asset_scope TEXT NOT NULL DEFAULT 'unknown',
            symbol TEXT NOT NULL,
            as_of TEXT NOT NULL,
            horizon_days INTEGER NOT NULL,
            features_json TEXT NOT NULL,
            expected_return_pct REAL,
            outcome TEXT,
            realized_return_pct REAL,
            evaluated_at TEXT,
            context_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS quantlab_migration_registry (
            component TEXT NOT NULL,
            version INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            PRIMARY KEY(component,version)
        )
        """,
    )
    for statement in statements:
        db.execute(statement)


def _add_columns(db: sqlite3.Connection) -> None:
    additions = {
        "decision_runs": {
            "requested_as_of": "TEXT",
            "effective_as_of": "TEXT",
            "origin": "TEXT NOT NULL DEFAULT 'legacy_unclassified'",
            "evidence_stage": "TEXT NOT NULL DEFAULT 'legacy_quarantined'",
            "settlement_eligible": "INTEGER NOT NULL DEFAULT 0",
            "training_eligible": "INTEGER NOT NULL DEFAULT 0",
            "registration_id": "TEXT",
            "context_id": "TEXT",
            "context_fingerprint": "TEXT",
            "quarantine_reason": "TEXT",
        },
        "forecast_predictions": {
            "origin": "TEXT NOT NULL DEFAULT 'legacy_unclassified'",
            "evidence_stage": "TEXT NOT NULL DEFAULT 'legacy_quarantined'",
            "settlement_eligible": "INTEGER NOT NULL DEFAULT 0",
            "training_eligible": "INTEGER NOT NULL DEFAULT 0",
            "registration_id": "TEXT",
            "quarantine_reason": "TEXT",
        },
        "learning_samples": {
            "origin": "TEXT NOT NULL DEFAULT 'legacy_unclassified'",
            "evidence_stage": "TEXT NOT NULL DEFAULT 'legacy_quarantined'",
            "settlement_eligible": "INTEGER NOT NULL DEFAULT 0",
            "training_eligible": "INTEGER NOT NULL DEFAULT 0",
            "registration_id": "TEXT",
            "quarantine_reason": "TEXT",
        },
    }
    for table, columns in additions.items():
        present = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in present:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def ensure_decision_research_indexes(db: sqlite3.Connection) -> None:
    """Keep the lightweight research index fast without touching report payloads."""

    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_decision_runs_created_at "
        "ON decision_runs(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_decision_runs_action_created_at "
        "ON decision_runs(action, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_decision_runs_evidence_stage_created_at "
        "ON decision_runs(evidence_stage, created_at DESC)",
    ):
        db.execute(statement)


def _quarantine_legacy_rows(db: sqlite3.Connection) -> None:
    rows = db.execute(
        "SELECT run_id,symbol,as_of,payload FROM decision_runs "
        "WHERE origin='legacy_unclassified'"
    ).fetchall()
    for run_id, symbol, effective, raw_payload in rows:
        requested, context_id, fingerprint = _recover_identity(
            str(symbol), str(effective), raw_payload
        )
        db.execute(
            """UPDATE decision_runs SET requested_as_of=?,effective_as_of=?,context_id=?,
               context_fingerprint=?,evidence_stage='legacy_quarantined',
               settlement_eligible=0,training_eligible=0,
               quarantine_reason='legacy_origin_unproven'
               WHERE run_id=?""",
            (requested, effective, context_id, fingerprint, run_id),
        )
    db.execute(
        """UPDATE forecast_predictions SET origin='legacy_unclassified',
           evidence_stage='legacy_quarantined',settlement_eligible=0,
           training_eligible=0,quarantine_reason='legacy_origin_unproven'
           WHERE origin='legacy_unclassified'"""
    )
    learning_rows = db.execute(
        "SELECT sample_key,source,context_json FROM learning_samples "
        "WHERE origin='legacy_unclassified'"
    ).fetchall()
    for sample_key, source, raw_context in learning_rows:
        context = _json_object(raw_context)
        explicit = context.get("training_eligible") is True
        if str(source) == "live_decision":
            origin = "legacy_unclassified"
            stage = "legacy_quarantined"
            eligible = 0
            reason = "legacy_live_decision_origin_unproven"
        else:
            origin = _legacy_learning_origin(str(source))
            stage = "historical_training" if explicit else "research_only"
            eligible = int(explicit)
            reason = None if explicit else "legacy_training_eligibility_unproven"
        db.execute(
            """UPDATE learning_samples SET origin=?,evidence_stage=?,
               settlement_eligible=0,training_eligible=?,quarantine_reason=?
               WHERE sample_key=?""",
            (origin, stage, eligible, reason, sample_key),
        )


def _recover_identity(
    symbol: str, effective_as_of: str, raw_payload: Any
) -> tuple[str | None, str | None, str | None]:
    payload = _json_object(raw_payload)
    context = payload.get("research_context")
    context = context if isinstance(context, dict) else {}
    price_history = context.get("price_history")
    price_history = price_history if isinstance(price_history, dict) else {}
    requested = _iso_date(price_history.get("requested_cutoff_date"))
    pack = context.get("analysis_context_pack")
    pack = pack if isinstance(pack, dict) else {}
    decision = payload.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    if decision.get("symbol") not in {None, symbol} or pack.get("symbol") not in {
        None,
        symbol,
    }:
        return None, None, None
    pack_as_of = _iso_date(pack.get("as_of"))
    decision_as_of = _iso_date(decision.get("as_of"))
    if any(item not in {None, effective_as_of} for item in (pack_as_of, decision_as_of)):
        return None, None, None
    context_id = decision.get("context_id") or pack.get("context_id")
    fingerprint = decision.get("context_fingerprint") or pack.get("fingerprint")
    return (
        requested,
        str(context_id) if context_id else None,
        str(fingerprint) if fingerprint else None,
    )


def _legacy_learning_origin(source: str) -> str:
    if source in {"historical_factor", "stock_market_point_in_time"}:
        return "historical_research"
    if "test" in source:
        return "test_research"
    if "demo" in source or "replay" in source:
        return "demo_research"
    return "legacy_unclassified"


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _iso_date(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _record_migration(db: sqlite3.Connection) -> None:
    checksum = hashlib.sha256(
        DECISION_LEARNING_MIGRATION_IDENTITY.encode("utf-8")
    ).hexdigest()
    existing = db.execute(
        "SELECT checksum FROM quantlab_migration_registry "
        "WHERE component='decision_learning' AND version=?",
        (DECISION_LEARNING_SCHEMA_VERSION,),
    ).fetchone()
    if existing is not None and str(existing[0]) != checksum:
        raise RuntimeError("migration checksum mismatch for decision_learning version 1")
    db.execute(
        """INSERT OR IGNORE INTO quantlab_migration_registry(
           component,version,ordinal,checksum,applied_at) VALUES(?,?,?,?,?)""",
        (
            "decision_learning",
            DECISION_LEARNING_SCHEMA_VERSION,
            0,
            checksum,
            datetime.now(UTC).isoformat(),
        ),
    )


__all__ = [
    "DECISION_LEARNING_MIGRATION_IDENTITY",
    "DECISION_LEARNING_SCHEMA_VERSION",
    "apply_decision_learning_schema",
    "decision_learning_schema_ready",
]
