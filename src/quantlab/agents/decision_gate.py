from __future__ import annotations

from typing import Any

from quantlab.agents.schemas import ReviewReport
from quantlab.domain.models import DecisionCard, Forecast


ETF_GATE_POLICY_VERSION = "2026-07-14.v2"

ETF_GATE_POLICY_CATALOG = {
    "current_strict": {
        "status": "incumbent",
        "description": "Only an approved final buy/add may carry the decision-card weight.",
    },
    "etf_score_tiered_v1": {
        "status": "retrospective_candidate",
        "description": "ETF-specific score tiers with forecast confirmation and partial sizing.",
    },
    "etf_forecast_confirmed_v1": {
        "status": "retrospective_candidate",
        "description": "A small ETF position only when strategy, score and forecast agree.",
    },
    "strategy_unless_bearish_v1": {
        "status": "explanatory_ablation",
        "description": "Use the strategy weight unless the reviewed forecast is materially bearish.",
    },
    "calibrated_strategy_primary_v1": {
        "status": "retrospective_candidate_v2",
        "description": (
            "Keep the quantitative ETF strategy in control; only a validated point-in-time "
            "statistical model may reduce risk."
        ),
    },
}


def evaluate_etf_gate_policies(
    *,
    decision: DecisionCard,
    decision_trace: dict[str, Any],
    reviewer: ReviewReport,
    forecast: Forecast,
    strategy_weight: float,
) -> dict[str, dict[str, Any]]:
    """Evaluate frozen ETF gate arms without changing the production decision.

    Every challenger retains the deterministic safety contract. The returned weights are
    counterfactual research allocations and must not be interpreted as live orders.
    """

    strategy_weight = _bounded_weight(strategy_weight, strategy_weight)
    current_eligible = (
        decision.action in {"buy", "add"}
        and not decision.requires_human_review
        and reviewer.approved
    )
    current_weight = (
        _bounded_weight(float(decision.target_weight), strategy_weight)
        if current_eligible
        else 0.0
    )
    output = {
        "current_strict": _allocation(
            current_weight,
            "approved_buy" if current_weight > 0 else "blocked",
            [] if current_weight > 0 else _current_blockers(decision, reviewer),
            ["production action and target weight preserved exactly"],
        )
    }

    components = decision_trace.get("components", {})
    composite = float(decision_trace.get("composite_score", 0.0))
    strategy_score = float(components.get("strategy_signal_score", 0.0))
    confidence = float(decision_trace.get("confidence", decision.confidence))
    forecast_edge = float(forecast.up_probability - forecast.down_probability)
    expected_return = float(forecast.expected_return_pct)
    forecast_confidence = float(forecast.confidence)
    blockers = _safety_blockers(
        decision_trace=decision_trace,
        reviewer=reviewer,
        confidence=confidence,
        strategy_score=strategy_score,
    )

    score_weight = 0.0
    score_tier = "blocked"
    score_reasons: list[str] = []
    score_blockers = list(blockers)
    if not score_blockers:
        if (
            composite >= 0.30
            and forecast_edge >= 0.10
            and expected_return > 0
            and forecast_confidence >= 0.45
        ):
            score_weight = strategy_weight * 0.75
            score_tier = "confirmed"
            score_reasons.append("composite>=0.30 and forecast edge>=0.10")
        elif (
            composite >= 0.12
            and forecast.up_probability >= 0.50
            and forecast_edge >= 0.20
            and expected_return > 0
            and forecast_confidence >= 0.55
        ):
            score_weight = strategy_weight * 0.35
            score_tier = "exploratory"
            score_reasons.append("watch-band composite with strong positive forecast edge")
        else:
            score_blockers.append("score_tier_conditions_not_met")
    output["etf_score_tiered_v1"] = _allocation(
        score_weight,
        score_tier,
        score_blockers,
        score_reasons,
    )

    forecast_weight = 0.0
    forecast_blockers = list(blockers)
    forecast_reasons: list[str] = []
    if not forecast_blockers:
        conditions = {
            "composite_below_0.12": composite < 0.12,
            "strategy_score_below_0.20": strategy_score < 0.20,
            "up_probability_below_0.50": forecast.up_probability < 0.50,
            "forecast_edge_below_0.20": forecast_edge < 0.20,
            "non_positive_expected_return": expected_return <= 0,
            "forecast_confidence_below_0.55": forecast_confidence < 0.55,
        }
        forecast_blockers.extend(name for name, failed in conditions.items() if failed)
        if not forecast_blockers:
            forecast_weight = strategy_weight * 0.50
            forecast_reasons.append("strategy, composite and calibrated forecast agree")
    output["etf_forecast_confirmed_v1"] = _allocation(
        forecast_weight,
        "confirmed" if forecast_weight > 0 else "blocked",
        forecast_blockers,
        forecast_reasons,
    )

    ablation_weight = 0.0
    ablation_blockers = list(blockers)
    ablation_reasons: list[str] = []
    if not ablation_blockers:
        if expected_return < 0:
            ablation_blockers.append("negative_expected_return")
        if forecast_edge <= -0.10:
            ablation_blockers.append("materially_bearish_forecast_edge")
        if not ablation_blockers:
            ablation_weight = strategy_weight
            ablation_reasons.append("no reviewed materially bearish forecast")
    output["strategy_unless_bearish_v1"] = _allocation(
        ablation_weight,
        "full_strategy" if ablation_weight > 0 else "blocked",
        ablation_blockers,
        ablation_reasons,
    )

    calibrated_weight = 0.0
    calibrated_blockers = []
    calibrated_reasons: list[str] = []
    if bool(decision_trace.get("council_diagnostics", {}).get("veto_triggered")):
        calibrated_blockers.append("council_veto")
    if strategy_score <= 0:
        calibrated_blockers.append("non_positive_strategy_signal")
    statistical_weight = float(forecast.statistical_weight or 0.0)
    statistical_edge = None
    if (
        forecast.statistical_up_probability is not None
        and forecast.statistical_down_probability is not None
    ):
        statistical_edge = float(
            forecast.statistical_up_probability - forecast.statistical_down_probability
        )
    if not calibrated_blockers:
        calibrated_weight = strategy_weight
        if (
            forecast.statistical_model_id
            and statistical_weight >= 0.25
            and statistical_edge is not None
            and statistical_edge <= -0.15
        ):
            calibrated_weight *= 0.50
            calibrated_reasons.append("validated statistical model is materially bearish; halve risk")
        elif forecast.statistical_model_id:
            calibrated_reasons.append("validated statistical model does not justify a risk reduction")
        else:
            calibrated_reasons.append("no point-in-time statistical model; uncalibrated LLM is advisory")
    output["calibrated_strategy_primary_v1"] = _allocation(
        calibrated_weight,
        "risk_reduced"
        if 0 < calibrated_weight < strategy_weight
        else "strategy_primary"
        if calibrated_weight > 0
        else "blocked",
        calibrated_blockers,
        calibrated_reasons,
    )

    for item in output.values():
        item["diagnostics"] = {
            "composite_score": composite,
            "strategy_signal_score": strategy_score,
            "decision_confidence": confidence,
            "forecast_up_probability": float(forecast.up_probability),
            "forecast_down_probability": float(forecast.down_probability),
            "forecast_edge": forecast_edge,
            "forecast_expected_return_pct": expected_return,
            "forecast_confidence": forecast_confidence,
            "statistical_model_id": forecast.statistical_model_id,
            "statistical_weight": statistical_weight,
            "statistical_edge": statistical_edge,
        }
    return output


def _safety_blockers(
    *,
    decision_trace: dict[str, Any],
    reviewer: ReviewReport,
    confidence: float,
    strategy_score: float,
) -> list[str]:
    diagnostics = decision_trace.get("council_diagnostics", {})
    checks = {
        "reviewer_not_approved": not reviewer.approved,
        "council_veto": bool(diagnostics.get("veto_triggered")),
        "high_conflict_review": bool(decision_trace.get("high_conflict_review_triggered")),
        "decision_confidence_below_0.25": confidence < 0.25,
        "non_positive_strategy_signal": strategy_score <= 0,
    }
    return [name for name, failed in checks.items() if failed]


def _current_blockers(decision: DecisionCard, reviewer: ReviewReport) -> list[str]:
    blockers = []
    if decision.action not in {"buy", "add"}:
        blockers.append(f"production_action={decision.action}")
    if decision.requires_human_review:
        blockers.append("human_review_required")
    if not reviewer.approved:
        blockers.append("reviewer_not_approved")
    if float(decision.target_weight) <= 0:
        blockers.append("zero_decision_target_weight")
    return blockers


def _allocation(
    weight: float,
    tier: str,
    blockers: list[str],
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "policy_version": ETF_GATE_POLICY_VERSION,
        "eligible": weight > 0,
        "tier": tier,
        "target_weight": max(0.0, float(weight)),
        "blockers": list(dict.fromkeys(blockers)),
        "reasons": list(dict.fromkeys(reasons)),
    }


def _bounded_weight(weight: float, strategy_weight: float) -> float:
    return max(0.0, min(float(weight), max(0.0, float(strategy_weight))))
