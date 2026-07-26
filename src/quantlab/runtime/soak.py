from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from quantlab.config import Settings
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.round6 import Round6Repository
from quantlab.persistence.round7 import Round7Repository
from quantlab.persistence.round8 import Round8Repository


def capture_soak_observation(
    settings: Settings,
    *,
    observed_at: datetime | None = None,
    source: str = "scheduler",
) -> dict[str, Any]:
    observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
    path = settings.resolve(settings.get("system.database_path"))
    round5 = Round5Repository(path)
    round6 = Round6Repository(path)
    processes = round6.processes()
    maximum_age = float(settings.get("runtime.runtime_health_maximum_age_seconds", 90))
    process_summary = {}
    for process in processes:
        heartbeat = datetime.fromisoformat(process["heartbeat_at"]).astimezone(UTC)
        age = max(0.0, (observed - heartbeat).total_seconds())
        process_summary[process["process_type"]] = {
            "status": process["status"],
            "healthy": bool(process["status"] == "running" and age <= maximum_age),
            "heartbeat_age_seconds": round(age, 3),
            "started_at": process["started_at"],
            "instance_id": process["instance_id"],
        }
    experiment = round5.primary_experiment()
    registrations = round5.registration_runs(experiment["experiment_id"]) if experiment else []
    with sqlite3.connect(path, timeout=30) as db:
        db.row_factory = sqlite3.Row
        selection_rows = Round8Repository(path).provider_selections(limit=1_000)
        latest_refresh_id = selection_rows[0]["refresh_id"] if selection_rows else None
        payload = {
            "processes": process_summary,
            "data_sources": {
                item["batch_type"]: {
                    "status": item["status"],
                    "minimum_ready": bool(item["minimum_ready"]),
                    "record_count": int(item["symbol_count"]),
                    "field_coverage": float(item["field_coverage"]),
                    "consecutive_failures": int(item["consecutive_failures"]),
                    "last_success_at": item["last_success_at"],
                }
                for item in round6.data_source_states()
            },
            "provider_states": [
                {
                    "provider": item["provider_key"],
                    "component": item["component"],
                    "status": item["status"],
                    "consecutive_failures": item["consecutive_failures"],
                    "last_success_at": item["last_success_at"],
                }
                for item in Round7Repository(path).provider_states()
            ],
            "provider_selections": [
                item
                for item in selection_rows
                if item["refresh_id"] == latest_refresh_id
            ],
            "jobs": _job_counts(db),
            "formal_experiment": {
                "primary_started": experiment is not None,
                "primary_start_count": _count(db, "forward_experiments", "is_primary=1"),
                "formal_samples": sum(int(item["registered_samples"]) for item in registrations),
                "shadow_accounts": _count(db, "shadow_accounts"),
                "forward_outcomes": _count(db, "forward_ablation_outcomes"),
            },
            "orders": {
                "pending_user_orders": _count(
                    db,
                    "user_paper_orders",
                    "status IN ('pending','partially_filled')",
                ),
            },
            "notifications": {
                "unread": _count(
                    db,
                    "notifications",
                    "read_at IS NULL AND archived_at IS NULL",
                ),
                "outbox_pending": _count(db, "notification_outbox", "status='pending'"),
                "outbox_dead_letter": _count(
                    db, "notification_outbox", "status='dead_letter'"
                ),
            },
            "llm": _llm_counts(db, observed),
            "database": {
                "bytes": path.stat().st_size if path.is_file() else 0,
                "wal_bytes": path.with_name(path.name + "-wal").stat().st_size
                if path.with_name(path.name + "-wal").is_file()
                else 0,
            },
            "backups": _backup_summary(settings),
        }
    return Round7Repository(path).save_soak_observation(
        payload,
        source=source,
        observed_at=observed,
    )


def soak_report(
    settings: Settings,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> dict[str, Any]:
    path = settings.resolve(settings.get("system.database_path"))
    observations = Round7Repository(path).soak_observations(
        start_at=start_at,
        end_at=end_at,
        limit=100_000,
    )
    ordered = sorted(observations, key=lambda item: item["observed_at"])
    if not ordered:
        return {
            "status": "no_observations",
            "observation_count": 0,
            "actual_duration_seconds": 0.0,
            "claim_boundary": "No continuous-runtime duration is claimed without observations.",
        }
    first = datetime.fromisoformat(ordered[0]["observed_at"]).astimezone(UTC)
    last = datetime.fromisoformat(ordered[-1]["observed_at"]).astimezone(UTC)
    process_samples: dict[str, list[bool]] = {}
    provider_switches = 0
    provider_switch_events: list[dict[str, Any]] = []
    previous_selected: dict[str, str] = {}
    restart_counts: Counter[str] = Counter()
    previous_instances: dict[str, str] = {}
    for observation in ordered:
        payload = observation["payload"]
        for name, process in payload.get("processes", {}).items():
            process_samples.setdefault(name, []).append(bool(process.get("healthy")))
            instance = str(process.get("instance_id") or "")
            if name in previous_instances and previous_instances[name] != instance:
                restart_counts[name] += 1
            previous_instances[name] = instance
        for selection in payload.get("provider_selections", []):
            name = str(selection.get("selected_provider") or "")
            if not name:
                continue
            component = str(selection.get("component"))
            if component in previous_selected and previous_selected[component] != name:
                provider_switches += 1
                provider_switch_events.append(
                    {
                        "component": component,
                        "from_provider": previous_selected[component],
                        "to_provider": name,
                        "observed_at": observation["observed_at"],
                        "reason": selection.get("selection_reason"),
                        "related_failures": selection.get("related_failures") or [],
                    }
                )
            previous_selected[component] = name
    latest = ordered[-1]["payload"]
    return {
        "status": "observed",
        "observation_count": len(ordered),
        "first_observed_at": first.isoformat(),
        "last_observed_at": last.isoformat(),
        "actual_duration_seconds": max(0.0, (last - first).total_seconds()),
        "process_availability": {
            name: sum(values) / len(values) for name, values in process_samples.items()
        },
        "restart_counts": dict(restart_counts),
        "provider_switches": provider_switches,
        "provider_switch_events": provider_switch_events,
        "latest": latest,
        "claim_boundary": (
            "Duration is the actual interval between stored observations. Isolated accelerated "
            "tests and historical replay are not counted as real multi-day runtime evidence."
        ),
    }


def _job_counts(db: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(db, "background_jobs"):
        return {}
    rows = db.execute(
        "SELECT status,COUNT(*) AS count FROM background_jobs GROUP BY status"
    ).fetchall()
    counts = {str(row["status"]): int(row["count"]) for row in rows}
    duplicate_groups = int(
        db.execute(
            """SELECT COUNT(*) FROM (
                   SELECT idempotency_key,COUNT(*) AS count FROM background_jobs
                   GROUP BY idempotency_key HAVING COUNT(*)>1
               )"""
        ).fetchone()[0]
    )
    return {"by_status": counts, "duplicate_idempotency_groups": duplicate_groups}


def _llm_counts(db: sqlite3.Connection, observed: datetime) -> dict[str, Any]:
    if not _table_exists(db, "llm_governed_calls"):
        return {"calls_24h": 0, "failures_24h": 0, "cost_usd_24h": 0.0}
    row = db.execute(
        """SELECT COUNT(*),SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),
                  COALESCE(SUM(estimated_cost_usd),0)
           FROM llm_governed_calls WHERE created_at>=?""",
        ((observed - timedelta(hours=24)).isoformat(),),
    ).fetchone()
    return {
        "calls_24h": int(row[0] or 0),
        "failures_24h": int(row[1] or 0),
        "cost_usd_24h": float(row[2] or 0.0),
    }


def _backup_summary(settings: Settings) -> dict[str, Any]:
    root = settings.resolve(settings.get("runtime.backup_directory", "data/backups"))
    manifests = sorted(root.glob("*.manifest.json"), key=lambda item: item.stat().st_mtime)
    return {
        "count": len(manifests),
        "latest": str(manifests[-1]) if manifests else None,
    }


def _count(db: sqlite3.Connection, table: str, where: str | None = None) -> int:
    if not _table_exists(db, table):
        return 0
    query = f'SELECT COUNT(*) FROM "{table}"'  # noqa: S608 - internal fixed table names only
    if where:
        query += f" WHERE {where}"
    return int(db.execute(query).fetchone()[0])


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


__all__ = ["capture_soak_observation", "soak_report"]
