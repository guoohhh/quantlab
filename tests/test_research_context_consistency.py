from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, time, timedelta

import pytest

import quantlab.workflows.chat as chat_workflow
import quantlab.market.quotes as quote_module
from quantlab.config import Settings
from quantlab.domain import (
    AnalysisContextPack,
    AssetType,
    DataQuality,
    EvidenceBlock,
    EvidenceDomain,
    EvidenceQuality,
    MarketQuote,
)
from quantlab.persistence import (
    ChatRepository,
    DecisionRepository,
    EvidenceRepository,
    UserPaperTradingRepository,
)
from quantlab.persistence.migrations import initialize_or_upgrade_database
from quantlab.persistence.jobs import JobRepository
from quantlab.workflows.chat import (
    ChatToolRegistry,
    confirm_chat_action,
    create_chat_conversation,
    handle_chat_message,
)
from quantlab.workflows.chat_jobs import submit_chat_job
from quantlab.workflows.simulator import (
    create_user_paper_account,
    run_pretrade_check,
    settle_user_paper_order,
    user_simulator_repository,
)


def _settings(tmp_path) -> Settings:
    return Settings(
        values={
            "system": {
                "database_path": "quantlab.db",
                "data_dir": "data",
                "test_mode": True,
                "timezone": "Asia/Shanghai",
            },
            "llm": {"provider": "mock"},
            "risk": {
                "max_total_exposure": 0.80,
                "max_single_position": 0.15,
                "max_industry_exposure": 0.30,
            },
            "costs": {
                "stock": {
                    "commission_rate": 0.00025,
                    "minimum_commission": 5.0,
                    "stamp_duty_rate": 0.0005,
                    "transfer_fee_rate": 0.00001,
                    "slippage_bps": 10.0,
                    "stop_slippage_bps": 25.0,
                    "trade_lot": 100,
                },
                "etf": {
                    "commission_rate": 0.0001,
                    "minimum_commission": 5.0,
                    "stamp_duty_rate": 0.0,
                    "transfer_fee_rate": 0.0,
                    "slippage_bps": 5.0,
                    "stop_slippage_bps": 15.0,
                    "trade_lot": 100,
                },
            },
            "strategies": {"etf_rotation": {"universe": ["sh510300"]}},
        },
        root=tmp_path,
    )


def _business_day() -> date:
    value = datetime.now(UTC).date()
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


def _previous_business_day(value: date) -> date:
    value -= timedelta(days=1)
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


def _at(value: date) -> datetime:
    return datetime.combine(value, time(2, 0), tzinfo=UTC)


def _quote(
    symbol: str,
    as_of: date,
    *,
    price: float = 10.0,
    asset_type: AssetType = AssetType.STOCK,
) -> MarketQuote:
    return MarketQuote(
        symbol=symbol,
        name="identity fixture",
        asset_type=asset_type,
        raw_price=price,
        as_of=as_of,
        available_at=_at(as_of),
        source="server_owned_identity_fixture",
        data_quality=DataQuality.AVAILABLE,
        authoritative=False,
        industry="manufacturing",
        trade_lot=100,
        t_plus_one=asset_type == AssetType.STOCK,
        session_status="open",
        risk_metadata={
            "risk_check_complete": True,
            "financial_check_complete": True,
            "financial_quality_score": 0.8,
            "listing_days": 1_000,
        },
    )


def _context_pack(
    symbol: str,
    as_of: date,
    *,
    context_id: str,
    marker: str,
) -> AnalysisContextPack:
    observed_at = _at(as_of)
    block = EvidenceBlock(
        block_id=f"{context_id}-market",
        domain=EvidenceDomain.MARKET,
        title=f"market {marker}",
        source=f"context_{marker}",
        methodology="point-in-time identity fixture",
        as_of=observed_at,
        available_at=observed_at,
        fetched_at=observed_at,
        freshness="fresh",
        quality=EvidenceQuality.AVAILABLE,
        payload={"marker": marker, "current_raw_price": 10.0},
    )
    return AnalysisContextPack(
        context_id=context_id,
        symbol=symbol,
        asset_type=AssetType.STOCK,
        as_of=as_of,
        cutoff_at=observed_at + timedelta(hours=6),
        generated_at=observed_at + timedelta(hours=6),
        blocks=[block],
        critical_gaps=[],
    )


def _seed_research(
    settings: Settings,
    *,
    run_id: str,
    symbol: str,
    as_of: date,
    asset_type: str = "stock",
    evidence_stage: str = "research_only",
    context_id: str | None = None,
    context_fingerprint: str | None = None,
) -> None:
    decision = {
        "symbol": symbol,
        "as_of": as_of.isoformat(),
        "action": "buy",
        "confidence": 0.75,
        "reasons": ["matching point-in-time fixture"],
        "risks": ["fixture risk"],
        "invalidation_conditions": ["identity changes"],
        "degraded_sources": [],
        "requires_human_review": False,
    }
    analysis_context = {
        "symbol": symbol,
        "asset_type": asset_type,
        "as_of": as_of.isoformat(),
    }
    if context_id:
        decision["context_id"] = context_id
        analysis_context["context_id"] = context_id
    if context_fingerprint:
        decision["context_fingerprint"] = context_fingerprint
        analysis_context["fingerprint"] = context_fingerprint
    payload = {
        "decision": decision,
        "reports": {"reviewer": {"approved": True}},
        "research_context": {
            "schema_version": "2.0",
            "report_type": "quantlab_research_audit",
            "generated_at": _at(as_of).isoformat(),
            "data": {
                "effective_as_of": as_of.isoformat(),
                "requested_as_of": as_of.isoformat(),
                "asset_type": asset_type,
                "evidence_stage": evidence_stage,
                "source": "identity_fixture",
            },
            "analysis_context_pack": analysis_context,
            "execution_boundary": "manual_orders_only",
        },
        "research_identity": {
            "run_id": run_id,
            "symbol": symbol,
            "requested_as_of": as_of.isoformat(),
            "effective_as_of": as_of.isoformat(),
            "origin": "test_research" if evidence_stage == "test" else "user_interactive_research",
            "evidence_stage": evidence_stage,
            "context_id": context_id,
            "context_fingerprint": context_fingerprint,
        },
    }
    repository = DecisionRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    with repository.connect() as db:
        db.execute(
            """
            INSERT INTO decision_runs(
                run_id,symbol,as_of,action,confidence,payload,requested_as_of,
                effective_as_of,origin,evidence_stage,settlement_eligible,
                training_eligible,context_id,context_fingerprint
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                symbol,
                as_of.isoformat(),
                "buy",
                0.75,
                json.dumps(payload, ensure_ascii=False),
                as_of.isoformat(),
                as_of.isoformat(),
                "test_research" if evidence_stage == "test" else "user_interactive_research",
                evidence_stage,
                0,
                0,
                context_id,
                context_fingerprint,
            ),
        )


def _account(settings: Settings, key: str) -> dict:
    return create_user_paper_account(
        settings,
        name="research identity account",
        idempotency_key=key,
    )


def _freeze_chat_clock(monkeypatch, value: datetime) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(chat_workflow, "datetime", FrozenDateTime)
    monkeypatch.setattr(quote_module, "datetime", FrozenDateTime)


def test_cross_symbol_run_is_rejected_by_chat_and_pretrade(tmp_path):
    settings = _settings(tmp_path)
    trade_day = _business_day()
    run_id = "research-sh510300"
    _seed_research(
        settings,
        run_id=run_id,
        symbol="sh510300",
        as_of=trade_day,
        asset_type="etf",
    )
    account = _account(settings, "cross-symbol-account")

    with pytest.raises(ValueError, match="symbol.*match|match.*symbol"):
        create_chat_conversation(
            settings,
            title="forged cross-symbol conversation",
            account_id=account["account_id"],
            symbol="sh600519",
            research_run_id=run_id,
            idempotency_key="cross-symbol-conversation",
        )

    conversation = create_chat_conversation(
        settings,
        title="clean symbol conversation",
        account_id=account["account_id"],
        symbol="sh600519",
        idempotency_key="clean-symbol-conversation",
    )
    with pytest.raises(ValueError, match="symbol.*match|match.*symbol"):
        handle_chat_message(
            settings,
            conversation_id=conversation["conversation_id"],
            content="帮我模拟买入100股",
            research_run_id=run_id,
            quote=_quote("sh600519", trade_day),
        )

    with pytest.raises(ValueError, match="symbol.*match|match.*symbol"):
        run_pretrade_check(
            settings,
            account_id=account["account_id"],
            symbol="sh600519",
            side="buy",
            quantity=100,
            quote=_quote("sh600519", trade_day),
            research_run_id=run_id,
            requested_at=_at(trade_day),
        )

    simulator = user_simulator_repository(settings)
    assert simulator.orders(account["account_id"]) == []
    assert ChatRepository(simulator.path).actions(conversation["conversation_id"]) == []


def test_explicit_missing_run_never_falls_back_or_runs_research(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    account = _account(settings, "missing-run-account")
    with pytest.raises(ValueError, match="research run not found"):
        create_chat_conversation(
            settings,
            title="missing research identity",
            account_id=account["account_id"],
            symbol="sh600519",
            research_run_id="does-not-exist",
            idempotency_key="missing-run-bound-conversation",
        )
    conversation = create_chat_conversation(
        settings,
        title="missing research",
        account_id=account["account_id"],
        symbol="sh600519",
        idempotency_key="missing-run-conversation",
    )

    def unexpected_research(*_args, **_kwargs):
        raise AssertionError("explicit missing run_id must not invoke analyze_symbol")

    monkeypatch.setattr(chat_workflow, "analyze_symbol", unexpected_research)
    registry = ChatToolRegistry(settings, conversation)
    with pytest.raises(ValueError, match="research run not found"):
        registry.execute(
            "run_or_reuse_research",
            {
                "symbol": "sh600519",
                "research_run_id": "does-not-exist",
                "asset_type": "stock",
            },
        )

    trade_day = _business_day()
    with pytest.raises(ValueError, match="research run not found"):
        run_pretrade_check(
            settings,
            account_id=account["account_id"],
            symbol="sh600519",
            side="buy",
            quantity=100,
            quote=_quote("sh600519", trade_day),
            research_run_id="does-not-exist",
            requested_at=_at(trade_day),
        )


def test_chat_job_rejects_cross_symbol_run_before_writing_queue_state(tmp_path):
    settings = _settings(tmp_path)
    trade_day = _business_day()
    _seed_research(
        settings,
        run_id="queued-run-a",
        symbol="sh510300",
        as_of=trade_day,
        asset_type="etf",
    )
    account = _account(settings, "queued-cross-symbol-account")
    conversation = create_chat_conversation(
        settings,
        title="queue identity",
        account_id=account["account_id"],
        symbol="sh600519",
        idempotency_key="queue-identity-conversation",
    )

    with pytest.raises(ValueError, match="symbol.*match|match.*symbol"):
        submit_chat_job(
            settings,
            conversation_id=conversation["conversation_id"],
            content="分析并生成深度报告",
            idempotency_key="forged-queue-request",
            research_run_id="queued-run-a",
            allow_research=True,
        )

    database_path = settings.resolve(settings.get("system.database_path"))
    assert ChatRepository(database_path).messages(conversation["conversation_id"]) == []
    assert JobRepository(database_path).jobs(job_type="chat_request") == []


def test_research_later_than_market_quote_fails_closed(tmp_path):
    settings = _settings(tmp_path)
    trade_day = _business_day()
    run_id = "future-research-run"
    _seed_research(
        settings,
        run_id=run_id,
        symbol="sh600519",
        as_of=trade_day + timedelta(days=1),
    )
    account = _account(settings, "future-run-account")

    with pytest.raises(ValueError, match="later than.*market quote|market quote.*later"):
        run_pretrade_check(
            settings,
            account_id=account["account_id"],
            symbol="sh600519",
            side="buy",
            quantity=100,
            quote=_quote("sh600519", trade_day),
            research_run_id=run_id,
            requested_at=_at(trade_day),
        )


@pytest.mark.parametrize(
    ("asset_type", "evidence_stage", "error"),
    [
        ("etf", "research_only", "asset type.*match|match.*asset type"),
        ("stock", "test", "demo or test data|test data"),
    ],
)
def test_asset_mismatch_and_test_research_cannot_be_linked(
    tmp_path,
    asset_type,
    evidence_stage,
    error,
):
    settings = _settings(tmp_path)
    trade_day = _business_day()
    run_id = f"invalid-{asset_type}-{evidence_stage}"
    _seed_research(
        settings,
        run_id=run_id,
        symbol="sh600519",
        as_of=trade_day,
        asset_type=asset_type,
        evidence_stage=evidence_stage,
    )
    account = _account(settings, f"invalid-boundary-{asset_type}-{evidence_stage}")

    with pytest.raises(ValueError, match=error):
        run_pretrade_check(
            settings,
            account_id=account["account_id"],
            symbol="sh600519",
            side="buy",
            quantity=100,
            quote=_quote("sh600519", trade_day),
            asset_type="stock",
            research_run_id=run_id,
            requested_at=_at(trade_day),
        )


def test_no_explicit_research_stays_unlinked_even_when_latest_exists(tmp_path):
    settings = _settings(tmp_path)
    trade_day = _business_day()
    _seed_research(
        settings,
        run_id="latest-but-not-selected",
        symbol="sh600519",
        as_of=trade_day,
    )
    account = _account(settings, "unlinked-account")

    check = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol="sh600519",
        side="buy",
        quantity=100,
        quote=_quote("sh600519", trade_day),
        requested_at=_at(trade_day),
    )

    assert check["hard_risk_passed"] is True
    assert check["allowed_to_submit"] is True
    assert check["research_run_id"] is None
    assert check.get("research_link_status") == "unlinked"
    assert check["reviewer_status"] == "unavailable"
    assert any("unlinked" in item.lower() for item in check["data_gaps"])


def test_explicit_run_builds_pretrade_context_from_exact_research_not_latest(
    tmp_path,
):
    settings = _settings(tmp_path)
    trade_day = _business_day()
    research_day = _previous_business_day(trade_day)
    symbol = "sh600519"
    context_a = _context_pack(
        symbol,
        research_day,
        context_id="research-context-a",
        marker="run_a",
    )
    context_b = _context_pack(
        symbol,
        trade_day,
        context_id="latest-context-b",
        marker="latest_b",
    )
    evidence = EvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    evidence.save_context(context_a)
    evidence.save_context(context_b)
    assert evidence.latest_context(symbol)["context_id"] == context_b.context_id
    _seed_research(
        settings,
        run_id="research-run-a",
        symbol=symbol,
        as_of=research_day,
        context_id=context_a.context_id,
        context_fingerprint=context_a.fingerprint,
    )
    account = _account(settings, "exact-pretrade-context-account")

    check = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol=symbol,
        side="buy",
        quantity=100,
        quote=_quote(symbol, trade_day),
        research_run_id="research-run-a",
        requested_at=_at(trade_day),
    )
    trade_context = evidence.context(check["context_id"])
    assert trade_context is not None
    strategy = next(
        block for block in trade_context["blocks"] if block["domain"] == "strategy"
    )
    assert strategy["payload"]["research_run_id"] == "research-run-a"
    assert strategy["payload"]["research_symbol"] == symbol
    assert strategy["payload"]["source_context_id"] == context_a.context_id
    assert (
        strategy["payload"]["source_context_fingerprint"]
        == context_a.fingerprint
    )
    assert strategy["payload"]["source_context_id"] != context_b.context_id


def test_bound_chat_with_missing_exact_context_never_falls_back_to_latest(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    trade_day = _business_day()
    research_day = _previous_business_day(trade_day)
    symbol = "sh600519"
    latest_b = _context_pack(
        symbol,
        trade_day,
        context_id="chat-latest-context-b",
        marker="chat_latest_b",
    )
    evidence = EvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    evidence.save_context(latest_b)
    _seed_research(
        settings,
        run_id="chat-run-a",
        symbol=symbol,
        as_of=research_day,
        context_id="missing-chat-context-a",
        context_fingerprint="missing-chat-context-a-fingerprint",
    )
    account = _account(settings, "missing-exact-chat-context-account")
    conversation = create_chat_conversation(
        settings,
        title="bound run with missing exact context",
        account_id=account["account_id"],
        symbol=symbol,
        research_run_id="chat-run-a",
        idempotency_key="missing-exact-chat-context-conversation",
    )

    def unexpected_context_answer(*_args, **_kwargs):
        raise AssertionError("bound run A must not consume latest context B")

    monkeypatch.setattr(
        chat_workflow,
        "_answer_context_question",
        unexpected_context_answer,
    )
    response = handle_chat_message(
        settings,
        conversation_id=conversation["conversation_id"],
        content="为什么建议买入，这项结论引用了哪些证据？",
    )

    assert evidence.latest_context(symbol)["context_id"] == latest_b.context_id
    assert response["message"]["status"] == "degraded"
    assert response["message"]["context_id"] is None
    assert response["citations"] == []
    assert response["actions"] == []


def test_bound_chat_registry_tools_use_exact_context_for_primary_symbol(tmp_path):
    settings = _settings(tmp_path)
    trade_day = _business_day()
    research_day = _previous_business_day(trade_day)
    symbol = "sh600519"
    exact_a = _context_pack(
        symbol,
        research_day,
        context_id="registry-exact-context-a",
        marker="registry_a",
    )
    latest_b = _context_pack(
        symbol,
        trade_day,
        context_id="registry-latest-context-b",
        marker="registry_b",
    )
    comparison = _context_pack(
        "sh600001",
        trade_day,
        context_id="registry-comparison-context",
        marker="registry_comparison",
    )
    evidence = EvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    for context in (exact_a, latest_b, comparison):
        evidence.save_context(context)
    _seed_research(
        settings,
        run_id="registry-run-a",
        symbol=symbol,
        as_of=research_day,
        context_id=exact_a.context_id,
        context_fingerprint=exact_a.fingerprint,
    )
    account = _account(settings, "registry-exact-context-account")
    conversation = create_chat_conversation(
        settings,
        title="registry exact context",
        account_id=account["account_id"],
        symbol=symbol,
        research_run_id="registry-run-a",
        idempotency_key="registry-exact-context-conversation",
    )
    registry = ChatToolRegistry(settings, conversation)

    assert registry.execute("query_context_pack", {"symbol": symbol})[
        "context_id"
    ] == exact_a.context_id
    assert registry.execute("query_macro_evidence", {"symbol": symbol})[
        "context_id"
    ] == exact_a.context_id
    assert registry.execute("query_events", {"symbol": symbol})[
        "context_id"
    ] == exact_a.context_id
    compared = registry.execute(
        "compare_contexts",
        {"symbols": [symbol, comparison.symbol]},
    )
    assert [item["context_id"] for item in compared["contexts"]] == [
        exact_a.context_id,
        comparison.context_id,
    ]
    assert evidence.latest_context(symbol)["context_id"] == latest_b.context_id


def test_chat_idempotency_key_cannot_rebind_a_different_research_identity(
    tmp_path,
):
    settings = _settings(tmp_path)
    trade_day = _business_day()
    symbol = "sh600519"
    _seed_research(
        settings,
        run_id="idempotent-run-a",
        symbol=symbol,
        as_of=trade_day,
    )
    _seed_research(
        settings,
        run_id="idempotent-run-b",
        symbol=symbol,
        as_of=trade_day,
    )
    account = _account(settings, "chat-idempotency-identity-account")
    first = create_chat_conversation(
        settings,
        title="identity A",
        account_id=account["account_id"],
        symbol=symbol,
        research_run_id="idempotent-run-a",
        idempotency_key="shared-chat-identity-key",
    )
    assert first["research_run_id"] == "idempotent-run-a"

    with pytest.raises(ValueError, match="idempotency.*different|different.*identity"):
        create_chat_conversation(
            settings,
            title="identity B",
            account_id=account["account_id"],
            symbol=symbol,
            research_run_id="idempotent-run-b",
            idempotency_key="shared-chat-identity-key",
        )


def test_simulator_v6_upgrade_backs_up_before_adding_research_audit_columns(
    tmp_path,
):
    path = tmp_path / "simulator-v5.db"
    UserPaperTradingRepository(path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE user_paper_fills DROP COLUMN research_run_id")
        db.execute("ALTER TABLE user_paper_fills DROP COLUMN context_id")
        db.execute("ALTER TABLE user_paper_order_events DROP COLUMN research_run_id")
        db.execute("ALTER TABLE user_paper_order_events DROP COLUMN context_id")
        db.execute(
            "DELETE FROM quantlab_migration_registry "
            "WHERE component='simulator' AND version=6"
        )
        db.execute("CREATE TABLE user_preserved_research_identity(value TEXT)")
        db.execute(
            "INSERT INTO user_preserved_research_identity(value) VALUES('keep-me')"
        )

    upgraded = initialize_or_upgrade_database(path)

    backup = upgraded["pre_upgrade_backup"]
    assert backup is not None
    assert "simulator:6" in backup["pending_migrations"]
    with sqlite3.connect(backup["database"]) as db:
        assert db.execute(
            "SELECT value FROM user_preserved_research_identity"
        ).fetchone()[0] == "keep-me"
        assert "research_run_id" not in {
            row[1] for row in db.execute("PRAGMA table_info(user_paper_fills)")
        }
        assert "context_id" not in {
            row[1]
            for row in db.execute("PRAGMA table_info(user_paper_order_events)")
        }
    with sqlite3.connect(path) as db:
        fill_columns = {
            row[1] for row in db.execute("PRAGMA table_info(user_paper_fills)")
        }
        event_columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(user_paper_order_events)")
        }
        registry_row = db.execute(
            "SELECT COUNT(*),MAX(checksum) FROM quantlab_migration_registry "
            "WHERE component='simulator' AND version=6"
        ).fetchone()
    assert {"research_run_id", "context_id"} <= fill_columns
    assert {"research_run_id", "context_id"} <= event_columns
    assert registry_row[0] == 1
    assert registry_row[1] == (
        "b9eeea8f5372a43d3be10c3efb8a29dcea71125b459243f8b3b1a58a781edb01"
    )
    assert initialize_or_upgrade_database(path)["pre_upgrade_backup"] is None

    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE quantlab_migration_registry SET checksum='tampered' "
            "WHERE component='simulator' AND version=6"
        )
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        UserPaperTradingRepository(path)


def test_matching_run_survives_chat_pretrade_order_events_and_fill(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    trade_day = _business_day()
    fixed_now = _at(trade_day)
    _freeze_chat_clock(monkeypatch, fixed_now)
    run_id = "matching-research-run"
    symbol = "sh600519"
    _seed_research(
        settings,
        run_id=run_id,
        symbol=symbol,
        as_of=trade_day,
    )
    account = _account(settings, "matching-identity-account")
    conversation = create_chat_conversation(
        settings,
        title="matching identity",
        account_id=account["account_id"],
        symbol=symbol,
        research_run_id=run_id,
        idempotency_key="matching-identity-conversation",
    )
    server_quote = _quote(symbol, trade_day, price=10.0)

    response = handle_chat_message(
        settings,
        conversation_id=conversation["conversation_id"],
        content="帮我模拟买入100股",
        quote=server_quote,
    )
    action = response["actions"][0]
    draft_check = response["message"]["payload"]["pretrade_check"]
    simulator = user_simulator_repository(settings)
    assert action["status"] == "confirmation_required"
    assert action["research_run_id"] == run_id
    assert draft_check["research_run_id"] == run_id
    assert simulator.orders(account["account_id"]) == []

    with pytest.raises(ValueError, match="quantity does not match"):
        confirm_chat_action(
            settings,
            action_id=action["action_id"],
            quantity=200,
        )
    assert simulator.orders(account["account_id"]) == []

    forged_caller_quote = _quote(symbol, trade_day, price=999.0)
    confirmed = confirm_chat_action(
        settings,
        action_id=action["action_id"],
        quantity=100,
        quote=forged_caller_quote,
        simulation_mode="intraday_simulation",
    )
    order = simulator.order(confirmed["order_id"])
    assert order is not None
    assert confirmed["status"] == "confirmed"
    assert order["status"] == "pending"
    assert order["research_run_id"] == run_id
    assert order["reference_price"] == server_quote.raw_price
    assert order["check_id"] != draft_check["check_id"]
    assert order["payload"]["pretrade_check"]["research_run_id"] == run_id

    submitted_events = simulator.order_events(order["order_id"])
    assert [item["event_type"] for item in submitted_events] == ["submitted"]
    assert submitted_events[0]["payload"]["research_run_id"] == run_id

    settled = settle_user_paper_order(
        settings,
        order_id=order["order_id"],
        quote=server_quote,
        fill_key="matching-identity-fill",
    )
    assert settled["order"]["status"] == "filled"
    assert settled["order"]["research_run_id"] == run_id
    assert settled["fill"]["payload"]["research_run_id"] == run_id

    final_events = simulator.order_events(order["order_id"])
    assert [item["event_type"] for item in final_events] == [
        "submitted",
        "filled",
    ]
    assert all(item["payload"]["research_run_id"] == run_id for item in final_events)
    decision = simulator.pretrade_check(order["check_id"])
    assert decision is not None
    assert decision["research_run_id"] == run_id
    assert decision["check_payload"]["symbol"] == symbol
    assert decision["check_payload"]["research_run_id"] == run_id
