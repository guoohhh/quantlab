from __future__ import annotations

import hmac
import hashlib
import ipaddress
import json
import os
import time
import uuid
from datetime import UTC, date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from quantlab.api.schemas import (
    AlertRequest,
    ChatActionConfirmRequest,
    ChatConversationRequest,
    ChatMessageRequest,
    CapitalRequest,
    CapitalFlowCalculateRequest,
    CandidateTournamentRequest,
    CandidateTournamentSettlementRequest,
    DailyCycleRequest,
    EtfVariantResearchRequest,
    HistoricalReplayRequest,
    ManualTradeRequest,
    LLMReplayRequest,
    MarketSymbol,
    MarketEventRequest,
    NotificationPreferencesRequest,
    NotificationRuleRequest,
    PortfolioPlanRequest,
    PaperCycleRequest,
    ResearchRequest,
    ContextPackBuildRequest,
    ContextCommitteeRequest,
    RoleChallengeDecisionRequest,
    RoleObservationRequest,
    RoundtableRequest,
    RiskProfileRequest,
    SignalRequest,
    StockRecommendationRequest,
    StockPaperCycleRequest,
    StockMarketReplayRequest,
    StockRankingReplayRequest,
    StockResearchBatchRequest,
    StockScreenRequest,
    WatchlistRequest,
    WalkForwardRequest,
    UserAccountMarkRequest,
    UserOrderCancelRequest,
    UserOrderConfirmRequest,
    UserOrderSettlementRequest,
    UserPaperAccountRequest,
    UserPaperNewSeasonRequest,
    UserPreTradeRequest,
    UserTradeReviewRequest,
    AShareV4PoolRequest,
    BackgroundJobRequest,
    BackupRequest,
    ConvertibleBondPoolRequest,
    EtfPointInTimePoolRequest,
    ForwardSampleRequest,
    ForwardSettlementRequest,
    InvestorAdoptionRequest,
    InvestorCsvPreviewRequest,
    InvestorImportConfirmRequest,
    InvestorPortfolioRequest,
    InvestorRecommendationRequest,
    InvestorTradeRequest,
    InvestmentThesisCheckRequest,
    InvestmentThesisDraftEditRequest,
    DecisionTaskStatusRequest,
    ExperimentRunRequest,
    ExperimentArtifactRequest,
    NextTradingDayAcceptanceRequest,
    JobCancelRequest,
    NotificationChannelRequest,
    NotificationEmailTestRequest,
    ScheduleRunRequest,
    SmoothedRebalanceRequest,
    WorkerRunRequest,
    PointInTimeMasterBatchRequest,
    PointInTimeStatusBatchRequest,
    StrategyEvidenceRunRequest,
    TradingCalendarBatchRequest,
    TradeQuoteRequest,
)
from quantlab.config import Settings
from quantlab.learning import LearningRepository
from quantlab.domain import AnalysisContextPack, MarketQuote, ResearchProvenance
from quantlab.domain.strategy_evidence import (
    PointInTimePoolSnapshot,
    PointInTimeSecurity,
    PointInTimeTradeStatus,
)
from quantlab.domain.data_governance import DataNamespace, DataTrustLevel
from quantlab.llm import provider_configuration_summary, run_llm_replay
from quantlab.persistence import (
    AShareUniverseRepository,
    DecisionRepository,
    EvidenceRepository,
    HistoricalReplayRepository,
    RoundtableRepository,
    StockRankingReplayRepository,
    TerminalRepository,
    ChatRepository,
    NotificationRepository,
    UserPaperTradingRepository,
    WideResearchRepository,
)
from quantlab.persistence.jobs import JobRepository
from quantlab.persistence.migrations import ensure_database_initialized
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.round8 import Round8Repository
from quantlab.persistence.round9 import Round9Repository
from quantlab.portfolio.smoothing import BudgetSmoothingPolicy, plan_smoothed_rebalance
from quantlab.reporting import (
    audit_package_json,
    build_research_audit_package,
    build_stored_audit_package,
    prepare_historical_replay_export,
    render_research_markdown,
    render_historical_replay_markdown,
    research_persistence_context,
)
from quantlab.security import safe_error_detail, sanitize_for_export
from quantlab.workflows import (
    analyze_symbol,
    build_evidence_summary,
    build_analysis_context_pack,
    build_market_radar,
    build_today_brief,
    candidate_tournament_scorecard,
    collect_all_events,
    generate_portfolio_plan,
    latest_portfolio_plan,
    learning_status,
    load_quant_report,
    paper_scorecard,
    run_learning_cycle,
    run_daily_cycle,
    run_historical_blind_replay,
    run_expert_roundtable,
    run_paper_cycle,
    run_stock_paper_cycle,
    run_stock_ranking_replay,
    run_market_wide_stock_replay,
    refresh_a_share_security_master,
    capture_point_in_time_universe,
    run_stock_research_batch,
    recommend_stocks,
    screen_selected_stocks,
    search_stocks,
    settle_candidate_tournaments,
    train_learning_models,
    run_etf_walk_forward,
    run_etf_variant_research,
    run_adaptive_etf_candidate_lab,
    run_candidate_tournament,
    roundtable_participant_catalog,
    cancel_chat_action,
    cancel_user_paper_order,
    confirm_chat_action,
    create_chat_conversation,
    create_user_paper_account,
    handle_chat_message,
    mark_user_paper_account,
    run_pretrade_check,
    settle_user_paper_order,
    submit_user_paper_order,
    user_simulator_repository,
    calculate_industry_flow,
    calculate_market_flow,
    calculate_stock_flow,
    decide_role_challenge,
    emit_context_quality_notifications,
    evaluate_flow_notification_rules,
    freeze_role_challenge,
    record_role_outcome,
    role_scorecard,
    run_context_committee,
)
from quantlab.workflows.capital_flow import unavailable_flow_block
from quantlab.workflows.chat_jobs import requires_background_chat, submit_chat_job
from quantlab.workflows.forward_ablation import (
    forward_ablation_scorecard,
    forward_account_scorecard,
)
from quantlab.workflows.forward_experiment import (
    formal_forward_scorecard,
    save_manual_forward_exploration,
)
from quantlab.workflows.shadow_trading import shadow_scorecard
from quantlab.workflows.investor_portfolio import (
    build_investor_recommendation,
    confirm_investor_import,
    create_investor_portfolio,
    investor_csv_template,
    investor_recommendation_detail,
    investor_recommendation_effects,
    preview_investor_csv,
    record_investor_trade,
    record_recommendation_adoption,
)
from quantlab.workflows.investment_thesis import (
    check_investment_thesis,
    edit_investment_thesis_draft,
    freeze_investment_thesis_revision,
)
from quantlab.workflows.reflection import controlled_research_memory
from quantlab.workflows.experiment_recorder import (
    ExperimentRecorder,
    next_trading_day_acceptance_report,
)
from quantlab.workflows.point_in_time import (
    build_a_share_v4_candidates,
    build_point_in_time_convertible_bond_pool,
    build_point_in_time_etf_pool,
    persist_point_in_time_pool,
    register_round3_protocol,
    round3_protocol,
)
from quantlab.workflows.product_demo import live_demo_status, run_historical_research_demo
from quantlab.runtime.notification_delivery import NotificationDeliveryWorker
from quantlab.runtime.operations import backup_database
from quantlab.runtime.scheduler import RuntimeScheduler
from quantlab.runtime.soak import capture_soak_observation, soak_report
from quantlab.runtime.readiness import (
    formal_experiment_status,
    primary_start_readiness,
    runtime_health,
)
from quantlab.runtime.worker import JobWorker
from quantlab.market import StoredTestQuoteProvider, TradingCalendarService

app = FastAPI(
    title="QuantLab API",
    version="1.2.0",
    description="Auditable multi-agent quantitative research API",
)


@app.middleware("http")
async def maintenance_mode_guard(request: Request, call_next):
    settings = _settings()
    database = settings.resolve(settings.get("system.database_path"))
    lock_path = database.with_suffix(database.suffix + ".maintenance.lock")
    if lock_path.exists() and request.url.path != "/api/health":
        return JSONResponse(
            status_code=503,
            content={"detail": "QuantLab is in database maintenance mode"},
        )
    return await call_next(request)


@app.middleware("http")
async def api_rate_limit_and_audit(request: Request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    settings = _settings()
    database = settings.resolve(settings.get("system.database_path"))
    if (
        database.with_suffix(database.suffix + ".maintenance.lock").exists()
        and request.url.path != "/api/health"
    ):
        return JSONResponse(
            status_code=503,
            content={"detail": "QuantLab is in database maintenance mode"},
        )
    started = time.perf_counter()
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    client_value = request.client.host if request.client else "unknown"
    client_fingerprint = hashlib.sha256(client_value.encode("utf-8")).hexdigest()[:24]
    repository = JobRepository(settings.resolve(settings.get("system.database_path")))
    rate = repository.check_rate_limit(
        client_fingerprint=client_fingerprint,
        maximum_requests=int(settings.get("runtime.api_requests_per_minute", 300)),
    )
    if not rate["allowed"]:
        response = JSONResponse(
            status_code=429,
            content={"detail": "API rate limit exceeded", "rate_limit": rate},
        )
    else:
        response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    repository.record_api_audit(
        request_id=request_id,
        client_fingerprint=client_fingerprint,
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    return response


@app.middleware("http")
async def optional_api_token_auth(request: Request, call_next):
    expected = os.getenv("QUANTLAB_API_TOKEN")
    protected_path = (
        request.url.path.startswith("/internal/")
        or (
            request.url.path.startswith("/api/")
            and request.url.path != "/api/health"
        )
    )
    if expected and protected_path:
        supplied = request.headers.get("X-QuantLab-Token", "")
        if not supplied or not hmac.compare_digest(supplied, expected):
            return JSONResponse(status_code=401, content={"detail": "valid API token required"})
    elif (
        protected_path
        and not _is_local_request(request)
    ):
        return JSONResponse(
            status_code=403,
            content={"detail": "sensitive API access requires a loopback client or API token"},
        )
    return await call_next(request)


@app.exception_handler(Exception)
async def sanitized_unhandled_exception(_request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": safe_error_detail(exc)},
    )


def _settings() -> Settings:
    settings = Settings.load()
    ensure_database_initialized(settings.resolve(settings.get("system.database_path")))
    return settings


def _terminal() -> TerminalRepository:
    settings = _settings()
    return TerminalRepository(settings.resolve(settings.get("system.database_path")))


def _simulator() -> UserPaperTradingRepository:
    return user_simulator_repository(_settings())


def _public_import_manifest(
    batch_type: str,
    payload: object,
    *,
    source: str,
    source_version: str,
    available_at: datetime,
    record_count: int,
) -> dict:
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return Round5Repository(
        _settings().resolve(_settings().get("system.database_path"))
    ).create_manifest(
        batch_type=batch_type,
        namespace=DataNamespace.RESEARCH,
        trust_level=DataTrustLevel.USER_IMPORTED,
        provider="public_api",
        source=source,
        endpoint=f"/api/point-in-time/{batch_type}",
        source_version=source_version,
        available_at=available_at,
        license_status="user_supplied_unverified",
        payload=payload,
        raw_fingerprint=fingerprint,
        record_count=record_count,
    )


def _research_snapshot(
    snapshot: PointInTimePoolSnapshot,
    manifest_id: str | None,
) -> PointInTimePoolSnapshot:
    payload = snapshot.model_dump(mode="json")
    payload.update(
        {
            "namespace": DataNamespace.RESEARCH.value,
            "trust_level": DataTrustLevel.USER_IMPORTED.value,
            "manifest_id": manifest_id,
            "fingerprint": "",
        }
    )
    return PointInTimePoolSnapshot.model_validate(payload)


def _chat() -> ChatRepository:
    settings = _settings()
    return ChatRepository(settings.resolve(settings.get("system.database_path")))


def _notifications() -> NotificationRepository:
    settings = _settings()
    return NotificationRepository(settings.resolve(settings.get("system.database_path")))


def _evidence_repository() -> EvidenceRepository:
    settings = _settings()
    return EvidenceRepository(settings.resolve(settings.get("system.database_path")))


def _jobs() -> JobRepository:
    settings = _settings()
    return JobRepository(settings.resolve(settings.get("system.database_path")))


def _is_local_request(request: Request) -> bool:
    if request.client is None:
        return False
    forwarded = _forwarded_client_hosts(request)
    if forwarded and any(not _is_loopback_host(host) for host in forwarded):
        return False
    return _is_loopback_host(request.client.host)


def _forwarded_client_hosts(request: Request) -> list[str]:
    values: list[str] = []
    for item in request.headers.get("x-forwarded-for", "").split(","):
        if item.strip():
            values.append(item.strip())
    for item in request.headers.get("forwarded", "").split(","):
        for part in item.split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key.lower() == "for":
                values.append(value.strip().strip('"'))
    return values


def _is_loopback_host(value: str) -> bool:
    host = value.strip().lower()
    if host == "localhost":
        return True
    if host.startswith("[") and "]" in host:
        host = host[1 : host.index("]")]
    elif host.count(":") == 1:
        candidate, port = host.rsplit(":", 1)
        if port.isdigit():
            host = candidate
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@app.get("/api/health")
def health():
    settings = _settings()
    database = settings.resolve(settings.get("system.database_path"))
    maintenance = database.with_suffix(database.suffix + ".maintenance.lock").exists()
    return {
        "status": "maintenance" if maintenance else "ok",
        "version": app.version,
        "llm_provider": settings.get("llm.provider"),
        "api_auth": "required" if os.getenv("QUANTLAB_API_TOKEN") else "disabled_local_only",
        "execution_mode": "manual_orders_only",
        "maintenance_mode": maintenance,
    }


@app.post("/internal/test/quotes")
def internal_test_quote_import(request: Request, quote: TradeQuoteRequest):
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="test quote import is loopback-only")
    if os.getenv("QUANTLAB_ENABLE_TEST_QUOTES", "").lower() not in {"1", "true", "yes"}:
        raise HTTPException(status_code=404, detail="test quote import is disabled")
    parsed = MarketQuote.model_validate(quote.model_dump())
    parsed.authoritative = False
    parsed.evidence_stage = "test"
    parsed.source = "stored_test_quote_provider"
    parsed.provider = "stored_test_quote_provider"
    parsed.source_version = "test-v1"
    StoredTestQuoteProvider(
        _settings().resolve(_settings().get("system.database_path"))
    ).save(parsed)
    return {
        "saved": True,
        "symbol": parsed.symbol,
        "as_of": parsed.as_of.isoformat(),
        "authoritative": False,
        "evidence_stage": "test",
    }


@app.get("/api/engine/status")
def engine_status():
    settings = _settings()
    path = settings.resolve(settings.get("system.database_path"))
    repository = Round5Repository(path)
    experiment = repository.primary_experiment()
    provider = provider_configuration_summary(settings.section("llm"))
    real_llm_endpoints = (
        int(provider.get("openai_key_count", 0))
        + int(provider.get("deepseek_key_count", 0))
        + int(provider.get("local_endpoint_count", 0))
    )
    registration_runs = (
        repository.registration_runs(experiment["experiment_id"])
        if experiment
        else []
    )
    formal_samples = sum(int(item["registered_samples"]) for item in registration_runs)
    matured_by_horizon = {}
    if experiment:
        strategy_repository = StrategyEvidenceRepository(path)
        for horizon in experiment["horizons"]:
            scorecard = strategy_repository.forward_scorecard(
                cohort_id=experiment["cohort_id"],
                horizon_days=int(horizon),
                registration_origin="automatic_primary",
            )
            matured_by_horizon[str(horizon)] = int(
                scorecard["variants"]["full_system"]["matured_samples"]
            )
    trusted_batches = {
        batch: next(
            (
                item
                for item in repository.manifests(batch, limit=20)
                if item["namespace"] == DataNamespace.PRODUCTION.value
                and item["status"] in {"completed", "partial"}
                and int(item["record_count"]) > 0
            ),
            None,
        )
        for batch in ("trading_calendar", "industry_membership", "point_in_time_pool")
    }
    minimum_trust = DataTrustLevel(
        experiment["minimum_trust_level"]
        if experiment
        else settings.get(
            "strategies.forward_primary.minimum_trust_level",
            DataTrustLevel.SERVER_OBSERVED.value,
        )
    )
    today = date.today()
    try:
        current_calendar = TradingCalendarService.from_settings(settings).day(
            today,
            formal=True,
            minimum_trust=minimum_trust,
        )
    except ValueError:
        current_calendar = None
    current_pool = StrategyEvidenceRepository(path).latest_pool_metadata(
        "a_share",
        today,
        namespace=DataNamespace.PRODUCTION,
        minimum_trust=minimum_trust,
    )
    current_pool_ready = bool(
        current_pool
        and str(current_pool.get("snapshot_date"))[:10] == today.isoformat()
    )
    blockers = []
    if real_llm_endpoints == 0:
        blockers.append("no_real_llm_provider_configured")
    if current_calendar is None:
        blockers.append("trusted_production_calendar_does_not_cover_today")
    if trusted_batches["industry_membership"] is None:
        blockers.append("trusted_production_industry_unavailable")
    if not current_pool_ready:
        blockers.append("trusted_production_point_in_time_pool_for_today_unavailable")
    evidence_blockers = []
    if not matured_by_horizon or min(matured_by_horizon.values()) < 30:
        evidence_blockers.append("fewer_than_30_matured_formal_samples")
    evidence_blockers.append("profitability_and_incremental_value_not_yet_promoted")
    return {
        "factor_engine": "ready",
        "multi_agent": "ready",
        "forecast_calibration": "ready",
        "market_radar": "ready",
        "stock_search_and_discovery": "ready",
        "user_selected_stock_screen": "ready",
        "batch_stock_multi_agent_research": "ready",
        "stock_point_in_time_ranking_replay": "ready",
        "a_share_versioned_security_master": "ready",
        "a_share_historical_market_universe": "ready",
        "a_share_historical_st_suspension_and_limits": "ready",
        "a_share_delisted_price_history": "ready",
        "a_share_market_wide_point_in_time_replay": "ready",
        "a_share_stock_model_training": "admission_gated",
        "a_share_strategy_deployment": "evidence_and_performance_gated",
        "stock_prospective_shadow_accounts": "active" if experiment else "not_started",
        "deterministic_stock_valuation_range": "ready",
        "expert_roundtable": "ready",
        "audit_report_export": "ready",
        "prospective_paper_trading": "ready",
        "benchmark_and_ablation_evidence": "ready",
        "preregistered_strategy_candidate_lab": "ready",
        "daily_cycle": "ready",
        "broker_execution": "disabled",
        "readiness": {
            "engineering": "ready",
            "formal_forward_experiment": (
                "collecting" if formal_samples else "frozen_waiting_for_samples"
                if experiment
                else "not_started"
            ),
            "formal_forward_samples": formal_samples,
            "matured_formal_samples_by_horizon": matured_by_horizon,
            "real_llm_endpoints": real_llm_endpoints,
            "trusted_inputs": {
                "trading_calendar": current_calendar is not None,
                "industry_membership": trusted_batches["industry_membership"] is not None,
                "point_in_time_pool": current_pool_ready,
            },
            "formal_run_ready": bool(experiment and not blockers),
            "production_ready": False,
            "blockers": blockers,
            "strategy_admission_blockers": evidence_blockers,
            "claim_boundary": (
                "Engineering readiness does not mean the strategy has prospective profit evidence."
            ),
        },
    }


@app.get("/api/roundtables/participants")
def roundtable_participants():
    return {"participants": roundtable_participant_catalog()}


@app.post("/api/roundtables")
def create_roundtable(request: RoundtableRequest):
    try:
        return run_expert_roundtable(
            _settings(),
            request.source_run_id,
            request.participants,
            request.topic,
            rounds=request.rounds,
            save=request.save,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/roundtables")
def list_roundtables(limit: int = Query(default=20, ge=1, le=100)):
    settings = _settings()
    repository = RoundtableRepository(settings.resolve(settings.get("system.database_path")))
    return repository.recent(limit)


@app.get("/api/roundtables/{session_id}")
def roundtable_detail(session_id: str):
    settings = _settings()
    repository = RoundtableRepository(settings.resolve(settings.get("system.database_path")))
    result = repository.get(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="roundtable session not found")
    return result


@app.get("/api/engine/quant")
def quant_engine(code: MarketSymbol, as_of: date | None = None):
    try:
        output = load_quant_report(_settings(), code, as_of)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc
    return {
        "source": output["source"],
        "bars": output["bars"],
        "degraded_sources": output["degraded_sources"],
        "report": output["report"].model_dump(mode="json"),
    }


@app.post("/api/multi-agent/analyze")
def multi_agent_analysis(code: MarketSymbol, as_of: date | None = None):
    try:
        output = analyze_symbol(_settings(), code, as_of)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc
    run = output["decision_run"]
    return sanitize_for_export(
        {
            "run_id": run.run_id,
            "factor_report": output["report"].model_dump(mode="json"),
            "financial_report": (
                output["financial_report"].model_dump(mode="json")
                if output["financial_report"]
                else None
            ),
            "council": run.reports["council"].model_dump(mode="json"),
            "forecasts": [item.model_dump(mode="json") for item in run.forecasts],
            "decision": run.decision.model_dump(mode="json"),
            "decision_trace": run.decision_trace,
            "audit_log": [item.model_dump(mode="json") for item in run.audit_log],
            "llm_audit": run.llm_audit,
        }
    )


@app.post("/api/research/analyze")
def create_research_report(request: ResearchRequest):
    settings = _settings()
    if request.background:
        key = request.idempotency_key or (
            f"research:{request.symbol}:{request.as_of or date.today()}:"
            f"{request.account_id or 'none'}"
        )
        return JobRepository(
            settings.resolve(settings.get("system.database_path"))
        ).submit(
            job_type="research",
            payload=request.model_dump(mode="json", exclude={"background", "idempotency_key"}),
            idempotency_key=key,
            concurrency_key=f"research:{request.symbol}",
            timeout_seconds=int(settings.get("runtime.research_job_timeout_seconds", 1800)),
            cost_budget_usd=float(settings.get("llm.task_cost_budget_usd", 8.0)),
        )
    try:
        output = analyze_symbol(
            settings,
            request.symbol,
            request.as_of,
            asset_type=request.asset_type,
            include_events=request.include_events,
            account_id=request.account_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc
    package = build_research_audit_package(output)
    if request.save:
        repository = DecisionRepository(
            settings.resolve(settings.get("system.database_path"))
        )
        repository.save(
            output["decision_run"],
            research_persistence_context(output),
            provenance=ResearchProvenance(
                origin="user_interactive_research",
                requested_as_of=request.as_of or date.today(),
                evidence_stage="research_only",
            ),
        )
        record = repository.get(output["decision_run"].run_id)
        if record is None:
            raise HTTPException(status_code=500, detail="persisted research is unavailable")
        package = build_stored_audit_package(record)
    return package


@app.post("/api/research/jobs")
def create_research_job(request: ResearchRequest):
    settings = _settings()
    key = request.idempotency_key or (
        f"research:{request.symbol}:{request.as_of or date.today()}:"
        f"{request.account_id or 'none'}"
    )
    return _jobs().submit(
        job_type="research",
        payload=request.model_dump(mode="json", exclude={"background", "idempotency_key"}),
        idempotency_key=key,
        concurrency_key=f"research:{request.symbol}",
        timeout_seconds=int(settings.get("runtime.research_job_timeout_seconds", 1800)),
        cost_budget_usd=float(settings.get("llm.task_cost_budget_usd", 8.0)),
    )


@app.get("/api/market/radar")
def market_radar(
    as_of: date | None = None,
    include_sectors: bool = False,
    sector_limit: int = Query(default=15, ge=1, le=50),
):
    try:
        return build_market_radar(_settings(), as_of, include_sectors, sector_limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/market/search")
def market_search(keyword: str = Query(min_length=1, max_length=100), limit: int = 20):
    try:
        return search_stocks(_settings(), keyword, limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.post("/api/stocks/screen")
def stock_screen(request: StockScreenRequest):
    try:
        return screen_selected_stocks(
            _settings(),
            request.symbols,
            request.as_of,
            top_n=request.top_n,
            max_correlation=request.max_correlation,
            save=request.save,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.post("/api/stocks/recommend")
def stock_recommend(request: StockRecommendationRequest):
    try:
        return recommend_stocks(
            _settings(),
            request.as_of,
            styles=request.styles or None,
            candidate_limit=request.candidate_limit,
            top_n=request.top_n,
            max_correlation=request.max_correlation,
            save=request.save,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.post("/api/stocks/research-batch")
def stock_research_batch(request: StockResearchBatchRequest):
    try:
        return run_stock_research_batch(
            _settings(),
            request.symbols,
            request.as_of,
            include_events=request.include_events,
            save=request.save,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.post("/api/stocks/ranking-replay")
def stock_ranking_replay(request: StockRankingReplayRequest):
    try:
        return run_stock_ranking_replay(
            _settings(),
            request.symbols,
            request.start,
            request.end,
            horizon_days=request.horizon_days,
            episodes=request.episodes,
            top_k=request.top_k,
            max_correlation=request.max_correlation,
            save=request.save,
            record_learning_samples=request.record_learning_samples,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/stocks/ranking-replays")
def stock_ranking_replays(limit: int = Query(default=20, ge=1, le=100)):
    settings = _settings()
    return StockRankingReplayRepository(
        settings.resolve(settings.get("system.database_path"))
    ).list(limit)


@app.get("/api/stocks/ranking-replays/{replay_id}")
def stock_ranking_replay_detail(replay_id: int):
    settings = _settings()
    record = StockRankingReplayRepository(
        settings.resolve(settings.get("system.database_path"))
    ).get(replay_id)
    if record is None:
        raise HTTPException(status_code=404, detail="stock ranking replay not found")
    return sanitize_for_export(record)


@app.post("/api/stocks/market-replay")
def stock_market_replay(request: StockMarketReplayRequest):
    try:
        return run_market_wide_stock_replay(
            _settings(),
            request.start,
            request.end,
            horizon_days=request.horizon_days,
            episodes=request.episodes,
            sample_size=request.sample_size,
            top_k=request.top_k,
            max_correlation=request.max_correlation,
            save=request.save,
            record_learning_samples=request.record_learning_samples,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.post("/api/stocks/security-master/refresh")
def stock_security_master_refresh():
    try:
        return refresh_a_share_security_master(_settings())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/stocks/universe/{snapshot_date}")
def stock_universe_snapshot(snapshot_date: date):
    settings = _settings()
    records = AShareUniverseRepository(
        settings.resolve(settings.get("system.database_path"))
    ).snapshot(snapshot_date)
    if not records:
        raise HTTPException(status_code=404, detail="point-in-time universe snapshot not found")
    sources = sorted({str(item.get("source") or "unknown") for item in records})
    captured_at = sorted(
        str(item["captured_at"]) for item in records if item.get("captured_at")
    )
    return {
        "snapshot_date": snapshot_date.isoformat(),
        "source": sources[0] if len(sources) == 1 else "mixed",
        "sources": sources,
        "cache_hit": True,
        "capture_attempts": 0,
        "stored_only": True,
        "securities": len(records),
        "tradable": sum(bool(item.get("trade_status")) for item in records),
        "captured_at": captured_at[0] if captured_at else None,
    }


@app.post("/api/stocks/universe/{snapshot_date}")
def stock_universe_capture(snapshot_date: date, force: bool = False):
    try:
        output = capture_point_in_time_universe(_settings(), snapshot_date, force=force)
        return {key: value for key, value in output.items() if key != "records"}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/stocks/discoveries")
def stock_discovery_runs(limit: int = Query(default=20, ge=1, le=100)):
    return _terminal().stock_discovery_runs(limit)


@app.get("/api/stocks/discoveries/{discovery_id}")
def stock_discovery_detail(discovery_id: int):
    output = _terminal().stock_discovery(discovery_id)
    if output is None:
        raise HTTPException(status_code=404, detail="stock discovery run not found")
    return output


@app.get("/api/multi-agent/momentum-screen")
def momentum_screen(code: str, as_of: date | None = None):
    try:
        output = screen_selected_stocks(
            _settings(),
            [code],
            as_of,
            top_n=1,
            save=False,
        )
        return output["candidates"][0]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/multi-agent/momentum-batch-screen")
def momentum_batch_screen(
    codes: str,
    top_n: int = Query(default=10, ge=1, le=20),
    as_of: date | None = None,
):
    try:
        return screen_selected_stocks(
            _settings(),
            codes,
            as_of,
            top_n=top_n,
            save=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/today")
def today_brief(as_of: date | None = None):
    try:
        return build_today_brief(_settings(), as_of)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/evidence")
def evidence_summary(asset_scope: str = "etf"):
    if asset_scope not in {"etf", "stock", "convertible_bond"}:
        raise HTTPException(status_code=422, detail="invalid asset_scope")
    return build_evidence_summary(_settings(), asset_scope)


@app.post("/api/paper/cycle")
def paper_cycle(request: PaperCycleRequest):
    try:
        return run_paper_cycle(
            _settings(), request.as_of, request.run_research, request.research_limit
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.post("/api/paper/stocks/cycle")
def stock_paper_cycle(request: StockPaperCycleRequest):
    try:
        return run_stock_paper_cycle(
            _settings(),
            request.symbols,
            request.as_of,
            top_n=request.top_n,
            max_correlation=request.max_correlation,
            run_research=request.run_research,
            research_limit=request.research_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/paper/scorecard")
def get_paper_scorecard():
    return paper_scorecard(_settings())


@app.post("/api/scheduler/run/daily")
def scheduler_daily(request: DailyCycleRequest):
    return run_daily_cycle(_settings(), request.as_of, request.run_research)


@app.post("/api/daily-cycle/jobs")
def daily_cycle_job(request: DailyCycleRequest):
    resolved = request.as_of or date.today()
    return _jobs().submit(
        job_type="daily_cycle",
        payload=request.model_dump(mode="json"),
        idempotency_key=f"daily-cycle:{resolved.isoformat()}:{int(request.run_research)}",
        concurrency_key="daily-cycle",
        timeout_seconds=3_600,
    )


@app.post("/api/replay/historical")
def historical_replay(request: HistoricalReplayRequest):
    try:
        return run_historical_blind_replay(
            _settings(),
            request.start,
            request.end,
            horizon_days=request.horizon_days,
            episodes=request.episodes,
            save=request.save,
            allow_large_run=request.confirm_large_run,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.post("/api/replay/historical/jobs")
def historical_replay_job(request: HistoricalReplayRequest):
    key = (
        f"historical-replay:{request.start}:{request.end}:{request.horizon_days}:"
        f"{request.episodes}"
    )
    return _jobs().submit(
        job_type="historical_replay",
        payload=request.model_dump(mode="json"),
        idempotency_key=key,
        concurrency_key="historical-replay",
        timeout_seconds=7_200,
        max_attempts=2,
        cost_budget_usd=float(_settings().get("llm.task_cost_budget_usd", 8.0)),
    )


@app.get("/api/replay/historical")
def historical_replays(limit: int = Query(default=20, ge=1, le=100)):
    settings = _settings()
    return HistoricalReplayRepository(settings.resolve(settings.get("system.database_path"))).list(
        limit
    )


@app.get("/api/replay/historical/{replay_id}")
def historical_replay_detail(replay_id: int, format: Literal["json", "markdown"] = "json"):
    settings = _settings()
    record = HistoricalReplayRepository(settings.resolve(settings.get("system.database_path"))).get(
        replay_id
    )
    if record is None:
        raise HTTPException(status_code=404, detail="historical replay not found")
    if format == "markdown":
        return Response(
            content=render_historical_replay_markdown(record["payload"]),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="historical-replay-{replay_id}.md"'
            },
        )
    return sanitize_for_export(
        {**record, "payload": prepare_historical_replay_export(record["payload"])}
    )


@app.get("/api/reports/research/{run_id}")
def research_report(
    run_id: str,
    format: Literal["json", "markdown"] = "json",
    download: bool = False,
):
    settings = _settings()
    record = DecisionRepository(settings.resolve(settings.get("system.database_path"))).get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="research run not found")
    package = build_stored_audit_package(record)
    filename = f"quantlab-{package['symbol']}-{package['as_of']}-{run_id[:8]}"
    if format == "markdown":
        return Response(
            content=render_research_markdown(package),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}.md"'},
        )
    if download:
        return Response(
            content=audit_package_json(package),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}.json"'},
        )
    return package


@app.get("/api/backtest/strategies")
def backtest_strategies():
    return {
        "strategies": [
            "etf_rotation",
            "stock_reversal",
            "convertible_bond_double_low",
            "pullback_reversal",
        ]
    }


@app.post("/api/backtest/etf-walk-forward")
def etf_walk_forward(request: WalkForwardRequest):
    try:
        return run_etf_walk_forward(
            _settings(),
            request.start,
            request.end,
            request.train_days,
            request.test_days,
            request.save,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.post("/api/backtest/adaptive-etf-lab")
def adaptive_etf_lab(save: bool = True):
    try:
        return run_adaptive_etf_candidate_lab(_settings(), save)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.post("/api/backtest/etf-variant-research")
def etf_variant_research(request: EtfVariantResearchRequest):
    try:
        return run_etf_variant_research(
            _settings(),
            request.start,
            request.end,
            request.strategy_variant,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/dashboard/summary")
def dashboard_summary(limit: int = Query(default=10, ge=1, le=100)):
    settings = _settings()
    repository = DecisionRepository(settings.resolve(settings.get("system.database_path")))
    terminal = _terminal()
    return {
        "recent_decisions": repository.recent(limit),
        "calibration": repository.calibration_report(
            minimum_samples=int(settings.get("calibration.minimum_samples", 30))
        ).model_dump(mode="json"),
        "watchlist_count": len(terminal.list_watchlist()),
        "portfolio": terminal.portfolio_overview(settings.get("system.initial_capital")),
    }


@app.get("/api/watchlist/")
def list_watchlist():
    return _terminal().list_watchlist()


@app.post("/api/watchlist/add")
def add_watchlist(request: WatchlistRequest):
    _terminal().upsert_watchlist(request.symbol, request.name, request.group_name, request.notes)
    return {"status": "ok", "symbol": request.symbol}


@app.delete("/api/watchlist/remove")
def remove_watchlist(symbol: MarketSymbol):
    if not _terminal().remove_watchlist(symbol):
        raise HTTPException(status_code=404, detail="watchlist symbol not found")
    return {"status": "ok", "symbol": symbol}


@app.get("/api/watchlist/groups")
def watchlist_groups():
    return _terminal().watchlist_groups()


@app.get("/api/signals/latest")
def latest_signals(limit: int = Query(default=50, ge=1, le=500)):
    return _terminal().latest_signals(limit)


@app.post("/api/signals/record")
def record_signal(request: SignalRequest):
    signal_id = _terminal().record_signal(
        request.symbol,
        request.strategy,
        request.score,
        request.action,
        request.as_of,
        request.payload,
    )
    return {"status": "ok", "id": signal_id}


@app.get("/api/signals/alerts")
def list_alerts():
    return _terminal().list_alerts()


@app.post("/api/signals/alerts/add")
def add_alert(request: AlertRequest):
    alert_id = _terminal().add_alert(request.symbol, request.condition_type, request.threshold)
    return {"status": "ok", "id": alert_id}


@app.delete("/api/signals/alerts/remove")
def remove_alert(alert_id: int):
    if not _terminal().remove_alert(alert_id):
        raise HTTPException(status_code=404, detail="alert not found")
    return {"status": "ok", "id": alert_id}


@app.get("/api/portfolio/risk-profile")
def get_risk_profile():
    settings = _settings()
    return _terminal().portfolio_settings(settings.get("system.initial_capital"))


@app.post("/api/portfolio/risk-profile")
def set_risk_profile(request: RiskProfileRequest):
    return _terminal().set_risk_profile(request.profile)


@app.post("/api/portfolio/set-capital")
def set_capital(request: CapitalRequest):
    return _terminal().set_capital(request.capital)


@app.post("/api/portfolio/trade")
def record_manual_trade(request: ManualTradeRequest):
    try:
        trade_id = _terminal().record_trade(
            request.symbol,
            request.side,
            request.quantity,
            request.price,
            request.fees,
            request.trade_date,
            request.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=safe_error_detail(exc)) from exc
    return {
        "status": "recorded",
        "id": trade_id,
        "execution": "manual ledger only; no broker order was sent",
    }


@app.get("/api/portfolio/trades")
def portfolio_trades(limit: int = Query(default=200, ge=1, le=100_000)):
    return _terminal().trades(limit)


@app.get("/api/portfolio/overview")
def portfolio_overview():
    settings = _settings()
    return _terminal().portfolio_overview(settings.get("system.initial_capital"))


@app.post("/api/portfolio/plan")
def create_portfolio_plan(request: PortfolioPlanRequest):
    try:
        return generate_portfolio_plan(
            _settings(),
            request.as_of,
            request.reversal_limit,
            request.check_stock_risks,
            request.save,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/portfolio/plan/latest")
def get_latest_portfolio_plan():
    output = latest_portfolio_plan(_settings())
    if output is None:
        raise HTTPException(status_code=404, detail="no portfolio plan has been generated")
    return output


@app.get("/api/portfolio/plans")
def portfolio_plans(limit: int = Query(default=20, ge=1, le=200)):
    return _terminal().portfolio_plans(limit)


@app.post("/api/tournament")
def create_candidate_tournament(request: CandidateTournamentRequest):
    if request.shortlist_size > request.candidate_limit:
        raise HTTPException(
            status_code=422,
            detail="shortlist_size must be no greater than candidate_limit",
        )
    try:
        return run_candidate_tournament(
            _settings(),
            request.as_of,
            candidate_limit=request.candidate_limit,
            shortlist_size=request.shortlist_size,
            max_correlation=request.max_correlation,
            save=request.save,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/tournaments")
def candidate_tournaments(limit: int = Query(default=20, ge=1, le=100)):
    return _terminal().candidate_tournaments(limit)


@app.post("/api/tournaments/settle")
def settle_tournaments(request: CandidateTournamentSettlementRequest):
    try:
        return settle_candidate_tournaments(_settings(), request.as_of, limit=request.limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/tournaments/scorecard")
def tournament_scorecard(limit: int = Query(default=100, ge=1, le=200)):
    return candidate_tournament_scorecard(_settings(), limit)


@app.get("/api/tournaments/{tournament_id}")
def candidate_tournament_detail(tournament_id: int):
    output = _terminal().candidate_tournament(tournament_id)
    if output is None:
        raise HTTPException(status_code=404, detail="candidate tournament not found")
    return output


@app.get("/api/scheduler/status")
def scheduler_status():
    return {
        "mode": "manual_or_os_scheduler",
        "daily_command": "quantlab daily-cycle",
        "background_scheduler": "operating-system trigger not configured",
        "runs": _terminal().scheduler_status(),
    }


@app.post("/api/scheduler/run/review")
def scheduler_review(as_of: date | None = None):
    terminal = _terminal()
    try:
        output = run_learning_cycle(_settings(), as_of)
        terminal.record_scheduler_run("review", "ok", output)
    except Exception as exc:
        detail = safe_error_detail(exc)
        terminal.record_scheduler_run("review", "error", {"error": detail})
        raise HTTPException(status_code=502, detail=detail) from exc
    return output


@app.get("/api/llm/config")
def llm_config():
    settings = _settings()
    return {
        **provider_configuration_summary(settings.section("llm")),
        "note": "API keys are read from environment variables and are never returned",
    }


@app.post("/api/llm/replay")
def llm_replay(request: LLMReplayRequest):
    try:
        return run_llm_replay(_settings(), request.suite, request.runs, request.save)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"LLM replay failed: {type(exc).__name__}"
        ) from exc


@app.get("/api/llm/evaluations")
def llm_evaluations(limit: int = Query(default=20, ge=1, le=200)):
    return _terminal().llm_evaluations(limit)


@app.get("/api/learning/status")
def get_learning_status():
    return learning_status(_settings())


@app.post("/api/learning/train")
def train_learning(horizon_days: int | None = None, asset_scope: str = "etf", force: bool = False):
    if horizon_days not in (None, 5, 20):
        raise HTTPException(status_code=422, detail="horizon_days must be 5 or 20")
    if asset_scope not in {"etf", "stock", "convertible_bond"}:
        raise HTTPException(status_code=422, detail="invalid asset_scope")
    return train_learning_models(_settings(), horizon_days, asset_scope, force)


@app.post("/api/learning/training-jobs")
def train_learning_job(
    horizon_days: Literal[5, 20] | None = None,
    asset_scope: str = Query(default="etf", min_length=1, max_length=50),
    force: bool = False,
):
    if asset_scope not in {"etf", "stock", "convertible_bond"}:
        raise HTTPException(status_code=422, detail="invalid asset_scope")
    return _jobs().submit(
        job_type="training",
        payload={
            "horizon_days": horizon_days,
            "asset_scope": asset_scope,
            "force": force,
        },
        idempotency_key=f"training:{asset_scope}:{horizon_days or 'all'}:{int(force)}",
        concurrency_key=f"training:{asset_scope}",
        timeout_seconds=7_200,
        max_attempts=2,
    )


@app.post("/api/learning/events")
def record_market_event(request: MarketEventRequest):
    settings = _settings()
    event_id = LearningRepository(settings.resolve(settings.get("system.database_path"))).add_event(
        request.symbol,
        request.event_date,
        request.event_type,
        request.title,
        request.source,
        request.sentiment,
        request.impact_score,
        request.payload,
    )
    return {"status": "recorded", "event_id": event_id}


@app.get("/api/learning/events")
def list_market_events(
    symbol: MarketSymbol | None = None,
    limit: int = Query(default=50, ge=1, le=500),
):
    settings = _settings()
    return LearningRepository(settings.resolve(settings.get("system.database_path"))).recent_events(
        symbol, limit
    )


@app.post("/api/learning/collect-events")
def collect_learning_events(symbol: MarketSymbol, start: date, end: date | None = None):
    return collect_all_events(_settings(), symbol, start, end or date.today())


@app.post("/api/learning/cycle")
def learning_cycle(as_of: date | None = None):
    return run_learning_cycle(_settings(), as_of)


@app.post("/api/context-packs")
def create_context_pack(request: ContextPackBuildRequest):
    try:
        payload = build_analysis_context_pack(
            _settings(),
            symbol=request.symbol,
            as_of=request.as_of,
            asset_type=request.asset_type,
            account_id=request.account_id,
            include_events=request.include_events,
            save=request.save,
        )
        emit_context_quality_notifications(
            _settings(),
            payload,
            account_id=request.account_id,
        )
        return payload
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/context-packs/{context_id}")
def context_pack(context_id: str):
    payload = _evidence_repository().context(context_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="analysis context pack not found")
    return payload


@app.get("/api/context-packs/latest/{symbol}")
def latest_context_pack(symbol: MarketSymbol, as_of: date | None = None):
    payload = _evidence_repository().latest_context(
        symbol,
        as_of=as_of.isoformat() if as_of else None,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="analysis context pack not found")
    return payload


@app.post("/api/capital-flow/calculate")
def capital_flow_calculate(request: CapitalFlowCalculateRequest):
    try:
        if request.scope == "market":
            blocks = [
                calculate_market_flow(
                    request.records,
                    as_of=request.as_of,
                    source=request.source,
                    methodology=request.methodology,
                    supplemental=request.supplemental,
                )
            ]
        elif request.scope == "industry":
            blocks = calculate_industry_flow(
                request.records,
                as_of=request.as_of,
                source=request.source,
                methodology=request.methodology,
            )
        else:
            if request.symbol is None:
                raise ValueError("stock flow calculation requires symbol")
            blocks = [
                calculate_stock_flow(
                    request.records,
                    symbol=request.symbol,
                    as_of=request.as_of,
                    source=request.source,
                    methodology=request.methodology,
                )
            ]
        output = []
        repository = _evidence_repository()
        if request.scope == "industry" and request.save:
            repository.save_industry_memberships(
                request.records,
                source=request.source,
                source_version=hashlib.sha256(
                    request.methodology.encode("utf-8")
                ).hexdigest()[:16],
                namespace=DataNamespace.RESEARCH,
                trust_level=DataTrustLevel.USER_IMPORTED,
            )
        for block in blocks:
            payload = repository.save_flow(block) if request.save else block.model_dump(mode="json")
            triggered = (
                evaluate_flow_notification_rules(
                    _settings(),
                    block,
                    account_id=request.account_id,
                )
                if request.save
                else []
            )
            output.append({"snapshot": payload, "triggered_notifications": triggered})
        return {"flows": output}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.get("/api/capital-flow/market")
def capital_flow_market(as_of: date | None = None, refresh: bool = False):
    end = as_of or date.today()
    if refresh:
        raise HTTPException(
            status_code=409,
            detail="capital-flow refresh must be submitted through /api/capital-flow/refresh-jobs",
        )
    existing = _evidence_repository().flows("market", as_of=end.isoformat(), limit=1)
    if existing:
        return existing[0]
    return unavailable_flow_block(
        scope="market",
        key="cn_market",
        as_of=end,
        source="unavailable",
        reason="market capital-flow snapshot has not been refreshed",
    ).model_dump(mode="json")


@app.post("/api/capital-flow/refresh-jobs")
def capital_flow_refresh_job(
    as_of: date | None = None,
    include_sectors: bool = True,
    symbols: list[MarketSymbol] = Query(default=[]),
):
    resolved = as_of or date.today()
    resolved_symbols = sorted(set(symbols))
    if len(resolved_symbols) > 200:
        raise HTTPException(
            status_code=422,
            detail="capital-flow refresh supports at most 200 symbols",
        )
    symbol_fingerprint = hashlib.sha256(
        ",".join(resolved_symbols).encode("utf-8")
    ).hexdigest()[:12]
    return _jobs().submit(
        job_type="capital_flow_refresh",
        payload={
            "as_of": resolved.isoformat(),
            "include_sectors": include_sectors,
            "symbols": resolved_symbols,
        },
        idempotency_key=(
            f"capital-flow-refresh:{resolved.isoformat()}:{int(include_sectors)}:"
            f"{symbol_fingerprint}"
        ),
        concurrency_key="capital-flow-refresh",
        timeout_seconds=1_800,
        max_attempts=3,
    )


@app.get("/api/capital-flow/industries")
def capital_flow_industries(
    as_of: date | None = None,
    limit: int = Query(default=50, ge=1, le=500),
):
    end = as_of or date.today()
    rows = _evidence_repository().flows("industry", as_of=end.isoformat(), limit=limit)
    if rows:
        return {"industries": rows}
    unavailable = unavailable_flow_block(
        scope="industry",
        key="all",
        as_of=end,
        source="unavailable",
        reason="reliable point-in-time industry flow history has not been ingested",
    )
    return {"industries": [unavailable.model_dump(mode="json")]}


@app.get("/api/capital-flow/stocks/{symbol}")
def capital_flow_stock(symbol: MarketSymbol, as_of: date | None = None, refresh: bool = False):
    end = as_of or date.today()
    if refresh:
        raise HTTPException(
            status_code=409,
            detail="capital-flow refresh must be submitted through /api/capital-flow/refresh-jobs",
        )
    existing = _evidence_repository().flows(
        "stock",
        scope_key=symbol,
        as_of=end.isoformat(),
        limit=1,
    )
    if existing:
        return existing[0]
    return unavailable_flow_block(
        scope="stock",
        key=symbol,
        as_of=end,
        source="unavailable",
        reason="stock capital-flow snapshot has not been refreshed",
    ).model_dump(mode="json")


@app.post("/api/llm/context-committee")
def context_committee(request: ContextCommitteeRequest):
    payload = _evidence_repository().context(request.context_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="analysis context pack not found")
    try:
        return run_context_committee(
            _settings(),
            pack=AnalysisContextPack.model_validate(payload),
            deterministic_max_weight=request.deterministic_max_weight,
            idempotency_key=request.idempotency_key,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.post("/api/llm/roles/observations")
def llm_role_observation(request: RoleObservationRequest):
    try:
        return record_role_outcome(
            _settings(),
            role=request.role,
            run_id=request.run_id,
            symbol=request.symbol,
            as_of=request.as_of.isoformat(),
            horizon_days=request.horizon_days,
            probabilities=request.probabilities,
            realized_direction=request.realized_direction,
            realized_return_pct=request.realized_return_pct,
            market_regime=request.market_regime,
            drawdown_reduction=request.drawdown_reduction,
            fact_errors=request.fact_errors,
            quant_incremental_return_pct=request.quant_incremental_return_pct,
            cost_usd=request.cost_usd,
            latency_ms=request.latency_ms,
            payload=request.payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.get("/api/llm/roles/{role}/scorecard")
def llm_role_scorecard(role: str):
    return role_scorecard(_settings(), role)


@app.post("/api/llm/roles/{role}/challenges")
def llm_role_freeze_challenge(role: str):
    try:
        return freeze_role_challenge(_settings(), role)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=safe_error_detail(exc)) from exc


@app.post("/api/llm/challenges/{challenge_id}/decision")
def llm_role_decide_challenge(
    challenge_id: str,
    request: RoleChallengeDecisionRequest,
):
    try:
        return decide_role_challenge(
            _settings(),
            challenge_id,
            passed=request.passed,
            decision=request.decision,
            reason=request.reason,
            applicable_regimes=request.applicable_regimes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=safe_error_detail(exc)) from exc


@app.post("/api/notification-rules")
def notification_rule_create(request: NotificationRuleRequest):
    try:
        return _notifications().create_rule(**request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.get("/api/notification-rules")
def notification_rules(
    account_id: str | None = None,
    rule_type: str | None = None,
    enabled_only: bool = False,
):
    return {
        "rules": _notifications().rules(
            account_id=account_id,
            rule_type=rule_type,
            enabled_only=enabled_only,
        )
    }


@app.post("/api/simulator/accounts")
def simulator_create_account(request: UserPaperAccountRequest):
    try:
        return create_user_paper_account(
            _settings(),
            name=request.name,
            initial_capital=request.initial_capital,
            benchmark_symbol=request.benchmark_symbol,
            idempotency_key=request.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.get("/api/simulator/accounts")
def simulator_accounts(include_closed: bool = True):
    return {"accounts": _simulator().accounts(include_closed)}


@app.get("/api/simulator/accounts/{account_id}")
def simulator_account(account_id: str):
    try:
        return _simulator().overview(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=safe_error_detail(exc)) from exc


@app.post("/api/simulator/accounts/{account_id}/new-season")
def simulator_new_season(account_id: str, request: UserPaperNewSeasonRequest):
    try:
        return _simulator().start_new_season(
            account_id,
            name=request.name,
            initial_capital=request.initial_capital,
            idempotency_key=request.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=safe_error_detail(exc)) from exc


@app.post("/api/simulator/pretrade-check")
def simulator_pretrade_check(request: UserPreTradeRequest):
    try:
        return run_pretrade_check(
            _settings(),
            account_id=request.account_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            amount=request.amount,
            research_run_id=request.research_run_id,
            user_context=request.user_context,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.post("/api/simulator/orders")
def simulator_submit_order(request: UserOrderConfirmRequest):
    try:
        return submit_user_paper_order(
            _settings(),
            check_id=request.check_id,
            quantity=request.quantity,
            idempotency_key=request.idempotency_key,
            user_confirmation=request.user_confirmation.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=safe_error_detail(exc)) from exc


@app.get("/api/simulator/orders/{order_id}")
def simulator_order(order_id: str):
    order = _simulator().order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="user paper order not found")
    return order


@app.get("/api/simulator/orders/{order_id}/events")
def simulator_order_events(
    order_id: str,
    limit: int = Query(default=500, ge=1, le=5_000),
):
    if _simulator().order(order_id) is None:
        raise HTTPException(status_code=404, detail="user paper order not found")
    return {"events": _simulator().order_events(order_id, limit)}


@app.post("/api/simulator/orders/{order_id}/cancel")
def simulator_cancel_order(order_id: str, request: UserOrderCancelRequest):
    try:
        return cancel_user_paper_order(_settings(), order_id, request.reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=safe_error_detail(exc)) from exc


@app.delete("/api/simulator/orders/{order_id}")
def simulator_delete_pending_order(order_id: str):
    try:
        return cancel_user_paper_order(_settings(), order_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=safe_error_detail(exc)) from exc


@app.post("/api/simulator/orders/{order_id}/settle")
def simulator_settle_order(order_id: str, request: UserOrderSettlementRequest):
    try:
        return settle_user_paper_order(
            _settings(),
            order_id=order_id,
            fill_quantity=request.fill_quantity,
            fill_key=request.fill_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.post("/api/simulator/orders/{order_id}/settlement-jobs")
def simulator_settlement_job(order_id: str, request: UserOrderSettlementRequest):
    return _jobs().submit(
        job_type="simulator_settlement",
        payload={
            "order_id": order_id,
            "fill_quantity": request.fill_quantity,
            "fill_key": request.fill_key,
        },
        idempotency_key=f"simulator-settlement:{order_id}:{request.fill_key}",
        concurrency_key=f"simulator-order:{order_id}",
        timeout_seconds=300,
        max_attempts=3,
    )


@app.get("/api/simulator/accounts/{account_id}/orders")
def simulator_account_orders(
    account_id: str,
    status: str | None = None,
    limit: int = Query(default=200, ge=1, le=2_000),
):
    return {"orders": _simulator().orders(account_id, status=status, limit=limit)}


@app.get("/api/simulator/accounts/{account_id}/fills")
def simulator_account_fills(
    account_id: str,
    limit: int = Query(default=500, ge=1, le=5_000),
):
    return {"fills": _simulator().fills(account_id, limit)}


@app.get("/api/simulator/accounts/{account_id}/positions")
def simulator_account_positions(account_id: str):
    return {"positions": _simulator().positions(account_id)}


@app.post("/api/simulator/accounts/{account_id}/mark")
def simulator_mark_account(account_id: str, request: UserAccountMarkRequest):
    try:
        return mark_user_paper_account(
            _settings(),
            account_id=account_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/simulator/accounts/{account_id}/equity-curve")
def simulator_equity_curve(
    account_id: str,
    limit: int = Query(default=500, ge=1, le=5_000),
):
    return {"snapshots": _simulator().snapshots(account_id, limit)}


@app.get("/api/simulator/accounts/{account_id}/performance")
def simulator_performance(account_id: str):
    try:
        return _simulator().performance(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=safe_error_detail(exc)) from exc


@app.post("/api/simulator/accounts/{account_id}/reviews")
def simulator_record_review(account_id: str, request: UserTradeReviewRequest):
    try:
        return _simulator().record_review(
            account_id,
            order_id=request.order_id,
            symbol=request.symbol,
            review_type=request.review_type,
            payload=request.payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.get("/api/simulator/accounts/{account_id}/reviews")
def simulator_reviews(
    account_id: str,
    limit: int = Query(default=100, ge=1, le=1_000),
):
    return {"reviews": _simulator().reviews(account_id, limit)}


@app.post("/api/chat/conversations")
def chat_create_conversation(request: ChatConversationRequest):
    try:
        return create_chat_conversation(
            _settings(),
            title=request.title,
            account_id=request.account_id,
            symbol=request.symbol,
            research_run_id=request.research_run_id,
            idempotency_key=request.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.get("/api/chat/conversations")
def chat_conversations(limit: int = Query(default=50, ge=1, le=500)):
    return {"conversations": _chat().conversations(limit)}


@app.get("/api/chat/conversations/{conversation_id}")
def chat_conversation(conversation_id: str):
    conversation = _chat().conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="chat conversation not found")
    return conversation


@app.post("/api/chat/conversations/{conversation_id}/messages")
def chat_post_message(conversation_id: str, request: ChatMessageRequest):
    try:
        if requires_background_chat(
            content=request.content,
            allow_research=request.allow_research,
            explicit_background=request.background,
        ):
            return submit_chat_job(
                _settings(),
                conversation_id=conversation_id,
                content=request.content,
                idempotency_key=request.idempotency_key,
                account_id=request.account_id,
                symbol=request.symbol,
                quantity=request.quantity,
                research_run_id=request.research_run_id,
                allow_research=request.allow_research,
            )
        return handle_chat_message(
            _settings(),
            conversation_id=conversation_id,
            content=request.content,
            account_id=request.account_id,
            symbol=request.symbol,
            quantity=request.quantity,
            research_run_id=request.research_run_id,
            allow_research=request.allow_research,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=safe_error_detail(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc


@app.get("/api/chat/conversations/{conversation_id}/messages")
def chat_messages(
    conversation_id: str,
    limit: int = Query(default=100, ge=1, le=500),
):
    return {"messages": _chat().messages(conversation_id, limit)}


@app.get("/api/chat/conversations/{conversation_id}/actions")
def chat_actions(
    conversation_id: str,
    limit: int = Query(default=100, ge=1, le=500),
):
    return {"actions": _chat().actions(conversation_id, limit)}


@app.post("/api/chat/actions/{action_id}/confirm")
def chat_confirm_action(action_id: str, request: ChatActionConfirmRequest):
    try:
        return confirm_chat_action(
            _settings(),
            action_id=action_id,
            quantity=request.quantity,
            simulation_mode=request.simulation_mode,
            close_reference_acknowledged=request.close_reference_acknowledged,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=safe_error_detail(exc)) from exc


@app.post("/api/chat/actions/{action_id}/cancel")
def chat_cancel_action(action_id: str):
    try:
        return cancel_chat_action(_settings(), action_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=safe_error_detail(exc)) from exc


@app.get("/api/notifications")
def notifications_list(
    unread_only: bool = False,
    include_archived: bool = False,
    account_id: str | None = None,
    symbol: str | None = None,
    severity: Literal["info", "warning", "critical"] | None = None,
    notification_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=1_000),
):
    return {
        "notifications": _notifications().list(
            unread_only=unread_only,
            include_archived=include_archived,
            account_id=account_id,
            symbol=symbol,
            severity=severity,
            notification_type=notification_type,
            limit=limit,
        )
    }


@app.get("/api/notifications/unread-count")
def notifications_unread_count(account_id: str | None = None):
    return {"unread_count": _notifications().unread_count(account_id)}


@app.post("/api/notifications/{notification_id}/read")
def notifications_mark_read(notification_id: str):
    if not _notifications().mark_read(notification_id):
        raise HTTPException(status_code=404, detail="notification not found")
    return {"status": "read", "notification_id": notification_id}


@app.post("/api/notifications/read-all")
def notifications_mark_all_read(account_id: str | None = None):
    return {"updated": _notifications().mark_all_read(account_id)}


@app.post("/api/notifications/{notification_id}/archive")
def notifications_archive(notification_id: str):
    if not _notifications().archive(notification_id):
        raise HTTPException(status_code=404, detail="notification not found")
    return {"status": "archived", "notification_id": notification_id}


@app.get("/api/notifications/preferences")
def notifications_preferences():
    return {"preferences": _notifications().preferences()}


@app.put("/api/notifications/preferences")
def notifications_update_preferences(request: NotificationPreferencesRequest):
    try:
        return {
            "preferences": _notifications().update_preferences(
                [item.model_dump() for item in request.preferences]
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.delete("/api/chat/conversations/{conversation_id}")
def chat_delete_conversation(conversation_id: str):
    try:
        return _chat().delete_conversation(conversation_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=safe_error_detail(exc)) from exc


@app.post("/api/jobs")
def background_job_submit(request: BackgroundJobRequest):
    try:
        return _jobs().submit(
            job_type=request.job_type,
            payload=request.payload,
            idempotency_key=request.idempotency_key,
            priority=request.priority,
            timeout_seconds=request.timeout_seconds,
            max_attempts=request.max_attempts,
            cost_budget_usd=request.cost_budget_usd,
            dependency_job_ids=request.dependency_job_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.get("/api/jobs")
def background_jobs(
    status: Literal["queued", "running", "cancelled", "completed", "failed"] | None = None,
    job_type: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=1_000),
):
    return {"jobs": _jobs().jobs(status=status, job_type=job_type, limit=limit)}


@app.get("/api/jobs/{job_id}")
def background_job(job_id: str):
    job = _jobs().job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/api/jobs/{job_id}/events")
def background_job_events(
    job_id: str,
    after_event_id: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=5_000),
):
    if _jobs().job(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {"events": _jobs().events(job_id, after_event_id, limit)}


@app.post("/api/jobs/{job_id}/cancel")
def background_job_cancel(job_id: str, request: JobCancelRequest):
    try:
        job = _jobs().cancel(job_id, request.reason)
        if job["job_type"] == "chat_request" and job["payload"].get("user_message_id"):
            _chat().update_message_status(
                job["payload"]["user_message_id"],
                status="cancelled",
                degraded_reason=request.reason,
            )
        return job
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=safe_error_detail(exc)) from exc


@app.post("/api/runtime/worker/run")
def background_worker_run(request: WorkerRunRequest):
    worker = JobWorker(_settings(), worker_id=request.worker_id)
    return {"jobs": worker.run_until_empty(request.maximum_jobs)}


@app.get("/api/runtime/status")
def runtime_status():
    return {**_jobs().runtime_status(), **runtime_health(_settings())}


@app.get("/api/runtime/readiness")
def runtime_readiness():
    return primary_start_readiness(_settings())


@app.post("/api/runtime/soak/observations")
def runtime_soak_observation_create():
    return capture_soak_observation(_settings(), source="api")


@app.get("/api/runtime/soak")
def runtime_soak_report():
    return soak_report(_settings())


@app.get("/api/demo/live/status")
def demo_live_status():
    return live_demo_status(_settings())


@app.post("/api/demo/historical/run")
def demo_historical_run():
    return run_historical_research_demo(_settings())


@app.get("/api/runtime/formal-experiment")
def runtime_formal_experiment():
    return formal_experiment_status(_settings())


@app.post("/api/runtime/trusted-data/refresh")
def runtime_trusted_data_refresh():
    today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()
    return _jobs().submit(
        job_type="trusted_data_refresh",
        payload={"as_of": today.isoformat(), "origin": "operator_api"},
        idempotency_key=f"trusted-data-refresh:{today.isoformat()}",
        concurrency_key="trusted_data_refresh",
        timeout_seconds=1_800,
        max_attempts=3,
    )


@app.post("/api/runtime/schedules/run")
def runtime_schedule_run(request: ScheduleRunRequest):
    scheduler = RuntimeScheduler(_settings())
    try:
        if request.backfill:
            if request.run_date is None:
                raise HTTPException(status_code=422, detail="backfill requires run_date")
            return scheduler.backfill(request.run_date)
        return scheduler.tick(run_date=request.run_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.get("/api/runtime/schedules")
def runtime_schedules():
    scheduler = RuntimeScheduler(_settings())
    scheduler.register_defaults()
    return {
        "schedules": scheduler.repository.schedules(),
        "runs": scheduler.repository.schedule_runs(limit=200),
    }


@app.post("/api/runtime/trading-calendar")
def runtime_trading_calendar_save(request: TradingCalendarBatchRequest):
    payload = [item.model_dump(mode="json") for item in request.items]
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return TradingCalendarService.from_settings(_settings()).ingest(
        payload,
        namespace=DataNamespace.RESEARCH,
        trust_level=DataTrustLevel.USER_IMPORTED,
        provider="public_api",
        source=request.items[0].source,
        endpoint="/api/runtime/trading-calendar",
        source_version=fingerprint[:16],
        available_at=max(item.available_at for item in request.items),
        license_status="user_supplied_unverified",
        raw_fingerprint=fingerprint,
    )


@app.get("/api/runtime/trading-calendar/{trade_date}")
def runtime_trading_calendar_day(trade_date: date):
    return TradingCalendarService.from_settings(_settings()).day(trade_date, formal=False)


@app.post("/api/runtime/backups")
def runtime_backup(request: BackupRequest):
    return backup_database(_settings(), label=request.label)


@app.post("/api/strategy-protocols/round3/register")
def round3_protocol_register():
    return register_round3_protocol(_settings())


@app.get("/api/strategy-protocols/round3")
def round3_protocol_detail():
    registered = StrategyEvidenceRepository(
        _settings().resolve(_settings().get("system.database_path"))
    ).protocol(round3_protocol()["version"])
    return {"protocol": round3_protocol(), "registered": registered}


@app.post("/api/point-in-time/etf-pools")
def point_in_time_etf_pool(request: EtfPointInTimePoolRequest):
    raw_payload = request.model_dump(mode="json")
    manifest = (
        _public_import_manifest(
            "etf_pool",
            raw_payload,
            source="public_api_user_import",
            source_version=request.source_version,
            available_at=max(item.available_at for item in request.trade_statuses),
            record_count=len(request.master_records) + len(request.trade_statuses),
        )
        if request.save
        else None
    )
    snapshot = build_point_in_time_etf_pool(
        snapshot_date=request.snapshot_date,
        cutoff_at=request.cutoff_at,
        master_records=[
            PointInTimeSecurity.model_validate(
                {
                    **item.model_dump(),
                    "namespace": "research",
                    "trust_level": "user_imported",
                    "manifest_id": manifest["manifest_id"] if manifest else None,
                }
            )
            for item in request.master_records
        ],
        trade_statuses=[
            PointInTimeTradeStatus.model_validate(
                {
                    **item.model_dump(),
                    "namespace": "research",
                    "trust_level": "user_imported",
                    "manifest_id": manifest["manifest_id"] if manifest else None,
                }
            )
            for item in request.trade_statuses
        ],
        source_version=request.source_version,
        minimum_amount=request.minimum_amount,
        minimum_fund_size=request.minimum_fund_size,
    )
    snapshot = _research_snapshot(snapshot, manifest["manifest_id"] if manifest else None)
    if request.save:
        return persist_point_in_time_pool(_settings(), snapshot)
    return snapshot.model_dump(mode="json")


@app.post("/api/point-in-time/security-master")
def point_in_time_security_master_save(request: PointInTimeMasterBatchRequest):
    raw_payload = request.model_dump(mode="json")
    manifest = _public_import_manifest(
        "security_master",
        raw_payload,
        source="public_api_user_import",
        source_version=request.master_version,
        available_at=max(item.available_at for item in request.records),
        record_count=len(request.records),
    )
    records = [
        PointInTimeSecurity.model_validate(
            {
                **item.model_dump(),
                "namespace": "research",
                "trust_level": "user_imported",
                "manifest_id": manifest["manifest_id"],
            }
        )
        for item in request.records
    ]
    count = StrategyEvidenceRepository(
        _settings().resolve(_settings().get("system.database_path"))
    ).save_security_master(master_version=request.master_version, records=records)
    return {"master_version": request.master_version, "records_saved": count}


@app.post("/api/point-in-time/trade-status")
def point_in_time_trade_status_save(request: PointInTimeStatusBatchRequest):
    raw_payload = request.model_dump(mode="json")
    manifest = _public_import_manifest(
        "trade_status",
        raw_payload,
        source="public_api_user_import",
        source_version=hashlib.sha256(
            json.dumps(raw_payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16],
        available_at=max(item.available_at for item in request.records),
        record_count=len(request.records),
    )
    records = [
        PointInTimeTradeStatus.model_validate(
            {
                **item.model_dump(),
                "namespace": "research",
                "trust_level": "user_imported",
                "manifest_id": manifest["manifest_id"],
            }
        )
        for item in request.records
    ]
    count = StrategyEvidenceRepository(
        _settings().resolve(_settings().get("system.database_path"))
    ).save_trade_status(security_type=request.security_type, records=records)
    return {"security_type": request.security_type, "records_saved": count}


@app.post("/api/point-in-time/a-share-v4-pools")
def point_in_time_a_share_v4_pool(request: AShareV4PoolRequest):
    correlations: dict[tuple[str, str], float] = {}
    for key, value in request.correlations.items():
        parts = key.split("|")
        if len(parts) != 2:
            raise HTTPException(status_code=422, detail="correlation keys must be SYMBOL|SYMBOL")
        correlations[(parts[0], parts[1])] = value
    snapshot = build_a_share_v4_candidates(
        snapshot_date=request.snapshot_date,
        cutoff_at=request.cutoff_at,
        records=request.records,
        correlations=correlations,
        risk_on=request.risk_on,
        source=request.source,
        source_version=request.source_version,
    )
    manifest = (
        _public_import_manifest(
            "a_share_pool",
            request.model_dump(mode="json"),
            source=request.source,
            source_version=request.source_version,
            available_at=datetime.now(UTC),
            record_count=len(request.records),
        )
        if request.save
        else None
    )
    snapshot = _research_snapshot(snapshot, manifest["manifest_id"] if manifest else None)
    if request.save:
        return persist_point_in_time_pool(_settings(), snapshot)
    return snapshot.model_dump(mode="json")


@app.post("/api/point-in-time/convertible-bond-pools")
def point_in_time_convertible_bond_pool(request: ConvertibleBondPoolRequest):
    raw_payload = request.model_dump(mode="json")
    manifest = (
        _public_import_manifest(
            "convertible_bond_pool",
            raw_payload,
            source="public_api_user_import",
            source_version=request.source_version,
            available_at=max(item.available_at for item in request.trade_statuses),
            record_count=len(request.master_records) + len(request.trade_statuses),
        )
        if request.save
        else None
    )
    snapshot = build_point_in_time_convertible_bond_pool(
        snapshot_date=request.snapshot_date,
        cutoff_at=request.cutoff_at,
        master_records=[
            PointInTimeSecurity.model_validate(
                {
                    **item.model_dump(),
                    "namespace": "research",
                    "trust_level": "user_imported",
                    "manifest_id": manifest["manifest_id"] if manifest else None,
                }
            )
            for item in request.master_records
        ],
        trade_statuses=[
            PointInTimeTradeStatus.model_validate(
                {
                    **item.model_dump(),
                    "namespace": "research",
                    "trust_level": "user_imported",
                    "manifest_id": manifest["manifest_id"] if manifest else None,
                }
            )
            for item in request.trade_statuses
        ],
        source_version=request.source_version,
        minimum_remaining_balance=request.minimum_remaining_balance,
        minimum_amount=request.minimum_amount,
    )
    snapshot = _research_snapshot(snapshot, manifest["manifest_id"] if manifest else None)
    if request.save:
        return persist_point_in_time_pool(_settings(), snapshot)
    return snapshot.model_dump(mode="json")


@app.get("/api/point-in-time/pools/{snapshot_type}/latest")
def point_in_time_latest_pool(
    snapshot_type: Literal["etf", "a_share", "convertible_bond"],
    as_of: date | None = None,
):
    snapshot = StrategyEvidenceRepository(
        _settings().resolve(_settings().get("system.database_path"))
    ).latest_pool_snapshot(snapshot_type, as_of)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="point-in-time pool not found")
    return snapshot


@app.post("/api/forward-ablation/cohorts")
def forward_cohort_create():
    raise HTTPException(
        status_code=403,
        detail=(
            "formal primary cohorts are activated by the current-day scheduler only; "
            "manual symbols belong in /api/forward-ablation/samples as research-only "
            "explorations"
        ),
    )


@app.post("/api/forward-ablation/samples")
def forward_sample_create(request: ForwardSampleRequest):
    try:
        return {
            "manual_exploration": save_manual_forward_exploration(
                _settings(), **request.model_dump(exclude={"cohort_id"})
            ),
            "formal_scorecard_eligible": False,
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.post("/api/forward-ablation/settlements")
def forward_sample_settle(request: ForwardSettlementRequest):
    return _jobs().submit(
        job_type="forward_settlement_scan",
        payload={**request.model_dump(), "limit": 1},
        idempotency_key=(
            f"forward-settlement:{request.cohort_id}:{request.sample_key}:"
            f"{request.horizon_days}"
        ),
        concurrency_key="forward-settlement-scan",
        timeout_seconds=900,
        max_attempts=3,
    )


@app.get("/api/forward-ablation/cohorts/{cohort_id}/scorecard")
def forward_scorecard(cohort_id: str, horizon_days: Literal[5, 20] = 5):
    try:
        return forward_ablation_scorecard(
            _settings(), cohort_id=cohort_id, horizon_days=horizon_days
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=safe_error_detail(exc)) from exc


@app.get("/api/forward-ablation/cohorts/{cohort_id}/accounts/{account_id}/scorecard")
def forward_account_scorecard_api(cohort_id: str, account_id: str):
    try:
        return forward_account_scorecard(
            _settings(), cohort_id=cohort_id, account_id=account_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=safe_error_detail(exc)) from exc


@app.post("/api/forward-experiments/primary/ensure")
def forward_primary_ensure():
    raise HTTPException(
        status_code=403,
        detail=(
            "formal primary experiment activation is scheduler-only; run the current-day "
            "runtime schedule instead"
        ),
    )


@app.get("/api/forward-experiments")
def forward_experiments():
    return {
        "experiments": Round5Repository(
            _settings().resolve(_settings().get("system.database_path"))
        ).experiments()
    }


@app.get("/api/forward-experiments/scorecard")
def forward_primary_scorecard():
    return formal_forward_scorecard(_settings())


@app.post("/api/forward-experiments/registration-jobs")
def forward_registration_job():
    raise HTTPException(
        status_code=403,
        detail=(
            "formal forward registration is scheduler-only; run the current-day runtime "
            "schedule instead"
        ),
    )


@app.post("/api/wide-forward/registration-jobs")
def wide_forward_registration_job():
    raise HTTPException(
        status_code=403,
        detail=(
            "wide forward registration is scheduler-only and historical backfill is "
            "never admitted as forward evidence"
        ),
    )


@app.get("/api/wide-forward/experiments")
def wide_forward_experiments():
    repository = WideResearchRepository(
        _settings().resolve(_settings().get("system.database_path"))
    )
    return {"experiments": repository.experiments()}


@app.get("/api/wide-forward/batches")
def wide_forward_batches(experiment_id: str | None = None):
    repository = WideResearchRepository(
        _settings().resolve(_settings().get("system.database_path"))
    )
    return {"batches": repository.batches(experiment_id)}


@app.get("/api/wide-forward/batches/{batch_id}")
def wide_forward_batch_detail(batch_id: str):
    item = WideResearchRepository(
        _settings().resolve(_settings().get("system.database_path"))
    ).batch(batch_id)
    if item is None:
        raise HTTPException(status_code=404, detail="wide forward batch not found")
    return item


@app.get("/api/wide-forward/scorecard")
def wide_forward_scorecard(
    experiment_id: str | None = None,
    horizon_days: Literal[5, 20] = 5,
):
    repository = WideResearchRepository(
        _settings().resolve(_settings().get("system.database_path"))
    )
    if experiment_id:
        experiment = repository.experiment(experiment_id)
    else:
        from quantlab.workflows.wide_forward import WIDE_PROTOCOL_VERSION

        experiment = repository.experiment_by_protocol_version(WIDE_PROTOCOL_VERSION)
    if experiment is None:
        raise HTTPException(status_code=404, detail="wide forward experiment not found")
    return repository.scorecard(experiment["experiment_id"], horizon_days)


@app.get("/api/research-portfolios")
def research_portfolios(experiment_id: str | None = None):
    repository = WideResearchRepository(
        _settings().resolve(_settings().get("system.database_path"))
    )
    return {
        "portfolios": repository.portfolios(experiment_id),
        "claim_boundary": (
            "Fractional research NAVs are normalized comparisons, not executable A-share "
            "accounts. User paper accounts and seven formal shadow accounts are excluded."
        ),
    }


@app.get("/api/research-portfolios/{portfolio_id}")
def research_portfolio_detail(portfolio_id: str):
    item = WideResearchRepository(
        _settings().resolve(_settings().get("system.database_path"))
    ).portfolio(portfolio_id)
    if item is None:
        raise HTTPException(status_code=404, detail="research portfolio not found")
    return item


@app.get("/api/user-simulator/adoption-outcomes")
def simulator_adoption_outcomes(
    account_id: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
):
    return {
        "outcomes": WideResearchRepository(
            _settings().resolve(_settings().get("system.database_path"))
        ).user_adoption_outcomes(account_id=account_id, limit=limit),
        "formal_forward_scorecard_eligible": False,
    }


@app.get("/api/shadow-accounts")
def shadow_accounts():
    settings = _settings()
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    experiment = repository.primary_experiment()
    return {
        "experiment": experiment,
        "accounts": repository.shadow_accounts(experiment["cohort_id"])
        if experiment
        else [],
        "status": "active" if experiment else "not_started",
    }


@app.get("/api/shadow-accounts/scorecard")
def shadow_accounts_scorecard():
    return shadow_scorecard(_settings())


@app.get("/api/shadow-accounts/{account_id}")
def shadow_account_detail(account_id: str):
    try:
        return Round5Repository(
            _settings().resolve(_settings().get("system.database_path"))
        ).shadow_overview(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=safe_error_detail(exc)) from exc


@app.post("/api/investor-portfolios")
def investor_portfolio_create(request: InvestorPortfolioRequest):
    return create_investor_portfolio(_settings(), **request.model_dump())


@app.get("/api/investor-portfolios")
def investor_portfolios():
    return {
        "portfolios": Round5Repository(
            _settings().resolve(_settings().get("system.database_path"))
        ).investor_portfolios()
    }


@app.get("/api/investor-portfolios/csv-template")
def investor_portfolio_csv_template(import_type: Literal["positions", "trades"]):
    return Response(content=investor_csv_template(import_type), media_type="text/csv")


@app.get("/api/investor-portfolios/{portfolio_id}")
def investor_portfolio_detail(portfolio_id: str):
    try:
        return Round5Repository(
            _settings().resolve(_settings().get("system.database_path"))
        ).investor_overview(portfolio_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=safe_error_detail(exc)) from exc


@app.post("/api/investor-portfolios/{portfolio_id}/imports/preview")
def investor_import_preview(portfolio_id: str, request: InvestorCsvPreviewRequest):
    try:
        return preview_investor_csv(
            _settings(), portfolio_id=portfolio_id, **request.model_dump()
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.post("/api/investor-imports/{import_id}/confirm")
def investor_import_confirm(import_id: str, request: InvestorImportConfirmRequest):
    try:
        return confirm_investor_import(
            _settings(), import_id=import_id, confirm=request.confirm
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.post("/api/investor-portfolios/{portfolio_id}/trades")
def investor_trade_record(portfolio_id: str, request: InvestorTradeRequest):
    try:
        return record_investor_trade(
            _settings(), portfolio_id=portfolio_id, **request.model_dump()
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.post("/api/investor-portfolios/{portfolio_id}/mark-jobs")
def investor_mark_job(portfolio_id: str, as_of: date | None = None):
    resolved = as_of or date.today()
    return _jobs().submit(
        job_type="investor_mark_to_market",
        payload={"portfolio_id": portfolio_id, "as_of": resolved.isoformat()},
        idempotency_key=f"investor-mark:{portfolio_id}:{resolved.isoformat()}",
        concurrency_key=f"investor-portfolio:{portfolio_id}",
    )


@app.post("/api/investor-portfolios/{portfolio_id}/recommendations")
def investor_recommendation_create(
    portfolio_id: str, request: InvestorRecommendationRequest
):
    try:
        return build_investor_recommendation(
            _settings(), portfolio_id=portfolio_id, **request.model_dump()
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.get("/api/investor-portfolios/{portfolio_id}/recommendations")
def investor_recommendations(portfolio_id: str):
    return {
        "recommendations": Round5Repository(
            _settings().resolve(_settings().get("system.database_path"))
        ).recommendations(portfolio_id)
    }


@app.get("/api/investor-portfolios/{portfolio_id}/recommendation-effects")
def investor_portfolio_recommendation_effects(portfolio_id: str):
    return investor_recommendation_effects(_settings(), portfolio_id=portfolio_id)


@app.get("/api/investor-recommendations/{recommendation_id}")
def investor_recommendation_get(recommendation_id: str):
    try:
        return investor_recommendation_detail(
            _settings(), recommendation_id=recommendation_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=safe_error_detail(exc)) from exc


@app.post("/api/investor-recommendations/{recommendation_id}/adoption")
def investor_recommendation_adoption(
    recommendation_id: str, request: InvestorAdoptionRequest
):
    try:
        return record_recommendation_adoption(
            _settings(), recommendation_id=recommendation_id, **request.model_dump()
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.get("/api/investment-theses")
def investment_theses(portfolio_id: str | None = None, status: str | None = None):
    statuses = tuple(item.strip() for item in status.split(",") if item.strip()) if status else None
    return {
        "theses": Round8Repository(
            _settings().resolve(_settings().get("system.database_path"))
        ).theses(portfolio_id=portfolio_id, statuses=statuses)
    }


@app.get("/api/investment-theses/{thesis_id}")
def investment_thesis_detail(thesis_id: str):
    thesis = Round8Repository(
        _settings().resolve(_settings().get("system.database_path"))
    ).thesis(thesis_id)
    if thesis is None:
        raise HTTPException(status_code=404, detail="investment thesis not found")
    return thesis


@app.post("/api/investment-theses/{thesis_id}/checks")
def investment_thesis_check(thesis_id: str, request: InvestmentThesisCheckRequest):
    try:
        return check_investment_thesis(
            _settings(), thesis_id=thesis_id, **request.model_dump()
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.post("/api/investment-theses/{thesis_id}/revisions")
def investment_thesis_revision_create(
    thesis_id: str, request: InvestmentThesisDraftEditRequest
):
    try:
        return edit_investment_thesis_draft(
            _settings(), thesis_id=thesis_id, payload=request.model_dump()
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.post("/api/investment-theses/{thesis_id}/revisions/{revision_id}/freeze")
def investment_thesis_revision_freeze(thesis_id: str, revision_id: str):
    try:
        return freeze_investment_thesis_revision(
            _settings(), thesis_id=thesis_id, revision_id=revision_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.get("/api/experiment-runs/{run_id}")
def experiment_run_detail(run_id: str):
    run = Round8Repository(
        _settings().resolve(_settings().get("system.database_path"))
    ).run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="experiment run not found")
    return run


@app.get("/api/decision-runs/{run_id}/audit-bundle")
def decision_run_audit_export(run_id: str):
    try:
        return Round9Repository(
            _settings().resolve(_settings().get("system.database_path"))
        ).export_decision_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=safe_error_detail(exc)) from exc


@app.get("/api/historical-scorecards")
def historical_scorecards(limit: int = 100):
    return {
        "scorecards": Round9Repository(
            _settings().resolve(_settings().get("system.database_path"))
        ).historical_scorecards(limit=max(1, min(limit, 500)))
    }


@app.get("/api/decision-tasks")
def decision_tasks(status: str | None = None, account_id: str | None = None):
    return Round9Repository(
        _settings().resolve(_settings().get("system.database_path"))
    ).decision_tasks(status=status, account_id=account_id)


@app.patch("/api/decision-tasks/{task_id}")
def decision_task_update(task_id: str, request: DecisionTaskStatusRequest):
    try:
        return Round9Repository(
            _settings().resolve(_settings().get("system.database_path"))
        ).update_task_status(task_id, request.status, reason=request.reason, actor="user")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.get("/api/decision-tasks/{task_id}/events")
def decision_task_events(task_id: str):
    repository = Round9Repository(
        _settings().resolve(_settings().get("system.database_path"))
    )
    if not any(item["task_id"] == task_id for item in repository.decision_tasks(limit=1_000)):
        raise HTTPException(status_code=404, detail="decision task not found")
    return {"events": repository.decision_task_events(task_id)}


@app.post("/api/experiment-runs")
def experiment_run_create(request: ExperimentRunRequest):
    try:
        return ExperimentRecorder(_settings()).start(**request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.post("/api/experiment-runs/{run_id}/artifacts")
def experiment_artifact_create(run_id: str, request: ExperimentArtifactRequest):
    try:
        return ExperimentRecorder(_settings()).artifact(run_id, **request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.get("/api/research-memory/{symbol}")
def research_memory(symbol: str):
    return controlled_research_memory(_settings(), symbol=symbol)


@app.post("/api/runtime/next-trading-day-acceptance")
def runtime_next_trading_day_acceptance(request: NextTradingDayAcceptanceRequest):
    return next_trading_day_acceptance_report(_settings(), trade_date=request.trade_date)


def _submit_strategy_evidence_job(job_type: str, request: StrategyEvidenceRunRequest):
    return _jobs().submit(
        job_type=job_type,
        payload=request.model_dump(mode="json", exclude={"idempotency_key"}),
        idempotency_key=request.idempotency_key,
        concurrency_key=f"strategy-evidence:{job_type}",
        timeout_seconds=7_200,
        max_attempts=2,
    )


@app.post("/api/strategies/a-share-v4/research-jobs")
def a_share_v4_research_job(request: StrategyEvidenceRunRequest):
    return _submit_strategy_evidence_job("a_share_v4_research", request)


@app.post("/api/strategies/convertible-bond/research-jobs")
def convertible_bond_research_job(request: StrategyEvidenceRunRequest):
    return _submit_strategy_evidence_job("convertible_bond_research", request)


@app.post("/api/strategies/etf-point-in-time/replay-jobs")
def etf_point_in_time_replay_job(request: StrategyEvidenceRunRequest):
    return _submit_strategy_evidence_job("etf_pit_replay", request)


@app.post("/api/portfolio/smoothed-rebalance")
def smoothed_rebalance(request: SmoothedRebalanceRequest):
    try:
        policy = BudgetSmoothingPolicy(**request.policy) if request.policy else None
        return plan_smoothed_rebalance(
            nav=request.nav,
            available_cash=request.available_cash,
            current_quantities=request.current_quantities,
            desired_weights=request.desired_weights,
            prices=request.prices,
            sellable_quantities=request.sellable_quantities,
            policy=policy,
            evidence_degraded=request.evidence_degraded,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.put("/api/notifications/channels")
def notification_channel_configure(request: NotificationChannelRequest):
    try:
        return NotificationDeliveryWorker(
            _settings(), worker_id="api-configuration"
        ).configure_channel(**request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc


@app.get("/api/notifications/channels")
def notification_channels(account_id: str | None = None):
    worker = NotificationDeliveryWorker(_settings(), worker_id="api-query")
    return {
        "preferences": worker.preferences(account_id),
        "email_status": worker.channel_status("email", account_id=account_id),
    }


@app.post("/api/notifications/channels/email/test")
def notification_email_test(request: NotificationEmailTestRequest):
    try:
        return NotificationDeliveryWorker(
            _settings(), worker_id="api-test-delivery"
        ).queue_email_test(account_id=request.account_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=safe_error_detail(exc)) from exc
