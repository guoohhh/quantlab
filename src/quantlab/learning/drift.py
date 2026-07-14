from __future__ import annotations

import numpy as np

from quantlab.learning.repository import LearningRepository


LABELS = ("up", "flat", "down")


def monitor_active_model(
    repository: LearningRepository,
    horizon_days: int,
    asset_scope: str,
    minimum_online_samples: int = 30,
    recent_window: int = 50,
    degradation_factor: float = 1.25,
) -> dict:
    active = repository.active_model(horizon_days, asset_scope)
    if active is None:
        return {
            "status": "no_active_model",
            "horizon_days": horizon_days,
            "asset_scope": asset_scope,
        }
    samples = repository.completed_live_samples(horizon_days, asset_scope, active["model_id"])
    samples = [
        item
        for item in samples
        if all(
            value is not None
            for value in item["context"].get("forecast_components", {}).get("statistical", [])
        )
    ]
    if len(samples) < minimum_online_samples:
        return {
            "status": "insufficient_online_samples",
            "horizon_days": horizon_days,
            "asset_scope": asset_scope,
            "model_id": active["model_id"],
            "samples": len(samples),
            "minimum_online_samples": minimum_online_samples,
        }
    recent = samples[-recent_window:]
    metrics = {
        "statistical_brier": _component_brier(recent, "statistical"),
        "final_brier": _component_brier(recent, "final"),
        "raw_llm_brier": _component_brier(recent, "raw_llm"),
        "validation_brier": float(active["metrics"]["brier_score"]),
        "baseline_brier": float(active["metrics"]["baseline_brier"]),
        "window": len(recent),
    }
    degraded = metrics["statistical_brier"] > min(
        metrics["baseline_brier"],
        metrics["validation_brier"] * degradation_factor,
    )
    action = "deactivated" if degraded else "healthy"
    if degraded:
        repository.deactivate_model(
            active["model_id"],
            "online statistical Brier exceeded drift threshold",
        )
    repository.record_monitoring(
        active["model_id"],
        horizon_days,
        asset_scope,
        len(recent),
        metrics,
        action,
    )
    return {
        "status": action,
        "horizon_days": horizon_days,
        "asset_scope": asset_scope,
        "model_id": active["model_id"],
        "samples": len(recent),
        "metrics": metrics,
    }


def monitor_all_models(
    repository: LearningRepository,
    minimum_online_samples: int = 30,
    recent_window: int = 50,
    degradation_factor: float = 1.25,
) -> list[dict]:
    output = []
    for asset_scope in ("etf", "stock", "convertible_bond"):
        for horizon in (5, 20):
            output.append(
                monitor_active_model(
                    repository,
                    horizon,
                    asset_scope,
                    minimum_online_samples,
                    recent_window,
                    degradation_factor,
                )
            )
    return output


def _component_brier(samples: list[dict], component: str) -> float:
    scores = []
    for sample in samples:
        probabilities = np.asarray(
            sample["context"]["forecast_components"].get(component), dtype=float
        )
        if len(probabilities) != 3 or not np.isfinite(probabilities).all():
            continue
        actual = np.zeros(3)
        actual[LABELS.index(sample["outcome"])] = 1.0
        scores.append(float(np.sum((probabilities - actual) ** 2) / 3))
    return float(np.mean(scores)) if scores else 1.0
