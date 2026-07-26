from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from threading import Event, Thread
from typing import Any, Callable
from zoneinfo import ZoneInfo

from quantlab.config import Settings
from quantlab.domain.jobs import JobBudgetExceeded, JobCancelled, JobStatus
from quantlab.persistence.jobs import JobRepository
from quantlab.persistence.notifications import NotificationRepository


JobHandler = Callable[["JobContext", dict[str, Any]], dict[str, Any] | None]

DEFAULT_REPLAY_SAFE_JOB_TYPES = frozenset(
    {
        "research",
        "historical_replay",
        "capital_flow_refresh",
        "forward_preflight",
        "trusted_data_refresh",
        "training",
        "simulator_settlement",
        "daily_cycle",
        "notification_dispatch",
        "premarket_digest",
        "account_daily_report",
        "forward_settlement_scan",
        "forward_sample_registration",
        "wide_forward_registration",
        "wide_research_portfolio_mark",
        "shadow_account_cycle",
        "investor_mark_to_market",
        "investor_outcome_settlement",
        "mark_to_market",
        "a_share_v4_research",
        "convertible_bond_research",
        "etf_pit_replay",
        "retention_cleanup",
        "database_backup",
        "chat_request",
        "thesis_due_scan",
        "thesis_event_check",
        "thesis_price_invalidation_check",
        "authoritative_reflection_settlement",
        "controlled_memory_refresh",
        "decision_task_refresh",
    }
)


@dataclass
class JobContext:
    repository: JobRepository
    job: dict[str, Any]
    worker_id: str

    def progress(
        self,
        value: float,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.check_cancelled()
        if not self.repository.heartbeat(
            self.job["job_id"],
            self.worker_id,
            progress=value,
            message=message,
            payload=payload,
        ):
            raise JobCancelled("job lease is no longer active")

    def heartbeat(self) -> None:
        self.progress(float(self.job.get("progress", 0.0)), "heartbeat")

    def check_cancelled(self) -> None:
        if self.repository.is_cancelled(self.job["job_id"]):
            raise JobCancelled("job was cancelled")

    def consume_cost(self, amount_usd: float) -> None:
        if not self.repository.consume_cost(
            self.job["job_id"], self.worker_id, amount_usd
        ):
            raise JobBudgetExceeded("job cost budget exceeded")


class JobWorker:
    def __init__(
        self,
        settings: Settings,
        *,
        worker_id: str,
        handlers: dict[str, JobHandler] | None = None,
        queue_name: str = "default",
        maximum_running: int = 4,
        per_type_limits: dict[str, int] | None = None,
        replay_safe_job_types: set[str] | frozenset[str] | None = None,
    ):
        from quantlab.persistence.migrations import ensure_database_initialized

        ensure_database_initialized(settings.resolve(settings.get("system.database_path")))
        self.settings = settings
        self.worker_id = worker_id
        self.queue_name = queue_name
        self.maximum_running = maximum_running
        self.per_type_limits = per_type_limits or {
            "research": 2,
            "chat_request": 2,
            "historical_replay": 1,
            "training": 1,
            "daily_cycle": 1,
        }
        self.repository = JobRepository(
            settings.resolve(settings.get("system.database_path"))
        )
        self.handlers = handlers or default_job_handlers(settings)
        self.replay_safe_job_types = set(
            replay_safe_job_types
            if replay_safe_job_types is not None
            else DEFAULT_REPLAY_SAFE_JOB_TYPES
            if handlers is None
            else ()
        )

    def run_once(self) -> dict[str, Any] | None:
        lock_path = self.repository.path.with_suffix(
            self.repository.path.suffix + ".maintenance.lock"
        )
        if lock_path.exists():
            return None
        job = self.repository.claim(
            worker_id=self.worker_id,
            queue_name=self.queue_name,
            maximum_running=self.maximum_running,
            per_type_limits=self.per_type_limits,
        )
        if job is None:
            return None
        context = JobContext(self.repository, job, self.worker_id)
        handler = self.handlers.get(job["job_type"])
        if handler is None:
            return self.repository.fail(
                job["job_id"],
                self.worker_id,
                f"no handler registered for job type {job['job_type']}",
                retryable=False,
            )
        if job.get("side_effect_state") == "completed" and job.get(
            "side_effect_result_payload"
        ) is not None:
            return self.repository.complete(
                job["job_id"],
                self.worker_id,
                result=job["side_effect_result_payload"],
            )
        if (
            job.get("side_effect_state") == "started"
            and int(job["attempts"]) > 1
            and job["job_type"] not in self.replay_safe_job_types
        ):
            return self.repository.block_uncertain_side_effect(
                job["job_id"], self.worker_id
            )
        heartbeat_stop = Event()
        heartbeat_thread = Thread(
            target=self._lease_heartbeat_loop,
            args=(job, heartbeat_stop),
            name=f"quantlab-lease-{job['job_id'][:8]}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            context.progress(0.01, "worker started")
            context.check_cancelled()
            self.repository.mark_side_effect_state(
                job["job_id"], self.worker_id, "started"
            )
            result = handler(context, job["payload"]) or {}
            self.repository.mark_side_effect_state(
                job["job_id"], self.worker_id, "completed", result=result
            )
            context.check_cancelled()
            completed = self.repository.complete(
                job["job_id"], self.worker_id, result=result
            )
            _emit_job_notification(self.settings, completed)
            return completed
        except JobCancelled:
            return self.repository.acknowledge_cancel(
                job["job_id"],
                self.worker_id,
                reason="worker observed cooperative cancellation",
            )
        except JobBudgetExceeded as exc:
            failed = self.repository.fail(
                job["job_id"], self.worker_id, exc, retryable=False
            )
            _mark_chat_message_failed(self.settings, job, exc, status="failed")
            _emit_job_notification(self.settings, failed)
            return failed
        except Exception as exc:
            failed = self.repository.fail(
                job["job_id"],
                self.worker_id,
                exc,
                retryable=job["job_type"] in self.replay_safe_job_types,
            )
            _mark_chat_message_failed(
                self.settings,
                job,
                exc,
                status="queued" if failed["status"] == "queued" else "failed",
            )
            if failed["status"] == JobStatus.FAILED.value:
                _emit_job_notification(self.settings, failed)
            return failed
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2.0)

    def run_until_empty(self, maximum_jobs: int = 100) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for _ in range(maximum_jobs):
            result = self.run_once()
            if result is None:
                break
            output.append(result)
        return output

    def _lease_heartbeat_loop(self, job: dict[str, Any], stop: Event) -> None:
        interval = max(0.25, min(10.0, float(job["timeout_seconds"]) / 3.0))
        while not stop.wait(interval):
            try:
                if not self.repository.heartbeat(job["job_id"], self.worker_id):
                    return
            except Exception:
                return


def default_job_handlers(settings: Settings) -> dict[str, JobHandler]:
    def research(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.persistence import DecisionRepository
        from quantlab.domain import ResearchProvenance
        from quantlab.reporting import (
            build_research_audit_package,
            research_persistence_context,
        )
        from quantlab.workflows.research import analyze_symbol

        context.progress(0.10, "building point-in-time research evidence")
        output = analyze_symbol(
            settings,
            payload["symbol"],
            _optional_date(payload.get("as_of")),
            asset_type=payload.get("asset_type"),
            include_events=bool(payload.get("include_events", True)),
            account_id=payload.get("account_id"),
        )
        context.progress(0.80, "persisting research report")
        if bool(payload.get("save", True)):
            decision_repository = DecisionRepository(
                settings.resolve(settings.get("system.database_path"))
            )
            decision_repository.save(
                output["decision_run"],
                research_persistence_context(output),
                provenance=ResearchProvenance(
                    origin="user_interactive_research",
                    requested_as_of=payload.get("as_of"),
                    evidence_stage="research_only",
                ),
            )
            record = decision_repository.get(output["decision_run"].run_id)
            if record is None:
                raise RuntimeError("persisted research is unavailable")
            from quantlab.reporting import build_stored_audit_package

            package = build_stored_audit_package(record)
        else:
            package = build_research_audit_package(output)
        decision = package.get("decision", {})
        return {
            "run_id": output["decision_run"].run_id,
            "symbol": payload["symbol"],
            "action": decision.get("action"),
            "context_id": package.get("analysis_context_pack", {}).get("context_id"),
            "report": package,
        }

    def historical_replay(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.workflows.replay import run_historical_blind_replay

        def update(event: dict[str, Any]) -> None:
            completed = float(event.get("completed", 0.0))
            total = max(1.0, float(event.get("total", 1.0)))
            context.progress(
                min(0.95, 0.05 + 0.90 * completed / total),
                str(event.get("message") or "historical replay running"),
                event,
            )

        return run_historical_blind_replay(
            settings,
            _required_date(payload["start"]),
            _required_date(payload["end"]),
            horizon_days=int(payload.get("horizon_days", 20)),
            episodes=int(payload.get("episodes", 3)),
            save=bool(payload.get("save", True)),
            allow_large_run=bool(payload.get("confirm_large_run", False)),
            progress_callback=update,
        )

    def capital_flow_refresh(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.persistence import EvidenceRepository, TerminalRepository
        from quantlab.workflows.capital_flow import (
            build_live_stock_flow,
            industry_flow_blocks_from_radar,
            unavailable_flow_block,
        )
        from quantlab.workflows.context import market_flow_block_from_radar
        from quantlab.workflows.radar import build_market_radar
        from quantlab.workflows.simulator import user_simulator_repository

        as_of = _optional_date(payload.get("as_of")) or date.today()
        repository = EvidenceRepository(
            settings.resolve(settings.get("system.database_path"))
        )

        def last_success(scope: str, key: str) -> str | None:
            for item in repository.flows(scope, scope_key=key, limit=100):
                if (
                    item.get("quality") != "unavailable"
                    and item.get("payload", {}).get("status") != "unavailable"
                ):
                    return str(item.get("available_at") or item.get("as_of"))
            return None

        context.progress(0.15, "refreshing market data")
        context.check_cancelled()
        radar_error: Exception | None = None
        try:
            radar = build_market_radar(
                settings,
                as_of,
                include_sectors=bool(payload.get("include_sectors", True)),
            )
        except Exception as exc:
            radar_error = exc
            radar = {
                "status": "unavailable",
                "as_of": as_of.isoformat(),
                "source": "market_radar",
                "reason": f"market_radar_unavailable:{type(exc).__name__}",
                "sectors": [],
            }
        context.check_cancelled()
        context.progress(0.80, "calculating capital-flow evidence")
        block = (
            market_flow_block_from_radar(radar, as_of)
            if radar_error is None
            else None
        )
        if block is None:
            block = unavailable_flow_block(
                scope="market",
                key="cn_market",
                as_of=as_of,
                source=str(radar.get("source") or "market_radar"),
                reason=(
                    f"market_radar_unavailable:{type(radar_error).__name__}"
                    if radar_error is not None
                    else "market radar did not produce a capital-flow evidence block"
                ),
                last_success_at=last_success("market", "cn_market"),
            )
        saved_market = repository.save_flow(block)
        industry_blocks = (
            industry_flow_blocks_from_radar(radar, as_of=as_of)
            if radar_error is None
            else [
                unavailable_flow_block(
                    scope="industry",
                    key="all",
                    as_of=as_of,
                    source=str(radar.get("source") or "market_radar"),
                    reason=f"market_radar_unavailable:{type(radar_error).__name__}",
                    last_success_at=last_success("industry", "all"),
                )
            ]
        )
        saved_industries = [repository.save_flow(item) for item in industry_blocks]
        symbols: set[str] = set()
        symbols.update(str(item) for item in payload.get("symbols", []))
        simulator = user_simulator_repository(settings)
        for account in simulator.accounts(include_closed=False):
            symbols.update(
                position["symbol"]
                for position in simulator.positions(account["account_id"])
            )
        try:
            symbols.update(
                item["symbol"]
                for item in TerminalRepository(repository.path).list_watchlist()
            )
        except Exception:
            pass
        stock_results: list[dict[str, Any]] = []
        for symbol in sorted(symbols)[:200]:
            context.check_cancelled()
            try:
                stock_results.append(
                    repository.save_flow(
                        build_live_stock_flow(
                            settings,
                            symbol,
                            _optional_date(payload.get("as_of")),
                        )
                    )
                )
            except Exception as exc:
                stock_results.append(
                    repository.save_flow(
                        unavailable_flow_block(
                            scope="stock",
                            key=symbol,
                            as_of=as_of,
                            source="capital_flow_refresh",
                            reason=f"stock_flow_unavailable:{type(exc).__name__}",
                            last_success_at=last_success("stock", symbol),
                        )
                    )
                )
        return {
            "radar": radar,
            "market_snapshot": saved_market,
            "industry_snapshots": saved_industries,
            "stock_snapshots": stock_results,
            "persisted": True,
            "degraded": radar_error is not None
            or any(item.get("quality") == "unavailable" for item in stock_results),
        }

    def trusted_data_refresh(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.workflows.trusted_data import refresh_trusted_data

        as_of = _optional_date(payload.get("as_of")) or date.today()
        context.progress(0.10, "refreshing server-configured trusted data")
        refreshed = refresh_trusted_data(
            settings,
            as_of=as_of,
        )
        if not payload.get("require_forward_readiness"):
            return refreshed

        from quantlab.runtime.readiness import primary_start_readiness

        readiness = primary_start_readiness(
            settings,
            trade_date=as_of,
            require_runtime=False,
        )
        calendar_day = readiness["data"].get("calendar_day")
        retry_deadline = _local_schedule_deadline(
            settings,
            as_of=as_of,
            value=payload.get("forward_readiness_retry_deadline_local"),
        )
        retry_window_open = retry_deadline is None or _utc_now() < retry_deadline
        readiness_summary = {
            "ready": bool(readiness["sample_registration_allowed"] and retry_window_open),
            "checked_at": readiness["checked_at"],
            "blockers": list(readiness["blockers"]),
            "eligible_members": int(
                readiness["data"]["point_in_time_pool"].get("eligible_members", 0)
            ),
            "retry_window_open": retry_window_open,
            "retry_deadline_at": retry_deadline.isoformat() if retry_deadline else None,
        }
        if calendar_day is not None and not calendar_day["is_open"]:
            return {
                **refreshed,
                "forward_readiness": {
                    **readiness_summary,
                    "required": False,
                    "reason": "non_trading_day",
                },
            }
        if readiness["sample_registration_allowed"] and retry_window_open:
            return {**refreshed, "forward_readiness": readiness_summary}

        attempts = int(context.job.get("attempts") or 1)
        max_attempts = int(context.job.get("max_attempts") or 1)
        if attempts < max_attempts and retry_window_open:
            raise RuntimeError(
                "trusted data readiness pending for scheduled forward work: "
                + ",".join(readiness["blockers"])
            )
        return {
            **refreshed,
            "status": "degraded",
            "forward_readiness": {
                **readiness_summary,
                "retry_exhausted": True,
                "retry_window_closed": not retry_window_open,
                "attempts": attempts,
                "max_attempts": max_attempts,
            },
            "claim_boundary": (
                "The bounded refresh retry budget was exhausted. Downstream strict "
                "registration must inspect readiness and skip rather than create or "
                "recover formal evidence."
            ),
        }

    def forward_preflight(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.workflows.forward_preflight import morning_forward_preflight

        context.progress(0.10, "checking forward-window providers without creating signals")
        return morning_forward_preflight(
            settings,
            as_of=_optional_date(payload.get("as_of")) or date.today(),
        )

    def training(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.workflows.learning import train_learning_models

        context.progress(0.10, "training started")
        result = train_learning_models(
            settings,
            horizon_days=payload.get("horizon_days"),
            asset_scope=str(payload.get("asset_scope", "etf")),
            force=bool(payload.get("force", False)),
        )
        return {"models": result}

    def simulator_settlement(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.workflows.simulator import settle_user_paper_order

        context.progress(0.25, "validating settlement")
        return settle_user_paper_order(
            settings,
            order_id=payload["order_id"],
            fill_quantity=payload.get("fill_quantity"),
            fill_key=payload["fill_key"],
        )

    def daily_cycle(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.workflows.daily import run_daily_cycle

        context.progress(0.05, "daily dependency chain started")
        return run_daily_cycle(
            settings,
            as_of=_optional_date(payload.get("as_of")),
            run_research=bool(payload.get("run_research", False)),
        )

    def notification_dispatch(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.runtime.notification_delivery import NotificationDeliveryWorker

        context.progress(0.10, "notification outbox dispatch started")
        return NotificationDeliveryWorker(settings, worker_id=context.worker_id).run_once(
            limit=int(payload.get("limit", 100))
        )

    def premarket_digest(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.runtime.summaries import generate_premarket_digest

        context.progress(0.20, "building pre-market digest")
        return generate_premarket_digest(
            settings,
            report_date=_optional_date(payload.get("as_of")) or date.today(),
            account_id=payload.get("account_id"),
        )

    def account_daily_report(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.runtime.summaries import generate_account_daily_report

        context.progress(0.20, "building account daily report")
        return generate_account_daily_report(
            settings,
            report_date=_optional_date(payload.get("as_of")) or date.today(),
            account_id=payload.get("account_id"),
        )

    def forward_settlement_scan(
        context: JobContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
        from quantlab.workflows.forward_ablation import settle_due_forward_sample

        context.progress(0.30, "scanning genuinely due forward samples")
        due = StrategyEvidenceRepository(
            settings.resolve(settings.get("system.database_path"))
        ).due_forward_samples(as_of=datetime.now(UTC), limit=int(payload.get("limit", 500)))
        cohort_id = payload.get("cohort_id")
        sample_key = payload.get("sample_key")
        horizon_days = payload.get("horizon_days")
        if cohort_id:
            due = [
                item
                for item in due
                if item["cohort_id"] == cohort_id
                and item["sample_key"] == sample_key
                and int(item["horizon_days"]) == int(horizon_days)
            ]
        results = []
        for index, sample in enumerate(due, start=1):
            context.progress(
                0.30 + 0.65 * index / max(1, len(due)),
                f"settling forward sample {sample['sample_key']}",
            )
            results.append(settle_due_forward_sample(settings, sample))
        from quantlab.workflows.forward_experiment import update_forward_milestones

        milestones = update_forward_milestones(settings)
        return {
            "due_samples": due,
            "settled": sum(bool(item.get("settled")) for item in results),
            "pending": sum(not bool(item.get("settled")) for item in results),
            "results": results,
            "milestones": milestones,
            "claim_boundary": (
                "Formal samples use exact due-day research bars meeting the cohort trust "
                "floor. Missing data remains pending with an explicit reason."
            ),
        }

    def forward_sample_registration(
        context: JobContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        from quantlab.workflows.forward_experiment import register_primary_forward_samples

        schedule_run = context.repository.schedule_run_for_job(context.job["job_id"])
        if schedule_run is None:
            return {
                "status": "skipped",
                "reason": "primary forward registration requires a scheduler-owned job",
            }
        if (
            schedule_run["schedule_name"] != "forward_sample_registration"
            or schedule_run["schedule_job_type"] != "forward_sample_registration"
        ):
            return {
                "status": "skipped",
                "reason": "job is not linked to the formal forward registration schedule",
            }
        if payload.get("is_backfill") or schedule_run["is_backfill"]:
            return {
                "status": "skipped",
                "reason": "operator backfills cannot create primary forward evidence",
            }
        scheduled_date = date.fromisoformat(schedule_run["run_date"])
        payload_date = _optional_date(payload.get("as_of"))
        if payload_date != scheduled_date:
            return {
                "status": "skipped",
                "reason": "formal registration date must match the immutable schedule run date",
            }
        registration_deadline = _local_schedule_deadline(
            settings,
            as_of=scheduled_date,
            value=payload.get("strict_registration_deadline_local"),
        )
        if registration_deadline is not None and _utc_now() >= registration_deadline:
            return {
                "status": "skipped",
                "reason": "strict_forward_registration_window_closed",
                "registration_deadline_at": registration_deadline.isoformat(),
                "claim_boundary": (
                    "The original strict registration run did not begin before its "
                    "configured cutoff. It cannot be converted into a same-day recovery."
                ),
            }
        context.progress(0.15, "building the primary point-in-time candidate set")
        return register_primary_forward_samples(
            settings,
            trade_date=scheduled_date,
            activation_origin="scheduler",
            activation_reference={
                "job_id": context.job["job_id"],
                "schedule_run_id": schedule_run["schedule_run_id"],
                "schedule_name": schedule_run["schedule_name"],
                "run_date": schedule_run["run_date"],
                "attempt_number": schedule_run.get("attempt_number", 1),
                "recovery_of_schedule_run_id": schedule_run.get(
                    "recovery_of_schedule_run_id"
                ),
                "recovery_reason": schedule_run.get("recovery_reason"),
            },
        )

    def shadow_account_cycle(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.workflows.shadow_trading import (
            execute_pending_shadow_orders,
            mark_shadow_accounts,
        )

        resolved = _optional_date(payload.get("as_of")) or date.today()
        context.progress(0.20, "executing eligible independent shadow orders")
        execution = execute_pending_shadow_orders(settings, as_of=resolved)
        context.progress(0.65, "marking seven independent shadow accounts")
        marking = mark_shadow_accounts(settings, as_of=resolved)
        return {"execution": execution, "marking": marking}

    def wide_forward_registration(
        context: JobContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        from quantlab.workflows.wide_forward import (
            WIDE_PROTOCOL_VERSION,
            register_wide_forward_batch,
        )

        schedule_run = context.repository.schedule_run_for_job(context.job["job_id"])
        if schedule_run is None:
            return {
                "status": "skipped",
                "reason": "wide forward registration requires a scheduler-owned job",
            }
        if (
            schedule_run["schedule_name"] != "wide_forward_registration"
            or schedule_run["schedule_job_type"] != "wide_forward_registration"
        ):
            return {
                "status": "skipped",
                "reason": "job is not linked to the wide forward schedule",
            }
        if payload.get("is_backfill") or schedule_run["is_backfill"]:
            return {
                "status": "skipped",
                "reason": "backfills cannot create wide forward evidence",
            }
        scheduled_date = date.fromisoformat(schedule_run["run_date"])
        payload_date = _optional_date(payload.get("as_of"))
        if payload_date != scheduled_date:
            return {
                "status": "skipped",
                "reason": "wide registration date must match the immutable schedule run date",
            }
        registration_deadline = _local_schedule_deadline(
            settings,
            as_of=scheduled_date,
            value=payload.get("strict_registration_deadline_local"),
        )
        if registration_deadline is not None and _utc_now() >= registration_deadline:
            return {
                "status": "skipped",
                "reason": "strict_wide_registration_window_closed",
                "registration_deadline_at": registration_deadline.isoformat(),
                "claim_boundary": (
                    "The original strict wide registration did not begin before its "
                    "configured cutoff. Any later work must use the separate late-start "
                    "research boundary."
                ),
            }
        from quantlab.persistence.wide_research import WideResearchRepository

        experiment = WideResearchRepository(
            settings.resolve(settings.get("system.database_path"))
        ).experiment_by_protocol_version(WIDE_PROTOCOL_VERSION)
        if experiment is not None and scheduled_date < date.fromisoformat(
            experiment["signal_start_date"]
        ):
            return {
                "status": "skipped",
                "reason": "wide forward protocol starts on a later trading day",
                "signal_start_date": experiment["signal_start_date"],
            }
        if payload.get("require_forward_readiness"):
            from quantlab.runtime.readiness import primary_start_readiness

            readiness = primary_start_readiness(
                settings,
                trade_date=scheduled_date,
                require_runtime=False,
            )
            eligible_members = int(
                readiness["data"]["point_in_time_pool"].get("eligible_members", 0)
            )
            target_sample_size = int(
                settings.get("strategies.wide_forward.target_sample_size", 24)
            )
            blockers = list(readiness["blockers"])
            if eligible_members < target_sample_size:
                blockers.append("point_in_time_pool_has_fewer_than_wide_sample_target")
            if blockers or not readiness["sample_registration_allowed"]:
                return {
                    "status": "skipped",
                    "reason": "wide_forward_readiness_failed",
                    "blockers": list(dict.fromkeys(blockers)),
                    "eligible_members": eligible_members,
                    "target_sample_size": target_sample_size,
                    "quality_gate": readiness["quality_gate"],
                    "claim_boundary": (
                        "A strict wide-forward batch requires the exact-date trusted "
                        "pool and current readiness. A red gate never becomes a later "
                        "same-day strict recovery."
                    ),
                }
        try:
            return register_wide_forward_batch(
                settings,
                trade_date=scheduled_date,
                schedule_run_id=schedule_run["schedule_run_id"],
                progress_callback=lambda value, message: context.progress(value, message),
            )
        except ValueError as exc:
            selection_failures = (
                "trusted point-in-time universe is smaller",
                "exact-date trusted production point-in-time pool is unavailable",
                "wide forward snapshot violates point-in-time field identity",
                "diversity constraints left fewer",
                "wide sample cannot represent",
                "wide sample does not meet",
            )
            if payload.get("require_forward_readiness") and str(exc).startswith(
                selection_failures
            ):
                return {
                    "status": "skipped",
                    "reason": "wide_forward_selection_not_ready",
                    "detail": str(exc),
                    "claim_boundary": (
                        "Selection validation failed before a wide batch was created. "
                        "The strict run remains a skipped observation."
                    ),
                }
            raise

    def wide_research_portfolio_mark(
        context: JobContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        from quantlab.workflows.wide_forward import mark_wide_research_portfolios

        schedule_run = context.repository.schedule_run_for_job(context.job["job_id"])
        if schedule_run is None or schedule_run["schedule_name"] != "wide_research_portfolio_mark":
            return {
                "status": "skipped",
                "reason": "wide research NAV marking requires the scheduler-owned job",
            }
        context.progress(0.20, "marking matured fractional research portfolios")
        return mark_wide_research_portfolios(settings)

    def investor_mark_to_market(
        context: JobContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        from quantlab.workflows.investor_portfolio import mark_investor_portfolios

        context.progress(0.20, "marking read-only investor portfolios")
        return mark_investor_portfolios(
            settings,
            as_of=_optional_date(payload.get("as_of")) or date.today(),
            portfolio_id=payload.get("portfolio_id"),
        )

    def investor_outcome_settlement(
        context: JobContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        from quantlab.workflows.investor_portfolio import (
            settle_investor_recommendation_outcomes,
        )

        context.progress(0.20, "settling due investor recommendation outcomes")
        return settle_investor_recommendation_outcomes(
            settings,
            as_of=_optional_date(payload.get("as_of")) or date.today(),
        )

    def mark_to_market(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.workflows.simulator import (
            mark_user_paper_account,
            user_simulator_repository,
        )

        repository = user_simulator_repository(settings)
        accounts = repository.accounts(include_closed=False)
        results = []
        failures = []
        snapshot_date = _optional_date(payload.get("as_of")) or date.today()
        for index, account in enumerate(accounts, start=1):
            context.progress(
                0.05 + 0.90 * index / max(1, len(accounts)),
                f"marking account {account['account_id']}",
            )
            try:
                results.append(mark_user_paper_account(
                    settings,
                    account_id=account["account_id"],
                    snapshot_date=snapshot_date,
                ))
            except Exception as exc:
                failures.append(
                    {
                        "account_id": account["account_id"],
                        "status": "failed",
                        "error_type": type(exc).__name__,
                    }
                )
        return {
            "snapshot_date": snapshot_date.isoformat(),
            "accounts": results,
            "failures": failures,
        }

    def a_share_v4_research(
        context: JobContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        from quantlab.workflows.stock_strategy_lab_v4 import run_a_share_strategy_lab_v4

        context.progress(0.10, "validating point-in-time A-share V4 episodes")
        options = payload.get("options", {})
        return run_a_share_strategy_lab_v4(
            settings,
            episodes=payload["episodes"],
            source=payload["source"],
            source_version=payload["source_version"],
            bootstrap_simulations=options.get("bootstrap_simulations"),
            save=bool(payload.get("save", True)),
        )

    def convertible_bond_research(
        context: JobContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        from quantlab.workflows.convertible_bond_evidence import (
            run_convertible_bond_point_in_time_evidence,
        )

        context.progress(0.10, "validating point-in-time convertible-bond episodes")
        options = payload.get("options", {})
        return run_convertible_bond_point_in_time_evidence(
            settings,
            episodes=payload["episodes"],
            source_version=payload["source_version"],
            selection_count=int(options.get("selection_count", 10)),
            save=bool(payload.get("save", True)),
        )

    def etf_pit_replay(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.workflows.etf_point_in_time import run_point_in_time_etf_replay

        context.progress(0.10, "validating versioned ETF pool snapshots")
        options = payload.get("options", {})
        return run_point_in_time_etf_replay(
            settings,
            episodes=payload["episodes"],
            top_k=int(options.get("top_k", 3)),
            total_exposure=float(options.get("total_exposure", 0.80)),
            save=bool(payload.get("save", True)),
        )

    def retention_cleanup(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.persistence.chat import ChatRepository
        from quantlab.runtime.notification_delivery import NotificationDeliveryWorker

        context.progress(0.25, "purging expired Chat records")
        chat = ChatRepository(settings.resolve(settings.get("system.database_path"))).purge_expired()
        context.progress(0.65, "purging expired notification delivery records")
        notification = NotificationDeliveryWorker(
            settings, worker_id=context.worker_id
        ).cleanup(
            message_retention_days=int(
                settings.get("runtime.notification_retention_days", 365)
            )
        )
        runtime = context.repository.purge_runtime_history(
            audit_retention_days=int(settings.get("runtime.audit_retention_days", 365)),
            job_retention_days=int(settings.get("runtime.job_retention_days", 365)),
        )
        return {"chat": chat, "notification": notification, "runtime": runtime}

    def database_backup(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.runtime.operations import backup_database

        context.progress(0.20, "creating SQLite online backup")
        return backup_database(
            settings,
            label=str(payload.get("schedule_name") or payload.get("label") or "scheduled"),
        )

    def thesis_due_scan_job(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.workflows.decision_lifecycle import thesis_due_scan

        context.progress(0.20, "scanning investment theses due for review")
        context.check_cancelled()
        return thesis_due_scan(settings, as_of=_optional_date(payload.get("as_of")))

    def thesis_event_check_job(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.workflows.decision_lifecycle import thesis_event_check

        context.progress(0.20, "checking thesis event evidence")
        context.check_cancelled()
        return thesis_event_check(settings, as_of=_optional_date(payload.get("as_of")))

    def thesis_price_check_job(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        from quantlab.workflows.decision_lifecycle import thesis_price_invalidation_check

        context.progress(0.20, "checking thesis price invalidation evidence")
        context.check_cancelled()
        return thesis_price_invalidation_check(
            settings, as_of=_optional_date(payload.get("as_of"))
        )

    def authoritative_reflection_job(
        context: JobContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        from quantlab.workflows.decision_lifecycle import (
            authoritative_reflection_settlement,
        )

        context.progress(0.20, "creating reflections from authoritative matured outcomes")
        context.check_cancelled()
        return authoritative_reflection_settlement(
            settings, limit=int(payload.get("limit", 500))
        )

    def controlled_memory_refresh_job(
        context: JobContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        del payload
        from quantlab.workflows.decision_lifecycle import controlled_memory_refresh

        context.progress(0.20, "refreshing challenge eligibility for controlled memory")
        context.check_cancelled()
        return controlled_memory_refresh(settings)

    def decision_task_refresh_job(
        context: JobContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        del payload
        from quantlab.workflows.decision_tasks import refresh_decision_tasks

        context.progress(0.20, "refreshing user-facing decision tasks")
        context.check_cancelled()
        return refresh_decision_tasks(settings)

    handlers: dict[str, JobHandler] = {
        "research": research,
        "historical_replay": historical_replay,
        "capital_flow_refresh": capital_flow_refresh,
        "forward_preflight": forward_preflight,
        "trusted_data_refresh": trusted_data_refresh,
        "training": training,
        "simulator_settlement": simulator_settlement,
        "daily_cycle": daily_cycle,
        "notification_dispatch": notification_dispatch,
        "premarket_digest": premarket_digest,
        "account_daily_report": account_daily_report,
        "forward_settlement_scan": forward_settlement_scan,
        "forward_sample_registration": forward_sample_registration,
        "wide_forward_registration": wide_forward_registration,
        "wide_research_portfolio_mark": wide_research_portfolio_mark,
        "shadow_account_cycle": shadow_account_cycle,
        "investor_mark_to_market": investor_mark_to_market,
        "investor_outcome_settlement": investor_outcome_settlement,
        "mark_to_market": mark_to_market,
        "a_share_v4_research": a_share_v4_research,
        "convertible_bond_research": convertible_bond_research,
        "etf_pit_replay": etf_pit_replay,
        "retention_cleanup": retention_cleanup,
        "database_backup": database_backup,
        "thesis_due_scan": thesis_due_scan_job,
        "thesis_event_check": thesis_event_check_job,
        "thesis_price_invalidation_check": thesis_price_check_job,
        "authoritative_reflection_settlement": authoritative_reflection_job,
        "controlled_memory_refresh": controlled_memory_refresh_job,
        "decision_task_refresh": decision_task_refresh_job,
    }
    try:
        from quantlab.workflows.chat_jobs import execute_chat_job

        handlers["chat_request"] = lambda context, payload: execute_chat_job(
            settings, context, payload
        )
    except ImportError:
        pass
    try:
        from quantlab.workflows.roundtable_jobs import execute_roundtable_job

        handlers["roundtable_request"] = lambda context, payload: execute_roundtable_job(
            settings, context, payload
        )
    except ImportError:
        pass
    return handlers


def _emit_job_notification(settings: Settings, job: dict[str, Any]) -> None:
    if job["status"] not in {JobStatus.COMPLETED.value, JobStatus.FAILED.value}:
        return
    payload = job.get("payload") or {}
    event_type = (
        "chat_job_completed"
        if job["job_type"] == "chat_request" and job["status"] == "completed"
        else "chat_job_failed"
        if job["job_type"] == "chat_request"
        else "background_job_completed"
        if job["status"] == "completed"
        else "background_job_failed"
    )
    NotificationRepository(
        settings.resolve(settings.get("system.database_path"))
    ).emit(
        event_type=event_type,
        aggregate_type="background_job",
        aggregate_id=job["job_id"],
        payload={
            "account_id": payload.get("account_id"),
            "symbol": payload.get("symbol"),
            "task_id": job["job_id"],
            "severity": "info" if job["status"] == "completed" else "warning",
            "content": (
                f"Background task {job['job_type']} completed."
                if job["status"] == "completed"
                else f"Background task {job['job_type']} failed: {job.get('error_detail') or ''}"
            ),
            "action_type": "query_job",
            "action_payload": {"job_id": job["job_id"]},
        },
        dedup_key=f"{event_type}:{job['job_id']}",
    )


def _mark_chat_message_failed(
    settings: Settings, job: dict[str, Any], error: Exception, *, status: str
) -> None:
    if job.get("job_type") != "chat_request":
        return
    message_id = (job.get("payload") or {}).get("user_message_id")
    if not message_id:
        return
    from quantlab.persistence.chat import ChatRepository
    from quantlab.security import safe_error_detail

    ChatRepository(settings.resolve(settings.get("system.database_path"))).update_message_status(
        message_id,
        status=status,
        degraded_reason=safe_error_detail(error),
    )


def _required_date(value: Any) -> date:
    result = _optional_date(value)
    if result is None:
        raise ValueError("date is required")
    return result


def _optional_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value)[:10])


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _local_schedule_deadline(
    settings: Settings,
    *,
    as_of: date,
    value: Any,
) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    try:
        local_time = datetime.strptime(str(value), "%H:%M").time()
    except ValueError as exc:
        raise ValueError("scheduled deadline must use HH:MM local time") from exc
    timezone = ZoneInfo(str(settings.get("system.timezone", "Asia/Shanghai")))
    return datetime.combine(as_of, local_time, tzinfo=timezone).astimezone(UTC)


__all__ = [
    "DEFAULT_REPLAY_SAFE_JOB_TYPES",
    "JobContext",
    "JobHandler",
    "JobWorker",
    "default_job_handlers",
]
