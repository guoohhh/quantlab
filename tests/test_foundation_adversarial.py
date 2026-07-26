from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from quantlab.api.app import app
from quantlab.config import Settings
from quantlab.domain import (
    AnalysisContextPack,
    AssetType,
    EvidenceBlock,
    EvidenceDomain,
    EvidenceQuality,
    MarketQuote,
)
from quantlab.domain.data_governance import DataTrustLevel
from quantlab.market import ExecutionQuoteService
from quantlab.persistence.jobs import JobRepository
from quantlab.persistence.round5 import Round5Repository
from quantlab.runtime.scheduler import DEFAULT_SCHEDULES, RuntimeScheduler
from quantlab.runtime.worker import JobWorker
from quantlab.workflows.forward_ablation import (
    _execute_variants,
    freeze_forward_ablation_sample,
    run_raw_forward_llm,
)
from quantlab.workflows.forward_experiment import ensure_primary_forward_experiment
from quantlab.workflows.llm_committee import run_context_committee
from quantlab.workflows.simulator import (
    create_user_paper_account,
    mark_user_paper_account,
    user_simulator_repository,
)


api_module = importlib.import_module("quantlab.api.app")


def _settings(tmp_path) -> Settings:
    return Settings(
        values={
            "system": {
                "database_path": "quantlab.db",
                "data_dir": "data",
                "test_mode": True,
                "timezone": "Asia/Shanghai",
            },
            "llm": {
                "provider": "mock",
                "allow_mock_fallback": True,
                "maximum_committee_roles": 2,
                "maximum_committee_rounds": 1,
                "task_cost_budget_usd": 1.0,
            },
            "risk": {"max_single_position": 0.15},
            "strategies": {
                "forward_primary": {"simple_baseline_weight": 0.05},
                "etf_rotation": {"universe": []},
            },
            "runtime": {"api_requests_per_minute": 300},
        },
        root=tmp_path,
    )


def _request(
    method: str,
    path: str,
    *,
    client: tuple[str, int] = ("127.0.0.1", 12345),
    headers: dict[str, str] | None = None,
):
    async def request():
        transport = httpx.ASGITransport(app=app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as session:
            return await session.request(method, path, headers=headers)

    return asyncio.run(request())


def _pack() -> AnalysisContextPack:
    observed = datetime(2026, 7, 17, 7, 30, tzinfo=UTC)
    blocks = [
        EvidenceBlock(
            domain=domain,
            title=domain.value,
            source="adversarial_fixture",
            methodology="deterministic",
            as_of=observed,
            available_at=observed,
            fetched_at=observed,
            freshness="fresh",
            quality=EvidenceQuality.AVAILABLE,
            payload={"momentum_score": 5.0, "return_20_pct": 5.0},
        )
        for domain in (
            EvidenceDomain.MARKET,
            EvidenceDomain.TECHNICAL,
            EvidenceDomain.CAPITAL_FLOW,
            EvidenceDomain.STRATEGY,
        )
    ]
    return AnalysisContextPack(
        symbol="sh600001",
        asset_type=AssetType.STOCK,
        as_of=date(2026, 7, 17),
        cutoff_at=observed,
        blocks=blocks,
        deterministic_summary={"market_regime": "range"},
    )


class _PreviousDayQuoteProvider:
    provider_name = "previous_day_fixture"
    provider_version = "v1"
    authoritative = True

    def quote(self, symbol: str, *, asset_type: AssetType, as_of: date) -> MarketQuote:
        previous = as_of - timedelta(days=1)
        return MarketQuote(
            symbol=symbol,
            asset_type=asset_type,
            raw_price=10.0,
            as_of=previous,
            available_at=datetime.now(UTC) - timedelta(minutes=5),
            source=self.provider_name,
            provider=self.provider_name,
            source_version=self.provider_version,
            trust_level=DataTrustLevel.SERVER_OBSERVED,
            license_status="fixture",
            endpoint="fixture",
            session_status="closed",
        )


class _UnavailableQuoteService:
    def get(self, *_args, **_kwargs):
        raise ValueError("quote unavailable")


class _FixedQuoteService:
    def __init__(self, as_of: date, stock_price: float):
        self.as_of = as_of
        self.stock_price = stock_price

    def get(self, symbol: str, *, asset_type, **_kwargs) -> MarketQuote:
        return MarketQuote(
            symbol=symbol,
            asset_type=asset_type,
            raw_price=self.stock_price if symbol == "sh600001" else 3000.0,
            as_of=self.as_of,
            available_at=datetime.combine(self.as_of, datetime.min.time(), tzinfo=UTC),
            source="fixed_quote_fixture",
            provider="fixed_quote_fixture",
            source_version="v1",
            session_status="closed",
            authoritative=False,
            evidence_stage="test",
            trust_level=DataTrustLevel.TEST,
        )


def test_shadow_account_get_is_read_only(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)

    response = _request("GET", "/api/shadow-accounts")

    assert response.status_code == 200
    assert response.json()["status"] == "not_started"
    assert response.json()["accounts"] == []
    assert Round5Repository(settings.resolve("quantlab.db")).primary_experiment() is None


def test_public_api_cannot_choose_when_primary_forward_experiment_starts(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)

    legacy = _request("POST", "/api/forward-ablation/cohorts")
    direct = _request("POST", "/api/forward-experiments/primary/ensure")

    assert legacy.status_code == 403
    assert direct.status_code == 403
    assert Round5Repository(settings.resolve("quantlab.db")).primary_experiment() is None


def test_production_primary_forward_activation_requires_scheduler_provenance(tmp_path):
    settings = _settings(tmp_path)
    settings.values["system"]["test_mode"] = False

    with pytest.raises(ValueError, match="only be activated"):
        ensure_primary_forward_experiment(settings)


def test_replaced_primary_protocol_cannot_be_resurrected(tmp_path):
    settings = _settings(tmp_path)
    first = ensure_primary_forward_experiment(settings)
    repository = Round5Repository(settings.resolve("quantlab.db"))
    repository.create_experiment(
        protocol_version="replacement-test-v1",
        cohort_id="replacement-cohort",
        frozen_payload={**first["frozen_payload"], "replacement_test": True},
        make_primary=True,
        reason="test replacement",
    )

    with pytest.raises(ValueError, match="cannot be promoted again"):
        repository.create_experiment(
            protocol_version=first["protocol_version"],
            cohort_id=first["cohort_id"],
            frozen_payload=first["frozen_payload"],
            make_primary=True,
        )


def test_engine_readiness_does_not_claim_production_admission(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEYS", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEYS", raising=False)

    response = _request("GET", "/api/engine/status")

    readiness = response.json()["readiness"]
    assert readiness["formal_forward_experiment"] == "not_started"
    assert readiness["formal_forward_samples"] == 0
    assert readiness["production_ready"] is False
    assert "fewer_than_30_matured_formal_samples" in readiness[
        "strategy_admission_blockers"
    ]


def test_forwarded_remote_client_is_not_treated_as_loopback(tmp_path, monkeypatch):
    monkeypatch.delenv("QUANTLAB_API_TOKEN", raising=False)
    monkeypatch.setattr(api_module, "_settings", lambda: _settings(tmp_path))

    response = _request(
        "GET",
        "/api/jobs",
        headers={"X-Forwarded-For": "203.0.113.7"},
    )

    assert response.status_code == 403


def test_internal_test_endpoint_requires_configured_api_token(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTLAB_API_TOKEN", "test-api-token")
    monkeypatch.setattr(api_module, "_settings", lambda: _settings(tmp_path))

    response = _request("POST", "/internal/test/quotes")

    assert response.status_code == 401


def test_maintenance_lock_blocks_api_database_access(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.delenv("QUANTLAB_API_TOKEN", raising=False)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)
    database = settings.resolve("quantlab.db")
    database.parent.mkdir(parents=True, exist_ok=True)
    database.with_suffix(database.suffix + ".maintenance.lock").write_text(
        "maintenance",
        encoding="utf-8",
    )

    blocked = _request("GET", "/api/jobs")
    health = _request("GET", "/api/health")

    assert blocked.status_code == 503
    assert health.status_code == 200
    assert health.json()["status"] == "maintenance"


def test_non_backfill_scheduler_cannot_run_a_selected_historical_date(tmp_path):
    scheduler = RuntimeScheduler(_settings(tmp_path))

    with pytest.raises(ValueError, match="current local date"):
        scheduler.tick(
            now=datetime(2026, 7, 18, 8, 0, tzinfo=UTC),
            run_date=date(2026, 7, 17),
            backfill=False,
        )


def test_schedule_owned_job_is_identifiable_before_backlink_is_written(tmp_path):
    repository = JobRepository(_settings(tmp_path).resolve("quantlab.db"))
    schedule = repository.register_schedule(
        name="forward_sample_registration",
        job_type="forward_sample_registration",
        local_time="15:45",
    )
    schedule_run = repository.create_schedule_run(
        schedule_id=schedule["schedule_id"],
        run_date=date(2026, 7, 17),
        is_backfill=False,
        payload={},
    )
    job = repository.submit(
        job_type="forward_sample_registration",
        payload={"as_of": "2026-07-17", "is_backfill": False},
        idempotency_key="schedule-link-race",
        schedule_run_id=schedule_run["schedule_run_id"],
    )

    resolved = repository.schedule_run_for_job(job["job_id"])

    assert resolved is not None
    assert resolved["schedule_name"] == "forward_sample_registration"
    assert resolved["run_date"] == "2026-07-17"


def test_formal_settlement_schedule_runs_after_due_time():
    schedules = {item["name"]: item["local_time"] for item in DEFAULT_SCHEDULES}

    assert schedules["prediction_settlement"] > "15:30"
    assert schedules["forward_sample_registration"] > schedules["prediction_settlement"]
    assert schedules["shadow_account_cycle"] > schedules["forward_sample_registration"]


def test_formal_registration_rejects_previous_day_quote(tmp_path):
    settings = _settings(tmp_path)
    requested = date(2026, 7, 18)
    pack = _pack().model_copy(
        update={"as_of": requested, "cutoff_at": datetime(2026, 7, 18, 7, 30, tzinfo=UTC)}
    )

    with pytest.raises(ValueError, match="exact signal-day quote"):
        freeze_forward_ablation_sample(
            settings,
            cohort_id="not-reached",
            symbol="sh600001",
            horizon_days=5,
            quote_service=ExecutionQuoteService(_PreviousDayQuoteProvider()),
            context_pack=pack,
            formal=True,
            as_of=requested,
        )


def test_mock_llm_is_incomplete_and_cannot_trigger_forward_trades(tmp_path):
    settings = _settings(tmp_path)
    pack = _pack()

    raw = run_raw_forward_llm(
        settings,
        pack=pack,
        horizon_days=5,
        idempotency_key="mock-forward-adversarial",
    )
    committee = run_context_committee(
        settings,
        pack=pack,
        deterministic_max_weight=0.15,
        idempotency_key="mock-committee-adversarial",
    )
    predictions = _execute_variants(
        settings,
        pack=pack,
        committee=committee,
        horizon_days=5,
        deterministic_max_weight=0.15,
        raw_llm=raw,
    )
    by_variant = {item.variant.value: item for item in predictions}

    assert raw["complete"] is False
    assert raw["provider"] == "mock"
    assert committee["action"] == "review_required"
    assert committee["llm_runtime"]["production_provider_available"] is False
    assert by_variant["raw_llm"].actually_triggered is False
    assert by_variant["llm_trade_gate"].actually_triggered is False
    assert by_variant["full_system"].actually_triggered is False
    assert by_variant["raw_llm"].role_completeness == 0.0
    assert by_variant["full_system"].role_completeness == 0.0


def test_cached_auto_router_mock_is_still_not_a_real_llm(tmp_path):
    settings = _settings(tmp_path)
    settings.values["llm"]["provider"] = "auto"
    pack = _pack()

    first = run_raw_forward_llm(
        settings,
        pack=pack,
        horizon_days=5,
        idempotency_key="cached-auto-mock",
    )
    second = run_raw_forward_llm(
        settings,
        pack=pack,
        horizon_days=5,
        idempotency_key="cached-auto-mock",
    )

    assert first["complete"] is False
    assert second["complete"] is False


def test_declared_replay_safe_handler_recovers_after_worker_crash(tmp_path):
    settings = _settings(tmp_path)
    repository = JobRepository(settings.resolve("quantlab.db"))
    job = repository.submit(
        job_type="safe",
        payload={},
        idempotency_key="safe-recovery",
        timeout_seconds=1,
        max_attempts=2,
        available_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    claimed = repository.claim(
        worker_id="crashed-worker",
        now=datetime.now(UTC) - timedelta(seconds=5),
    )
    assert claimed["job_id"] == job["job_id"]
    repository.mark_side_effect_state(job["job_id"], "crashed-worker", "started")
    assert repository.recover_stale()["recovered"] == 1
    calls: list[int] = []
    worker = JobWorker(
        settings,
        worker_id="replacement-worker",
        handlers={"safe": lambda _context, _payload: calls.append(1) or {"ok": True}},
        replay_safe_job_types={"safe"},
    )

    result = worker.run_once()

    assert result["status"] == "completed"
    assert result["result_payload"] == {"ok": True}
    assert calls == [1]


def test_worker_reuses_completed_side_effect_result_after_crash(tmp_path):
    settings = _settings(tmp_path)
    repository = JobRepository(settings.resolve("quantlab.db"))
    job = repository.submit(
        job_type="safe-completed",
        payload={},
        idempotency_key="completed-side-effect-recovery",
        timeout_seconds=1,
        max_attempts=2,
        available_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    claimed = repository.claim(
        worker_id="crashed-after-side-effect",
        now=datetime.now(UTC) - timedelta(seconds=5),
    )
    assert claimed["job_id"] == job["job_id"]
    repository.mark_side_effect_state(
        job["job_id"],
        "crashed-after-side-effect",
        "completed",
        result={"persisted": True},
    )
    assert repository.recover_stale()["recovered"] == 1
    calls: list[int] = []
    worker = JobWorker(
        settings,
        worker_id="recovery-worker",
        handlers={
            "safe-completed": lambda _context, _payload: calls.append(1) or {"wrong": True}
        },
    )

    result = worker.run_once()

    assert result["status"] == "completed"
    assert result["result_payload"] == {"persisted": True}
    assert calls == []


def test_worker_does_not_claim_jobs_while_database_maintenance_lock_exists(tmp_path):
    settings = _settings(tmp_path)
    repository = JobRepository(settings.resolve("quantlab.db"))
    job = repository.submit(
        job_type="safe",
        payload={},
        idempotency_key="maintenance-worker-job",
    )
    lock_path = repository.path.with_suffix(repository.path.suffix + ".maintenance.lock")
    lock_path.write_text("maintenance", encoding="utf-8")
    worker = JobWorker(
        settings,
        worker_id="maintenance-worker",
        handlers={"safe": lambda _context, _payload: {"ok": True}},
    )

    assert worker.run_once() is None
    assert repository.job(job["job_id"])["status"] == "queued"


def test_worker_fails_closed_when_job_type_has_no_registered_handler(tmp_path):
    settings = _settings(tmp_path)
    repository = JobRepository(settings.resolve("quantlab.db"))
    job = repository.submit(
        job_type="unregistered",
        payload={},
        idempotency_key="unregistered-handler-job",
        max_attempts=1,
    )
    worker = JobWorker(
        settings,
        worker_id="missing-handler-worker",
        handlers={"different": lambda _context, _payload: {"ok": True}},
    )

    result = worker.run_once()

    assert result["job_id"] == job["job_id"]
    assert result["status"] == "failed"
    assert "no handler registered" in result["error_detail"]


def test_simulator_mark_keeps_last_price_when_one_symbol_is_unavailable(tmp_path):
    settings = _settings(tmp_path)
    account = create_user_paper_account(
        settings,
        name="stale-mark",
        initial_capital=100_000,
        idempotency_key="stale-mark-account",
    )
    repository = user_simulator_repository(settings)
    previous_time = datetime(2026, 7, 16, 7, 0, tzinfo=UTC).isoformat()
    with repository.transaction() as db:
        db.execute(
            "UPDATE user_paper_accounts SET current_cash=99000 WHERE account_id=?",
            (account["account_id"],),
        )
        db.execute(
            """INSERT INTO user_paper_positions(
                account_id,symbol,name,asset_type,industry,quantity,average_cost,
                latest_price,latest_price_at,mark_source,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                account["account_id"],
                "sh600001",
                "fixture",
                "stock",
                "industry",
                100,
                10.0,
                10.0,
                previous_time,
                "previous_server_quote",
                previous_time,
            ),
        )

    snapshot = mark_user_paper_account(
        settings,
        account_id=account["account_id"],
        snapshot_date=date(2026, 7, 17),
        quote_service=_UnavailableQuoteService(),
    )

    assert snapshot["equity"] == 100_000
    assert "sh600001:stale_or_missing_mark" in snapshot["payload"]["warnings"]
    assert snapshot["payload"]["marks"][0]["data_quality"] == "stale"

    refreshed = mark_user_paper_account(
        settings,
        account_id=account["account_id"],
        snapshot_date=date(2026, 7, 17),
        quote_service=_FixedQuoteService(date(2026, 7, 17), 12.0),
    )

    assert refreshed["equity"] == 100_200
    assert refreshed["today_pnl"] == 200
    assert refreshed["payload"]["warnings"] == []
    assert len(repository.snapshots(account["account_id"])) == 1
