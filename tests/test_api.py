import asyncio
import importlib

import httpx

from quantlab.api.app import app

api_module = importlib.import_module("quantlab.api.app")
FAKE_KEY = "sk-" + "abcdefghijklmnop"


def _get(path, headers=None):
    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, headers=headers)

    return asyncio.run(request())


def _post(path, payload, headers=None):
    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json=payload, headers=headers)

    return asyncio.run(request())


def test_health_discloses_manual_execution_boundary():
    response = _get("/api/health")

    assert response.status_code == 200
    assert response.json()["execution_mode"] == "manual_orders_only"
    assert response.json()["api_auth"] == "disabled_local_only"


def test_backtest_strategy_catalog_contains_three_core_strategies():
    response = _get("/api/backtest/strategies")

    assert response.status_code == 200
    strategies = set(response.json()["strategies"])
    assert {
        "etf_rotation",
        "stock_reversal",
        "convertible_bond_double_low",
    } <= strategies


def test_market_radar_endpoint_uses_auditable_workflow(monkeypatch):
    monkeypatch.setattr(
        api_module,
        "build_market_radar",
        lambda settings, as_of, include_sectors, sector_limit: {
            "as_of": "2026-01-02",
            "market_regime": "range",
            "risk_appetite": "neutral",
            "instruments": [],
            "degraded_sources": [],
        },
    )

    response = _get("/api/market/radar")

    assert response.status_code == 200
    assert response.json()["risk_appetite"] == "neutral"


def test_adaptive_strategy_lab_endpoint_remains_research_only(monkeypatch):
    monkeypatch.setattr(
        api_module,
        "run_adaptive_etf_candidate_lab",
        lambda settings, save: {
            "status": "historical_holdout_rejected",
            "research_only": True,
            "formal_strategy_changed": False,
            "saved": save,
        },
    )

    response = _post("/api/backtest/adaptive-etf-lab?save=false", {})

    assert response.status_code == 200
    assert response.json()["research_only"] is True
    assert response.json()["formal_strategy_changed"] is False
    assert response.json()["saved"] is False


def test_candidate_tournament_endpoint_runs_cross_symbol_workflow(monkeypatch):
    monkeypatch.setattr(
        api_module,
        "run_candidate_tournament",
        lambda settings, as_of, **kwargs: {
            "as_of": "2026-07-13",
            "candidates": [{"symbol": "sh510300", "tournament_rank": 1}],
            "diversified_shortlist": [{"symbol": "sh510300"}],
            "comparison_portfolio": {"hypothetical_only": True},
        },
    )

    response = _post(
        "/api/tournament",
        {"candidate_limit": 2, "shortlist_size": 1, "save": False},
    )

    assert response.status_code == 200
    assert response.json()["candidates"][0]["tournament_rank"] == 1
    assert response.json()["comparison_portfolio"]["hypothetical_only"] is True


def test_candidate_tournament_endpoint_rejects_shortlist_larger_than_field():
    response = _post(
        "/api/tournament",
        {"candidate_limit": 2, "shortlist_size": 3, "save": False},
    )

    assert response.status_code == 422


def test_candidate_tournament_settlement_and_scorecard_endpoints(monkeypatch):
    monkeypatch.setattr(
        api_module,
        "settle_candidate_tournaments",
        lambda settings, as_of, limit: {"settled": [{"tournament_id": 1}], "pending": []},
    )
    monkeypatch.setattr(
        api_module,
        "candidate_tournament_scorecard",
        lambda settings, limit: {"tournaments": 1, "horizons": {"20": {"samples": 1}}},
    )

    settlement = _post("/api/tournaments/settle", {"limit": 5})
    scorecard = _get("/api/tournaments/scorecard?limit=5")

    assert settlement.status_code == 200
    assert settlement.json()["settled"][0]["tournament_id"] == 1
    assert scorecard.status_code == 200
    assert scorecard.json()["horizons"]["20"]["samples"] == 1


def test_stock_discovery_search_screen_recommend_and_batch_endpoints(monkeypatch):
    monkeypatch.setattr(
        api_module,
        "search_stocks",
        lambda settings, keyword, limit: {
            "keyword": keyword,
            "results": [{"symbol": "sh600519"}],
        },
    )
    monkeypatch.setattr(
        api_module,
        "screen_selected_stocks",
        lambda settings, symbols, as_of, **kwargs: {
            "candidates": [{"symbol": "sh600519", "screen_rank": 1}],
            "diversified_shortlist": [{"symbol": "sh600519"}],
        },
    )
    monkeypatch.setattr(
        api_module,
        "recommend_stocks",
        lambda settings, as_of, **kwargs: {
            "run_type": "system_recommendation",
            "candidates": [{"symbol": "sh600519"}],
        },
    )
    monkeypatch.setattr(
        api_module,
        "run_stock_research_batch",
        lambda settings, symbols, as_of, **kwargs: {
            "summaries": [{"symbol": "sh600519", "action": "watch"}],
            "analyses": [],
        },
    )

    search = _get("/api/market/search?keyword=600519")
    screen = _post("/api/stocks/screen", {"symbols": ["600519"], "save": False})
    recommendation = _post(
        "/api/stocks/recommend",
        {"styles": ["value_quality"], "candidate_limit": 5, "top_n": 3, "save": False},
    )
    batch = _post(
        "/api/stocks/research-batch",
        {"symbols": ["600519"], "include_events": False, "save": False},
    )
    compatibility = _get("/api/multi-agent/momentum-batch-screen?codes=600519,000858")

    assert search.status_code == 200
    assert search.json()["results"][0]["symbol"] == "sh600519"
    assert screen.status_code == 200
    assert screen.json()["candidates"][0]["screen_rank"] == 1
    assert recommendation.status_code == 200
    assert recommendation.json()["run_type"] == "system_recommendation"
    assert batch.status_code == 200
    assert batch.json()["summaries"][0]["action"] == "watch"
    assert compatibility.status_code == 200


def test_paper_cycle_endpoint_preserves_next_open_contract(monkeypatch):
    monkeypatch.setattr(
        api_module,
        "run_paper_cycle",
        lambda settings, as_of, run_research, research_limit: {
            "as_of": "2026-01-02",
            "fills": [],
            "queued_orders": [{"status": "pending_next_open"}],
        },
    )

    response = _post("/api/paper/cycle", {"run_research": False})

    assert response.status_code == 200
    assert response.json()["queued_orders"][0]["status"] == "pending_next_open"


def test_evidence_endpoint_rejects_unknown_asset_scope():
    response = _get("/api/evidence?asset_scope=crypto")

    assert response.status_code == 422


def test_public_symbol_inputs_reject_unexpected_formats_before_workflow_execution():
    query = _get("/api/engine/quant?code=../../secret")
    body = _post(
        "/api/research/analyze",
        {"symbol": "BTCUSDT", "asset_type": "etf", "save": False},
    )

    assert query.status_code == 422
    assert body.status_code == 422


def test_api_error_detail_redacts_keys_and_local_paths(monkeypatch):
    monkeypatch.setattr(
        api_module,
        "load_quant_report",
        lambda settings, code, as_of: (_ for _ in ()).throw(
            RuntimeError(f"upstream {FAKE_KEY} at E:\\private\\config.env")
        ),
    )

    response = _get("/api/engine/quant?code=sh510300")
    detail = response.json()["detail"]

    assert response.status_code == 502
    assert FAKE_KEY not in detail
    assert "E:\\private" not in detail
    assert "[REDACTED_API_KEY]" in detail
    assert "[REDACTED_PATH]" in detail


def test_historical_replay_api_allows_measured_scale_but_caps_abuse():
    above_hard_cap = _post(
        "/api/replay/historical",
        {
            "start": "2020-01-01",
            "end": "2025-01-01",
            "horizon_days": 20,
            "episodes": 61,
            "save": False,
        },
    )
    unconfirmed_large_run = _post(
        "/api/replay/historical",
        {
            "start": "2020-01-01",
            "end": "2025-01-01",
            "horizon_days": 20,
            "episodes": 13,
            "save": False,
        },
    )

    assert above_hard_cap.status_code == 422
    assert unconfirmed_large_run.status_code == 422
    assert "allow_large_run" in unconfirmed_large_run.json()["detail"]


def test_optional_api_token_protects_all_api_routes(monkeypatch):
    monkeypatch.setenv("QUANTLAB_API_TOKEN", "test-secret-token")

    rejected = _get("/api/health")
    accepted = _get("/api/health", headers={"X-QuantLab-Token": "test-secret-token"})

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["api_auth"] == "required"
    assert "test-secret-token" not in accepted.text
