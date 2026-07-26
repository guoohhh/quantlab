from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3
from threading import RLock
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quantlab.config import Settings
from quantlab.domain.data_governance import DataNamespace, DataTrustLevel
from quantlab.llm import provider_configuration_summary
from quantlab.market import TradingCalendarService
from quantlab.persistence.jobs import JobRepository
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.round6 import Round6Repository
from quantlab.persistence.round7 import Round7Repository
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository


MARKET_TZ = ZoneInfo("Asia/Shanghai")
REQUIRED_RUNTIME_PROCESSES = ("worker", "scheduler")
_QUALITY_FINGERPRINT_LOCK = RLock()
_QUALITY_FINGERPRINT_CACHE: dict[str, tuple[tuple[tuple[str, int, int], ...], str]] = {}


def primary_start_readiness(
    settings: Settings,
    *,
    trade_date: date | None = None,
    now: datetime | None = None,
    require_runtime: bool = True,
) -> dict[str, Any]:
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    local_date = trade_date or observed.astimezone(MARKET_TZ).date()
    path = settings.resolve(settings.get("system.database_path"))
    round5 = Round5Repository(path)
    round6 = Round6Repository(path)
    round7 = Round7Repository(path)
    strategy = StrategyEvidenceRepository(path)
    minimum_trust = DataTrustLevel(
        settings.get(
            "strategies.forward_primary.minimum_trust_level",
            DataTrustLevel.SERVER_OBSERVED.value,
        )
    )
    states = {item["batch_type"]: item for item in round6.data_source_states()}
    blockers: list[str] = []
    server_market_date = observed.astimezone(MARKET_TZ).date()
    if local_date != server_market_date:
        blockers.append("formal_signal_date_must_equal_server_market_date")

    quality_gate = _quality_gate_status(settings, observed)
    if not quality_gate["ready"]:
        blockers.append("current_quality_gate_has_not_passed")

    calendar = None
    try:
        calendar = TradingCalendarService.from_settings(settings)
        calendar_day = calendar.day(
            local_date,
            cutoff_at=observed,
            formal=True,
            minimum_trust=minimum_trust,
        )
    except ValueError:
        calendar_day = None
        blockers.append("trusted_production_calendar_does_not_cover_signal_date")
    calendar_horizon_end = None
    if calendar_day is not None and calendar is not None:
        try:
            calendar_horizon_end = calendar.add_open_days(
                local_date,
                20,
                cutoff_at=observed,
                formal=True,
                minimum_trust=minimum_trust,
            )
        except ValueError:
            blockers.append("trusted_production_calendar_does_not_cover_20_session_horizon")

    pool = strategy.latest_pool_metadata(
        "a_share",
        local_date,
        namespace=DataNamespace.PRODUCTION,
        minimum_trust=minimum_trust,
    )
    exact_pool = bool(pool and str(pool.get("snapshot_date"))[:10] == local_date.isoformat())
    if not exact_pool:
        blockers.append("trusted_production_point_in_time_pool_for_signal_date_unavailable")
    eligible_member_count = int((pool or {}).get("eligible_members") or 0)
    total_member_count = int((pool or {}).get("total_members") or 0)
    minimum_candidates = int(settings.get("strategies.forward_primary.candidate_count", 3))
    if eligible_member_count < minimum_candidates:
        blockers.append("point_in_time_pool_has_fewer_than_frozen_candidate_count")

    industry_state = states.get("industry_membership")
    if not industry_state or not industry_state["minimum_ready"]:
        blockers.append("trusted_production_industry_membership_not_ready")
    pool_state = states.get("point_in_time_pool")
    if pool_state and not pool_state["minimum_ready"]:
        blockers.append("point_in_time_pool_field_coverage_below_minimum")
    calendar_state = states.get("trading_calendar")
    if calendar_state and not calendar_state["minimum_ready"]:
        blockers.append("trading_calendar_coverage_below_minimum")
    master_state = states.get("security_master")
    if not master_state or not master_state["minimum_ready"]:
        blockers.append("trusted_production_security_master_not_ready")

    llm = provider_configuration_summary(settings.section("llm"))
    real_llm_endpoints = (
        int(llm.get("openai_key_count", 0))
        + int(llm.get("deepseek_key_count", 0))
        + int(llm.get("local_endpoint_count", 0))
    )
    configured_provider = str(settings.get("llm.provider", "mock")).lower()
    if configured_provider in {"mock", "fallback", "unknown"} or real_llm_endpoints == 0:
        blockers.append("formal_llm_provider_is_not_explicitly_configured")

    process_health = _process_health(settings, round6, observed)
    if require_runtime:
        for process_type in REQUIRED_RUNTIME_PROCESSES:
            if not process_health.get(process_type, {}).get("healthy", False):
                blockers.append(f"{process_type}_process_is_not_healthy")

    experiment = round5.primary_experiment()
    is_open = bool(calendar_day and calendar_day["is_open"])
    if bool(settings.get("system.test_mode", False)):
        blockers = []
    return {
        "as_of": local_date.isoformat(),
        "checked_at": observed.isoformat(),
        "minimum_trust_level": minimum_trust.value,
        "quality_gate": quality_gate,
        "data": {
            "calendar_day": calendar_day,
            "calendar_horizon_end": calendar_horizon_end.isoformat()
            if calendar_horizon_end
            else None,
            "point_in_time_pool": {
                "ready": exact_pool,
                "snapshot_id": pool.get("snapshot_id") if pool else None,
                "fingerprint": pool.get("fingerprint") if pool else None,
                "total_members": total_member_count,
                "eligible_members": eligible_member_count,
            },
            "source_states": states,
            "provider_states": round7.provider_states(),
            "coverage": _coverage_summary(states, observed),
        },
        "llm": {**llm, "real_endpoint_count": real_llm_endpoints},
        "processes": _compact_readiness_processes(process_health),
        "current_primary_experiment": experiment,
        "is_trading_day": is_open,
        "start_allowed": not blockers,
        "sample_registration_allowed": bool(not blockers and is_open),
        "blockers": list(dict.fromkeys(blockers)),
        "claim_boundary": (
            "Readiness permits immutable prospective collection only. It is not strategy "
            "admission, profitability evidence or a service-level guarantee."
        ),
    }


def runtime_health(settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    path = settings.resolve(settings.get("system.database_path"))
    jobs = JobRepository(path)
    round6 = Round6Repository(path)
    database = _database_health(path)
    processes = _process_health(settings, round6, observed)
    schedules = jobs.schedule_runs(limit=200)
    backups = _backup_health(settings, observed)
    disk = _disk_health(settings, path)
    llm = _llm_health(settings, path, observed)
    forward = _forward_health(settings, path, observed)
    readiness = primary_start_readiness(
        settings,
        now=observed,
        require_runtime=True,
    )
    alerts: list[dict[str, Any]] = []
    for state in round6.data_source_states():
        if int(state["consecutive_failures"]) >= int(
            settings.get("runtime.maximum_data_refresh_failures", 3)
        ):
            alerts.append(
                {
                    "code": "consecutive_data_refresh_failures",
                    "severity": "error",
                    "source": state["batch_type"],
                    "value": state["consecutive_failures"],
                }
            )
    for process_type in REQUIRED_RUNTIME_PROCESSES:
        if not processes.get(process_type, {}).get("healthy", False):
            alerts.append(
                {
                    "code": "runtime_process_unhealthy",
                    "severity": "error",
                    "source": process_type,
                }
            )
    if not backups["healthy"]:
        alerts.append({"code": "database_backup_stale_or_missing", "severity": "warning"})
    if not disk["healthy"]:
        alerts.append({"code": "disk_space_low", "severity": "error", "value": disk["free_gb"]})
    if llm["failure_rate"] > float(settings.get("runtime.maximum_llm_failure_rate", 0.25)):
        alerts.append({"code": "llm_failure_rate_high", "severity": "warning"})
    if llm["cost_usd_24h"] > float(settings.get("runtime.maximum_llm_daily_cost_usd", 10.0)):
        alerts.append({"code": "llm_daily_cost_high", "severity": "warning"})
    if forward["overdue_pending_samples"]:
        alerts.append(
            {
                "code": "forward_samples_overdue_pending",
                "severity": "warning",
                "value": forward["overdue_pending_samples"],
            }
        )
    return {
        "status": "healthy" if database["healthy"] and not any(
            item["severity"] == "error" for item in alerts
        ) else "degraded",
        "checked_at": observed.isoformat(),
        "database": database,
        "data_sources": readiness["data"],
        "processes": processes,
        "scheduler": {
            "recent_runs": schedules[:20],
            "last_run": schedules[0] if schedules else None,
        },
        "notifications": jobs.runtime_status().get("notification_outbox", {}),
        "llm": llm,
        "backup": backups,
        "disk": disk,
        "formal_experiment": forward,
        "readiness": readiness,
        "alerts": alerts,
    }


def formal_experiment_status(settings: Settings) -> dict[str, Any]:
    from quantlab.workflows.forward_experiment import formal_forward_scorecard
    from quantlab.workflows.shadow_trading import shadow_scorecard

    path = settings.resolve(settings.get("system.database_path"))
    repository = Round5Repository(path)
    experiment = repository.primary_experiment()
    registrations = repository.registration_runs(experiment["experiment_id"]) if experiment else []
    prediction = formal_forward_scorecard(settings)
    trading = shadow_scorecard(settings)
    return {
        "experiment": experiment,
        "started": experiment is not None,
        "formal_samples": sum(int(item["registered_samples"]) for item in registrations),
        "pending_registrations": sum(item["status"] == "running" for item in registrations),
        "prediction_scorecard": prediction,
        "shadow_trading_scorecard": trading,
        "scorecard_boundary": {
            "prediction": "Brier, Log Loss, direction accuracy and calibration by horizon",
            "trading": "seven isolated account NAV ledgers, fills, costs, turnover and drawdown",
        },
    }


def _quality_gate_status(settings: Settings, now: datetime) -> dict[str, Any]:
    path = settings.resolve("data/reports/quality-gate-latest.json")
    if not path.is_file():
        return {"ready": False, "status": "missing", "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        generated_at = datetime.fromisoformat(str(payload["generated_at"]))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=UTC)
        age_hours = (now - generated_at.astimezone(UTC)).total_seconds() / 3600
        gate = payload.get("quality_gates", {})
        passed = bool(
            payload.get("status") == "passed"
            and gate.get("ruff") == "passed"
            and gate.get("compileall") == "passed"
            and gate.get("pytest") == "passed"
        )
        fresh = age_hours <= float(settings.get("runtime.quality_gate_maximum_age_hours", 168))
        current_fingerprint = quality_source_fingerprint(settings)
        fingerprint_matches = payload.get("source_fingerprint") == current_fingerprint
        return {
            "ready": bool(passed and fresh and fingerprint_matches),
            "status": payload.get("status"),
            "generated_at": generated_at.isoformat(),
            "age_hours": round(max(0.0, age_hours), 2),
            "fresh": fresh,
            "fingerprint_matches": fingerprint_matches,
            "source_fingerprint": current_fingerprint,
            "path": str(path),
        }
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return {
            "ready": False,
            "status": "invalid",
            "error_type": type(exc).__name__,
            "path": str(path),
        }


def _process_health(
    settings: Settings,
    repository: Round6Repository,
    now: datetime,
) -> dict[str, dict[str, Any]]:
    maximum_age = float(settings.get("runtime.runtime_health_maximum_age_seconds", 90))
    output = {}
    for item in repository.processes():
        heartbeat = datetime.fromisoformat(item["heartbeat_at"])
        age = max(0.0, (now - heartbeat.astimezone(UTC)).total_seconds())
        output[item["process_type"]] = {
            **item,
            "heartbeat_age_seconds": round(age, 2),
            "healthy": bool(item["status"] == "running" and age <= maximum_age),
        }
    return output


def _coverage_summary(
    states: dict[str, dict[str, Any]],
    observed: datetime,
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for batch_type, state in states.items():
        last_success = state.get("last_success_at")
        delay_seconds = None
        if last_success:
            delay_seconds = max(
                0.0,
                (
                    observed
                    - datetime.fromisoformat(str(last_success)).astimezone(UTC)
                ).total_seconds(),
            )
        detail = state.get("detail") or {}
        output[batch_type] = {
            "status": state.get("status"),
            "record_count": int(state.get("symbol_count") or 0),
            "eligible_members": int(detail.get("eligible_members") or 0),
            "minimum_ready": bool(state.get("minimum_ready")),
            "field_coverage": float(state.get("field_coverage") or 0.0),
            "average_field_coverage": detail.get("average_field_coverage"),
            "field_coverage_by_field": detail.get("field_coverage_by_field") or {},
            "provider_distribution": detail.get("provider_distribution") or {},
            "last_attempt_at": state.get("last_attempt_at"),
            "last_success_at": last_success,
            "data_delay_seconds": delay_seconds,
            "consecutive_failures": int(state.get("consecutive_failures") or 0),
        }
    return output


def _compact_readiness_processes(
    processes: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Keep readiness serializable and independent from process-owned result payloads.

    Scheduler heartbeat detail contains its previous tick. Embedding that detail in the next
    readiness result would recursively nest every prior tick until serialization fails.
    """

    fields = (
        "process_type",
        "instance_id",
        "pid",
        "status",
        "started_at",
        "heartbeat_at",
        "stop_requested",
        "stopped_at",
        "heartbeat_age_seconds",
        "healthy",
    )
    return {
        process_type: {field: item.get(field) for field in fields}
        for process_type, item in processes.items()
    }


def quality_source_fingerprint(settings: Settings) -> str:
    """Hash quality-gate inputs, avoiding repeated full-file reads on UI reruns.

    The stat signature changes whenever a normal source edit changes a tracked
    file's content, timestamp, or size.  We still enumerate the complete input
    set on each call, but only read every file again after that signature has
    changed.  This preserves the fail-closed source-fingerprint comparison
    while making readiness practical for interactive pages.
    """

    files = _quality_source_files(settings)
    signature = tuple(
        (
            path.relative_to(settings.root).as_posix(),
            path.stat().st_mtime_ns,
            path.stat().st_size,
        )
        for path in files
    )
    cache_key = str(settings.root.resolve()).casefold()
    with _QUALITY_FINGERPRINT_LOCK:
        cached = _QUALITY_FINGERPRINT_CACHE.get(cache_key)
        if cached is not None and cached[0] == signature:
            return cached[1]

    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(settings.root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    fingerprint = digest.hexdigest()
    with _QUALITY_FINGERPRINT_LOCK:
        _QUALITY_FINGERPRINT_CACHE[cache_key] = (signature, fingerprint)
    return fingerprint


def _quality_source_files(settings: Settings) -> list[Path]:
    roots = ["src", "tests", "dashboard", "config", "scripts"]
    files: list[Path] = []
    for name in roots:
        root = settings.resolve(name)
        if root.is_dir():
            files.extend(
                path
                for path in root.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and ".pytest_cache" not in path.parts
                and path.suffix.lower()
                in {".py", ".toml", ".ps1", ".json", ".yaml", ".yml"}
            )
    for extra in ("pyproject.toml",):
        path = settings.resolve(extra)
        if path.is_file():
            files.append(path)
    return sorted(set(files), key=lambda item: item.as_posix())


def _database_health(path: Path) -> dict[str, Any]:
    try:
        with sqlite3.connect(path, timeout=10) as db:
            integrity = str(db.execute("PRAGMA quick_check").fetchone()[0])
            wal = str(db.execute("PRAGMA journal_mode").fetchone()[0])
        return {"healthy": integrity == "ok", "integrity": integrity, "journal_mode": wal}
    except sqlite3.Error as exc:
        return {"healthy": False, "error_type": type(exc).__name__}


def _backup_health(settings: Settings, now: datetime) -> dict[str, Any]:
    root = settings.resolve(settings.get("runtime.backup_directory", "data/backups"))
    candidates = sorted(root.glob("*.manifest.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not candidates:
        return {"healthy": False, "status": "missing", "directory": str(root)}
    latest = candidates[0]
    modified = datetime.fromtimestamp(latest.stat().st_mtime, tz=UTC)
    age_hours = max(0.0, (now - modified).total_seconds() / 3600)
    return {
        "healthy": age_hours <= float(settings.get("runtime.maximum_backup_age_hours", 48)),
        "status": "available",
        "latest_manifest": str(latest),
        "age_hours": round(age_hours, 2),
    }


def _disk_health(settings: Settings, path: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(path.parent)
    free_gb = usage.free / (1024**3)
    minimum = float(settings.get("runtime.minimum_free_disk_gb", 1.0))
    return {
        "healthy": free_gb >= minimum,
        "free_gb": round(free_gb, 2),
        "minimum_free_gb": minimum,
    }


def _llm_health(settings: Settings, path: Path, now: datetime) -> dict[str, Any]:
    since = (now - timedelta(hours=24)).isoformat()
    with sqlite3.connect(path) as db:
        table = db.execute(
            """SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_governed_calls'"""
        ).fetchone()
        if table:
            row = db.execute(
                """SELECT COUNT(*),SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),
                          COALESCE(SUM(estimated_cost_usd),0)
                   FROM llm_governed_calls WHERE created_at>=?""",
                (since,),
            ).fetchone()
        else:
            row = (0, 0, 0.0)
    calls = int(row[0] or 0)
    failures = int(row[1] or 0)
    return {
        "configuration": provider_configuration_summary(settings.section("llm")),
        "calls_24h": calls,
        "failures_24h": failures,
        "failure_rate": failures / calls if calls else 0.0,
        "cost_usd_24h": float(row[2] or 0.0),
    }


def _forward_health(settings: Settings, path: Path, now: datetime) -> dict[str, Any]:
    repository = Round5Repository(path)
    experiment = repository.primary_experiment()
    with sqlite3.connect(path) as db:
        table = db.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='forward_ablation_predictions'"""
        ).fetchone()
        has_wide_links = db.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='wide_forward_prediction_links'"""
        ).fetchone()
        linked_wide_clause = (
            """AND (
                   p.registration_origin NOT LIKE 'wide_forward_%'
                   OR EXISTS(
                       SELECT 1 FROM wide_forward_prediction_links l
                       WHERE l.prediction_id=p.prediction_id
                   )
               )"""
            if has_wide_links
            else ""
        )
        pending = int(
            db.execute(
                f"""SELECT COUNT(DISTINCT cohort_id || ':' || sample_key || ':' || horizon_days)
                   FROM forward_ablation_predictions p
                   WHERE due_at<=? AND NOT EXISTS(
                       SELECT 1 FROM forward_ablation_outcomes o
                       WHERE o.prediction_id=p.prediction_id
                   ) {linked_wide_clause}""",
                (now.isoformat(),),
            ).fetchone()[0]
        ) if table else 0
    registrations = repository.registration_runs(experiment["experiment_id"]) if experiment else []
    return {
        "experiment": experiment,
        "status": "active" if experiment else "not_started",
        "registration_runs": len(registrations),
        "registered_samples": sum(int(item["registered_samples"]) for item in registrations),
        "overdue_pending_samples": pending,
        "last_registration": registrations[0] if registrations else None,
    }


__all__ = [
    "formal_experiment_status",
    "primary_start_readiness",
    "quality_source_fingerprint",
    "runtime_health",
]
