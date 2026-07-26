from __future__ import annotations

import sqlite3

from quantlab.api.app import app
from quantlab.api.schemas import (
    ChatActionConfirmRequest,
    ForwardSampleRequest,
    ForwardSettlementRequest,
    InvestorAdoptionRequest,
    RoleChallengeDecisionRequest,
    UserPreTradeRequest,
)
from quantlab.config import Settings
from quantlab.persistence.migrations import COMPONENT_ORDER, initialize_or_upgrade_database
from quantlab.runtime.worker import default_job_handlers
from quantlab.workflows.chat import ChatToolRegistry


def _settings(tmp_path) -> Settings:
    return Settings(
        values={
            "system": {
                "database_path": "quantlab.db",
                "data_dir": "data",
                "test_mode": True,
            },
            "llm": {"maximum_task_cost_usd": 1.0},
        },
        root=tmp_path,
    )


def test_rounds_one_to_six_required_database_contract(tmp_path):
    path = tmp_path / "quantlab.db"
    status = initialize_or_upgrade_database(path)
    with sqlite3.connect(path) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    required = {
        # Round 1: simulator, Chat and notification foundation.
        "user_paper_accounts",
        "user_paper_orders",
        "user_paper_fills",
        "user_paper_positions",
        "user_paper_equity_snapshots",
        "user_trade_decision_links",
        "user_trade_reviews",
        "chat_conversations",
        "chat_messages",
        "chat_tool_calls",
        "chat_citations",
        "chat_action_drafts",
        "chat_conversation_summaries",
        "notifications",
        "notification_preferences",
        "notification_events",
        "notification_outbox",
        "notification_delivery_attempts",
        # Round 2: context, capital flow and LLM governance.
        "analysis_context_packs",
        "capital_flow_snapshots",
        "industry_membership_history",
        "llm_governed_calls",
        "llm_role_observations",
        "llm_role_challenges",
        "llm_role_policies",
        "notification_rules",
        # Round 3/4: point-in-time evidence and runtime platform.
        "strategy_protocols",
        "pit_security_master",
        "pit_trade_status",
        "pit_pool_snapshots",
        "pit_pool_members",
        "strategy_research_runs",
        "forward_ablation_cohorts",
        "forward_ablation_predictions",
        "forward_ablation_outcomes",
        "forward_settlement_attempts",
        "background_jobs",
        "background_job_dependencies",
        "background_job_events",
        "runtime_schedules",
        "runtime_schedule_runs",
        "trading_calendar",
        "runtime_failures",
        "api_audit_log",
        "quantlab_migration_registry",
        # Round 5: scientific forward experiment and read-only investor ledgers.
        "trusted_data_manifests",
        "trusted_calendar_days",
        "trusted_industry_membership",
        "forward_experiment_protocols",
        "primary_cohort_governance",
        "forward_registration_runs",
        "forward_registration_samples",
        "forward_milestone_scorecards",
        "manual_forward_explorations",
        "shadow_accounts",
        "shadow_orders",
        "shadow_fills",
        "shadow_positions",
        "shadow_nav",
        "shadow_events",
        "investor_portfolios",
        "investor_imports",
        "investor_import_rows",
        "investor_positions",
        "investor_trades",
        "investor_nav",
        "investor_recommendations",
        "investor_recommendation_adoptions",
        "investor_recommendation_outcomes",
        "runtime_processes",
        "trusted_data_source_state",
        "product_usage_events",
    }
    assert required <= tables
    assert status["component_order"] == list(COMPONENT_ORDER)


def test_rounds_one_to_six_required_api_contract():
    routes = {
        (route.path, method)
        for route in app.routes
        for method in (getattr(route, "methods", None) or set())
    }
    required = {
        ("/api/simulator/accounts", "POST"),
        ("/api/simulator/pretrade-check", "POST"),
        ("/api/simulator/orders", "POST"),
        ("/api/simulator/orders/{order_id}/settle", "POST"),
        ("/api/chat/conversations", "POST"),
        ("/api/chat/conversations/{conversation_id}/messages", "POST"),
        ("/api/chat/actions/{action_id}/confirm", "POST"),
        ("/api/notifications", "GET"),
        ("/api/context-packs", "POST"),
        ("/api/capital-flow/refresh-jobs", "POST"),
        ("/api/llm/context-committee", "POST"),
        ("/api/jobs", "POST"),
        ("/api/jobs/{job_id}/cancel", "POST"),
        ("/api/runtime/status", "GET"),
        ("/api/runtime/readiness", "GET"),
        ("/api/runtime/formal-experiment", "GET"),
        ("/api/runtime/trusted-data/refresh", "POST"),
        ("/api/point-in-time/etf-pools", "POST"),
        ("/api/forward-ablation/samples", "POST"),
        ("/api/forward-ablation/settlements", "POST"),
        ("/api/forward-experiments/primary/ensure", "POST"),
        ("/api/forward-experiments", "GET"),
        ("/api/forward-experiments/scorecard", "GET"),
        ("/api/forward-experiments/registration-jobs", "POST"),
        ("/api/shadow-accounts", "GET"),
        ("/api/shadow-accounts/scorecard", "GET"),
        ("/api/shadow-accounts/{account_id}", "GET"),
        ("/api/investor-portfolios", "POST"),
        ("/api/investor-portfolios", "GET"),
        ("/api/investor-portfolios/{portfolio_id}", "GET"),
        ("/api/investor-portfolios/{portfolio_id}/imports/preview", "POST"),
        ("/api/investor-imports/{import_id}/confirm", "POST"),
        ("/api/investor-portfolios/{portfolio_id}/trades", "POST"),
        ("/api/investor-portfolios/{portfolio_id}/recommendations", "POST"),
        ("/api/investor-recommendations/{recommendation_id}/adoption", "POST"),
        ("/api/investment-theses", "GET"),
        ("/api/investment-theses/{thesis_id}", "GET"),
        ("/api/investment-theses/{thesis_id}/checks", "POST"),
        ("/api/experiment-runs", "POST"),
        ("/api/experiment-runs/{run_id}", "GET"),
        ("/api/experiment-runs/{run_id}/artifacts", "POST"),
        ("/api/research-memory/{symbol}", "GET"),
        ("/api/runtime/next-trading-day-acceptance", "POST"),
    }
    assert required <= routes
    assert not any(path.endswith("/restore") for path, _method in routes)


def test_chat_tool_registry_is_complete_and_auditable(tmp_path):
    registry = ChatToolRegistry(_settings(tmp_path), {"account_id": "bound-account"})
    catalog = registry.catalog()
    by_name = {item["name"]: item for item in catalog}
    required = {
        "query_account",
        "query_positions",
        "query_orders_and_fills",
        "query_performance",
        "query_latest_quote",
        "query_research",
        "run_or_reuse_research",
        "query_reviewer",
        "query_constraints",
        "query_notifications",
        "query_context_pack",
        "query_market_flow",
        "query_industry_flow",
        "query_stock_flow",
        "query_macro_evidence",
        "query_events",
        "compare_contexts",
        "query_decision_history",
        "query_role_performance",
        "query_investment_theses",
        "query_investment_thesis",
        "query_research_memory",
        "run_pretrade_check",
        "create_price_alert",
        "create_flow_notification_rule",
        "mark_notification_read",
    }
    assert required <= set(by_name)
    for item in catalog:
        assert item["permission"] in {"read", "controlled_write"}
        assert item["input_schema"]
        assert item["timeout_seconds"] > 0
        assert item["cost_budget_usd"] >= 0
        assert item["data_domains"]
        assert item["read_only"] is (item["permission"] == "read")
        assert item["confirmation_required"] is (
            item["permission"] == "controlled_write"
        )


def test_runtime_handlers_and_public_trust_schemas_are_frozen(tmp_path):
    handlers = default_job_handlers(_settings(tmp_path))
    assert {
        "research",
        "historical_replay",
        "capital_flow_refresh",
        "training",
        "simulator_settlement",
        "daily_cycle",
        "notification_dispatch",
        "premarket_digest",
        "account_daily_report",
        "forward_settlement_scan",
        "mark_to_market",
        "a_share_v4_research",
        "convertible_bond_research",
        "etf_pit_replay",
        "retention_cleanup",
        "database_backup",
        "chat_request",
        "trusted_data_refresh",
        "forward_sample_registration",
        "shadow_account_cycle",
        "investor_mark_to_market",
        "investor_outcome_settlement",
    } <= set(handlers)

    assert set(UserPreTradeRequest.model_fields) == {
        "account_id",
        "symbol",
        "side",
        "quantity",
        "amount",
        "research_run_id",
        "user_context",
    }
    assert set(ChatActionConfirmRequest.model_fields) == {
        "quantity",
        "simulation_mode",
        "close_reference_acknowledged",
    }
    assert set(ForwardSampleRequest.model_fields) == {
        "cohort_id",
        "symbol",
        "account_id",
        "horizon_days",
    }
    assert set(ForwardSettlementRequest.model_fields) == {
        "cohort_id",
        "sample_key",
        "horizon_days",
    }
    assert "applicable_regimes" in RoleChallengeDecisionRequest.model_fields
    assert set(InvestorAdoptionRequest.model_fields) == {
        "decision",
        "trade_side",
        "actual_quantity",
        "actual_price",
        "actual_trade_date",
        "transaction_cost",
        "note",
    }
