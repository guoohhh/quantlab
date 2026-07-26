from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Lock

import pytest
from pydantic import BaseModel

from quantlab.config import Settings
from quantlab.domain import (
    AnalysisContextPack,
    AssetType,
    ChatEvidenceAnswer,
    CommitteeDecision,
    CommitteeRoleOpinion,
    EvidenceBlock,
    EvidenceDomain,
    EvidenceQuality,
)
from quantlab.llm.governance import GovernedLLMProvider, LLMBudgetExceeded, LLMTaskBudget
from quantlab.llm.providers import LLMProvider
from quantlab.persistence import EvidenceRepository, NotificationRepository
from quantlab.workflows.capital_flow import (
    SIGNED_TURNOVER_METHODOLOGY,
    calculate_industry_flow,
    calculate_market_flow,
    calculate_stock_flow,
)
from quantlab.workflows.context import assemble_analysis_context_pack
from quantlab.workflows.llm_committee import run_context_committee_with_provider
from quantlab.workflows.notification_rules import evaluate_flow_notification_rules
from quantlab.workflows.role_governance import (
    decide_role_challenge,
    freeze_role_challenge,
    record_role_outcome,
    role_scorecard,
)


def _settings(tmp_path, **llm_overrides) -> Settings:
    return Settings(
        values={
            "system": {"database_path": "quantlab.db", "data_dir": "data"},
            "llm": {
                "role_minimum_matured_samples": 2,
                "maximum_committee_roles": 6,
                "maximum_committee_rounds": 1,
                **llm_overrides,
            },
            "risk": {
                "max_total_exposure": 0.8,
                "max_single_position": 0.15,
                "max_industry_exposure": 0.3,
            },
            "strategies": {"etf_rotation": {"universe": []}},
        },
        root=tmp_path,
    )


def _block(
    domain: EvidenceDomain,
    *,
    as_of: date = date(2026, 7, 17),
    payload: dict | None = None,
    quality: EvidenceQuality = EvidenceQuality.AVAILABLE,
) -> EvidenceBlock:
    observed = datetime(2026, 7, 17, 7, 0, tzinfo=UTC)
    return EvidenceBlock(
        block_id=f"block-{domain.value}",
        domain=domain,
        title=domain.value,
        source="fixture",
        methodology="fixture point-in-time methodology",
        as_of=observed,
        available_at=observed,
        fetched_at=observed + timedelta(minutes=1),
        freshness="fresh",
        quality=quality,
        payload=payload or {"value": 1},
        missing_reason="fixture unavailable" if quality == EvidenceQuality.UNAVAILABLE else None,
    )


def _complete_pack(symbol: str = "sh600001") -> AnalysisContextPack:
    blocks = [
        _block(EvidenceDomain.MARKET, payload={"current_raw_price": 10.0}),
        _block(EvidenceDomain.TECHNICAL, payload={"trend": "up"}),
        _block(
            EvidenceDomain.CAPITAL_FLOW,
            payload={"scope": "stock", "scope_key": symbol, "flow_trend": {"5": 10}},
        ),
        _block(EvidenceDomain.FINANCIAL, payload={"revenue_yoy": 0.1}),
        _block(EvidenceDomain.VALUATION, payload={"pe_ttm": 15}),
        _block(EvidenceDomain.EVENT, payload={"events": []}),
        _block(EvidenceDomain.MACRO, payload={"rate": 0.02}),
        _block(EvidenceDomain.PORTFOLIO, payload={"cash": 100_000}),
        _block(EvidenceDomain.STRATEGY, payload={"signal": "watch"}),
    ]
    return AnalysisContextPack(
        context_id=f"context-{symbol}",
        symbol=symbol,
        asset_type=AssetType.STOCK,
        as_of=date(2026, 7, 17),
        cutoff_at=datetime(2026, 7, 17, 15, 59, tzinfo=UTC),
        blocks=blocks,
        critical_gaps=[],
        deterministic_summary={"maximum_single_weight": 0.15},
    )


def _stock_records(days: int = 25, *, industry: str = "制造") -> list[dict]:
    start = date(2026, 6, 1)
    output = []
    for index in range(days):
        current = start + timedelta(days=index)
        output.append(
            {
                "symbol": "sh600001",
                "industry": industry,
                "date": current,
                "close": 10 + index * 0.1,
                "amount": 100_000 + index * 10_000,
                "turnover_rate": 0.01 + index * 0.0001,
                "source": "fixture",
                "methodology": SIGNED_TURNOVER_METHODOLOGY,
                "available_at": datetime.combine(
                    current,
                    datetime.min.time(),
                    tzinfo=UTC,
                ),
            }
        )
    return output


def test_context_pack_version_fingerprint_cutoff_and_compression(tmp_path):
    pack = _complete_pack()
    repeated = _complete_pack()
    assert pack.schema_version == "2.0"
    assert pack.fingerprint == repeated.fingerprint
    assert pack.quality_score == 1.0
    assert pack.review_required is False
    payload = pack.llm_payload(maximum_bytes=8_000)
    assert payload["payload_bytes"] <= 8_000
    assert all("source" in item and "available_at" in item for item in payload["blocks"])

    repository = EvidenceRepository(tmp_path / "quantlab.db")
    first = repository.save_context(pack)
    second = repository.save_context(repeated)
    assert first["context_id"] == second["context_id"]

    future = _block(EvidenceDomain.EVENT)
    future.available_at = datetime(2026, 7, 18, tzinfo=UTC)
    with pytest.raises(ValueError, match="after context cutoff"):
        AnalysisContextPack(
            symbol="sh600001",
            asset_type=AssetType.STOCK,
            as_of=date(2026, 7, 17),
            cutoff_at=datetime(2026, 7, 17, 15, 59, tzinfo=UTC),
            blocks=[future],
        )


def test_context_compression_preserves_core_financial_authority_before_optional_blocks():
    pack = _complete_pack()
    oversized = "x" * 20_000
    for block in pack.blocks:
        block.payload = {
            **block.payload,
            "details": oversized,
        }

    payload = pack.llm_payload(maximum_bytes=8_000)
    blocks = {item["domain"]: item for item in payload["blocks"]}

    assert payload["payload_bytes"] <= 8_000
    for domain in {"market", "portfolio", "strategy"}:
        assert blocks[domain]["payload"].get("compression") != "summary_only"
    assert any(
        blocks[domain]["payload"].get("compression") == "summary_only"
        for domain in {"event", "macro", "memory"}
        if domain in blocks
    )


def test_context_assembler_excludes_future_and_marks_missing():
    pack = assemble_analysis_context_pack(
        symbol="sh600001",
        asset_type="stock",
        as_of=date(2026, 7, 17),
        market={
            "raw_price": 10,
            "source": "fixture",
            "as_of": "2026-07-17",
            "available_at": "2026-07-18T00:00:00+08:00",
        },
        technical=None,
        events=[
            {
                "event_type": "news",
                "title": "future news",
                "source": "fixture",
                "event_date": "2026-07-17",
                "available_at": "2026-07-18T01:00:00+08:00",
            }
        ],
        strategy={"source": "fixture", "as_of": "2026-07-17"},
    )
    assert pack.review_required is True
    assert "market data became available after cutoff" in pack.critical_gaps
    market = pack.block(EvidenceDomain.MARKET)
    assert market.quality == EvidenceQuality.UNAVAILABLE
    event = pack.block(EvidenceDomain.EVENT)
    assert event.quality == EvidenceQuality.UNAVAILABLE
    assert "future news" not in json.dumps(pack.model_dump(mode="json"))


def test_market_industry_and_stock_flow_are_recomputable_and_methodology_isolated():
    records = _stock_records()
    for item in _stock_records():
        item = dict(item)
        item["symbol"] = "sh600002"
        item["close"] = 20 - (item["date"] - date(2026, 6, 1)).days * 0.05
        records.append(item)
    end = max(item["date"] for item in records)
    market = calculate_market_flow(
        records,
        as_of=end,
        source="fixture",
        methodology=SIGNED_TURNOVER_METHODOLOGY,
    )
    assert market.payload["turnover"]["latest"] > 0
    assert market.payload["breadth"]["up"] == 1
    assert market.payload["breadth"]["down"] == 1
    assert market.payload["financing_balance"]["status"] == "unavailable"
    assert market.estimated is True

    industries = calculate_industry_flow(
        records,
        as_of=end,
        source="fixture",
        methodology=SIGNED_TURNOVER_METHODOLOGY,
    )
    assert industries[0].payload["flow_trend"]["5"] is not None
    assert industries[0].payload["flow_price_consistency"] in {
        "flow_price_confirmation",
        "inflow_not_confirmed",
        "price_flow_divergence",
        "outflow_price_confirmation",
        "neutral",
    }

    stock = calculate_stock_flow(
        records,
        symbol="sh600001",
        as_of=end,
        source="fixture",
    )
    assert stock.payload["historical_flow_percentile"] is not None
    assert stock.payload["order_size_structure"]["large_order_flow"]["status"] == (
        "unavailable"
    )
    assert "not confirmed" in stock.payload["claim_boundary"]

    mixed = [dict(item) for item in records[:3]]
    mixed[-1]["source"] = "other-vendor"
    with pytest.raises(ValueError, match="sources must be normalized separately"):
        calculate_stock_flow(
            mixed,
            symbol="sh600001",
            as_of=end,
            source="fixture",
        )


def test_flow_notification_requires_persistence_and_continuity(tmp_path):
    settings = _settings(tmp_path)
    notifications = NotificationRepository(tmp_path / "quantlab.db")
    rule = notifications.create_rule(
        rule_type="flow_positive_streak",
        idempotency_key="flow-rule-0001",
        account_id="account-1",
        symbol="sh600001",
        threshold=1,
        consecutive_periods=2,
        cooldown_seconds=86_400,
    )
    evidence = EvidenceRepository(tmp_path / "quantlab.db")
    records = _stock_records(4)
    first_date = records[-2]["date"]
    first = calculate_stock_flow(
        records[:-1],
        symbol="sh600001",
        as_of=first_date,
        source="fixture",
    )
    evidence.save_flow(first)
    assert evaluate_flow_notification_rules(settings, first, account_id="account-1") == []

    second = calculate_stock_flow(
        records,
        symbol="sh600001",
        as_of=records[-1]["date"],
        source="fixture",
    )
    evidence.save_flow(second)
    triggered = evaluate_flow_notification_rules(settings, second, account_id="account-1")
    assert triggered[0]["rule_id"] == rule["rule_id"]
    assert triggered[0]["formula"] == "2 consecutive one-period flow values"
    assert evaluate_flow_notification_rules(settings, second, account_id="account-1") == []
    row = notifications.list(account_id="account-1")[0]
    assert row["notification_type"] == "industry_flow_streak"
    assert row["action_payload"]["source"] == "fixture"


class CountingProvider(LLMProvider):
    provider_name = "fixture"
    model = "fixture-model"

    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    async def structured(self, system: str, prompt: str, schema: type[BaseModel]):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider unavailable")
        payload = json.loads(prompt) if prompt.startswith("{") else {}
        if schema is CommitteeRoleOpinion:
            blocks = payload.get("evidence_blocks", [])
            refs = [blocks[0]["block_id"]] if blocks else []
            return CommitteeRoleOpinion(
                role="fixture",
                stance="bullish",
                confidence=0.8,
                importance=0.8,
                summary="evidence supports a bounded bullish scenario",
                evidence_refs=refs,
                suggested_weight=0.8,
            )
        if schema is CommitteeDecision:
            blocks = payload["context"]["blocks"]
            return CommitteeDecision(
                action="buy",
                confidence=0.8,
                suggested_weight_min=0.2,
                suggested_weight_max=0.8,
                deterministic_max_weight=0.8,
                evidence_refs=[blocks[0]["block_id"], blocks[1]["block_id"]],
                counter_evidence_refs=[],
                context_id=payload["context"]["context_id"],
                context_version=payload["context"]["schema_version"],
                context_fingerprint=payload["context"]["fingerprint"],
                requires_user_review=False,
            )
        if schema is ChatEvidenceAnswer:
            blocks = payload["contexts"][0]["blocks"]
            return ChatEvidenceAnswer(
                answer="基于行情与技术证据，结论仍需用户确认。",
                facts=["raw price is supplied by the context"],
                llm_judgments=["evidence is moderately constructive"],
                evidence_refs=[blocks[0]["block_id"], blocks[1]["block_id"]],
                suggested_action="buy",
                suggested_weight_min=0.05,
                suggested_weight_max=0.8,
                requires_user_review=False,
            )
        raise AssertionError(schema)


def test_governed_llm_cache_idempotency_and_budget(tmp_path):
    repository = EvidenceRepository(tmp_path / "quantlab.db")
    base = CountingProvider()
    provider = GovernedLLMProvider(
        base,
        repository,
        context_id="context-1",
        context_fingerprint="fingerprint-1",
        task_id="task-1",
        budget=LLMTaskBudget(maximum_calls=1, maximum_total_tokens=10_000, maximum_cost_usd=1),
    )
    first = asyncio.run(
        provider.structured("technical role", "{}", CommitteeRoleOpinion)
    )
    second = asyncio.run(
        provider.structured("technical role", "{}", CommitteeRoleOpinion)
    )
    assert first.summary == second.summary
    assert base.calls == 1
    with pytest.raises(LLMBudgetExceeded):
        asyncio.run(
            provider.structured("macro role", '{"different":true}', CommitteeRoleOpinion)
        )
    usage = repository.task_usage("task-1")
    assert usage["calls"] == 1


class SlowSharedProvider(LLMProvider):
    provider_name = "shared-counting"
    model = "shared-model"

    def __init__(self):
        self.calls = 0
        self._lock = Lock()

    async def structured(self, system: str, prompt: str, schema):
        with self._lock:
            self.calls += 1
        await asyncio.sleep(0.15)
        return CommitteeRoleOpinion(
            role="technical",
            stance="neutral",
            confidence=0.5,
            summary="shared result",
        )


def test_governed_llm_cache_claim_prevents_cross_worker_duplicate_charge(tmp_path):
    repository = EvidenceRepository(tmp_path / "shared-cache.db")
    base = SlowSharedProvider()
    providers = [
        GovernedLLMProvider(
            base,
            repository,
            context_id="context-shared",
            context_fingerprint="fingerprint-shared",
            task_id=f"worker-task-{index}",
        )
        for index in range(2)
    ]

    def run(provider: GovernedLLMProvider):
        return asyncio.run(
            provider.structured("technical role", '{"same":true}', CommitteeRoleOpinion)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, providers))

    assert [item.summary for item in results] == ["shared result", "shared result"]
    assert base.calls == 1
    assert sum(repository.task_usage(f"worker-task-{index}")["calls"] for index in range(2)) == 1


def test_committee_clamps_weight_and_fails_conservatively(tmp_path):
    settings = _settings(tmp_path)
    pack = _complete_pack()
    provider = CountingProvider()
    decision = asyncio.run(
        run_context_committee_with_provider(
            settings,
            pack=pack,
            deterministic_max_weight=0.12,
            provider=provider,
        )
    )
    assert decision.action == "buy"
    assert decision.suggested_weight_max == 0.12
    assert set(decision.evidence_refs) <= {block.block_id for block in pack.blocks}
    assert decision.context_version == "2.0"

    degraded = asyncio.run(
        run_context_committee_with_provider(
            settings,
            pack=pack,
            deterministic_max_weight=0.12,
            provider=CountingProvider(fail=True),
        )
    )
    assert degraded.action == "review_required"
    assert degraded.suggested_weight_max == 0
    assert degraded.degraded_roles


def test_role_governance_requires_mature_samples_and_frozen_challenge(tmp_path):
    settings = _settings(tmp_path)
    for index in range(2):
        record_role_outcome(
            settings,
            role="technical",
            run_id=f"run-{index}",
            symbol="sh600001",
            as_of=f"2026-07-{10 + index:02d}",
            horizon_days=5,
            probabilities={"up": 0.6, "flat": 0.2, "down": 0.2},
            realized_direction="up",
            realized_return_pct=2.0,
            market_regime="bull",
            quant_incremental_return_pct=0.5,
            cost_usd=0.01,
            latency_ms=100,
        )
        scorecard = role_scorecard(settings, "technical")
        if index == 0:
            assert scorecard["stage"] == "shadow_observation"
    scorecard = role_scorecard(settings, "technical")
    assert scorecard["stage"] == "frozen_challenge_required"
    assert scorecard["automatic_weight_change_allowed"] is False
    assert scorecard["metrics"]["brier_score"] is not None
    challenge = freeze_role_challenge(settings, "technical")
    assert challenge["status"] == "frozen"
    promoted = decide_role_challenge(
        settings,
        challenge["challenge_id"],
        passed=True,
        decision="promote",
        reason="frozen challenge passed",
    )
    assert promoted["decision"] == "promote"
    assert role_scorecard(settings, "technical")["stage"] == "promoted"
