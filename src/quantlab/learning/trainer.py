from __future__ import annotations

from pathlib import Path
from datetime import date
from typing import Callable

import numpy as np

from quantlab.learning.features import FEATURE_NAMES, feature_vector
from quantlab.learning.model import ModelEvaluation, OnlineSoftmaxModel, label_index
from quantlab.learning.repository import LearningRepository


def train_registered_model(
    repository: LearningRepository,
    horizon_days: int,
    asset_scope: str,
    minimum_samples: int = 100,
    minimum_validation_samples: int = 20,
    validation_fraction: float = 0.20,
    validation_folds: int = 3,
    minimum_fold_pass_rate: float = 0.67,
    force: bool = False,
) -> dict:
    all_samples = repository.completed_samples(horizon_days, asset_scope)
    samples = [item for item in all_samples if bool(item.get("training_eligible"))]
    excluded_samples = len(all_samples) - len(samples)
    if len(samples) < minimum_samples:
        return {
            "status": "insufficient_samples",
            "horizon_days": horizon_days,
            "asset_scope": asset_scope,
            "samples": len(samples),
            "minimum_samples": minimum_samples,
            "excluded_ineligible_samples": excluded_samples,
        }
    latest_evaluated = max(item["evaluated_at"] for item in samples if item["evaluated_at"])
    active = repository.active_model(horizon_days, asset_scope)
    if (
        not force
        and active
        and active["trained_until"] >= latest_evaluated
        and active["metrics"].get("total_samples") == len(samples)
    ):
        return {
            "status": "up_to_date",
            "horizon_days": horizon_days,
            "asset_scope": asset_scope,
            "samples": len(samples),
            "excluded_ineligible_samples": excluded_samples,
            "model_id": active["model_id"],
            "version": active["version"],
        }
    folds = _rolling_time_evaluations(
        samples,
        minimum_samples,
        minimum_validation_samples,
        validation_fraction,
        validation_folds,
    )
    if not folds:
        return {
            "status": "insufficient_time_split",
            "horizon_days": horizon_days,
            "asset_scope": asset_scope,
            "samples": len(samples),
            "minimum_samples": minimum_samples,
            "minimum_validation_samples": minimum_validation_samples,
        }
    evaluation = _aggregate_evaluations([item["evaluation"] for item in folds])
    fold_pass_rate = sum(item["improved"] for item in folds) / len(folds)
    baseline_improved = (
        evaluation.brier_score < evaluation.baseline_brier
        and evaluation.log_loss < evaluation.baseline_log_loss
        and fold_pass_rate >= minimum_fold_pass_rate
    )
    last_training = folds[-1]["training"]
    candidate, calibration = _fit_time_calibrated_model(last_training)
    champion_challenger = _champion_challenger_evaluation(
        active,
        candidate,
        folds[-1],
    )
    if active is None:
        activate = baseline_improved
        decision = "activated" if activate else "rejected"
        decision_reason = (
            "candidate beat class-prior baselines across enough rolling folds"
            if activate
            else "candidate failed rolling baseline admission"
        )
    else:
        activate = bool(
            baseline_improved
            and champion_challenger.get("status") == "measured"
            and champion_challenger.get("candidate_brier", 1)
            < champion_challenger.get("champion_brier", 0)
            and champion_challenger.get("candidate_log_loss", 1)
            <= champion_challenger.get("champion_log_loss", 0)
            and champion_challenger.get("probability_candidate_brier_better", 0) >= 0.90
        )
        if activate:
            decision = "promoted"
            decision_reason = (
                "candidate beat the incumbent on a prospective time holdout with paired confidence"
            )
        elif champion_challenger.get("status") != "measured":
            decision = "challenge_pending"
            decision_reason = str(champion_challenger.get("reason", "prospective holdout unavailable"))
        else:
            decision = "rejected"
            decision_reason = "candidate did not beat the incumbent promotion thresholds"
    if activate:
        candidate, calibration = _fit_time_calibrated_model(samples)
    validation_start = folds[0]["validation_start"]
    validation_samples = sum(len(item["validation"]) for item in folds)
    registry = repository.save_model(
        horizon_days=horizon_days,
        asset_scope=asset_scope,
        trained_until=latest_evaluated,
        parameters_json=candidate.dumps(),
        metrics={
            **evaluation.as_dict(),
            "validation_start": validation_start,
            "validation_folds": [
                {
                    "fold": index + 1,
                    "validation_start": item["validation_start"],
                    "validation_end": item["validation_end"],
                    "training_samples": len(item["training"]),
                    "validation_samples": len(item["validation"]),
                    "improved": item["improved"],
                    "probability_calibration": item["calibration"],
                    **item["evaluation"].as_dict(),
                }
                for index, item in enumerate(folds)
            ],
            "fold_pass_rate": fold_pass_rate,
            "minimum_fold_pass_rate": minimum_fold_pass_rate,
            "admission_rule": (
                "aggregate Brier and log loss must beat class-prior baselines and the required "
                "fraction of rolling folds must beat both"
            ),
            "feature_importance": candidate.feature_importance(),
            "probability_calibration": calibration,
            "champion_challenger": champion_challenger,
            "promotion_decision": decision,
            "total_samples": len(samples),
        },
        training_samples=len(samples) if activate else len(last_training),
        validation_samples=validation_samples,
        activate=activate,
    )
    challenge_id = repository.record_model_challenge(
        horizon_days=horizon_days,
        asset_scope=asset_scope,
        champion_model_id=active["model_id"] if active else None,
        candidate_model_id=registry["model_id"],
        decision=decision,
        reason=decision_reason,
        metrics={
            "rolling_baseline_admitted": baseline_improved,
            "fold_pass_rate": fold_pass_rate,
            "aggregate_evaluation": evaluation.as_dict(),
            "champion_challenger": champion_challenger,
        },
    )
    deactivated_legacy_model_id = None
    if (
        not baseline_improved
        and active
        and not isinstance(active.get("metrics", {}).get("validation_folds"), list)
    ):
        repository.deactivate_model(
            active["model_id"],
            "legacy single-split model failed replacement rolling-validation governance",
        )
        deactivated_legacy_model_id = active["model_id"]
    return {
        "status": decision,
        "decision_reason": decision_reason,
        "challenge_id": challenge_id,
        "horizon_days": horizon_days,
        "asset_scope": asset_scope,
        "training_samples": len(samples) if activate else len(last_training),
        "validation_samples": validation_samples,
        "evaluation": evaluation.as_dict(),
        "validation_folds": len(folds),
        "fold_pass_rate": fold_pass_rate,
        "champion_challenger": champion_challenger,
        "excluded_ineligible_samples": excluded_samples,
        "deactivated_legacy_model_id": deactivated_legacy_model_id,
        **registry,
    }


def predict_active_model(
    repository: LearningRepository,
    horizon_days: int,
    asset_scope: str,
    features: dict[str, float],
) -> dict | None:
    record = repository.active_model(horizon_days, asset_scope)
    if record is None:
        return None
    return _predict_registered_record(record, features)


def predict_registered_model(
    repository: LearningRepository,
    model_id: str,
    features: dict[str, float],
) -> dict | None:
    record = repository.registered_model(model_id)
    if record is None:
        return None
    return _predict_registered_record(record, features)


def _predict_registered_record(
    record: dict,
    features: dict[str, float],
) -> dict:
    model = OnlineSoftmaxModel.loads(record["parameters_json"])
    probabilities = model.predict_proba(feature_vector(features, model.feature_names))[0]
    metrics = record["metrics"]
    baseline = max(float(metrics.get("baseline_brier", 0)), 1e-9)
    improvement = max(0.0, (baseline - float(metrics.get("brier_score", baseline))) / baseline)
    evidence = min(1.0, record["validation_samples"] / 250)
    weight = min(0.50, (0.10 + 0.40 * evidence) * min(1.0, improvement / 0.10))
    promotion_decision = metrics.get("promotion_decision")
    governed = promotion_decision in {"activated", "promoted"}
    if not governed:
        weight = 0.0
    return {
        "model_id": record["model_id"],
        "version": record["version"],
        "horizon_days": int(record["horizon_days"]),
        "asset_scope": str(record["asset_scope"]),
        "up_probability": float(probabilities[0]),
        "flat_probability": float(probabilities[1]),
        "down_probability": float(probabilities[2]),
        "ensemble_weight": weight,
        "governance_status": "governed" if governed else "legacy_awaiting_challenge",
        "validation_metrics": metrics,
    }


def build_predictor(
    path: str | Path, asset_scope: str
) -> Callable[[int, dict[str, float]], dict | None]:
    repository = LearningRepository(path)

    def predict(horizon_days: int, features: dict[str, float]):
        return predict_active_model(repository, horizon_days, asset_scope, features)

    return predict


def build_point_in_time_predictor(
    repository: LearningRepository,
    asset_scope: str,
    cutoff: date,
    *,
    minimum_samples: int = 100,
    minimum_validation_samples: int = 20,
    validation_fraction: float = 0.20,
    validation_folds: int = 3,
    minimum_fold_pass_rate: float = 0.67,
    maximum_weight: float = 0.50,
) -> tuple[Callable[[int, dict[str, float]], dict | None], dict]:
    """Fit replay-only models using labels that were already known before ``cutoff``."""

    cutoff_text = cutoff.isoformat()
    cache: dict[int, dict | None] = {}
    audit: dict = {
        "cutoff": cutoff_text,
        "asset_scope": asset_scope,
        "horizons": {},
        "contract": "as_of < cutoff and evaluated_at < cutoff",
    }

    def prepare(horizon_days: int) -> dict | None:
        samples = [
            item
            for item in repository.completed_samples(horizon_days, asset_scope)
            if bool(item.get("training_eligible"))
            and item["as_of"] < cutoff_text
            and item.get("evaluated_at")
            and item["evaluated_at"] < cutoff_text
        ]
        if len(samples) < minimum_samples:
            audit["horizons"][horizon_days] = {
                "status": "insufficient_samples",
                "samples": len(samples),
                "minimum_samples": minimum_samples,
            }
            return None
        folds = _rolling_time_evaluations(
            samples,
            minimum_samples,
            minimum_validation_samples,
            validation_fraction,
            validation_folds,
        )
        if not folds:
            audit["horizons"][horizon_days] = {
                "status": "insufficient_time_split",
                "samples": len(samples),
            }
            return None
        evaluation = _aggregate_evaluations([item["evaluation"] for item in folds])
        fold_pass_rate = sum(item["improved"] for item in folds) / len(folds)
        admitted = bool(
            evaluation.brier_score < evaluation.baseline_brier
            and evaluation.log_loss < evaluation.baseline_log_loss
            and fold_pass_rate >= minimum_fold_pass_rate
        )
        if not admitted:
            audit["horizons"][horizon_days] = {
                "status": "rejected",
                "samples": len(samples),
                "fold_pass_rate": fold_pass_rate,
                **evaluation.as_dict(),
            }
            return None
        model, calibration = _fit_time_calibrated_model(samples)
        baseline = max(evaluation.baseline_brier, 1e-9)
        improvement = max(0.0, (baseline - evaluation.brier_score) / baseline)
        validation_samples = sum(len(item["validation"]) for item in folds)
        evidence = min(1.0, validation_samples / 250)
        weight = min(
            maximum_weight,
            (0.10 + 0.40 * evidence) * min(1.0, improvement / 0.10),
        )
        record = {
            "model": model,
            "model_id": f"pit-{asset_scope}-{horizon_days}-{cutoff_text}",
            "version": 0,
            "ensemble_weight": weight,
        }
        audit["horizons"][horizon_days] = {
            "status": "admitted",
            "samples": len(samples),
            "validation_samples": validation_samples,
            "fold_pass_rate": fold_pass_rate,
            "ensemble_weight": weight,
            "probability_calibration": calibration,
            **evaluation.as_dict(),
        }
        return record

    def predict(horizon_days: int, features: dict[str, float]) -> dict | None:
        if horizon_days not in cache:
            cache[horizon_days] = prepare(horizon_days)
        record = cache[horizon_days]
        if record is None:
            return None
        probabilities = record["model"].predict_proba(
            feature_vector(features, record["model"].feature_names)
        )[0]
        return {
            "model_id": record["model_id"],
            "version": record["version"],
            "horizon_days": horizon_days,
            "asset_scope": asset_scope,
            "up_probability": float(probabilities[0]),
            "flat_probability": float(probabilities[1]),
            "down_probability": float(probabilities[2]),
            "ensemble_weight": record["ensemble_weight"],
        }

    return predict, audit


def _matrix(samples: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    x = np.vstack([feature_vector(item["features"]) for item in samples])
    y = np.asarray([label_index(item["outcome"]) for item in samples], dtype=int)
    return x, y


def _rolling_time_evaluations(
    samples: list[dict],
    minimum_samples: int,
    minimum_validation_samples: int,
    validation_fraction: float,
    maximum_folds: int,
) -> list[dict]:
    unique_dates = sorted({item["as_of"] for item in samples})
    validation_date_count = max(1, int(len(unique_dates) * validation_fraction))
    candidates = []
    for start_index in range(validation_date_count, len(unique_dates), validation_date_count):
        end_index = min(len(unique_dates), start_index + validation_date_count)
        validation_start = unique_dates[start_index]
        validation_end = unique_dates[end_index - 1]
        training = [
            item
            for item in samples
            if item["as_of"] < validation_start
            and item.get("evaluated_at")
            and item["evaluated_at"] < validation_start
        ]
        validation = [
            item for item in samples if validation_start <= item["as_of"] <= validation_end
        ]
        if len(training) < minimum_samples or len(validation) < minimum_validation_samples:
            continue
        validation_x, validation_y = _matrix(validation)
        model, calibration = _fit_time_calibrated_model(training)
        evaluation = model.evaluate(validation_x, validation_y)
        candidates.append(
            {
                "validation_start": validation_start,
                "validation_end": validation_end,
                "training": training,
                "validation": validation,
                "evaluation": evaluation,
                "calibration": calibration,
                "improved": (
                    evaluation.brier_score < evaluation.baseline_brier
                    and evaluation.log_loss < evaluation.baseline_log_loss
                ),
            }
        )
    return candidates[-max(1, maximum_folds) :]


def _fit_time_calibrated_model(samples: list[dict]) -> tuple[OnlineSoftmaxModel, dict]:
    """Fit on past data and tune conservative probability shrinkage on an inner time split."""

    all_x, all_y = _matrix(samples)
    final_model = OnlineSoftmaxModel(list(FEATURE_NAMES)).fit(all_x, all_y)
    unique_dates = sorted({item["as_of"] for item in samples})
    calibration_dates = max(1, int(len(unique_dates) * 0.20))
    if len(unique_dates) <= calibration_dates:
        return final_model, {"method": "none", "prior_blend": 0.0, "reason": "too_few_dates"}
    calibration_start = unique_dates[-calibration_dates]
    inner_training = [
        item
        for item in samples
        if item["as_of"] < calibration_start
        and item.get("evaluated_at")
        and item["evaluated_at"] < calibration_start
    ]
    calibration_samples = [item for item in samples if item["as_of"] >= calibration_start]
    if len(inner_training) < 100 or len(calibration_samples) < 20:
        return final_model, {
            "method": "none",
            "prior_blend": 0.0,
            "reason": "insufficient_inner_split",
            "inner_training_samples": len(inner_training),
            "calibration_samples": len(calibration_samples),
        }
    inner_x, inner_y = _matrix(inner_training)
    calibration_x, calibration_y = _matrix(calibration_samples)
    tuning_model = OnlineSoftmaxModel(list(FEATURE_NAMES)).fit(inner_x, inner_y)
    raw_probabilities = tuning_model.predict_proba(calibration_x)
    prior = np.tile(tuning_model.class_prior, (len(calibration_y), 1))
    actual = np.eye(3)[calibration_y]
    baseline_brier, baseline_log_loss = _probability_metrics(prior, actual, calibration_y)
    candidates = []
    for prior_blend in np.linspace(0.0, 0.9, 10):
        probabilities = (1 - prior_blend) * raw_probabilities + prior_blend * prior
        brier, log_loss = _probability_metrics(probabilities, actual, calibration_y)
        candidates.append(
            {
                "prior_blend": float(prior_blend),
                "brier_score": brier,
                "log_loss": log_loss,
                "normalized_score": brier / baseline_brier + log_loss / baseline_log_loss,
            }
        )
    selected = min(candidates, key=lambda item: (item["normalized_score"], item["prior_blend"]))
    final_model.prior_blend = selected["prior_blend"]
    return final_model, {
        "method": "inner_time_prior_blend",
        "calibration_start": calibration_start,
        "inner_training_samples": len(inner_training),
        "calibration_samples": len(calibration_samples),
        "baseline_brier": baseline_brier,
        "baseline_log_loss": baseline_log_loss,
        **selected,
    }


def _probability_metrics(
    probabilities: np.ndarray, actual: np.ndarray, labels: np.ndarray
) -> tuple[float, float]:
    brier = float(np.mean(np.sum((probabilities - actual) ** 2, axis=1) / actual.shape[1]))
    log_loss = float(
        -np.mean(np.log(np.clip(probabilities[np.arange(len(labels)), labels], 1e-12, 1)))
    )
    return brier, log_loss


def _aggregate_evaluations(evaluations: list[ModelEvaluation]) -> ModelEvaluation:
    total = sum(item.samples for item in evaluations)
    if total <= 0:
        raise ValueError("cannot aggregate empty model evaluations")

    def weighted(field: str) -> float:
        return sum(getattr(item, field) * item.samples for item in evaluations) / total

    return ModelEvaluation(
        samples=total,
        brier_score=weighted("brier_score"),
        log_loss=weighted("log_loss"),
        accuracy=weighted("accuracy"),
        baseline_brier=weighted("baseline_brier"),
        baseline_log_loss=weighted("baseline_log_loss"),
    )


def _champion_challenger_evaluation(
    active: dict | None,
    candidate: OnlineSoftmaxModel,
    latest_fold: dict,
) -> dict:
    if active is None:
        return {
            "status": "not_applicable",
            "reason": "no incumbent model; rolling baseline admission governs first activation",
        }
    validation_start = str(latest_fold["validation_start"])
    if validation_start <= str(active["trained_until"]):
        return {
            "status": "not_prospective",
            "reason": (
                "latest holdout starts before or on the incumbent trained_until date; "
                "collect newer settled outcomes before promotion"
            ),
            "validation_start": validation_start,
            "champion_trained_until": active["trained_until"],
        }
    validation_x, validation_y = _matrix(latest_fold["validation"])
    champion = OnlineSoftmaxModel.loads(active["parameters_json"])
    candidate_evaluation = candidate.evaluate(validation_x, validation_y)
    champion_evaluation = champion.evaluate(validation_x, validation_y)
    superiority = _paired_brier_superiority(
        candidate,
        champion,
        validation_x,
        validation_y,
    )
    return {
        "status": "measured",
        "validation_start": validation_start,
        "validation_end": latest_fold["validation_end"],
        "samples": len(validation_y),
        "champion_model_id": active["model_id"],
        "candidate_brier": candidate_evaluation.brier_score,
        "champion_brier": champion_evaluation.brier_score,
        "brier_improvement": (
            champion_evaluation.brier_score - candidate_evaluation.brier_score
        ),
        "candidate_log_loss": candidate_evaluation.log_loss,
        "champion_log_loss": champion_evaluation.log_loss,
        **superiority,
        "promotion_rule": (
            "candidate must pass rolling baselines, beat champion Brier and log loss, and have "
            "at least 90% paired-bootstrap probability of lower Brier"
        ),
    }


def _paired_brier_superiority(
    candidate: OnlineSoftmaxModel,
    champion: OnlineSoftmaxModel,
    features: np.ndarray,
    labels: np.ndarray,
    simulations: int = 2_000,
) -> dict[str, float | int]:
    actual = np.eye(3)[labels]
    candidate_loss = np.sum((candidate.predict_proba(features) - actual) ** 2, axis=1) / 3
    champion_loss = np.sum((champion.predict_proba(features) - actual) ** 2, axis=1) / 3
    improvement = champion_loss - candidate_loss
    rng = np.random.default_rng(20260714)
    indices = rng.integers(0, len(improvement), size=(simulations, len(improvement)))
    means = improvement[indices].mean(axis=1)
    return {
        "mean_paired_brier_improvement": float(improvement.mean()),
        "probability_candidate_brier_better": float(np.mean(means > 0)),
        "bootstrap_simulations": simulations,
    }
