from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from quantlab.config import Settings
from quantlab.persistence.jobs import JobRepository
from quantlab.market import TradingCalendarService


DEFAULT_SCHEDULES: tuple[dict[str, Any], ...] = (
    {
        "name": "premarket_digest",
        "job_type": "premarket_digest",
        "local_time": "08:15",
        "dependencies": [],
    },
    {
        "name": "forward_preflight",
        "job_type": "forward_preflight",
        "local_time": "08:30",
        "dependencies": [],
    },
    {
        "name": "trusted_data_refresh",
        "job_type": "trusted_data_refresh",
        "local_time": "15:10",
        "dependencies": [],
        "trading_days_only": False,
        "payload": {
            "require_forward_readiness": True,
            "forward_readiness_retry_deadline_local": "15:40",
        },
    },
    {
        "name": "capital_flow_refresh",
        "job_type": "capital_flow_refresh",
        "local_time": "15:25",
        "dependencies": ["trusted_data_refresh"],
    },
    {
        "name": "prediction_settlement",
        "job_type": "forward_settlement_scan",
        "local_time": "15:35",
        "dependencies": ["capital_flow_refresh"],
    },
    {
        "name": "forward_sample_registration",
        "job_type": "forward_sample_registration",
        "local_time": "15:45",
        "dependencies": ["prediction_settlement"],
        "payload": {"strict_registration_deadline_local": "16:00"},
    },
    {
        "name": "shadow_account_cycle",
        "job_type": "shadow_account_cycle",
        "local_time": "15:50",
        "dependencies": ["forward_sample_registration"],
    },
    {
        "name": "wide_forward_registration",
        "job_type": "wide_forward_registration",
        "local_time": "15:55",
        "dependencies": ["trusted_data_refresh", "prediction_settlement"],
        "payload": {
            "require_forward_readiness": True,
            "strict_registration_deadline_local": "16:00",
        },
    },
    {
        "name": "account_mark_to_market",
        "job_type": "mark_to_market",
        "local_time": "16:00",
        "dependencies": ["shadow_account_cycle"],
    },
    {
        "name": "investor_mark_to_market",
        "job_type": "investor_mark_to_market",
        "local_time": "16:00",
        "dependencies": ["shadow_account_cycle"],
    },
    {
        "name": "investor_outcome_settlement",
        "job_type": "investor_outcome_settlement",
        "local_time": "16:05",
        "dependencies": ["trusted_data_refresh"],
    },
    {
        "name": "wide_research_portfolio_mark",
        "job_type": "wide_research_portfolio_mark",
        "local_time": "16:05",
        "dependencies": ["prediction_settlement"],
    },
    {
        "name": "thesis_due_scan",
        "job_type": "thesis_due_scan",
        "local_time": "16:06",
        "dependencies": ["trusted_data_refresh"],
    },
    {
        "name": "thesis_event_check",
        "job_type": "thesis_event_check",
        "local_time": "16:07",
        "dependencies": ["thesis_due_scan"],
    },
    {
        "name": "thesis_price_invalidation_check",
        "job_type": "thesis_price_invalidation_check",
        "local_time": "16:08",
        "dependencies": ["thesis_event_check"],
    },
    {
        "name": "authoritative_reflection_settlement",
        "job_type": "authoritative_reflection_settlement",
        "local_time": "16:09",
        "dependencies": ["prediction_settlement", "investor_outcome_settlement"],
    },
    {
        "name": "controlled_memory_refresh",
        "job_type": "controlled_memory_refresh",
        "local_time": "16:09",
        "dependencies": ["authoritative_reflection_settlement"],
    },
    {
        "name": "decision_task_refresh",
        "job_type": "decision_task_refresh",
        "local_time": "16:09",
        "dependencies": ["controlled_memory_refresh", "thesis_price_invalidation_check"],
    },
    {
        "name": "account_daily_report",
        "job_type": "account_daily_report",
        "local_time": "16:10",
        "dependencies": [
            "prediction_settlement",
            "shadow_account_cycle",
            "account_mark_to_market",
            "investor_mark_to_market",
            "investor_outcome_settlement",
            "thesis_due_scan",
            "thesis_event_check",
            "thesis_price_invalidation_check",
            "authoritative_reflection_settlement",
            "controlled_memory_refresh",
            "decision_task_refresh",
        ],
    },
    {
        "name": "notification_dispatch",
        "job_type": "notification_dispatch",
        "local_time": "16:20",
        "dependencies": ["account_daily_report"],
    },
    {
        "name": "retention_cleanup",
        "job_type": "retention_cleanup",
        "local_time": "16:30",
        "dependencies": ["notification_dispatch"],
    },
    {
        "name": "database_backup",
        "job_type": "database_backup",
        "local_time": "16:40",
        "dependencies": ["retention_cleanup"],
    },
)


class RuntimeScheduler:
    def __init__(self, settings: Settings):
        from quantlab.persistence.migrations import ensure_database_initialized

        ensure_database_initialized(settings.resolve(settings.get("system.database_path")))
        self.settings = settings
        self.repository = JobRepository(
            settings.resolve(settings.get("system.database_path"))
        )
        self.timezone = ZoneInfo(str(settings.get("system.timezone", "Asia/Shanghai")))

    def register_defaults(self) -> list[dict[str, Any]]:
        return [
            self.repository.register_schedule(
                name=item["name"],
                job_type=item["job_type"],
                local_time=item["local_time"],
                dependency_names=item["dependencies"],
                trading_days_only=bool(item.get("trading_days_only", True)),
                enabled=True,
                payload={
                    **dict(item.get("payload") or {}),
                    "schedule_version": "wide-forward-research-v1",
                },
                timeout_seconds=(
                    7_200
                    if item["job_type"] == "wide_forward_registration"
                    else
                    1_800
                    if item["job_type"] in {"capital_flow_refresh", "forward_preflight"}
                    else 900
                ),
                max_attempts=3,
                cost_budget_usd=(
                    float(
                        self.settings.get(
                            "strategies.wide_forward.maximum_llm_cost_usd_per_batch",
                            40.0,
                        )
                    )
                    if item["job_type"] == "wide_forward_registration"
                    else 0.0
                ),
            )
            for item in DEFAULT_SCHEDULES
        ]

    def tick(
        self,
        *,
        now: datetime | None = None,
        run_date: date | None = None,
        backfill: bool = False,
    ) -> dict[str, Any]:
        self.register_defaults()
        resolved_now = now or datetime.now(UTC)
        if resolved_now.tzinfo is None:
            raise ValueError("scheduler time must include a timezone")
        local_now = resolved_now.astimezone(self.timezone)
        resolved_date = run_date or local_now.date()
        if not backfill and resolved_date != local_now.date():
            raise ValueError(
                "non-backfill scheduler runs are restricted to the server's current local date"
            )
        try:
            calendar = TradingCalendarService.from_settings(self.settings).day(
                resolved_date,
                cutoff_at=resolved_now,
                formal=True,
            )
        except ValueError:
            calendar = None
        created: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        run_jobs: dict[str, str] = {
            schedule_by_id[run["schedule_id"]]["name"]: run["job_id"]
            for run in self.repository.schedule_runs(resolved_date, 500)
            if run.get("job_id")
            for schedule_by_id in [
                {item["schedule_id"]: item for item in self.repository.schedules()}
            ]
            if run["schedule_id"] in schedule_by_id
        }
        schedules = self.repository.schedules(enabled_only=True)
        by_name = {item["name"]: item for item in schedules}
        for schedule in schedules:
            if backfill and schedule["job_type"] in {
                "forward_sample_registration",
                "wide_forward_registration",
            }:
                reason = (
                    "primary_forward_backfill_is_not_formal_evidence"
                    if schedule["job_type"] == "forward_sample_registration"
                    else "forward_backfill_is_not_preregistered_evidence"
                )
                skipped.append(
                    {
                        "name": schedule["name"],
                        "reason": reason,
                    }
                )
                continue
            if schedule["trading_days_only"]:
                if schedule["name"] == "trusted_data_refresh" and calendar is None:
                    if resolved_date.weekday() >= 5:
                        skipped.append(
                            {"name": schedule["name"], "reason": "non_trading_day"}
                        )
                        continue
                elif calendar is None:
                    skipped.append(
                        {"name": schedule["name"], "reason": "trusted_calendar_unavailable"}
                    )
                    continue
                if calendar is not None and not calendar["is_open"]:
                    skipped.append({"name": schedule["name"], "reason": "non_trading_day"})
                    continue
            scheduled_time = datetime.strptime(
                f"{resolved_date.isoformat()} {schedule['local_time']}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=self.timezone)
            if not backfill and local_now < scheduled_time:
                skipped.append({"name": schedule["name"], "reason": "not_due"})
                continue
            missing_dependencies = [
                name for name in schedule["dependency_names"] if name not in run_jobs
            ]
            if missing_dependencies:
                skipped.append(
                    {
                        "name": schedule["name"],
                        "reason": "dependencies_not_scheduled",
                        "dependencies": missing_dependencies,
                    }
                )
                continue
            schedule_run = self.repository.create_schedule_run(
                schedule_id=schedule["schedule_id"],
                run_date=resolved_date,
                is_backfill=backfill,
                payload={
                    "calendar": calendar or {"status": "unavailable"},
                    "scheduled_time": scheduled_time.isoformat(),
                    "dependency_names": schedule["dependency_names"],
                },
            )
            if schedule_run.get("job_id"):
                existing = self.repository.job(schedule_run["job_id"])
                if existing:
                    run_jobs[schedule["name"]] = existing["job_id"]
                    created.append(existing)
                continue
            dependency_job_ids = [run_jobs[name] for name in schedule["dependency_names"]]
            job = self.repository.submit(
                job_type=schedule["job_type"],
                payload={
                    **schedule["payload"],
                    "as_of": resolved_date.isoformat(),
                    "schedule_name": schedule["name"],
                    "schedule_run_id": schedule_run["schedule_run_id"],
                    "is_backfill": backfill,
                },
                idempotency_key=f"schedule:{schedule['name']}:{resolved_date.isoformat()}",
                concurrency_key=f"schedule:{schedule['name']}",
                max_attempts=int(schedule["max_attempts"]),
                timeout_seconds=int(schedule["timeout_seconds"]),
                cost_budget_usd=float(schedule["cost_budget_usd"]),
                schedule_run_id=schedule_run["schedule_run_id"],
                dependency_job_ids=dependency_job_ids,
            )
            self.repository.link_schedule_job(schedule_run["schedule_run_id"], job["job_id"])
            run_jobs[schedule["name"]] = job["job_id"]
            created.append(job)
        from quantlab.runtime.readiness import primary_start_readiness

        readiness = primary_start_readiness(
            self.settings,
            trade_date=resolved_date,
            now=resolved_now,
            require_runtime=False,
        )
        return {
            "run_date": resolved_date.isoformat(),
            "calendar": calendar or {"status": "unavailable", "formal_eligible": False},
            "jobs": created,
            "skipped": skipped,
            "dependency_order": [item["name"] for item in schedules if item["name"] in by_name],
            "idempotent": True,
            "readiness": readiness,
        }

    def backfill(self, run_date: date) -> dict[str, Any]:
        return self.tick(
            now=datetime.now(UTC),
            run_date=run_date,
            backfill=True,
        )

    def recover_same_day(
        self,
        schedule_name: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Create a new scheduler-owned, append-only attempt for today's formal chain."""
        if not str(reason).strip():
            raise ValueError("same-day schedule recovery requires an audit reason")
        self.register_defaults()
        observed = now or datetime.now(UTC)
        if observed.tzinfo is None:
            raise ValueError("scheduler recovery time must include a timezone")
        local_now = observed.astimezone(self.timezone)
        run_date = local_now.date()
        schedules = {item["name"]: item for item in self.repository.schedules(enabled_only=True)}
        schedule = schedules.get(schedule_name)
        if schedule is None:
            raise ValueError(f"unknown or disabled schedule: {schedule_name}")
        if schedule["trading_days_only"]:
            calendar = TradingCalendarService.from_settings(self.settings).day(
                run_date,
                cutoff_at=observed,
                formal=True,
            )
            if not calendar["is_open"]:
                raise ValueError("same-day formal recovery requires an open trading day")
        scheduled_time = datetime.strptime(
            f"{run_date.isoformat()} {schedule['local_time']}",
            "%Y-%m-%d %H:%M",
        ).replace(tzinfo=self.timezone)
        if local_now < scheduled_time:
            raise ValueError("same-day recovery is only allowed after the original schedule time")
        runs = self.repository.schedule_runs(run_date, 500)
        related = [
            item
            for item in runs
            if item["schedule_id"] == schedule["schedule_id"]
            and not item.get("is_backfill")
        ]
        if not related:
            raise ValueError(
                "same-day recovery requires a preserved non-backfill original schedule run"
            )
        latest = max(related, key=lambda item: int(item.get("attempt_number") or 1))
        latest_job = self.repository.job(latest["job_id"]) if latest.get("job_id") else None
        if latest_job and latest_job["status"] in {"queued", "running"}:
            return {
                "schedule_run": latest,
                "job": latest_job,
                "idempotent": True,
                "reason": "latest recovery attempt is still active",
            }
        latest_by_name: dict[str, dict[str, Any]] = {}
        schedules_by_id = {item["schedule_id"]: item for item in schedules.values()}
        for item in runs:
            definition = schedules_by_id.get(item["schedule_id"])
            if definition is None or not item.get("job_id"):
                continue
            current = latest_by_name.get(definition["name"])
            if current is None or int(item.get("attempt_number") or 1) > int(
                current.get("attempt_number") or 1
            ):
                latest_by_name[definition["name"]] = item
        missing_dependencies = [
            name for name in schedule["dependency_names"] if name not in latest_by_name
        ]
        if missing_dependencies:
            raise ValueError(
                "same-day recovery dependencies are unavailable: "
                + ",".join(missing_dependencies)
            )
        incomplete_dependencies = []
        for name in schedule["dependency_names"]:
            dependency = latest_by_name[name]
            dependency_job = self.repository.job(dependency["job_id"])
            if dependency_job is None or dependency_job["status"] != "completed":
                incomplete_dependencies.append(
                    f"{name}:{dependency_job['status'] if dependency_job else 'missing'}"
                )
        if incomplete_dependencies:
            raise ValueError(
                "same-day recovery dependencies are not completed: "
                + ",".join(incomplete_dependencies)
            )
        dependency_job_ids = [
            latest_by_name[name]["job_id"] for name in schedule["dependency_names"]
        ]
        schedule_run = self.repository.create_schedule_run(
            schedule_id=schedule["schedule_id"],
            run_date=run_date,
            is_backfill=False,
            payload={
                "scheduled_time": scheduled_time.isoformat(),
                "dependency_names": schedule["dependency_names"],
                "recovery": True,
                "recovery_reason": str(reason).strip(),
            },
            force_new_attempt=True,
            recovery_of_schedule_run_id=latest["schedule_run_id"],
            recovery_reason=str(reason).strip(),
        )
        attempt = int(schedule_run["attempt_number"])
        job = self.repository.submit(
            job_type=schedule["job_type"],
            payload={
                **schedule["payload"],
                "as_of": run_date.isoformat(),
                "schedule_name": schedule["name"],
                "schedule_run_id": schedule_run["schedule_run_id"],
                "is_backfill": False,
                "scheduler_recovery": True,
                "recovery_attempt": attempt,
                "recovery_of_schedule_run_id": latest["schedule_run_id"],
                "recovery_reason": str(reason).strip(),
            },
            idempotency_key=(
                f"schedule-recovery:{schedule['name']}:{run_date.isoformat()}:attempt:{attempt}"
            ),
            concurrency_key=f"schedule:{schedule['name']}",
            max_attempts=int(schedule["max_attempts"]),
            timeout_seconds=int(schedule["timeout_seconds"]),
            cost_budget_usd=float(schedule["cost_budget_usd"]),
            schedule_run_id=schedule_run["schedule_run_id"],
            dependency_job_ids=dependency_job_ids,
        )
        self.repository.link_schedule_job(schedule_run["schedule_run_id"], job["job_id"])
        return {
            "schedule_run": self.repository.schedule_run_for_job(job["job_id"]),
            "job": job,
            "idempotent": False,
            "audit": {
                "origin": "scheduler_same_day_recovery",
                "reason": str(reason).strip(),
                "attempt": attempt,
                "recovery_of_schedule_run_id": latest["schedule_run_id"],
            },
        }


__all__ = ["DEFAULT_SCHEDULES", "RuntimeScheduler"]
