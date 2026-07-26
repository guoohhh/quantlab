from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from quantlab.agents import MultiAgentDecisionSystem, ResearchContext
from quantlab.agents.decision_policy import align_reviewer_report, evaluate_decision_policy
from quantlab.agents.schemas import ReviewReport
from quantlab.config import Settings
from quantlab.domain import CommitteeRoleOpinion
from quantlab.domain.models import StrategySignal
from quantlab.llm import MockLLMProvider
from quantlab.llm.governance import (
    GovernedLLMProvider,
    LLMBudgetExceeded,
    LLMTaskBudget,
    budget_for_workflow,
    workflow_plan_from_settings,
)
from quantlab.persistence import EvidenceRepository
from quantlab.workflows.llm_committee import (
    expected_context_committee_roles,
    run_context_committee_with_provider,
)

from test_context_flow_llm import _complete_pack, _settings


class UsageCountingMockProvider(MockLLMProvider):
    def __init__(self, *, delay: float = 0.0):
        self.calls = 0
        self.delay = delay

    async def structured(self, system, prompt, schema):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        result = await super().structured(system, prompt, schema)
        object.__setattr__(
            result,
            "_llm_usage",
            {"input_tokens": 10_000, "output_tokens": 1_000},
        )
        return result


def _full_stock_context() -> ResearchContext:
    return ResearchContext(
        symbol="STOCK_FIXTURE",
        as_of=date(2026, 7, 21),
        price=100.0,
        asset_type="stock",
        strategy_signals=[
            StrategySignal(
                strategy="factor_momentum",
                symbol="STOCK_FIXTURE",
                as_of=date(2026, 7, 21),
                score=0.4,
                confidence=0.7,
            )
        ],
        fundamentals={"quality_score": 0.8, "revenue_yoy": 0.1},
        news=[{"title": "fixture event", "available_at": "2026-07-21T08:00:00Z"}],
        quant_factors={"composite_score": 0.4},
        price_history={"normalized_adjusted_close_path_120": {"values": [90, 100]}},
        market_regime="range",
        data_quality=1.0,
        context_id="context-STOCK_FIXTURE",
        context_version="2.0",
        context_fingerprint="fingerprint-STOCK_FIXTURE",
    )


def _full_research_plan(settings: Settings, context: ResearchContext):
    pack = _complete_pack("STOCK_FIXTURE")
    phase_roles = MultiAgentDecisionSystem.expected_llm_phase_roles(context)
    phase_roles.update(
        {
            "context_roles": expected_context_committee_roles(pack, 6),
            "context_synthesis": ["context_synthesis"],
        }
    )
    return pack, workflow_plan_from_settings(
        settings.section("llm"),
        workflow="stock_full_research",
        phase_roles=phase_roles,
    )


def test_atomic_call_reservation_blocks_concurrent_different_prompts(tmp_path):
    repository = EvidenceRepository(tmp_path / "atomic-calls.db")
    base = UsageCountingMockProvider(delay=0.1)
    provider = GovernedLLMProvider(
        base,
        repository,
        context_id="context",
        context_fingerprint="fingerprint",
        task_id="atomic-call-task",
        budget=LLMTaskBudget(
            maximum_calls=1,
            maximum_total_tokens=100_000,
            maximum_cost_usd=1.0,
        ),
    )

    async def compete():
        return await asyncio.gather(
            provider.structured("technical role", '{"request":1}', CommitteeRoleOpinion),
            provider.structured("technical role", '{"request":2}', CommitteeRoleOpinion),
            return_exceptions=True,
        )

    results = asyncio.run(compete())
    assert sum(isinstance(item, LLMBudgetExceeded) for item in results) == 1
    assert base.calls == 1
    assert repository.task_usage("atomic-call-task")["actual_calls"] == 1


def test_atomic_cost_reservation_blocks_concurrent_budget_race(tmp_path):
    repository = EvidenceRepository(tmp_path / "atomic-cost.db")
    base = UsageCountingMockProvider(delay=0.1)
    provider = GovernedLLMProvider(
        base,
        repository,
        context_id="context",
        context_fingerprint="fingerprint-cost",
        task_id="atomic-cost-task",
        budget=LLMTaskBudget(
            maximum_calls=2,
            maximum_total_tokens=100_000,
            maximum_cost_usd=0.02,
        ),
    )

    async def compete():
        return await asyncio.gather(
            provider.structured("technical role", '{"cost":1}', CommitteeRoleOpinion),
            provider.structured("technical role", '{"cost":2}', CommitteeRoleOpinion),
            return_exceptions=True,
        )

    results = asyncio.run(compete())
    assert sum(isinstance(item, LLMBudgetExceeded) for item in results) == 1
    assert base.calls == 1


def test_full_stock_workflow_reserves_forecasts_reviewer_and_context_tail(tmp_path):
    settings = _settings(tmp_path)
    context = _full_stock_context()
    pack, plan = _full_research_plan(settings, context)
    assert plan.expected_calls == 25
    base = UsageCountingMockProvider()
    governed = GovernedLLMProvider(
        base,
        EvidenceRepository(tmp_path / "full-stock.db"),
        context_id=pack.context_id,
        context_fingerprint=pack.fingerprint,
        task_id="full-stock-task",
        budget=budget_for_workflow(settings.section("llm"), "stock_full_research"),
        workflow_plan=plan,
    )
    governed.prepare_workflow()
    run = asyncio.run(MultiAgentDecisionSystem(governed).run(context))
    committee = asyncio.run(
        run_context_committee_with_provider(
            settings,
            pack=pack,
            deterministic_max_weight=0.15,
            provider=governed,
        )
    )
    snapshot = governed.budget_snapshot()

    assert base.calls == 25
    assert len(run.forecasts) == 2
    assert run.reports["reviewer"].policy_action == run.decision.action
    assert committee.role_audit
    assert snapshot["missing_roles"] == []
    assert snapshot["actual_usage"]["calls"] == 25
    assert snapshot["reserved_usage"]["calls"] == 0


def test_insufficient_full_workflow_budget_fails_before_first_paid_call(tmp_path):
    settings = _settings(tmp_path)
    context = _full_stock_context()
    pack, plan = _full_research_plan(settings, context)
    base = UsageCountingMockProvider()
    governed = GovernedLLMProvider(
        base,
        EvidenceRepository(tmp_path / "preflight.db"),
        context_id=pack.context_id,
        context_fingerprint=pack.fingerprint,
        task_id="preflight-task",
        budget=LLMTaskBudget(
            maximum_calls=16,
            maximum_total_tokens=100_000,
            maximum_cost_usd=8.0,
        ),
        workflow_plan=plan,
    )

    with pytest.raises(LLMBudgetExceeded) as error:
        governed.prepare_workflow()

    assert base.calls == 0
    assert error.value.details["estimated_required"]["calls"] == 25
    assert "forecast_5d" in error.value.details["missing_roles"]
    assert "reviewer" in error.value.details["missing_roles"]
    assert "context_synthesis" in error.value.details["missing_roles"]


def test_cached_result_satisfies_planned_role_without_second_charge(tmp_path):
    repository = EvidenceRepository(tmp_path / "cache-recovery.db")
    base = UsageCountingMockProvider()
    first = GovernedLLMProvider(
        base,
        repository,
        context_id="context",
        context_fingerprint="cache-fingerprint",
        task_id="cache-source",
    )
    asyncio.run(first.structured("technical role", "{}", CommitteeRoleOpinion))
    plan = workflow_plan_from_settings(
        {},
        workflow="context_committee",
        phase_roles={"context_roles": ["technical"]},
    )
    resumed = GovernedLLMProvider(
        base,
        repository,
        context_id="context",
        context_fingerprint="cache-fingerprint",
        task_id="cache-resumed",
        budget=LLMTaskBudget(
            maximum_calls=1,
            maximum_total_tokens=20_000,
            maximum_cost_usd=1.0,
        ),
        workflow_plan=plan,
    )
    resumed.prepare_workflow()
    asyncio.run(resumed.structured("technical role", "{}", CommitteeRoleOpinion))

    assert base.calls == 1
    assert repository.task_usage("cache-resumed")["calls"] == 0
    assert resumed.budget_snapshot()["missing_roles"] == []


def test_sanitized_real_case_fixtures_keep_action_reasons_and_reviewer_consistent():
    path = Path(__file__).parent / "fixtures" / "decision_policy_cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    assert len(cases) == 4
    for case in cases:
        result = evaluate_decision_policy(
            composite_score=case["composite_score"],
            confidence=case["confidence"],
            evidence_coverage=case["evidence_coverage"],
            conflict=case["conflict"],
            council_veto=case["council_veto"],
            veto_roles=case["veto_roles"],
            reviewer_approved=case["reviewer_approved"],
            context_action=case["context_action"],
            context_requires_review=case["context_requires_review"],
        )
        assert result.action == case["expected_action"]
        assert set(case["expected_triggers"]) <= set(result.trigger_codes)
        assert not set(case["forbidden_triggers"]) & set(result.trigger_codes)
        assert all(reason.startswith("trigger:") for reason in result.reasons)
        review = ReviewReport(
            approved=case["reviewer_approved"],
            status="approved" if case["reviewer_approved"] else "rejected",
            summary="sanitized reviewer fixture",
        )
        align_reviewer_report(review, result)
        assert review.policy_action == result.action
        assert review.policy_trigger_codes == list(result.trigger_codes)
        assert f"policy_action={result.action}" in review.summary


def test_adjusted_signal_basis_is_separate_from_raw_execution_price():
    result = evaluate_decision_policy(
        composite_score=0.4,
        confidence=0.6,
        evidence_coverage=0.8,
        conflict=0.2,
        council_veto=False,
        price_is_executable=True,
    )
    unavailable = evaluate_decision_policy(
        composite_score=0.4,
        confidence=0.6,
        evidence_coverage=0.8,
        conflict=0.2,
        council_veto=False,
        price_is_executable=False,
    )

    assert result.signal_price_basis == "adjusted_close"
    assert result.execution_price_basis == "raw_market_price"
    assert unavailable.execution_price_basis == "unavailable"
