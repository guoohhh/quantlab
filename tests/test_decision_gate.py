from datetime import date

import pytest

from quantlab.agents.decision_gate import evaluate_etf_gate_policies
from quantlab.agents.schemas import ReviewReport
from quantlab.domain.models import DecisionCard, Forecast


def _decision(action: str = "watch", target_weight: float = 0.0) -> DecisionCard:
    return DecisionCard(
        symbol="ETF_CANDIDATE_01",
        as_of=date(2000, 2, 3),
        action=action,
        confidence=0.50,
        target_weight=target_weight,
        entry_price=None,
    )


def _forecast() -> Forecast:
    return Forecast(
        symbol="ETF_CANDIDATE_01",
        as_of=date(2000, 2, 3),
        horizon_days=20,
        up_probability=0.56,
        flat_probability=0.18,
        down_probability=0.26,
        expected_return_pct=1.2,
        lower_return_pct=-5.0,
        upper_return_pct=7.0,
        confidence=0.60,
        model="test",
        model_provider="test",
    )


def _trace(*, high_conflict: bool = False) -> dict:
    return {
        "composite_score": 0.32,
        "confidence": 0.50,
        "high_conflict_review_triggered": high_conflict,
        "components": {"strategy_signal_score": 0.40},
        "council_diagnostics": {"veto_triggered": False},
    }


def _review(approved: bool = True) -> ReviewReport:
    return ReviewReport(
        approved=approved,
        status="approved" if approved else "rejected",
        summary="test",
    )


def test_etf_gate_candidates_add_partial_exposure_without_changing_current_gate():
    output = evaluate_etf_gate_policies(
        decision=_decision(),
        decision_trace=_trace(),
        reviewer=_review(),
        forecast=_forecast(),
        strategy_weight=0.15,
    )

    assert output["current_strict"]["target_weight"] == 0.0
    assert output["etf_score_tiered_v1"]["target_weight"] == pytest.approx(0.1125)
    assert output["etf_forecast_confirmed_v1"]["target_weight"] == pytest.approx(0.075)
    assert output["strategy_unless_bearish_v1"]["target_weight"] == pytest.approx(0.15)


@pytest.mark.parametrize(
    ("reviewer", "trace", "blocker"),
    [
        (_review(False), _trace(), "reviewer_not_approved"),
        (_review(), _trace(high_conflict=True), "high_conflict_review"),
    ],
)
def test_etf_gate_candidates_never_bypass_review_or_conflict(reviewer, trace, blocker):
    output = evaluate_etf_gate_policies(
        decision=_decision(action="review_required"),
        decision_trace=trace,
        reviewer=reviewer,
        forecast=_forecast(),
        strategy_weight=0.15,
    )

    for name in (
        "etf_score_tiered_v1",
        "etf_forecast_confirmed_v1",
        "strategy_unless_bearish_v1",
    ):
        assert output[name]["target_weight"] == 0.0
        assert blocker in output[name]["blockers"]


def test_current_gate_preserves_an_approved_buy_weight():
    output = evaluate_etf_gate_policies(
        decision=_decision(action="buy", target_weight=0.08),
        decision_trace=_trace(),
        reviewer=_review(),
        forecast=_forecast(),
        strategy_weight=0.15,
    )

    assert output["current_strict"]["target_weight"] == pytest.approx(0.08)


def test_calibrated_strategy_primary_ignores_uncalibrated_llm_gate():
    output = evaluate_etf_gate_policies(
        decision=_decision(action="review_required"),
        decision_trace=_trace(),
        reviewer=_review(False),
        forecast=_forecast(),
        strategy_weight=0.15,
    )

    candidate = output["calibrated_strategy_primary_v1"]
    assert candidate["target_weight"] == pytest.approx(0.15)
    assert candidate["tier"] == "strategy_primary"
    assert "uncalibrated LLM is advisory" in candidate["reasons"][0]


def test_calibrated_strategy_primary_halves_risk_on_validated_bearish_model():
    forecast = _forecast().model_copy(
        update={
            "statistical_model_id": "point-in-time-model",
            "statistical_weight": 0.50,
            "statistical_up_probability": 0.20,
            "statistical_flat_probability": 0.30,
            "statistical_down_probability": 0.50,
        }
    )
    output = evaluate_etf_gate_policies(
        decision=_decision(),
        decision_trace=_trace(),
        reviewer=_review(),
        forecast=forecast,
        strategy_weight=0.15,
    )

    candidate = output["calibrated_strategy_primary_v1"]
    assert candidate["target_weight"] == pytest.approx(0.075)
    assert candidate["tier"] == "risk_reduced"
