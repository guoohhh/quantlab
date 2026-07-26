from __future__ import annotations

import hashlib
import gc
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantlab.config import Settings


def backup_database(settings: Settings, *, label: str = "manual") -> dict[str, Any]:
    source = settings.resolve(settings.get("system.database_path")).resolve()
    backup_root = (
        settings.resolve(settings.get("runtime.backup_directory", "data/backups")).resolve()
    )
    backup_root.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(character for character in label if character.isalnum() or character in "-_")
    safe_label = safe_label or "manual"
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = (backup_root / f"quantlab-{timestamp}-{safe_label}.db").resolve()
    if destination.parent != backup_root:
        raise ValueError("backup destination escaped the configured backup directory")
    with closing(sqlite3.connect(source)) as source_db, closing(
        sqlite3.connect(destination)
    ) as backup_db:
        with backup_db:
            source_db.backup(backup_db)
    sha256 = _file_sha256(destination)
    manifest = {
        "database": str(destination),
        "sha256": sha256,
        "size_bytes": destination.stat().st_size,
        "created_at": datetime.now(UTC).isoformat(),
        "source": str(source),
    }
    destination.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def restore_database(
    settings: Settings,
    *,
    backup_path: str | Path,
    expected_sha256: str,
    confirm: bool,
    maintenance_mode: bool = False,
) -> dict[str, Any]:
    if not confirm:
        raise ValueError("database restore requires explicit confirmation")
    if not maintenance_mode and os.getenv("QUANTLAB_MAINTENANCE_MODE", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        raise ValueError("database restore is a maintenance-mode CLI operation")
    source = Path(backup_path).resolve()
    backup_root = settings.resolve(
        settings.get("runtime.backup_directory", "data/backups")
    ).resolve()
    if backup_root not in source.parents or not source.is_file():
        raise ValueError("restore source must be a file inside the configured backup directory")
    actual_sha256 = _file_sha256(source)
    if actual_sha256.lower() != expected_sha256.lower():
        raise ValueError("backup checksum does not match")
    target = settings.resolve(settings.get("system.database_path")).resolve()
    lock_path = target.with_suffix(target.suffix + ".maintenance.lock")
    descriptor = None
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, f"restore:{datetime.now(UTC).isoformat()}".encode("utf-8"))
        with closing(sqlite3.connect(target, timeout=30)) as active_db:
            table = active_db.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='background_jobs'
                """
            ).fetchone()
            active_workers = (
                int(
                    active_db.execute(
                        "SELECT COUNT(*) FROM background_jobs WHERE status='running'"
                    ).fetchone()[0]
                )
                if table
                else 0
            )
            if active_workers:
                raise ValueError("database restore requires all Workers to be stopped")
        safety_copy = backup_database(settings, label="pre-restore")
        with closing(sqlite3.connect(source)) as source_db:
            source_integrity = source_db.execute("PRAGMA integrity_check").fetchone()[0]
            if source_integrity != "ok":
                raise ValueError(f"backup integrity check failed: {source_integrity}")
            with closing(sqlite3.connect(target, timeout=30)) as target_db:
                source_db.backup(target_db)
                target_db.commit()
                integrity = target_db.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise ValueError(f"restored database integrity check failed: {integrity}")
    finally:
        if descriptor is not None:
            os.close(descriptor)
            lock_path.unlink(missing_ok=True)
    return {
        "restored": True,
        "database": str(target),
        "source_backup": str(source),
        "sha256": actual_sha256,
        "pre_restore_backup": safety_copy,
    }


def verify_database_backup(
    settings: Settings,
    *,
    backup_path: str | Path,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    source = Path(backup_path).resolve()
    backup_root = settings.resolve(
        settings.get("runtime.backup_directory", "data/backups")
    ).resolve()
    if backup_root not in source.parents or not source.is_file():
        raise ValueError("backup must be a file inside the configured backup directory")
    actual_sha256 = _file_sha256(source)
    if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
        raise ValueError("backup checksum does not match")
    with closing(sqlite3.connect(source, timeout=30)) as db:
        integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        quick_check = str(db.execute("PRAGMA quick_check").fetchone()[0])
        table_count = int(
            db.execute(
                """SELECT COUNT(*) FROM sqlite_master
                   WHERE type='table' AND name NOT LIKE 'sqlite_%'"""
            ).fetchone()[0]
        )
    if integrity != "ok" or quick_check != "ok":
        raise ValueError(f"backup integrity check failed: {integrity}/{quick_check}")
    return {
        "verified": True,
        "database": str(source),
        "sha256": actual_sha256,
        "size_bytes": source.stat().st_size,
        "table_count": table_count,
        "integrity_check": integrity,
        "quick_check": quick_check,
    }


def restore_database_dry_run(
    settings: Settings,
    *,
    backup_path: str | Path,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    verification = verify_database_backup(
        settings,
        backup_path=backup_path,
        expected_sha256=expected_sha256,
    )
    from quantlab.persistence.migrations import initialize_or_upgrade_database

    with tempfile.TemporaryDirectory(prefix="quantlab-restore-dry-run-") as directory:
        candidate = Path(directory) / "candidate.db"
        with closing(sqlite3.connect(verification["database"], timeout=30)) as source_db, closing(
            sqlite3.connect(candidate, timeout=30)
        ) as candidate_db:
            source_db.backup(candidate_db)
        migration = initialize_or_upgrade_database(candidate)
        with closing(sqlite3.connect(candidate, timeout=30)) as db:
            integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise ValueError(f"restore dry-run integrity check failed: {integrity}")
        # Some repository constructors are short-lived and rely on finalizers.  Force
        # those handles closed before Windows attempts to remove the disposable copy.
        gc.collect()
    return {
        "dry_run": True,
        "production_database_modified": False,
        "backup": verification,
        "migration": migration,
        "post_migration_integrity": integrity,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "backup_database",
    "restore_database",
    "restore_database_dry_run",
    "verify_database_backup",
]
