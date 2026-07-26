from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DECISION_POLICY = {
    "minimum_confidence_for_provisional_decision": 0.25,
    "buy_composite_threshold": 0.35,
    "watch_composite_threshold": 0.12,
    "reduce_composite_threshold": -0.12,
    "sell_composite_threshold": -0.35,
    "high_conflict_threshold": 0.80,
    "high_conflict_neutral_composite_band": 0.12,
    "council_veto_forces_review_required": True,
    "strategy_signal_target_weight_is_advisory": True,
    "only_buy_action_may_have_nonzero_target_weight": True,
    "maximum_buy_target_weight": 0.15,
    "non_buy_target_weight": 0.0,
    "review_rejection_forces_human_review": True,
    "review_rejection_forces_zero_target_weight": True,
}
DECISION_POLICY_VERSION = "deterministic-decision-policy-v2"


@dataclass(frozen=True)
class DecisionPolicyResult:
    action: str
    target_weight: float
    suggested_weight_min: float
    suggested_weight_max: float
    trigger_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    requires_human_review: bool
    review_state: str
    policy_version: str = DECISION_POLICY_VERSION
    signal_price_basis: str = "adjusted_close"
    execution_price_basis: str = "raw_market_price"

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "target_weight": self.target_weight,
            "suggested_weight_min": self.suggested_weight_min,
            "suggested_weight_max": self.suggested_weight_max,
            "trigger_codes": list(self.trigger_codes),
            "reasons": list(self.reasons),
            "requires_human_review": self.requires_human_review,
            "review_state": self.review_state,
            "policy_version": self.policy_version,
            "signal_price_basis": self.signal_price_basis,
            "execution_price_basis": self.execution_price_basis,
        }


def evaluate_decision_policy(
    *,
    composite_score: float,
    confidence: float,
    evidence_coverage: float,
    conflict: float,
    council_veto: bool,
    veto_roles: list[str] | tuple[str, ...] = (),
    maximum_final_weight: float | None = None,
    reviewer_approved: bool | None = None,
    reviewer_issues: list[str] | tuple[str, ...] = (),
    context_action: str | None = None,
    context_requires_review: bool = False,
    context_weight_max: float | None = None,
    price_is_executable: bool = True,
    signal_price_basis: str = "adjusted_close",
) -> DecisionPolicyResult:
    """Return the sole deterministic action, review state, triggers and weight decision."""
    composite = float(composite_score)
    bounded_confidence = max(0.0, min(1.0, float(confidence)))
    coverage = max(0.0, min(1.0, float(evidence_coverage)))
    bounded_conflict = max(0.0, min(1.0, float(conflict)))
    triggers: list[str] = []
    reasons: list[str] = []

    if composite > DECISION_POLICY["buy_composite_threshold"]:
        action = "buy"
        _trigger(
            triggers,
            reasons,
            "buy_composite_band",
            f"composite_score={composite:.3f}>0.350",
        )
    elif composite > DECISION_POLICY["watch_composite_threshold"]:
        action = "watch"
        _trigger(
            triggers,
            reasons,
            "watch_composite_band",
            f"0.120<composite_score={composite:.3f}<=0.350",
        )
    elif composite < DECISION_POLICY["sell_composite_threshold"]:
        action = "sell"
        _trigger(
            triggers,
            reasons,
            "sell_composite_band",
            f"composite_score={composite:.3f}<-0.350",
        )
    elif composite < DECISION_POLICY["reduce_composite_threshold"]:
        action = "reduce"
        _trigger(
            triggers,
            reasons,
            "reduce_composite_band",
            f"-0.350<=composite_score={composite:.3f}<-0.120",
        )
    else:
        action = "hold"
        _trigger(
            triggers,
            reasons,
            "hold_composite_band",
            f"abs(composite_score={composite:.3f})<=0.120",
        )

    review_triggers: list[str] = []
    if council_veto and DECISION_POLICY["council_veto_forces_review_required"]:
        roles = ",".join(str(role) for role in veto_roles) or "unspecified"
        _trigger(
            review_triggers,
            reasons,
            "council_veto",
            f"council_veto_triggered roles={roles}",
        )
    if coverage < DECISION_POLICY["minimum_confidence_for_provisional_decision"]:
        _trigger(
            review_triggers,
            reasons,
            "insufficient_evidence",
            f"evidence_coverage={coverage:.3f}<0.250",
        )
    if bounded_confidence < DECISION_POLICY["minimum_confidence_for_provisional_decision"]:
        _trigger(
            review_triggers,
            reasons,
            "low_confidence",
            f"confidence={bounded_confidence:.3f}<0.250",
        )
    high_conflict = (
        bounded_conflict > DECISION_POLICY["high_conflict_threshold"]
        and abs(composite) < DECISION_POLICY["high_conflict_neutral_composite_band"]
    )
    if high_conflict:
        _trigger(
            review_triggers,
            reasons,
            "high_conflict",
            f"conflict={bounded_conflict:.3f}>0.800 and abs(composite)={abs(composite):.3f}<0.120",
        )
    if reviewer_approved is False:
        issue_count = len(tuple(reviewer_issues))
        _trigger(
            review_triggers,
            reasons,
            "reviewer_rejected",
            f"reviewer_rejected issue_count={issue_count}",
        )
    if context_requires_review or context_action == "review_required":
        _trigger(
            review_triggers,
            reasons,
            "context_committee_review_required",
            f"context_committee_requires_review action={context_action or 'unspecified'}",
        )

    triggers.extend(review_triggers)
    if review_triggers:
        action = "review_required"
    elif context_action == "reduce" and action != "sell":
        action = "reduce"
        _trigger(
            triggers,
            reasons,
            "context_committee_conservative_reduce",
            "context_committee_action=reduce",
        )
    elif context_action == "avoid" and action not in {"reduce", "sell"}:
        action = "watch"
        _trigger(
            triggers,
            reasons,
            "context_committee_conservative_avoid",
            "context_committee_action=avoid",
        )

    deterministic_maximum = min(
        float(DECISION_POLICY["maximum_buy_target_weight"]),
        float(maximum_final_weight)
        if maximum_final_weight is not None
        else float(DECISION_POLICY["maximum_buy_target_weight"]),
    )
    if context_weight_max is not None:
        deterministic_maximum = min(deterministic_maximum, max(0.0, float(context_weight_max)))
    target_weight = (
        max(0.0, min(deterministic_maximum, composite * 0.25))
        if action == "buy"
        else 0.0
    )
    requires_review = action == "review_required"
    if reviewer_approved is False:
        review_state = "rejected"
    elif requires_review:
        review_state = "required"
    elif reviewer_approved is True:
        review_state = "approved"
    else:
        review_state = "not_evaluated"
    return DecisionPolicyResult(
        action=action,
        target_weight=target_weight,
        suggested_weight_min=target_weight * 0.5 if target_weight > 0 else 0.0,
        suggested_weight_max=target_weight,
        trigger_codes=tuple(dict.fromkeys(triggers)),
        reasons=tuple(dict.fromkeys(reasons)),
        requires_human_review=requires_review,
        review_state=review_state,
        signal_price_basis=signal_price_basis,
        execution_price_basis="raw_market_price" if price_is_executable else "unavailable",
    )


def apply_policy_result(decision: Any, result: DecisionPolicyResult) -> None:
    decision.action = result.action
    decision.target_weight = result.target_weight
    decision.suggested_weight_min = result.suggested_weight_min
    decision.suggested_weight_max = result.suggested_weight_max
    decision.trigger_codes = list(result.trigger_codes)
    decision.reasons = list(result.reasons)
    decision.requires_human_review = result.requires_human_review
    decision.review_state = result.review_state
    decision.policy_version = result.policy_version
    decision.signal_price_basis = result.signal_price_basis
    decision.execution_price_basis = result.execution_price_basis


def align_reviewer_report(review: Any, result: DecisionPolicyResult) -> None:
    model_summary = getattr(review, "model_summary", None) or review.summary
    review.model_summary = model_summary
    review.policy_action = result.action
    review.policy_trigger_codes = list(result.trigger_codes)
    review.policy_version = result.policy_version
    codes = ",".join(result.trigger_codes) or "none"
    review.summary = (
        f"policy_action={result.action}; review_state={result.review_state}; "
        f"trigger_codes={codes}; reviewer_assessment={model_summary}"
    )


def _trigger(codes: list[str], reasons: list[str], code: str, reason: str) -> None:
    codes.append(code)
    reasons.append(f"trigger:{code} {reason}")


__all__ = [
    "DECISION_POLICY",
    "DECISION_POLICY_VERSION",
    "DecisionPolicyResult",
    "align_reviewer_report",
    "apply_policy_result",
    "evaluate_decision_policy",
]
