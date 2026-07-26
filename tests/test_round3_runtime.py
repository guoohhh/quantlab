from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest

from quantlab.persistence.chat import ChatRepository
from quantlab.persistence.jobs import JobRepository
from quantlab.persistence.notifications import NotificationRepository
from quantlab.runtime.notification_delivery import (
    MemoryChannelAdapter,
    NotificationDeliveryWorker,
    _quiet_until,
)
from quantlab.runtime.scheduler import RuntimeScheduler
from quantlab.runtime.scheduler import DEFAULT_SCHEDULES
from quantlab.market import TradingCalendarService
from quantlab.runtime.worker import JobWorker, default_job_handlers
from quantlab.runtime.operations import backup_database, restore_database
from quantlab.runtime.summaries import generate_account_daily_report, generate_premarket_digest
from quantlab.workflows.chat_jobs import submit_chat_job


def test_job_submit_claim_dependency_progress_and_idempotency(tmp_path):
    repository = JobRepository(tmp_path / "jobs.db")
    first = repository.submit(
        job_type="first",
        payload={"value": 1},
        idempotency_key="first-task",
    )
    duplicate = repository.submit(
        job_type="first",
        payload={"value": 999},
        idempotency_key="first-task",
    )
    assert duplicate["job_id"] == first["job_id"]
    second = repository.submit(
        job_type="second",
        payload={},
        idempotency_key="second-task",
        dependency_job_ids=[first["job_id"]],
    )
    claimed = repository.claim(worker_id="w1")
    assert claimed["job_id"] == first["job_id"]
    repository.heartbeat(first["job_id"], "w1", progress=0.5, message="half")
    assert repository.claim(worker_id="w2") is None
    repository.complete(first["job_id"], "w1", result={"ok": True})
    claimed_second = repository.claim(worker_id="w2")
    assert claimed_second["job_id"] == second["job_id"]
    requested = repository.cancel(second["job_id"])
    assert requested["status"] == "running"
    assert requested["cancel_requested"] is True
    assert repository.acknowledge_cancel(
        second["job_id"], "w2", reason="test worker stopped"
    )["status"] == "cancelled"
    events = repository.events(first["job_id"])
    assert {item["event_type"] for item in events} >= {"submitted", "claimed", "progress", "completed"}


def test_job_crash_recovery_retry_cost_and_concurrent_merge(tmp_path):
    repository = JobRepository(tmp_path / "jobs.db")
    job = repository.submit(
        job_type="costly",
        payload={},
        idempotency_key="costly-task",
        timeout_seconds=1,
        max_attempts=2,
        cost_budget_usd=1.0,
        available_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    repository.claim(worker_id="dead-worker", now=datetime(2026, 1, 1, tzinfo=UTC))
    recovery = repository.recover_stale(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=2))
    assert recovery["recovered"] == 1
    claimed = repository.claim(
        worker_id="new-worker", now=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=2)
    )
    assert claimed["job_id"] == job["job_id"]
    assert repository.consume_cost(job["job_id"], "new-worker", 0.8)
    assert not repository.consume_cost(job["job_id"], "new-worker", 0.3)

    def submit_same(_index: int) -> str:
        return repository.submit(
            job_type="merged",
            payload={},
            idempotency_key="same-key-concurrent",
        )["job_id"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(submit_same, range(30)))
    assert len(set(ids)) == 1


def test_worker_executes_custom_handler_and_scheduler_is_idempotent(settings):
    worker = JobWorker(
        settings,
        worker_id="test-worker",
        handlers={"echo": lambda context, payload: {"echo": payload["value"]}},
    )
    job = worker.repository.submit(
        job_type="echo", payload={"value": 7}, idempotency_key="echo-once"
    )
    completed = worker.run_once()
    assert completed["job_id"] == job["job_id"]
    assert completed["result_payload"] == {"echo": 7}

    scheduler = RuntimeScheduler(settings)
    saturday = date(2026, 7, 18)
    closed = scheduler.backfill(saturday)
    assert {item["job_type"] for item in closed["jobs"]} == {"trusted_data_refresh"}
    assert all(
        item["job_type"] != "forward_sample_registration" for item in closed["jobs"]
    )
    monday = date(2026, 7, 20)
    calendar = TradingCalendarService.from_settings(settings)
    calendar.ingest(
        [
            {"trade_date": saturday.isoformat(), "is_open": False},
            {"trade_date": monday.isoformat(), "is_open": True},
        ],
        namespace="production",
        trust_level="server_observed",
        provider="fixture",
        source="fixture",
        endpoint="fixture",
        source_version="v1",
        available_at=datetime(2026, 7, 17, tzinfo=UTC),
        license_status="fixture",
        raw_fingerprint="fixture-calendar",
    )
    first = scheduler.backfill(monday)
    second = scheduler.backfill(monday)
    assert [item["job_id"] for item in first["jobs"]] == [
        item["job_id"] for item in second["jobs"]
    ]
    assert len(first["jobs"]) < len(DEFAULT_SCHEDULES)
    assert any(
        item["name"] == "forward_sample_registration"
        and item["reason"] == "primary_forward_backfill_is_not_formal_evidence"
        for item in first["skipped"]
    )
    assert all(
        item["job_type"] != "forward_sample_registration" for item in first["jobs"]
    )


def test_scheduler_defaults_reconcile_obsolete_persisted_dependencies(settings):
    scheduler = RuntimeScheduler(settings)
    original = scheduler.repository.register_schedule(
        name="wide_research_portfolio_mark",
        job_type="wide_research_portfolio_mark",
        local_time="16:05",
        dependency_names=["wide_forward_registration", "prediction_settlement"],
    )

    scheduler.register_defaults()

    reconciled = next(
        item
        for item in scheduler.repository.schedules()
        if item["name"] == "wide_research_portfolio_mark"
    )
    assert reconciled["schedule_id"] == original["schedule_id"]
    assert reconciled["dependency_names"] == ["prediction_settlement"]
    trusted_refresh = next(
        item
        for item in scheduler.repository.schedules()
        if item["name"] == "trusted_data_refresh"
    )
    assert trusted_refresh["payload"]["require_forward_readiness"] is True
    assert trusted_refresh["payload"]["forward_readiness_retry_deadline_local"] == "15:40"
    wide_registration = next(
        item
        for item in scheduler.repository.schedules()
        if item["name"] == "wide_forward_registration"
    )
    assert wide_registration["payload"]["require_forward_readiness"] is True
    assert wide_registration["payload"]["strict_registration_deadline_local"] == "16:00"


def test_scheduled_data_refresh_uses_bounded_readiness_retries(settings, monkeypatch):
    handlers = default_job_handlers(settings)
    refreshes = []
    monkeypatch.setattr(
        "quantlab.workflows.trusted_data.refresh_trusted_data",
        lambda *_args, **_kwargs: refreshes.append(True) or {"sources": {}},
    )
    monkeypatch.setattr(
        "quantlab.runtime.readiness.primary_start_readiness",
        lambda *_args, **_kwargs: {
            "sample_registration_allowed": False,
            "checked_at": "2026-07-22T07:10:00+00:00",
            "blockers": ["trusted_production_industry_membership_not_ready"],
            "data": {
                "calendar_day": {"is_open": True},
                "point_in_time_pool": {"eligible_members": 0},
            },
        },
    )

    class Context:
        def __init__(self, attempts):
            self.job = {"attempts": attempts, "max_attempts": 3}

        def progress(self, *_args, **_kwargs):
            return None

    payload = {"as_of": "2026-07-22", "require_forward_readiness": True}
    monkeypatch.setattr(
        "quantlab.runtime.worker._utc_now",
        lambda: datetime(2026, 7, 22, 7, 39, 59, tzinfo=UTC),
    )
    payload["forward_readiness_retry_deadline_local"] = "15:40"
    with pytest.raises(RuntimeError, match="trusted data readiness pending"):
        handlers["trusted_data_refresh"](Context(attempts=1), payload)

    monkeypatch.setattr(
        "quantlab.runtime.worker._utc_now",
        lambda: datetime(2026, 7, 22, 7, 40, tzinfo=UTC),
    )
    exhausted = handlers["trusted_data_refresh"](Context(attempts=1), payload)
    assert exhausted["status"] == "degraded"
    assert exhausted["forward_readiness"]["retry_window_closed"] is True
    assert exhausted["forward_readiness"]["attempts"] == 1
    assert len(refreshes) == 2


def test_data_refresh_worker_retries_until_forward_readiness_is_green(settings, monkeypatch):
    readiness_states = iter(
        [
            {
                "sample_registration_allowed": False,
                "checked_at": "2026-07-22T07:10:00+00:00",
                "blockers": ["point_in_time_pool_field_coverage_below_minimum"],
                "data": {
                    "calendar_day": {"is_open": True},
                    "point_in_time_pool": {"eligible_members": 0},
                },
            },
            {
                "sample_registration_allowed": True,
                "checked_at": "2026-07-22T07:10:10+00:00",
                "blockers": [],
                "data": {
                    "calendar_day": {"is_open": True},
                    "point_in_time_pool": {"eligible_members": 24},
                },
            },
        ]
    )
    monkeypatch.setattr(
        "quantlab.workflows.trusted_data.refresh_trusted_data",
        lambda *_args, **_kwargs: {"sources": {}},
    )
    monkeypatch.setattr(
        "quantlab.runtime.readiness.primary_start_readiness",
        lambda *_args, **_kwargs: next(readiness_states),
    )
    monkeypatch.setattr(
        "quantlab.runtime.worker._utc_now",
        lambda: datetime(2026, 7, 22, 7, 20, tzinfo=UTC),
    )
    worker = JobWorker(
        settings,
        worker_id="refresh-worker",
        handlers=default_job_handlers(settings),
        replay_safe_job_types={"trusted_data_refresh"},
    )
    job = worker.repository.submit(
        job_type="trusted_data_refresh",
        payload={
            "as_of": "2026-07-22",
            "require_forward_readiness": True,
            "forward_readiness_retry_deadline_local": "15:40",
        },
        idempotency_key="refresh-retry-gate",
        max_attempts=3,
    )

    first = worker.run_once()
    assert first["status"] == "queued"
    with worker.repository.connect() as db:
        db.execute(
            "UPDATE background_jobs SET available_at=? WHERE job_id=?",
            (datetime.now(UTC).isoformat(), job["job_id"]),
        )
    second = worker.run_once()
    assert second["status"] == "completed"
    assert second["attempts"] == 2
    assert second["result_payload"]["forward_readiness"]["ready"] is True


def test_strict_registration_skips_after_configured_deadline(settings, monkeypatch):
    handlers = default_job_handlers(settings)
    monkeypatch.setattr(
        "quantlab.runtime.worker._utc_now",
        lambda: datetime(2026, 7, 22, 8, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "quantlab.workflows.forward_experiment.register_primary_forward_samples",
        lambda *_args, **_kwargs: pytest.fail("late strict registration must not run"),
    )

    class Repository:
        def schedule_run_for_job(self, _job_id):
            return {
                "schedule_name": "forward_sample_registration",
                "schedule_job_type": "forward_sample_registration",
                "is_backfill": False,
                "run_date": "2026-07-22",
                "schedule_run_id": "schedule-primary",
            }

    class Context:
        job = {"job_id": "job-primary"}
        repository = Repository()

        def progress(self, *_args, **_kwargs):
            return None

    result = handlers["forward_sample_registration"](
        Context(),
        {"as_of": "2026-07-22", "strict_registration_deadline_local": "16:00"},
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "strict_forward_registration_window_closed"


def test_chat_jobs_are_deduplicated_and_worker_persists_one_reply(settings):
    chat = ChatRepository(settings.resolve(settings.get("system.database_path")))
    conversation = chat.create_conversation(
        title="async",
        idempotency_key="conversation-async",
    )
    first = submit_chat_job(
        settings,
        conversation_id=conversation["conversation_id"],
        content="Please list the available bounded tools.",
        idempotency_key="message-once",
    )
    second = submit_chat_job(
        settings,
        conversation_id=conversation["conversation_id"],
        content="Please list the available bounded tools.",
        idempotency_key="message-once",
    )
    assert first["job_id"] == second["job_id"]
    worker = JobWorker(settings, worker_id="chat-worker")
    result = worker.run_once()
    assert result["status"] == "completed"
    messages = chat.messages_for_job(first["job_id"])
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert worker.run_once() is None


def test_notification_worker_retries_and_external_channels_are_not_placeholder_success(settings):
    path = settings.resolve(settings.get("system.database_path"))
    notification_repository = NotificationRepository(path)
    memory = MemoryChannelAdapter(channel="email", delivered=[], fail_count=1)
    worker = NotificationDeliveryWorker(
        settings,
        worker_id="notification-worker",
        adapters={"email": memory},
    )
    worker.configure_channel(
        channel="email",
        enabled=True,
        config={
            "smtp_host": "not-used-by-memory",
            "from_address": "from@example.com",
            "to_address": "to@example.com",
        },
    )
    notification_repository.emit(
        event_type="background_job_completed",
        aggregate_type="test",
        aggregate_id="1",
        payload={"content": "done"},
        dedup_key="delivery-test",
        defer=True,
    )
    first = worker.run_once()
    assert first["external_failed"] == 1
    with worker.connect() as db:
        db.execute(
            "UPDATE notification_channel_outbox SET available_at=? WHERE status='pending'",
            (datetime.now(UTC).isoformat(),),
        )
    second = worker.run_once()
    assert second["external_delivered"] == 1
    assert len(memory.delivered) == 1

    now = datetime(2026, 7, 17, 23, 0, tzinfo=UTC)
    quiet = _quiet_until(
        now,
        {
            "timezone": "UTC",
            "quiet_start": "22:00",
            "quiet_end": "07:00",
        },
    )
    assert quiet == datetime(2026, 7, 18, 7, 0, tzinfo=UTC)


def test_database_backup_restore_and_schema_migration(settings):
    repository = JobRepository(settings.resolve(settings.get("system.database_path")))
    repository.submit(job_type="echo", payload={"before": True}, idempotency_key="before-backup")
    backup = backup_database(settings, label="test")
    repository.submit(job_type="echo", payload={"after": True}, idempotency_key="after-backup")
    restored = restore_database(
        settings,
        backup_path=backup["database"],
        expected_sha256=backup["sha256"],
        confirm=True,
        maintenance_mode=True,
    )
    assert restored["restored"] is True
    reopened = JobRepository(settings.resolve(settings.get("system.database_path")))
    assert reopened.schema_status()["current_version"] == 6
    assert reopened.jobs(job_type="echo", limit=10)[0]["idempotency_key"] == "before-backup"


def test_default_operational_jobs_and_structured_summaries(settings):
    premarket = generate_premarket_digest(
        settings, report_date=date(2026, 7, 17)
    )
    report = generate_account_daily_report(
        settings, report_date=date(2026, 7, 17)
    )
    assert premarket["report_type"] == "premarket_digest"
    assert report["reports"] == []

    worker = JobWorker(settings, worker_id="operations-worker")
    job_types = [
        "forward_settlement_scan",
        "mark_to_market",
        "premarket_digest",
        "account_daily_report",
        "notification_dispatch",
        "retention_cleanup",
        "database_backup",
    ]
    for job_type in job_types:
        worker.repository.submit(
            job_type=job_type,
            payload={"as_of": "2026-07-17"},
            idempotency_key=f"operations:{job_type}",
        )
    results = worker.run_until_empty(20)
    assert len(results) == len(job_types)
    assert all(item["status"] == "completed" for item in results)


def test_worker_failure_and_budget_paths(settings):
    failing = JobWorker(
        settings,
        worker_id="failure-worker",
        handlers={"explode": lambda _context, _payload: 1 / 0},
    )
    failing.repository.submit(
        job_type="explode",
        payload={},
        idempotency_key="explode-once",
        max_attempts=1,
    )
    assert failing.run_once()["status"] == "failed"

    def costly(context, _payload):
        context.consume_cost(2.0)
        return {}

    budget = JobWorker(
        settings,
        worker_id="budget-worker",
        handlers={"costly": costly},
    )
    budget.repository.submit(
        job_type="costly",
        payload={},
        idempotency_key="budget-once",
        max_attempts=1,
        cost_budget_usd=1.0,
    )
    assert budget.run_once()["status"] == "failed"
