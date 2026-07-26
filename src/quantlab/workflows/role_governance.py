from __future__ import annotations

import math
from typing import Any

from quantlab.config import Settings
from quantlab.persistence import EvidenceRepository, NotificationRepository


def record_role_outcome(
    settings: Settings,
    *,
    role: str,
    run_id: str,
    symbol: str,
    as_of: str,
    horizon_days: int,
    probabilities: dict[str, float],
    realized_direction: str,
    realized_return_pct: float,
    market_regime: str | None = None,
    drawdown_reduction: float | None = None,
    fact_errors: int = 0,
    quant_incremental_return_pct: float | None = None,
    cost_usd: float = 0.0,
    latency_ms: float = 0.0,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if realized_direction not in {"up", "flat", "down"}:
        raise ValueError("realized direction must be up, flat or down")
    vector = [
        float(probabilities.get("up", 0)),
        float(probabilities.get("flat", 0)),
        float(probabilities.get("down", 0)),
    ]
    total = sum(vector)
    if abs(total - 1.0) > 0.02 or any(value < 0 or value > 1 for value in vector):
        raise ValueError("role probabilities must be valid and sum to one")
    labels = ["up", "flat", "down"]
    predicted = labels[max(range(3), key=lambda index: vector[index])]
    actual = [1.0 if label == realized_direction else 0.0 for label in labels]
    brier = sum((probability - target) ** 2 for probability, target in zip(vector, actual))
    actual_probability = max(1e-12, vector[labels.index(realized_direction)])
    log_loss = -math.log(actual_probability)
    return _repository(settings).record_role_observation(
        {
            "role": role,
            "run_id": run_id,
            "symbol": symbol,
            "as_of": as_of,
            "horizon_days": horizon_days,
            "market_regime": market_regime,
            "up_probability": vector[0],
            "flat_probability": vector[1],
            "down_probability": vector[2],
            "predicted_direction": predicted,
            "realized_direction": realized_direction,
            "realized_return_pct": realized_return_pct,
            "direction_correct": predicted == realized_direction,
            "brier_score": brier,
            "log_loss": log_loss,
            "drawdown_reduction": drawdown_reduction,
            "fact_errors": fact_errors,
            "quant_incremental_return_pct": quant_incremental_return_pct,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "matured": True,
            "payload": payload or {},
        }
    )


def role_scorecard(settings: Settings, role: str) -> dict[str, Any]:
    minimum = int(settings.get("llm.role_minimum_matured_samples", 30))
    return _repository(settings).role_scorecard(role, minimum)


def freeze_role_challenge(settings: Settings, role: str) -> dict[str, Any]:
    minimum = int(settings.get("llm.role_minimum_matured_samples", 30))
    return _repository(settings).freeze_role_challenge(role, minimum_samples=minimum)


def decide_role_challenge(
    settings: Settings,
    challenge_id: str,
    *,
    passed: bool,
    decision: str,
    reason: str,
    applicable_regimes: list[str] | None = None,
) -> dict[str, Any]:
    result = _repository(settings).decide_role_challenge(
        challenge_id,
        passed=passed,
        decision=decision,
        reason=reason,
        applicable_regimes=applicable_regimes,
    )
    NotificationRepository(
        settings.resolve(settings.get("system.database_path"))
    ).emit(
        event_type="model_challenge_completed",
        aggregate_type="llm_role_challenge",
        aggregate_id=challenge_id,
        payload={
            "content": f"角色{result['role']}挑战完成：{decision}",
            "data_as_of": result.get("decided_at"),
            "action_type": "view_role_scorecard",
            "action_payload": {
                "challenge_id": challenge_id,
                "role": result["role"],
                "decision": decision,
                "sample_count": result["sample_count"],
                "applicable_regimes": applicable_regimes or ["all"],
            },
        },
        dedup_key=f"role_challenge:{challenge_id}:{decision}",
    )
    return result


def _repository(settings: Settings) -> EvidenceRepository:
    return EvidenceRepository(settings.resolve(settings.get("system.database_path")))


__all__ = [
    "decide_role_challenge",
    "freeze_role_challenge",
    "record_role_outcome",
    "role_scorecard",
]
