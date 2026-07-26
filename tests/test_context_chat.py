from __future__ import annotations

import json
from datetime import UTC, date, datetime

from pydantic import BaseModel

import quantlab.workflows.chat as chat_workflow
from quantlab.config import Settings
from quantlab.domain import (
    AnalysisContextPack,
    AssetType,
    ChatEvidenceAnswer,
    EvidenceBlock,
    EvidenceDomain,
    EvidenceQuality,
)
from quantlab.llm.providers import LLMProvider
from quantlab.persistence import DecisionRepository, EvidenceRepository
from quantlab.workflows.chat import (
    ChatToolRegistry,
    create_chat_conversation,
    handle_chat_message,
)
from quantlab.workflows.simulator import create_user_paper_account


def _settings(tmp_path) -> Settings:
    return Settings(
        values={
            "system": {"database_path": "quantlab.db", "data_dir": "data"},
            "llm": {
                "provider": "mock",
                "task_maximum_calls": 5,
                "task_token_budget": 20_000,
                "task_cost_budget_usd": 1,
                "context_maximum_bytes": 20_000,
            },
            "risk": {
                "max_total_exposure": 0.8,
                "max_single_position": 0.15,
                "max_industry_exposure": 0.3,
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
                }
            },
            "strategies": {"etf_rotation": {"universe": []}},
        },
        root=tmp_path,
    )


def _pack(symbol: str, context_id: str, price: float) -> AnalysisContextPack:
    at = datetime(2026, 7, 17, 7, 0, tzinfo=UTC)

    def block(domain: EvidenceDomain, payload: dict):
        return EvidenceBlock(
            block_id=f"{context_id}-{domain.value}",
            domain=domain,
            title=domain.value,
            source=f"fixture-{domain.value}",
            methodology="point-in-time fixture",
            as_of=at,
            available_at=at,
            fetched_at=at,
            freshness="fresh",
            quality=EvidenceQuality.AVAILABLE,
            payload=payload,
        )

    return AnalysisContextPack(
        context_id=context_id,
        symbol=symbol,
        asset_type=AssetType.STOCK,
        as_of=date(2026, 7, 17),
        cutoff_at=datetime(2026, 7, 17, 15, 59, tzinfo=UTC),
        blocks=[
            block(EvidenceDomain.MARKET, {"current_raw_price": price}),
            block(EvidenceDomain.TECHNICAL, {"trend": "up"}),
            block(
                EvidenceDomain.CAPITAL_FLOW,
                {"scope": "stock", "scope_key": symbol, "flow_trend": {"5": 1}},
            ),
            block(EvidenceDomain.FINANCIAL, {"revenue_yoy": 0.1}),
            block(EvidenceDomain.EVENT, {"events": []}),
            block(EvidenceDomain.PORTFOLIO, {"cash": 100_000}),
            block(EvidenceDomain.STRATEGY, {"action": "watch"}),
        ],
        critical_gaps=[],
    )


class ChatProvider(LLMProvider):
    provider_name = "chat-fixture"
    model = "chat-fixture-model"

    async def structured(self, system: str, prompt: str, schema: type[BaseModel]):
        assert schema is ChatEvidenceAnswer
        payload = json.loads(prompt)
        blocks = payload["contexts"][0]["blocks"]
        return ChatEvidenceAnswer(
            answer="事实、量化结果和模型判断已经分开；最终决定仍由用户确认。",
            facts=["行情来自ContextPack"],
            quantitative_results=["趋势为上行"],
            llm_judgments=["证据偏正面但仍有风险"],
            user_assumptions=[],
            evidence_refs=[blocks[0]["block_id"], blocks[1]["block_id"]],
            suggested_action="buy",
            suggested_weight_min=0.05,
            suggested_weight_max=0.8,
            invalidation_conditions=["趋势转弱"],
            requires_user_review=False,
        )


def test_chat_uses_context_pack_citations_and_bounded_weight(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    account = create_user_paper_account(
        settings,
        name="Context Chat账户",
        idempotency_key="context-chat-account",
    )
    pack = _pack("sh600001", "context-chat-1", 10.0)
    EvidenceRepository(tmp_path / "quantlab.db").save_context(pack)
    conversation = create_chat_conversation(
        settings,
        title="Context Chat",
        account_id=account["account_id"],
        symbol="sh600001",
        idempotency_key="context-chat-conversation",
    )
    monkeypatch.setattr(chat_workflow, "build_provider", lambda _settings: ChatProvider())
    response = handle_chat_message(
        settings,
        conversation_id=conversation["conversation_id"],
        content="为什么建议买入，这项结论引用了哪些证据？",
    )
    message = response["message"]
    answer = message["payload"]["answer"]
    assert message["context_id"] == pack.context_id
    assert message["context_version"] == "2.0"
    assert answer["facts"]
    assert answer["llm_judgments"]
    assert answer["suggested_weight_max"] == 0.15
    assert {item["source"] for item in response["citations"]} == {
        "fixture-market",
        "fixture-technical",
    }
    assert all(item["available_at"] <= pack.cutoff_at.isoformat() for item in response["citations"])


def test_chat_compares_multiple_symbols_and_uses_frozen_report_context(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    account = create_user_paper_account(
        settings,
        name="比较账户",
        idempotency_key="compare-chat-account",
    )
    first = _pack("sh600001", "frozen-context", 10.0)
    latest = _pack("sh600001", "latest-context", 11.0)
    second = _pack("sz000002", "second-context", 20.0)
    repository = EvidenceRepository(tmp_path / "quantlab.db")
    repository.save_context(first)
    repository.save_context(latest)
    repository.save_context(second)
    with DecisionRepository(tmp_path / "quantlab.db").connect() as db:
        db.execute(
            """
            INSERT INTO decision_runs(
                run_id,symbol,as_of,action,confidence,payload,requested_as_of,
                effective_as_of,origin,evidence_stage,settlement_eligible,
                training_eligible,context_id,context_fingerprint
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "frozen-run",
                "sh600001",
                "2026-07-17",
                "watch",
                0.5,
                json.dumps(
                    {
                        "decision": {
                            "symbol": "sh600001",
                            "as_of": "2026-07-17",
                            "context_id": first.context_id,
                            "context_fingerprint": first.fingerprint,
                        },
                        "research_context": {
                            "analysis_context_pack": first.model_dump(mode="json")
                        },
                        "research_identity": {
                            "run_id": "frozen-run",
                            "symbol": "sh600001",
                            "requested_as_of": "2026-07-17",
                            "effective_as_of": "2026-07-17",
                            "origin": "user_interactive_research",
                            "evidence_stage": "research_only",
                        },
                    }
                ),
                "2026-07-17",
                "2026-07-17",
                "user_interactive_research",
                "research_only",
                0,
                0,
                first.context_id,
                first.fingerprint,
            ),
        )
    conversation = create_chat_conversation(
        settings,
        title="冻结研究追问",
        account_id=account["account_id"],
        symbol="sh600001",
        research_run_id="frozen-run",
        idempotency_key="frozen-chat-conversation",
    )
    monkeypatch.setattr(chat_workflow, "build_provider", lambda _settings: ChatProvider())
    frozen_response = handle_chat_message(
        settings,
        conversation_id=conversation["conversation_id"],
        content="原始投资逻辑是否失效？",
    )
    assert frozen_response["message"]["context_id"] == first.context_id

    comparison = handle_chat_message(
        settings,
        conversation_id=conversation["conversation_id"],
        content="比较 sh600001 和 sz000002 的证据和风险",
    )
    assert set(comparison["message"]["payload"]["context_ids"]) == {
        first.context_id,
        second.context_id,
    }
    assert {item["symbol"] for item in comparison["citations"]} <= {
        "sh600001",
        "sz000002",
    }


def test_chat_without_context_does_not_fabricate_and_registry_declares_policy(tmp_path):
    settings = _settings(tmp_path)
    account = create_user_paper_account(
        settings,
        name="无数据账户",
        idempotency_key="missing-context-account",
    )
    conversation = create_chat_conversation(
        settings,
        title="无数据",
        account_id=account["account_id"],
        symbol="sh600001",
        idempotency_key="missing-context-conversation",
    )
    response = handle_chat_message(
        settings,
        conversation_id=conversation["conversation_id"],
        content="当前资金和财务证据是什么？",
    )
    assert response["message"]["status"] == "degraded"
    assert response["citations"] == []
    assert "无法确认" in response["message"]["content"]

    registry = ChatToolRegistry(settings, conversation)
    catalog = {item["name"]: item for item in registry.catalog()}
    assert catalog["query_market_flow"]["read_only"] is True
    assert catalog["query_market_flow"]["data_domains"] == ["capital_flow", "market"]
    assert catalog["create_flow_notification_rule"]["confirmation_required"] is True
    assert catalog["create_flow_notification_rule"]["input_schema"]
