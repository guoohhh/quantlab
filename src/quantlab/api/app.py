from __future__ import annotations

import hmac
import os
from datetime import date
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from quantlab.api.schemas import (
    AlertRequest,
    CapitalRequest,
    CandidateTournamentRequest,
    CandidateTournamentSettlementRequest,
    DailyCycleRequest,
    EtfVariantResearchRequest,
    HistoricalReplayRequest,
    ManualTradeRequest,
    LLMReplayRequest,
    MarketSymbol,
    MarketEventRequest,
    PortfolioPlanRequest,
    PaperCycleRequest,
    ResearchRequest,
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
)
from quantlab.config import Settings
from quantlab.learning import LearningRepository
from quantlab.llm import provider_configuration_summary, run_llm_replay
from quantlab.persistence import (
    DecisionRepository,
    HistoricalReplayRepository,
    RoundtableRepository,
    StockRankingReplayRepository,
    TerminalRepository,
)
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
)

app = FastAPI(
    title="QuantLab API",
    version="0.9.0",
    description="Auditable multi-agent quantitative research API",
)


@app.middleware("http")
async def optional_api_token_auth(request: Request, call_next):
    expected = os.getenv("QUANTLAB_API_TOKEN")
    if expected and request.url.path.startswith("/api/"):
        supplied = request.headers.get("X-QuantLab-Token", "")
        if not supplied or not hmac.compare_digest(supplied, expected):
            return JSONResponse(status_code=401, content={"detail": "valid API token required"})
    return await call_next(request)


def _settings() -> Settings:
    return Settings.load()


def _terminal() -> TerminalRepository:
    settings = _settings()
    return TerminalRepository(settings.resolve(settings.get("system.database_path")))


@app.get("/api/health")
def health():
    settings = _settings()
    return {
        "status": "ok",
        "version": app.version,
        "llm_provider": settings.get("llm.provider"),
        "api_auth": "required" if os.getenv("QUANTLAB_API_TOKEN") else "disabled_local_only",
        "execution_mode": "manual_orders_only",
    }


@app.get("/api/engine/status")
def engine_status():
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
        "stock_prospective_shadow_accounts": "ready",
        "deterministic_stock_valuation_range": "ready",
        "expert_roundtable": "ready",
        "audit_report_export": "ready",
        "prospective_paper_trading": "ready",
        "benchmark_and_ablation_evidence": "ready",
        "preregistered_strategy_candidate_lab": "ready",
        "daily_cycle": "ready",
        "broker_execution": "disabled",
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


@app.get("/api/multi-agent/analyze")
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
    try:
        output = analyze_symbol(
            settings,
            request.symbol,
            request.as_of,
            asset_type=request.asset_type,
            include_events=request.include_events,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=safe_error_detail(exc)) from exc
    package = build_research_audit_package(output)
    if request.save:
        DecisionRepository(settings.resolve(settings.get("system.database_path"))).save(
            output["decision_run"], research_persistence_context(output)
        )
    return package


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
def stock_universe_snapshot(snapshot_date: date, force: bool = False):
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
