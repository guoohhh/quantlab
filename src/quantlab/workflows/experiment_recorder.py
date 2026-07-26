from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from quantlab.config import Settings
from quantlab.domain.data_governance import DataTrustLevel, trust_at_least
from quantlab.persistence.round8 import Round8Repository
from quantlab.persistence.round9 import Round9Repository
from quantlab.security import sanitize_for_export


def _stable_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            sanitize_for_export(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def source_code_fingerprint(settings: Settings) -> str:
    return source_build_manifest(settings)["source_fingerprint"]


def source_build_manifest(settings: Settings) -> dict[str, Any]:
    root = Path(settings.root).resolve()
    candidates: list[Path] = []
    for relative, pattern in (
        ("src/quantlab", "*.py"),
        ("config", "*.toml"),
        ("dashboard", "*.py"),
        ("scripts", "*.ps1"),
    ):
        base = root / relative
        if base.is_dir():
            candidates.extend(base.rglob(pattern))
    for relative in ("pyproject.toml", "PROMPT_GOVERNANCE.md"):
        path = root / relative
        if path.is_file():
            candidates.append(path)
    records = []
    for path in sorted({item.resolve() for item in candidates}):
        if "__pycache__" in path.parts or path.suffix in {".db", ".json"}:
            continue
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    source_fingerprint = _stable_hash(records)
    quality_report = root / "data/reports/quality-gate-latest.json"
    quality_gate_fingerprint = (
        hashlib.sha256(quality_report.read_bytes()).hexdigest()
        if quality_report.is_file()
        else None
    )
    prompt_records = [
        item
        for item in records
        if item["path"].startswith(("src/quantlab/agents/", "src/quantlab/llm/"))
        or item["path"]
        in {
            "src/quantlab/workflows/llm_committee.py",
            "PROMPT_GOVERNANCE.md",
        }
    ]
    dirty = None
    revision = None
    try:
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        )
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "source_fingerprint": source_fingerprint,
        "quality_gate_fingerprint": quality_gate_fingerprint,
        "prompt_fingerprint": _stable_hash(prompt_records),
        "file_count": len(records),
        "git_revision": revision,
        "dirty_worktree": dirty,
        "manifest_version": "round9-source-manifest-v1",
    }


def configuration_fingerprint(settings: Settings) -> str:
    return _stable_hash(settings.values)


def checkpoint_signature(
    settings: Settings,
    *,
    workflow_structure: str,
    model_routing: dict[str, Any],
    prompt_version: str,
    context_fingerprint: str,
    reasoning_effort: str | None = None,
    role_set: list[str] | tuple[str, ...] | None = None,
    schema_version: str | None = None,
    key_configuration: dict[str, Any] | None = None,
) -> str:
    build = source_build_manifest(settings)
    return _stable_hash(
        {
            "source_fingerprint": build["source_fingerprint"],
            "workflow_structure": workflow_structure,
            "model_routing": model_routing,
            "prompt_version": prompt_version,
            "prompt_fingerprint": build["prompt_fingerprint"],
            "context_fingerprint": context_fingerprint,
            "reasoning_effort": reasoning_effort,
            "role_set": sorted(role_set or []),
            "schema_version": schema_version,
            "key_configuration": key_configuration or {},
            "config_fingerprint": configuration_fingerprint(settings),
        }
    )


class ExperimentRecorder:
    """Small recorder facade; it never schedules work or changes frozen strategy rules."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.repository = Round8Repository(
            settings.resolve(settings.get("system.database_path"))
        )

    def start(
        self,
        *,
        experiment_name: str,
        experiment_type: str,
        run_type: str,
        evidence_boundary: str,
        idempotency_key: str,
        prompt_version: str | None = None,
        dataset_fingerprint: str | None = None,
        universe_fingerprint: str | None = None,
        context_fingerprint: str | None = None,
        quote_fingerprint: str | None = None,
        model_routing: dict[str, Any] | None = None,
        parameters: dict[str, Any] | None = None,
        cost_budget: dict[str, Any] | None = None,
        parent_run_id: str | None = None,
        workflow_version: str | None = None,
    ) -> dict[str, Any]:
        build = source_build_manifest(self.settings)
        versioned_key = f"{idempotency_key}:src:{build['source_fingerprint'][:16]}"
        run = self.repository.start_run(
            experiment_name=experiment_name,
            experiment_type=experiment_type,
            run_type=run_type,
            evidence_boundary=evidence_boundary,
            idempotency_key=versioned_key,
            code_fingerprint=build["source_fingerprint"],
            config_fingerprint=configuration_fingerprint(self.settings),
            prompt_version=prompt_version,
            dataset_fingerprint=dataset_fingerprint,
            universe_fingerprint=universe_fingerprint,
            context_fingerprint=context_fingerprint,
            quote_fingerprint=quote_fingerprint,
            model_routing=model_routing,
            parameters=parameters,
            cost_budget=cost_budget,
            quality_gate_fingerprint=build["quality_gate_fingerprint"],
            prompt_fingerprint=build["prompt_fingerprint"],
            build_state={
                "manifest_version": build["manifest_version"],
                "file_count": build["file_count"],
                "git_revision": build["git_revision"],
                "dirty_worktree": build["dirty_worktree"],
            },
            parent_run_id=parent_run_id,
            workflow_version=workflow_version,
        )
        if run["status"] in {"failed", "cancelled", "blocked"}:
            run = Round9Repository(self.repository.path).resume_decision_run(
                run["run_id"], reason="exact_input_retry"
            )
        return run

    def checkpoint(
        self,
        run_id: str,
        *,
        step_name: str,
        signature: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        existing = self.repository.load_checkpoint(run_id, step_name, signature)
        if existing:
            return {**existing, "resumed": True}
        saved = self.repository.save_checkpoint(
            run_id,
            step_name=step_name,
            checkpoint_signature=signature,
            payload=payload,
        )
        return {**saved, "resumed": False}

    def checkpointed_step(
        self,
        run_id: str,
        *,
        step_name: str,
        signature: str,
        callback: Callable[[], Any],
        payload_builder: Callable[[Any], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run a side-effecting research step once and persist its result.

        A completed checkpoint is authoritative for the exact signature.  This helper is
        deliberately synchronous so it can wrap both deterministic work and an LLM call;
        callers can provide a compact ``payload_builder`` rather than persisting a large
        object.  Signature changes never reuse an old result.
        """
        claim = self.repository.claim_checkpoint_step(
            run_id,
            step_name=step_name,
            checkpoint_signature=signature,
        )
        if claim["state"] == "completed":
            return {**claim["checkpoint"], "resumed": True}
        if claim["state"] == "in_progress":
            raise RuntimeError("checkpoint step is already running")
        try:
            result = callback()
        except Exception as exc:
            self.repository.fail_checkpoint_step(
                run_id,
                step_name=step_name,
                checkpoint_signature=signature,
                error_type=type(exc).__name__,
            )
            raise
        payload = payload_builder(result) if payload_builder else (
            result if isinstance(result, dict) else {"result": result}
        )
        saved = self.repository.save_checkpoint(
            run_id,
            step_name=step_name,
            checkpoint_signature=signature,
            payload=payload,
        )
        return {**saved, "resumed": False, "result": result}

    async def checkpointed_async_step(
        self,
        run_id: str,
        *,
        step_name: str,
        signature: str,
        callback: Callable[[], Awaitable[Any]],
        payload_builder: Callable[[Any], dict[str, Any]] | None = None,
        result_loader: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Await an expensive step only after an exact checkpoint miss.

        The callback must be an async callable, which prevents the common error of
        constructing/awaiting an LLM coroutine before checking the checkpoint.
        """
        if not inspect.iscoroutinefunction(callback):
            raise TypeError("async checkpoint callback must be an async callable")
        claim = self.repository.claim_checkpoint_step(
            run_id,
            step_name=step_name,
            checkpoint_signature=signature,
        )
        if claim["state"] == "completed":
            checkpoint = claim["checkpoint"]
            result = result_loader(checkpoint["payload"]) if result_loader else checkpoint["payload"]
            return {**checkpoint, "resumed": True, "result": result}
        if claim["state"] == "in_progress":
            raise RuntimeError("checkpoint step is already running")
        try:
            result = await callback()
        except Exception as exc:
            self.repository.fail_checkpoint_step(
                run_id,
                step_name=step_name,
                checkpoint_signature=signature,
                error_type=type(exc).__name__,
            )
            raise
        payload = payload_builder(result) if payload_builder else (
            result if isinstance(result, dict) else {"result": result}
        )
        saved = self.repository.save_checkpoint(
            run_id,
            step_name=step_name,
            checkpoint_signature=signature,
            payload=payload,
        )
        return {**saved, "resumed": False, "result": result}

    def complete(
        self, run_id: str, *, result_summary: dict[str, Any]
    ) -> dict[str, Any]:
        return self.repository.finish_run(
            run_id, status="completed", result_summary=result_summary
        )

    def fail(self, run_id: str, *, error_detail: str) -> dict[str, Any]:
        return self.repository.finish_run(
            run_id, status="failed", error_detail=error_detail
        )

    def link(
        self, run_id: str, *, entity_type: str, entity_id: str, relation: str
    ) -> None:
        self.repository.link_entity(
            run_id, entity_type=entity_type, entity_id=entity_id, relation=relation
        )

    def artifact(
        self,
        run_id: str,
        *,
        artifact_type: str,
        name: str,
        payload: dict[str, Any],
        uri: str | None = None,
    ) -> dict[str, Any]:
        return self.repository.save_artifact(
            run_id,
            artifact_type=artifact_type,
            name=name,
            payload=payload,
            uri=uri,
        )


def next_trading_day_acceptance_report(
    settings: Settings, *, trade_date: date
) -> dict[str, Any]:
    from quantlab.runtime.readiness import formal_experiment_status, primary_start_readiness

    checked_at = datetime.now(UTC)
    path = settings.resolve(settings.get("system.database_path"))
    repository = Round8Repository(path)
    persisted_readiness = _persisted_registration_readiness(path, trade_date)
    live_readiness: dict[str, Any] | None = None
    live_readiness_error: dict[str, str] | None = None
    try:
        live_readiness = primary_start_readiness(
            settings, trade_date=trade_date, require_runtime=True
        )
    except Exception as exc:
        live_readiness_error = {
            "failure_type": type(exc).__name__,
            "failure_detail": str(exc),
        }
    readiness = persisted_readiness or live_readiness
    try:
        formal = formal_experiment_status(settings)
    except Exception as exc:
        return repository.save_acceptance_report(
            trade_date.isoformat(),
            status="unavailable",
            checks={
                "checked_at": checked_at.isoformat(),
                "source_fingerprint": source_code_fingerprint(settings),
                "failure_type": type(exc).__name__,
            },
            blockers=["acceptance_evidence_unavailable"],
        )
    if readiness is None:
        return repository.save_acceptance_report(
            trade_date.isoformat(),
            status="unavailable",
            checks={
                "checked_at": checked_at.isoformat(),
                "source_fingerprint": source_code_fingerprint(settings),
                "live_readiness_error": live_readiness_error,
            },
            blockers=["acceptance_readiness_unavailable"],
        )
    calendar_day = readiness.get("data", {}).get("calendar_day") or {}
    if calendar_day and not bool(calendar_day.get("is_open")):
        return repository.save_acceptance_report(
            trade_date.isoformat(),
            status="skipped_non_trading_day",
            checks={
                "calendar_day": calendar_day,
                "production_pool_generated": False,
                "primary_start_count": 0,
                "formal_samples": 0,
                "shadow_accounts": 0,
                "fail_closed": True,
                "checked_at": checked_at.isoformat(),
                "source_fingerprint": source_code_fingerprint(settings),
            },
            blockers=["non_trading_day"],
        )
    provider_acceptance = _provider_refresh_acceptance_checks(
        path,
        trade_date=trade_date,
        checked_at=checked_at,
    )
    latest_refresh = provider_acceptance["provider_refresh_id"]
    current_selections = provider_acceptance["selected_providers"]
    pool = readiness["data"].get("point_in_time_pool") or {}
    source_states = readiness["data"].get("source_states") or {}
    point_state = source_states.get("point_in_time_pool") or {}
    experiment = formal.get("experiment")
    database_checks = _formal_acceptance_database_checks(
        path,
        trade_date=trade_date,
        experiment=experiment,
    )
    candidate_count = int(
        (experiment or {}).get("candidate_count")
        or settings.get("strategies.forward_primary.candidate_count", 3)
    )
    eligible_members = int(pool.get("eligible_members") or 0)
    pool_date = str(point_state.get("date_end") or "")[:10]
    if not pool_date:
        pool_date = trade_date.isoformat() if pool.get("ready") else ""
    required_coverage = (
        point_state.get("detail", {}).get("required_field_coverage") or {}
    )
    minimum_coverage = float(
        settings.get("runtime.trusted_data_minimum_field_coverage", 0.80)
    )
    coverage_passed = bool(required_coverage) and all(
        float(value) >= minimum_coverage for value in required_coverage.values()
    )
    selected_provider_current = bool(provider_acceptance["provider_selection_passed"])
    processes = readiness.get("processes") or {}
    services_healthy = all(
        bool((processes.get(name) or {}).get("healthy"))
        for name in ("worker", "scheduler")
    )
    checks = {
        "checked_at": checked_at.isoformat(),
        "source_fingerprint": source_code_fingerprint(settings),
        "readiness_source": "persisted_registration_event"
        if persisted_readiness is not None
        else "live",
        "event_time_readiness_persisted": persisted_readiness is not None,
        "live_readiness_error": live_readiness_error,
        "production_pool_generated": bool(pool.get("ready") and pool.get("total_members")),
        "production_pool_date": pool_date,
        "pool_date_matches": pool_date == trade_date.isoformat(),
        "eligible_members": eligible_members,
        "frozen_candidate_count": candidate_count,
        "eligible_count_passed": eligible_members >= candidate_count,
        "required_field_coverage": required_coverage,
        "field_coverage_threshold": minimum_coverage,
        "required_field_coverage_passed": coverage_passed,
        "provider_refresh_id": latest_refresh,
        "provider_refresh_market_date": provider_acceptance[
            "provider_refresh_market_date"
        ],
        "provider_components_expected": provider_acceptance[
            "provider_components_expected"
        ],
        "provider_components_observed": provider_acceptance[
            "provider_components_observed"
        ],
        "missing_provider_components": provider_acceptance[
            "missing_provider_components"
        ],
        "unavailable_provider_components": provider_acceptance[
            "unavailable_provider_components"
        ],
        "pool_refresh_matches": provider_acceptance["pool_refresh_matches"],
        "provider_selection_passed": provider_acceptance[
            "provider_selection_passed"
        ],
        "selected_providers": current_selections,
        "selected_provider_current_refresh": selected_provider_current,
        "primary_start_count": database_checks["primary_start_count"],
        "formal_samples": database_checks["formal_samples"],
        "shadow_accounts": database_checks["shadow_accounts"],
        "shadow_variants": database_checks["shadow_variants"],
        "shadow_accounts_independent": database_checks["shadow_accounts_independent"],
        "shadow_orders": int(database_checks.get("shadow_orders") or 0),
        "shadow_fills": int(database_checks.get("shadow_fills") or 0),
        "shadow_positions": int(database_checks.get("shadow_positions") or 0),
        "duplicate_jobs": _duplicate_count(path, "background_jobs", "idempotency_key"),
        "duplicate_formal_samples": database_checks["duplicate_formal_samples"],
        "demo_pollution": database_checks["demo_pollution"] + _demo_pollution_count(path),
        "acceptance_schema_ready": database_checks["schema_ready"],
        "missing_acceptance_schema": database_checks["missing_schema"],
        "services_healthy": services_healthy,
        "processes": processes,
        "quality_gate_ready": bool((readiness.get("quality_gate") or {}).get("ready")),
        "llm_ready": int((readiness.get("llm") or {}).get("real_endpoint_count") or 0) > 0,
        "fail_closed": not readiness.get("start_allowed")
        if readiness.get("blockers")
        else True,
    }
    blockers = list(readiness.get("blockers") or [])
    mandatory = {
        "production_pool_generated": checks["production_pool_generated"],
        "pool_date_matches": checks["pool_date_matches"],
        "eligible_count_passed": checks["eligible_count_passed"],
        "required_field_coverage_passed": checks["required_field_coverage_passed"],
        "selected_provider_current_refresh": checks["selected_provider_current_refresh"],
        "provider_selection_passed": checks["provider_selection_passed"],
        "primary_exactly_once": checks["primary_start_count"] == 1,
        "formal_samples_registered": checks["formal_samples"] > 0,
        "seven_independent_shadow_accounts": (
            checks["shadow_accounts"] == 7 and checks["shadow_accounts_independent"]
        ),
        "duplicate_jobs_zero": checks["duplicate_jobs"] == 0,
        "duplicate_formal_samples_zero": checks["duplicate_formal_samples"] == 0,
        "demo_pollution_zero": checks["demo_pollution"] == 0,
        "acceptance_schema_ready": checks["acceptance_schema_ready"],
        "services_healthy": checks["services_healthy"],
        "quality_gate_ready": checks["quality_gate_ready"],
        "llm_ready": checks["llm_ready"],
        "fail_closed": checks["fail_closed"],
        "event_time_readiness_persisted": checks["event_time_readiness_persisted"],
    }
    checks["mandatory"] = mandatory
    checks["sections"] = {
        "formal_registration": {
            "status": "passed"
            if mandatory["primary_exactly_once"]
            and mandatory["formal_samples_registered"]
            and mandatory["duplicate_formal_samples_zero"]
            else "blocked",
            "mandatory": True,
        },
        "shadow_account_initialization": {
            "status": "passed"
            if mandatory["seven_independent_shadow_accounts"]
            else "blocked",
            "mandatory": True,
        },
        "trade_execution": {
            "status": "observed"
            if checks["shadow_orders"] or checks["shadow_fills"] or checks["shadow_positions"]
            else "pending_no_executable_order",
            "mandatory": False,
            "orders": checks["shadow_orders"],
            "fills": checks["shadow_fills"],
            "positions": checks["shadow_positions"],
        },
        "audit_provenance": {
            "status": "passed"
            if mandatory["event_time_readiness_persisted"]
            else "blocked",
            "mandatory": True,
        },
        "natural_scheduler_attempt": _natural_schedule_attempt_checks(path, trade_date),
    }
    blockers.extend(key for key, passed in mandatory.items() if not passed)
    waiting = _acceptance_jobs_pending(path, trade_date)
    checks["waiting_jobs"] = waiting
    if all(mandatory.values()) and readiness.get("start_allowed"):
        status = "passed"
    elif waiting and not any(
        blocker in {"current_quality_gate_has_not_passed", "formal_llm_provider_is_not_explicitly_configured"}
        for blocker in blockers
    ):
        status = "waiting_for_scheduled_jobs"
    elif provider_acceptance["explicit_provider_failure"]:
        status = "unavailable"
    else:
        status = "blocked"
    return repository.save_acceptance_report(
        trade_date.isoformat(),
        status=status,
        checks=checks,
        blockers=list(dict.fromkeys(blockers)),
    )


def _provider_refresh_acceptance_checks(
    path: Path,
    *,
    trade_date: date,
    checked_at: datetime,
) -> dict[str, Any]:
    expected = (
        "trading_calendar",
        "security_master",
        "industry_membership",
        "point_in_time_universe",
        "market_spot",
        "point_in_time_pool",
    )
    expected_capabilities = {
        "trading_calendar": "trading_calendar",
        "security_master": "security_master",
        "industry_membership": "industry_membership",
        "point_in_time_universe": "trade_status",
        "market_spot": "market_spot",
        "point_in_time_pool": "point_in_time_pool",
    }
    successful_statuses = {"available", "completed"}
    failed_statuses = {
        "unavailable",
        "failed",
        "timeout",
        "circuit_open",
        "blocked_after_provider_timeout",
        "skipped_non_trading_day",
    }
    with sqlite3.connect(path, timeout=30) as db:
        db.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        pool = None
        if "pit_pool_snapshots" in tables:
            pool = db.execute(
                """SELECT * FROM pit_pool_snapshots
                   WHERE snapshot_date=? AND namespace='production'
                   ORDER BY created_at DESC LIMIT 1""",
                (trade_date.isoformat(),),
            ).fetchone()
        refresh_id = str(pool["refresh_id"] or "") if pool else ""
        selections = (
            db.execute(
                """SELECT * FROM provider_refresh_selections
                   WHERE refresh_id=? ORDER BY component""",
                (refresh_id,),
            ).fetchall()
            if refresh_id and "provider_refresh_selections" in tables
            else []
        )
        manifests: dict[str, dict[str, Any]] = {}
        if "trusted_data_manifests" in tables:
            manifest_ids = {
                str(row["manifest_id"])
                for row in selections
                if row["manifest_id"]
            }
            for manifest_id in manifest_ids:
                manifest = db.execute(
                    "SELECT * FROM trusted_data_manifests WHERE manifest_id=?",
                    (manifest_id,),
                ).fetchone()
                if manifest:
                    manifests[manifest_id] = dict(manifest)
    selection_items = []
    for row in selections:
        item = dict(row)
        item["related_failures"] = _json_object_list(row["related_failures"])
        item["attempts"] = _json_object_list(row["attempts"])
        selection_items.append(item)
    by_component = {item["component"]: item for item in selection_items}
    missing = [component for component in expected if component not in by_component]
    unavailable: list[str] = []
    component_checks: dict[str, dict[str, Any]] = {}
    pool_manifest_id = str(pool["manifest_id"] or "") if pool else ""
    pool_snapshot_id = str(pool["snapshot_id"] or "") if pool else ""
    pool_fingerprint = str(pool["fingerprint"] or "") if pool else ""
    normalized_checked_at = checked_at if checked_at.tzinfo else checked_at.replace(tzinfo=UTC)
    for component in expected:
        selection = by_component.get(component)
        if selection is None:
            component_checks[component] = {
                "present": False,
                "passed": False,
                "failed_checks": ["selection_present"],
            }
            continue
        observed_at = _optional_datetime(selection.get("observed_at"))
        market_date_matches = str(selection.get("market_date") or "")[:10] == trade_date.isoformat()
        observed_not_future = bool(observed_at and observed_at <= normalized_checked_at)
        selected_provider = str(selection.get("selected_provider") or "").strip()
        selected = bool(selected_provider)
        status = str(selection.get("status") or "unavailable").strip().lower()
        status_available = status in successful_statuses
        capability_matches = (
            str(selection.get("capability") or "").strip()
            == expected_capabilities[component]
        )
        source_version = str(selection.get("source_version") or "").strip()
        source_version_present = bool(source_version)
        manifest_id = str(selection.get("manifest_id") or "").strip()
        manifest = manifests.get(manifest_id)
        manifest_present = bool(manifest)
        expected_batch = {
            "trading_calendar": "trading_calendar",
            "security_master": "security_master",
            "industry_membership": "industry_membership",
        }.get(component, "point_in_time_pool")
        manifest_namespace_matches = bool(
            manifest and str(manifest.get("namespace") or "") == "production"
        )
        manifest_trust_matches = bool(
            manifest
            and _trust_at_least_server_observed(manifest.get("trust_level"))
        )
        manifest_provider_matches = bool(
            manifest
            and selected_provider
            and str(manifest.get("provider") or "").strip() == selected_provider
        )
        manifest_source_version_matches = bool(
            manifest
            and source_version
            and str(manifest.get("source_version") or "").strip() == source_version
        )
        manifest_batch_matches = bool(
            manifest and str(manifest.get("batch_type") or "") == expected_batch
        )
        manifest_date_start = _optional_date(manifest.get("date_start") if manifest else None)
        manifest_date_end = _optional_date(manifest.get("date_end") if manifest else None)
        manifest_dates_present = bool(manifest_date_start and manifest_date_end)
        manifest_dates_cover = bool(
            manifest_date_start
            and manifest_date_end
            and manifest_date_start <= trade_date <= manifest_date_end
        )
        manifest_available_at = _optional_datetime(
            manifest.get("available_at") if manifest else None
        )
        manifest_not_future = bool(
            manifest_available_at and manifest_available_at <= normalized_checked_at
        )
        manifest_status_available = bool(
            manifest
            and str(manifest.get("status") or "").strip().lower()
            in successful_statuses
        )
        related_failures = selection.get("related_failures") or []
        attempts = selection.get("attempts") or []
        selection_reason = str(selection.get("selection_reason") or "").strip()
        provider_attempt_audit = _provider_attempt_audit(
            selected_provider=selected_provider,
            selection_reason=selection_reason,
            related_failures=related_failures,
            attempts=attempts,
            successful_statuses=successful_statuses,
            failed_statuses=failed_statuses,
        )
        pool_link_matches = True
        pool_snapshot_date_matches = True
        pool_namespace_matches = True
        pool_trust_matches = True
        pool_times_not_future = True
        pool_manifest_matches = True
        pool_source_version_matches = True
        if component in {"point_in_time_universe", "market_spot", "point_in_time_pool"}:
            pool_link_matches = bool(
                refresh_id
                and selection.get("refresh_id") == refresh_id
                and str(selection.get("pool_snapshot_id") or "") == pool_snapshot_id
                and str(selection.get("pool_fingerprint") or "") == pool_fingerprint
                and (
                    component != "point_in_time_pool"
                    or manifest_id == pool_manifest_id
                )
            )
            pool_snapshot_date_matches = bool(
                pool and str(pool["snapshot_date"] or "") == trade_date.isoformat()
            )
            pool_namespace_matches = bool(pool and str(pool["namespace"] or "") == "production")
            pool_trust_matches = bool(
                pool and _trust_at_least_server_observed(pool["trust_level"])
            )
            pool_cutoff_at = _optional_datetime(pool["cutoff_at"] if pool else None)
            pool_created_at = _optional_datetime(pool["created_at"] if pool else None)
            pool_times_not_future = bool(
                pool_cutoff_at
                and pool_created_at
                and pool_cutoff_at <= normalized_checked_at
                and pool_created_at <= normalized_checked_at
                and manifest_not_future
            )
            pool_manifest_matches = bool(
                pool
                and (
                    component != "point_in_time_pool"
                    or str(pool["manifest_id"] or "") == manifest_id
                )
            )
            pool_source_version_matches = bool(
                pool
                and str(pool["source_version"] or "").strip() == source_version
                and manifest_source_version_matches
            )
        passed = all(
            (
                market_date_matches,
                observed_not_future,
                selected,
                status_available,
                capability_matches,
                source_version_present,
                manifest_present,
                manifest_namespace_matches,
                manifest_trust_matches,
                manifest_provider_matches,
                manifest_source_version_matches,
                manifest_batch_matches,
                manifest_dates_present,
                manifest_dates_cover,
                manifest_not_future,
                manifest_status_available,
                provider_attempt_audit["provider_attempt_audit_valid"],
                pool_link_matches,
                pool_snapshot_date_matches,
                pool_namespace_matches,
                pool_trust_matches,
                pool_times_not_future,
                pool_manifest_matches,
                pool_source_version_matches,
            )
        )
        checks = {
            "present": True,
            "market_date_matches": market_date_matches,
            "observed_not_future": observed_not_future,
            "selected_provider_present": selected,
            "status_available": status_available,
            "capability_matches": capability_matches,
            "source_version_present": source_version_present,
            "manifest_present": manifest_present,
            "manifest_namespace_matches": manifest_namespace_matches,
            "manifest_trust_matches": manifest_trust_matches,
            "manifest_provider_matches": manifest_provider_matches,
            "manifest_source_version_matches": manifest_source_version_matches,
            "manifest_batch_matches": manifest_batch_matches,
            "manifest_dates_present": manifest_dates_present,
            "manifest_dates_cover": manifest_dates_cover,
            "manifest_not_future": manifest_not_future,
            "manifest_status_available": manifest_status_available,
            **provider_attempt_audit,
            "pool_link_matches": pool_link_matches,
            "pool_snapshot_date_matches": pool_snapshot_date_matches,
            "pool_namespace_matches": pool_namespace_matches,
            "pool_trust_matches": pool_trust_matches,
            "pool_times_not_future": pool_times_not_future,
            "pool_manifest_matches": pool_manifest_matches,
            "pool_source_version_matches": pool_source_version_matches,
            "passed": passed,
        }
        checks["failed_checks"] = [
            key for key, value in checks.items() if key not in {"passed", "failed_checks"} and value is False
        ]
        component_checks[component] = checks
        if not passed:
            unavailable.append(component)
    pool_refresh_matches = bool(
        pool
        and refresh_id
        and len(selection_items) >= len(expected)
        and str(pool["snapshot_date"]) == trade_date.isoformat()
        and str(pool["namespace"] or "") == "production"
        and _trust_at_least_server_observed(pool["trust_level"])
        and bool(component_checks.get("point_in_time_pool", {}).get("passed"))
        and all(item["refresh_id"] == refresh_id for item in selection_items)
    )
    provider_selection_passed = bool(
        not missing
        and not unavailable
        and pool_refresh_matches
        and len(selection_items) >= len(expected)
    )
    refresh_market_dates = {
        str(item.get("market_date") or "")[:10]
        for item in selection_items
        if item.get("market_date")
    }
    return {
        "provider_refresh_id": refresh_id or None,
        "provider_refresh_market_date": (
            next(iter(refresh_market_dates)) if len(refresh_market_dates) == 1 else None
        ),
        "provider_components_expected": list(expected),
        "provider_components_observed": sorted(by_component),
        "missing_provider_components": missing,
        "unavailable_provider_components": sorted(set(unavailable)),
        "pool_refresh_matches": pool_refresh_matches,
        "provider_selection_passed": provider_selection_passed,
        "selected_providers": selection_items,
        "component_checks": component_checks,
        "unavailable_reasons": {
            component: list(component_checks.get(component, {}).get("failed_checks") or [])
            for component in expected
            if not component_checks.get(component, {}).get("passed")
        },
        "explicit_provider_failure": bool(unavailable or missing),
    }


def _optional_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _optional_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _json_object_list(value: Any) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value or "[]") if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _trust_at_least_server_observed(value: Any) -> bool:
    try:
        return trust_at_least(str(value or ""), DataTrustLevel.SERVER_OBSERVED)
    except ValueError:
        return False


def _provider_attempt_audit(
    *,
    selected_provider: str,
    selection_reason: str,
    related_failures: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    successful_statuses: set[str],
    failed_statuses: set[str],
) -> dict[str, bool]:
    """Validate automatic-provider selection from execution evidence, not labels.

    ``server_configured_file`` is the sole provider allowed to omit attempts. For
    routed providers, the selected provider must be the final successful attempt.
    A fallback exists only when another, higher-priority provider failed before
    that success; its attempt and aggregated failure record must both survive.
    """

    reason_claims_fallback = "fallback" in selection_reason.lower()
    configured_file_without_attempts = bool(
        selected_provider == "server_configured_file"
        and not attempts
        and not related_failures
        and not reason_claims_fallback
    )
    if configured_file_without_attempts:
        return {
            "selected_provider_success_attempt": True,
            "provider_attempt_order_valid": True,
            "fallback_priority_valid": True,
            "fallback_failure_chain_valid": True,
            "selection_reason_consistent": True,
            "provider_attempt_audit_valid": True,
            "fallback_audit_valid": True,
        }

    normalized_attempts = [
        {
            **item,
            "provider": str(item.get("provider") or "").strip(),
            "status": str(item.get("status") or "").strip().lower(),
        }
        for item in attempts
    ]
    recognized_statuses = successful_statuses | failed_statuses
    attempts_well_formed = bool(normalized_attempts) and all(
        item["provider"] and item["status"] in recognized_statuses
        for item in normalized_attempts
    )
    successful_indices = [
        index
        for index, item in enumerate(normalized_attempts)
        if item["status"] in successful_statuses
    ]
    selected_success_indices = [
        index
        for index in successful_indices
        if normalized_attempts[index]["provider"] == selected_provider
    ]
    selected_success_is_final = bool(
        attempts_well_formed
        and len(successful_indices) == 1
        and len(selected_success_indices) == 1
        and selected_success_indices[0] == len(normalized_attempts) - 1
    )
    selected_success_index = (
        selected_success_indices[0] if selected_success_is_final else None
    )
    prior_attempts = (
        normalized_attempts[:selected_success_index]
        if selected_success_index is not None
        else []
    )
    prior_attempts_failed = bool(
        selected_success_is_final
        and all(item["status"] in failed_statuses for item in prior_attempts)
    )
    fallback_attempts = [
        item for item in prior_attempts if item["provider"] != selected_provider
    ]
    fallback_detected = bool(fallback_attempts)
    provider_attempt_order_valid = bool(
        selected_success_is_final and prior_attempts_failed
    )

    fallback_priority_valid = True
    if fallback_detected:
        selected_priority = _provider_attempt_priority(
            normalized_attempts[selected_success_index]
        )
        ordered_priorities = [
            _provider_attempt_priority(item) for item in prior_attempts
        ]
        fallback_priority_valid = bool(
            selected_priority is not None
            and all(priority is not None for priority in ordered_priorities)
            and all(
                left <= right
                for left, right in zip(
                    ordered_priorities,
                    ordered_priorities[1:],
                    strict=False,
                )
            )
            and all(
                _provider_attempt_priority(item) < selected_priority
                for item in fallback_attempts
            )
        )

    normalized_failures = [
        {
            **item,
            "provider": str(item.get("provider") or "").strip(),
            "status": str(item.get("status") or "").strip().lower(),
        }
        for item in related_failures
    ]
    failures_well_formed = all(
        item["provider"]
        and item["provider"] != selected_provider
        and item["status"] in failed_statuses
        for item in normalized_failures
    )
    attempted_failure_providers = {
        item["provider"] for item in fallback_attempts
    }
    recorded_failure_providers = {
        item["provider"] for item in normalized_failures
    }
    failures_match_attempts = all(
        any(
            attempt["provider"] == failure["provider"]
            and attempt["status"] == failure["status"]
            and _provider_attempt_priority(attempt)
            == _provider_attempt_priority(failure)
            for attempt in fallback_attempts
        )
        for failure in normalized_failures
    )
    fallback_failure_chain_valid = bool(
        failures_well_formed
        and (
            attempted_failure_providers == recorded_failure_providers
            and failures_match_attempts
            if fallback_detected
            else not normalized_failures
        )
    )
    selection_reason_consistent = reason_claims_fallback == fallback_detected
    provider_attempt_audit_valid = all(
        (
            selected_success_is_final,
            provider_attempt_order_valid,
            fallback_priority_valid,
            fallback_failure_chain_valid,
            selection_reason_consistent,
        )
    )
    return {
        "selected_provider_success_attempt": selected_success_is_final,
        "provider_attempt_order_valid": provider_attempt_order_valid,
        "fallback_priority_valid": fallback_priority_valid,
        "fallback_failure_chain_valid": fallback_failure_chain_valid,
        "selection_reason_consistent": selection_reason_consistent,
        "provider_attempt_audit_valid": provider_attempt_audit_valid,
        # Backward-compatible report field retained for existing consumers.
        "fallback_audit_valid": provider_attempt_audit_valid,
    }


def _provider_attempt_priority(item: dict[str, Any]) -> int | None:
    value = item.get("priority")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _formal_acceptance_database_checks(
    path: Path, *, trade_date: date, experiment: dict[str, Any] | None
) -> dict[str, Any]:
    import sqlite3

    day = trade_date.isoformat()
    with sqlite3.connect(path, timeout=30) as db:
        db.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "forward_experiment_protocols" not in tables:
            return {
                "primary_start_count": 0,
                "formal_samples": 0,
                "shadow_accounts": 0,
                "shadow_variants": [],
                "shadow_accounts_independent": False,
                "shadow_orders": 0,
                "shadow_fills": 0,
                "shadow_positions": 0,
                "duplicate_formal_samples": 0,
                "demo_pollution": 0,
                "schema_ready": False,
                "missing_schema": ["forward_experiment_protocols"],
            }
        required_tables = {
            "forward_registration_runs",
            "forward_registration_samples",
            "shadow_accounts",
            "forward_ablation_predictions",
        }
        missing_tables = sorted(required_tables - tables)
        if missing_tables:
            return {
                "primary_start_count": 0,
                "formal_samples": 0,
                "shadow_accounts": 0,
                "shadow_variants": [],
                "shadow_accounts_independent": False,
                "shadow_orders": 0,
                "shadow_fills": 0,
                "shadow_positions": 0,
                "duplicate_formal_samples": 0,
                "demo_pollution": 0,
                "schema_ready": False,
                "missing_schema": missing_tables,
            }
        experiment_id = str((experiment or {}).get("experiment_id") or "")
        cohort_id = str((experiment or {}).get("cohort_id") or "")
        primary = int(
            db.execute(
                """SELECT COUNT(*) FROM forward_registration_runs r
                   JOIN forward_experiment_protocols p
                     ON p.experiment_id=r.experiment_id
                   WHERE p.is_primary=1 AND r.trade_date=?
                     AND (?='' OR r.experiment_id=?)""",
                (day, experiment_id, experiment_id),
            ).fetchone()[0]
        )
        formal_samples = int(
            db.execute(
                """SELECT COUNT(*) FROM forward_registration_samples
                   WHERE experiment_id=? AND trade_date=? AND status='registered'""",
                (experiment_id, day),
            ).fetchone()[0]
        ) if experiment_id else 0
        shadow_rows = db.execute(
            """SELECT account_id,variant,cohort_id FROM shadow_accounts
               WHERE cohort_id=? AND status='active'""",
            (cohort_id,),
        ).fetchall() if cohort_id else []
        duplicate_samples = int(
            db.execute(
                """SELECT COUNT(*) FROM (
                     SELECT cohort_id,sample_key,horizon_days,variant,COUNT(*) AS n
                     FROM forward_ablation_predictions
                     WHERE cohort_id=? AND as_of=?
                       AND registration_origin='automatic_primary'
                     GROUP BY cohort_id,sample_key,horizon_days,variant HAVING n>1
                   )""",
                (cohort_id, day),
            ).fetchone()[0]
        ) if cohort_id else 0
        pollution = int(
            db.execute(
                """SELECT COUNT(*) FROM forward_ablation_predictions
                   WHERE cohort_id=? AND as_of=?
                     AND registration_origin!='automatic_primary'""",
                (cohort_id, day),
            ).fetchone()[0]
        ) if cohort_id else 0
        shadow_orders = int(
            db.execute(
                """SELECT COUNT(*) FROM shadow_orders
                   WHERE account_id IN (
                       SELECT account_id FROM shadow_accounts WHERE cohort_id=?
                   )""",
                (cohort_id,),
            ).fetchone()[0]
        ) if cohort_id and "shadow_orders" in tables else 0
        shadow_fills = int(
            db.execute(
                """SELECT COUNT(*) FROM shadow_fills
                   WHERE account_id IN (
                       SELECT account_id FROM shadow_accounts WHERE cohort_id=?
                   )""",
                (cohort_id,),
            ).fetchone()[0]
        ) if cohort_id and "shadow_fills" in tables else 0
        shadow_positions = int(
            db.execute(
                """SELECT COUNT(*) FROM shadow_positions
                   WHERE account_id IN (
                       SELECT account_id FROM shadow_accounts WHERE cohort_id=?
                   )""",
                (cohort_id,),
            ).fetchone()[0]
        ) if cohort_id and "shadow_positions" in tables else 0
    variants = [str(row["variant"]) for row in shadow_rows]
    account_ids = [str(row["account_id"]) for row in shadow_rows]
    return {
        "primary_start_count": primary,
        "formal_samples": formal_samples,
        "shadow_accounts": len(shadow_rows),
        "shadow_variants": sorted(variants),
        "shadow_accounts_independent": (
            len(account_ids) == len(set(account_ids))
            and len(variants) == len(set(variants))
        ),
        "shadow_orders": shadow_orders,
        "shadow_fills": shadow_fills,
        "shadow_positions": shadow_positions,
        "duplicate_formal_samples": duplicate_samples,
        "demo_pollution": pollution,
        "schema_ready": True,
        "missing_schema": [],
    }


def _persisted_registration_readiness(
    path: Path, trade_date: date
) -> dict[str, Any] | None:
    import sqlite3

    with sqlite3.connect(path, timeout=30) as db:
        table = db.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='forward_registration_runs'"""
        ).fetchone()
        if table is None:
            return None
        rows = db.execute(
            """SELECT payload FROM forward_registration_runs
               WHERE trade_date=? AND status='completed' AND registered_samples>0
               ORDER BY completed_at DESC""",
            (trade_date.isoformat(),),
        ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row[0]) or "{}")
        except json.JSONDecodeError:
            continue
        readiness = payload.get("readiness")
        if (
            isinstance(readiness, dict)
            and readiness.get("as_of") == trade_date.isoformat()
            and bool(readiness.get("sample_registration_allowed"))
        ):
            return readiness
    return None


def _natural_schedule_attempt_checks(path: Path, trade_date: date) -> dict[str, Any]:
    import sqlite3

    required = (
        "trusted_data_refresh",
        "forward_sample_registration",
        "shadow_account_cycle",
    )
    attempts: dict[str, dict[str, Any]] = {}
    with sqlite3.connect(path, timeout=30) as db:
        db.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not {"runtime_schedules", "runtime_schedule_runs", "background_jobs"} <= tables:
            return {
                "status": "unavailable",
                "mandatory": False,
                "attempts": {},
            }
        rows = db.execute(
            """SELECT s.name,r.attempt_number,r.status schedule_status,
                      b.status job_status,b.result_payload,r.created_at
               FROM runtime_schedule_runs r
               JOIN runtime_schedules s ON s.schedule_id=r.schedule_id
               LEFT JOIN background_jobs b ON b.job_id=r.job_id
               WHERE r.run_date=? AND r.attempt_number=1
                 AND s.name IN (?,?,?)
               ORDER BY r.created_at""",
            (trade_date.isoformat(), *required),
        ).fetchall()
    for row in rows:
        result: dict[str, Any] = {}
        if row["result_payload"]:
            try:
                result = json.loads(str(row["result_payload"]))
            except json.JSONDecodeError:
                result = {}
        name = str(row["name"])
        passed = row["job_status"] == "completed"
        if name == "forward_sample_registration":
            passed = passed and int(result.get("registered_samples") or 0) > 0
        elif name == "shadow_account_cycle":
            passed = passed and int(
                (result.get("marking") or {}).get("accounts_marked") or 0
            ) == 7
        attempts[name] = {
            "passed": bool(passed),
            "schedule_status": row["schedule_status"],
            "job_status": row["job_status"],
            "created_at": row["created_at"],
        }
    if len(attempts) < len(required):
        status = "waiting"
    elif all(bool(attempts[name]["passed"]) for name in required):
        status = "passed"
    else:
        status = "blocked"
    return {
        "status": status,
        "mandatory": False,
        "attempts": attempts,
    }


def _acceptance_jobs_pending(path: Path, trade_date: date) -> list[dict[str, Any]]:
    import sqlite3

    with sqlite3.connect(path, timeout=30) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='background_jobs'"
        ).fetchone():
            return []
        columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(background_jobs)")
        }
        if not {"job_id", "job_type", "status", "created_at"} <= columns:
            return []
        rows = db.execute(
            """SELECT job_id,job_type,status,created_at FROM background_jobs
               WHERE status IN ('queued','running','retry_wait')
                 AND job_type IN ('trusted_data_refresh','forward_sample_registration')
                 AND substr(created_at,1,10)=? ORDER BY created_at""",
            (trade_date.isoformat(),),
        ).fetchall()
    return [
        {"job_id": row[0], "job_type": row[1], "status": row[2], "created_at": row[3]}
        for row in rows
    ]


def _duplicate_count(path, table: str, column: str) -> int:
    import sqlite3

    with sqlite3.connect(path, timeout=30) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            return 0
        columns = {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}
        if column not in columns:
            return 0
        return int(
            db.execute(
                f'''SELECT COUNT(*) FROM (
                       SELECT "{column}",COUNT(*) FROM "{table}"
                       GROUP BY "{column}" HAVING COUNT(*)>1
                   )'''  # noqa: S608 - fixed internal names
            ).fetchone()[0]
        )


def _demo_pollution_count(path) -> int:
    import sqlite3

    with sqlite3.connect(path, timeout=30) as db:
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        count = 0
        if "forward_experiments" in tables:
            count += int(
                db.execute(
                    "SELECT COUNT(*) FROM forward_experiments WHERE experiment_id LIKE '%demo%'"
                ).fetchone()[0]
            )
        if "unified_experiment_runs" in tables:
            count += int(
                db.execute(
                    """SELECT COUNT(*) FROM unified_experiment_runs
                       WHERE evidence_boundary='demo' AND status='completed'"""
                ).fetchone()[0]
            )
        return count


__all__ = [
    "ExperimentRecorder",
    "checkpoint_signature",
    "configuration_fingerprint",
    "next_trading_day_acceptance_report",
    "source_code_fingerprint",
    "source_build_manifest",
]
