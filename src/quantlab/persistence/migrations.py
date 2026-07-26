from __future__ import annotations

import hashlib
import gc
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


COMPONENT_ORDER = (
    "decision_learning",
    "simulator",
    "chat",
    "notifications",
    "evidence",
    "strategy_evidence",
    "jobs",
    "round5",
    "round6",
    "round7",
    "round8",
    "round9",
    "wide_research",
)
CURRENT_COMPONENT_VERSIONS = {
    "decision_learning": 1,
    "simulator": 6,
    "chat": 5,
    "notifications": 4,
    "evidence": 7,
    "strategy_evidence": 6,
    "jobs": 6,
    "round5": 3,
    "round6": 1,
    "round7": 1,
    "round8": 1,
    "round9": 4,
    "wide_research": 1,
}

_INITIALIZED_DATABASES: dict[str, tuple[int, int]] = {}
_INITIALIZATION_LOCK = threading.RLock()


def record_component_migration(
    path: str | Path,
    *,
    component: str,
    version: int,
    migration_identity: str,
) -> None:
    if component not in COMPONENT_ORDER:
        raise ValueError(f"unknown migration component: {component}")
    checksum = hashlib.sha256(migration_identity.encode("utf-8")).hexdigest()
    with sqlite3.connect(path, timeout=30) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS quantlab_migration_registry (
                component TEXT NOT NULL,
                version INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                PRIMARY KEY(component,version)
            )
            """
        )
        existing = db.execute(
            """
            SELECT checksum FROM quantlab_migration_registry
            WHERE component=? AND version=?
            """,
            (component, version),
        ).fetchone()
        if existing is not None and existing[0] != checksum:
            raise RuntimeError(
                f"migration checksum mismatch for {component} version {version}"
            )
        db.execute(
            """
            INSERT OR IGNORE INTO quantlab_migration_registry(
                component,version,ordinal,checksum,applied_at
            ) VALUES(?,?,?,?,?)
            """,
            (
                component,
                version,
                COMPONENT_ORDER.index(component),
                checksum,
                datetime.now(UTC).isoformat(),
            ),
        )


def migration_status(path: str | Path) -> dict[str, Any]:
    with sqlite3.connect(path, timeout=30) as db:
        table = db.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='quantlab_migration_registry'
            """
        ).fetchone()
        rows = (
            db.execute(
                """
                SELECT * FROM quantlab_migration_registry
                ORDER BY ordinal,version
                """
            ).fetchall()
            if table
            else []
        )
    return {
        "component_order": list(COMPONENT_ORDER),
        "migrations": [
            {
                "component": row[0],
                "version": row[1],
                "ordinal": row[2],
                "checksum": row[3],
                "applied_at": row[4],
            }
            for row in rows
        ],
    }


def initialize_or_upgrade_database(path: str | Path) -> dict[str, Any]:
    """Upgrade a staged database, verify it, then atomically publish it."""
    from quantlab.persistence.chat import ChatRepository
    from quantlab.persistence.evidence import EvidenceRepository
    from quantlab.persistence.jobs import JobRepository
    from quantlab.persistence.notifications import NotificationRepository
    from quantlab.persistence.simulator import UserPaperTradingRepository
    from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
    from quantlab.persistence.round5 import Round5Repository
    from quantlab.persistence.round6 import Round6Repository
    from quantlab.persistence.round7 import Round7Repository
    from quantlab.persistence.round8 import Round8Repository
    from quantlab.persistence.round9 import Round9Repository
    from quantlab.persistence.wide_research import WideResearchRepository
    from quantlab.persistence.decision_learning import apply_decision_learning_schema

    resolved_path = Path(path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    pending = _pending_migrations(resolved_path)
    if not pending:
        checks = _database_checks(resolved_path)
        return {**migration_status(resolved_path), "pre_upgrade_backup": None, **checks}
    backup = _backup_before_pending_upgrade(resolved_path, pending=pending)
    working = resolved_path.with_name(
        f".{resolved_path.name}.{os.getpid()}.{time.time_ns()}.migration"
    )
    if resolved_path.is_file() and resolved_path.stat().st_size:
        with sqlite3.connect(resolved_path, timeout=30) as source, sqlite3.connect(
            working, timeout=30
        ) as target:
            source.backup(target)
    constructors = {
        "decision_learning": apply_decision_learning_schema,
        "simulator": UserPaperTradingRepository,
        "chat": ChatRepository,
        "notifications": NotificationRepository,
        "evidence": EvidenceRepository,
        "strategy_evidence": StrategyEvidenceRepository,
        "jobs": JobRepository,
        "round5": Round5Repository,
        "round6": Round6Repository,
        "round7": Round7Repository,
        "round8": Round8Repository,
        "round9": Round9Repository,
        "wide_research": WideResearchRepository,
    }
    try:
        for component in COMPONENT_ORDER:
            constructors[component](working)
        checks = _database_checks(working)
        if checks["integrity_check"] != "ok" or checks["foreign_key_violations"]:
            raise RuntimeError("database verification failed after migration")
        gc.collect()
        _publish_staged_database(working, resolved_path)
    except Exception:
        gc.collect()
        try:
            working.unlink(missing_ok=True)
        except PermissionError:
            pass
        raise
    gc.collect()
    try:
        working.unlink(missing_ok=True)
    except PermissionError:
        pass
    return {**migration_status(resolved_path), "pre_upgrade_backup": backup, **checks}


def ensure_database_initialized(path: str | Path) -> dict[str, Any]:
    """Process-idempotent production entrypoint guard."""

    resolved = Path(path).resolve()
    key = str(resolved).casefold()
    with _INITIALIZATION_LOCK:
        if key in _INITIALIZED_DATABASES and not _pending_migrations(resolved):
            return {**migration_status(resolved), "pre_upgrade_backup": None}
        result = initialize_or_upgrade_database(resolved)
        _INITIALIZED_DATABASES[key] = _database_signature(resolved)
        return result


def _backup_before_pending_upgrade(
    path: Path, *, pending: list[str] | None = None
) -> dict[str, Any] | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    with sqlite3.connect(path, timeout=30) as db:
        tables = int(
            db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        )
        registry_exists = db.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='quantlab_migration_registry'"""
        ).fetchone()
        applied = {
            (str(row[0]), int(row[1]))
            for row in db.execute(
                "SELECT component,version FROM quantlab_migration_registry"
            ).fetchall()
        } if registry_exists else set()
    pending = pending or [
        f"{component}:{version}"
        for component, version in CURRENT_COMPONENT_VERSIONS.items()
        if (component, version) not in applied
    ]
    if not tables or not pending:
        return None
    backup_root = path.parent / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_root / f"{path.stem}-{timestamp}-pre-migration{path.suffix or '.db'}"
    with sqlite3.connect(path, timeout=30) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    checks = _database_checks(destination)
    if checks["integrity_check"] != "ok" or checks["foreign_key_violations"]:
        raise RuntimeError("pre-migration backup verification failed")
    return {
        "database": str(destination),
        "sha256": digest,
        "reason": "pre_pending_component_upgrade",
        "pending_migrations": pending,
        **checks,
    }


def _pending_migrations(path: Path) -> list[str]:
    if not path.is_file() or path.stat().st_size == 0:
        return [f"{name}:{version}" for name, version in CURRENT_COMPONENT_VERSIONS.items()]
    with sqlite3.connect(path, timeout=30) as db:
        registry_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='quantlab_migration_registry'"
        ).fetchone()
        applied = (
            {
                (str(row[0]), int(row[1]))
                for row in db.execute(
                    "SELECT component,version FROM quantlab_migration_registry"
                ).fetchall()
            }
            if registry_exists
            else set()
        )
    pending = [
        f"{component}:{version}"
        for component, version in CURRENT_COMPONENT_VERSIONS.items()
        if (component, version) not in applied
    ]
    from quantlab.persistence.decision_learning import decision_learning_schema_ready

    if not decision_learning_schema_ready(path) and "decision_learning:1" not in pending:
        raise RuntimeError("decision-learning registry exists but required schema is missing")
    return pending


def _database_checks(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"integrity_check": "unavailable", "foreign_key_violations": []}
    with sqlite3.connect(path, timeout=30) as db:
        integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        violations = [list(row) for row in db.execute("PRAGMA foreign_key_check").fetchall()]
    return {"integrity_check": integrity, "foreign_key_violations": violations}


def _database_signature(path: Path) -> tuple[int, int]:
    if not path.is_file():
        return (0, 0)
    stat = path.stat()
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _publish_staged_database(source_path: Path, target_path: Path) -> None:
    source = sqlite3.connect(source_path, timeout=30)
    target = sqlite3.connect(target_path, timeout=30)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


__all__ = [
    "COMPONENT_ORDER",
    "CURRENT_COMPONENT_VERSIONS",
    "initialize_or_upgrade_database",
    "ensure_database_initialized",
    "migration_status",
    "record_component_migration",
]
