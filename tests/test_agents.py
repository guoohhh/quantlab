import asyncio
from datetime import date

import pytest

from quantlab.agents import MultiAgentDecisionSystem, ResearchContext
from quantlab.agents.schemas import ExpertOpinion
from quantlab.domain.models import StrategySignal
from quantlab.llm import LLMProvider, MockLLMProvider


class FailingProvider(LLMProvider):
    provider_name = "failing"
    model = "failing-model"

    async def structured(self, system, prompt, schema):
        raise RuntimeError("offline")


class CountingMockProvider(MockLLMProvider):
    def __init__(self):
        self.calls = []
        self.prompts = []

    async def structured(self, system, prompt, schema):
        self.calls.append(schema.__name__)
        self.prompts.append(prompt)
        return await super().structured(system, prompt, schema)


def test_mock_multi_agent_run_is_structured():
    context = ResearchContext(
        symbol="sh510300",
        as_of=date(2026, 1, 2),
        price=4.0,
        strategy_signals=[
            StrategySignal(
                strategy="etf_rotation",
                symbol="sh510300",
                as_of=date(2026, 1, 2),
                score=0.5,
                confidence=0.7,
            )
        ],
        data_quality=0.5,
    )
    run = asyncio.run(MultiAgentDecisionSystem(MockLLMProvider()).run(context))
    assert len(run.forecasts) == 2
    assert run.decision.symbol == "sh510300"
    assert run.decision_trace["composite_score"] == pytest.approx(
        sum(
            run.decision_trace["weights"][name] * value
            for name, value in run.decision_trace["components"].items()
        )
    )
    assert run.decision_trace["evidence_coverage"]["adjusted"] == pytest.approx(0.275)
    assert run.audit_log
    assert run.llm_audit["secrets_exposed"] is False
    assert len(run.reports["council"].opinions) == 5
    if run.decision.action != "buy":
        assert run.decision.target_weight == 0.0
    assert {item.role for item in run.reports["council"].opinions} == {
        "technical",
        "momentum",
        "value_veto",
        "risk",
        "macro",
    }
    assert any(item.startswith("conflict=") for item in run.decision.reasons)
    assert any(item.startswith("abs_composite=") for item in run.decision.reasons)
    assert any(item.startswith("high_conflict_review_triggered=") for item in run.decision.reasons)
    assert any("review_required_when_high_conflict" in item for item in run.decision.reasons)
    assert run.decision_trace["high_conflict_review_triggered"] == (
        run.decision_trace["conflict"] > 0.8 and abs(run.decision_trace["composite_score"]) < 0.12
    )


def test_etf_skips_optional_analysts_and_only_calls_required_llm_roles():
    provider = CountingMockProvider()
    context = ResearchContext(
        symbol="sh510300",
        as_of=date(2026, 1, 2),
        price=4.0,
        asset_type="etf",
        strategy_signals=[
            StrategySignal(
                strategy="etf_rotation",
                symbol="sh510300",
                as_of=date(2026, 1, 2),
                score=0.5,
                confidence=0.7,
            )
        ],
        quant_factors={"composite_score": 0.5},
        market_regime="bull",
    )

    run = asyncio.run(MultiAgentDecisionSystem(provider).run(context))

    assert len(provider.calls) == 11
    assert MultiAgentDecisionSystem.expected_llm_role_keys(context) == [
        "quant",
        "technical",
        "momentum",
        "value_veto",
        "risk",
        "macro",
        "bull",
        "bear",
        "forecast",
        "forecast",
        "review",
    ]
    assert provider.calls.count("AnalystReport") == 1
    assert '"current_raw_price": 4.0' in provider.prompts[0]
    assert '"global_evidence_availability"' in provider.prompts[0]
    assert run.reports["fundamental"].missing_data == []
    assert "not required for ETF" in run.reports["fundamental"].summary
    assert run.reports["news"].missing_data == ["news data unavailable"]
    assert "only_buy_action_may_have_nonzero_target_weight" in provider.prompts[-1]
    assert len(run.decision.risks) <= 24
    assert all(":" in risk for risk in run.decision.risks)


def test_expert_score_sign_is_normalized_to_structured_stance():
    bearish = ExpertOpinion(
        role="munger",
        perspective="inversion",
        stance="bearish",
        score=0.3,
        confidence=0.8,
    )
    neutral = ExpertOpinion(
        role="buffett",
        perspective="quality",
        stance="neutral",
        score=0.6,
        confidence=0.8,
    )

    MultiAgentDecisionSystem._normalize_opinion_score(bearish)
    MultiAgentDecisionSystem._normalize_opinion_score(neutral)

    assert bearish.score == -0.3
    assert neutral.score == 0.0
    assert "adjusted from 0.300 to -0.300" in bearish.evidence[-1]
    assert "adjusted from 0.600 to 0.000" in neutral.evidence[-1]


def test_price_history_reaches_reviewer_and_restores_coverage_contribution():
    provider = CountingMockProvider()
    signal = StrategySignal(
        strategy="etf_rotation",
        symbol="sh510300",
        as_of=date(2026, 1, 2),
        score=0.5,
        confidence=0.7,
    )
    without_history = ResearchContext(
        symbol="sh510300",
        as_of=date(2026, 1, 2),
        price=4.0,
        asset_type="etf",
        strategy_signals=[signal],
        quant_factors={"composite_score": 0.5},
        market_regime="bull",
    )
    normalized_path = [round(80 + index * 0.1, 6) for index in range(119)] + [99.876543]
    with_history = ResearchContext(
        **{
            **without_history.__dict__,
            "price_history": {
                "evidence_type": "market_price_history",
                "normalized_adjusted_close_path_120": {"values": normalized_path},
            },
        }
    )

    no_history_coverage = MultiAgentDecisionSystem._evidence_coverage_trace(without_history)
    with_history_coverage = MultiAgentDecisionSystem._evidence_coverage_trace(with_history)
    asyncio.run(MultiAgentDecisionSystem(provider).run(with_history))

    assert no_history_coverage["availability"]["price_history"] is False
    assert with_history_coverage["availability"]["price_history"] is True
    assert with_history_coverage["adjusted"] - no_history_coverage["adjusted"] == pytest.approx(
        0.15
    )
    reviewer_prompt = provider.prompts[-1]
    assert '"price_history"' in reviewer_prompt
    assert '"normalized_adjusted_close_path_120"' in reviewer_prompt
    assert "99.876543" in reviewer_prompt


def test_active_statistical_model_blends_but_preserves_raw_llm_probabilities():
    def predictor(horizon, features):
        return {
            "model_id": f"model-{horizon}",
            "version": 2,
            "ensemble_weight": 0.5,
            "up_probability": 0.8,
            "flat_probability": 0.1,
            "down_probability": 0.1,
        }

    context = ResearchContext(
        symbol="sh510300",
        as_of=date(2026, 1, 2),
        price=4.0,
        asset_type="etf",
    )
    run = asyncio.run(MultiAgentDecisionSystem(MockLLMProvider(), predictor).run(context))
    forecast = run.forecasts[0]

    assert forecast.raw_llm_up_probability == 0.4
    assert forecast.up_probability == pytest.approx(0.6)
    assert forecast.flat_probability == pytest.approx(0.2)
    assert forecast.down_probability == pytest.approx(0.2)
    assert forecast.statistical_model_id == "model-5"


def test_individual_llm_failures_degrade_to_human_review_instead_of_aborting():
    context = ResearchContext(
        symbol="sh510300",
        as_of=date(2026, 1, 2),
        price=4.0,
        asset_type="etf",
        data_quality=0.8,
    )

    run = asyncio.run(MultiAgentDecisionSystem(FailingProvider()).run(context))

    assert run.decision.action == "review_required"
    assert run.decision.requires_human_review is True
    assert run.decision.target_weight == 0.0
    assert all(item.model_provider == "fallback" for item in run.forecasts)
    assert any(item.status == "degraded" for item in run.audit_log)
    assert run.llm_audit["prompts_persisted"] is False
