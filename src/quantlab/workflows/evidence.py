from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantlab.config import Settings
from quantlab.learning import LearningRepository
from quantlab.persistence import PaperTradingRepository, TerminalRepository


LABELS = ("up", "flat", "down")


def build_evidence_summary(settings: Settings, asset_scope: str = "etf") -> dict[str, Any]:
    from quantlab.workflows.tournament import candidate_tournament_scorecard

    database_path = settings.resolve(settings.get("system.database_path"))
    terminal = TerminalRepository(database_path)
    learning = LearningRepository(database_path)
    validation = terminal.latest_strategy_validation("etf_rotation")
    core_validation = terminal.latest_strategy_validation("etf_equal_weight_core")
    adaptive_candidate = terminal.latest_strategy_validation(
        "adaptive_etf_rotation_candidate"
    )
    probability_ablation = evaluate_probability_ablation(settings, asset_scope=asset_scope)
    tournament_scorecard = candidate_tournament_scorecard(settings)
    paper_scorecard = PaperTradingRepository(database_path).scorecard()
    strategy_evidence = _strategy_evidence(validation)
    production_core_evidence = _production_core_evidence(core_validation)
    portfolio_recommendation = _evidence_first_portfolio_recommendation(
        strategy_evidence,
        production_core_evidence,
    )
    active_models = [
        model for model in learning.models(asset_scope=asset_scope) if model.get("active")
    ]
    profitability_assessment = assess_profitability_evidence(
        strategy_evidence,
        probability_ablation,
        tournament_scorecard,
        paper_scorecard,
    )
    return {
        "profitability_assessment": profitability_assessment,
        "strategy_validation": strategy_evidence,
        "production_core_validation": production_core_evidence,
        "evidence_first_portfolio": portfolio_recommendation,
        "adaptive_strategy_candidate": _adaptive_strategy_candidate_evidence(
            adaptive_candidate
        ),
        "adaptive_v2_diagnostic": _adaptive_v2_diagnostic_evidence(settings),
        "strategy_robustness_audit": _saved_strategy_report(
            settings,
            "strategy-robustness-audit-latest.json",
            "run strategy-robustness-audit",
        ),
        "adaptive_v3_candidate": _saved_strategy_report(
            settings,
            "strategy-v3-diagnostic-latest.json",
            "run adaptive-etf-v3-audit",
        ),
        "a_share_strategy_v3": _saved_strategy_report(
            settings,
            "a-share-strategy-lab-v3-validation.json",
            "run stock-strategy-lab-v3-development and the one-shot validation",
        ),
        "active_statistical_models": active_models,
        "probability_ablation": probability_ablation,
        "candidate_tournament_scorecard": tournament_scorecard,
        "prospective_paper_scorecard": paper_scorecard,
        "claims": {
            "historical_strategy_evidence": bool(validation),
            "positive_cost_aware_historical_oos_return": portfolio_recommendation.get(
                "positive_cost_aware_oos", False
            ),
            "historical_alpha_statistically_supported": profitability_assessment["claims_allowed"][
                "statistically_supported_historical_alpha"
            ],
            "live_llm_increment_proven": any(
                item["status"] == "measured" for item in probability_ablation
            ),
            "tournament_ranking_measured": any(
                item["evidence_status"] == "measured"
                for item in tournament_scorecard["horizons"].values()
            ),
            "minimum_live_samples": int(settings.get("calibration.minimum_samples", 30)),
        },
        "guardrails": [
            "historical strategy validation and prospective LLM evidence are reported separately",
            "no LLM incremental-value claim is made before enough forecasts mature",
            "lower Brier score and log loss are better; higher accuracy is not sufficient alone",
            "tournament ranking value is compared with the original radar leader and all-candidate equal weight",
            "evidence grade is machine-derived from reproducibility, leakage, OOS, benchmark, statistics, cost stress and prospective samples",
        ],
    }


def assess_profitability_evidence(
    strategy: dict[str, Any],
    probability_ablation: list[dict[str, Any]],
    tournament_scorecard: dict[str, Any],
    paper_scorecard: dict[str, Any],
) -> dict[str, Any]:
    if strategy.get("status") == "missing":
        return {
            "score": 0,
            "grade": "missing",
            "dimensions": {},
            "claims_allowed": {
                "cost_aware_historical_oos": False,
                "historical_benchmark_improvement": False,
                "statistically_supported_historical_alpha": False,
                "prospective_incremental_value": False,
            },
            "blockers": ["run a saved ETF walk-forward experiment"],
            "claim_boundary": "no profitability claim is supported",
        }
    admission = strategy.get("admission", {})
    robustness = strategy.get("robustness", {})
    reproducibility = strategy.get("reproducibility", {})
    selected = strategy.get("selected_oos", {})
    benchmark_passed = bool(admission.get("benchmark_gate", {}).get("passed"))
    statistical_passed = bool(admission.get("statistical_gate", {}).get("passed"))
    stress_passed = bool(admission.get("cost_stress_gate", {}).get("passed"))
    data_coverage_passed = bool(admission.get("data_coverage_gate", {}).get("passed"))
    folds = int(selected.get("folds", 0))
    observations = int(robustness.get("oos_daily_observations", 0))
    embargo_days = int(strategy.get("embargo_days", 0))
    paper_observations = max(
        (int(item.get("snapshots", 0)) for item in paper_scorecard.get("accounts", [])),
        default=0,
    )
    measured_ablations = [
        item for item in probability_ablation if item.get("status") == "measured"
    ]
    positive_ablation = any(
        _comparison_value(item, "final_vs_raw_llm") > 0
        and _comparison_value(item, "final_vs_statistical") >= 0
        for item in measured_ablations
    )
    dimensions = {
        "cost_aware_validation": {
            "points": 5,
            "maximum": 5,
            "passed": True,
            "evidence": strategy.get("statement"),
        },
        "reproducibility": {
            "points": 5 if reproducibility.get("experiment_payload_sha256") else 0,
            "maximum": 5,
            "passed": bool(reproducibility.get("experiment_payload_sha256")),
            "evidence": reproducibility,
        },
        "data_coverage": {
            "points": 10 if data_coverage_passed else 0,
            "maximum": 10,
            "passed": data_coverage_passed,
            "evidence": admission.get("data_coverage_gate"),
        },
        "leakage_controls": {
            "points": 15 if embargo_days >= 1 else 5,
            "maximum": 15,
            "passed": embargo_days >= 1,
            "evidence": {"embargo_days": embargo_days, "guardrails": strategy.get("guardrails")},
        },
        "oos_depth": {
            "points": 15 if folds >= 5 and observations >= 504 else 8 if folds >= 3 else 0,
            "maximum": 15,
            "passed": folds >= 5 and observations >= 504,
            "evidence": {"folds": folds, "daily_observations": observations},
        },
        "benchmark_comparison": {
            "points": 15 if benchmark_passed else 0,
            "maximum": 15,
            "passed": benchmark_passed,
            "evidence": admission.get("benchmark_gate"),
        },
        "statistical_support": {
            "points": 20 if statistical_passed else 0,
            "maximum": 20,
            "passed": statistical_passed,
            "evidence": admission.get("statistical_gate"),
        },
        "cost_stress": {
            "points": 10 if stress_passed else 0,
            "maximum": 10,
            "passed": stress_passed,
            "evidence": admission.get("cost_stress_gate"),
        },
        "prospective_tracking": {
            "points": 5 if paper_observations >= 30 and measured_ablations else 0,
            "maximum": 5,
            "passed": paper_observations >= 30 and bool(measured_ablations),
            "evidence": {
                "paper_observations": paper_observations,
                "measured_probability_ablations": len(measured_ablations),
                "settled_tournament_runs": max(
                    (
                        int(value.get("samples", 0))
                        for value in tournament_scorecard.get("horizons", {}).values()
                    ),
                    default=0,
                ),
            },
        },
    }
    score = sum(int(item["points"]) for item in dimensions.values())
    admission_passed = bool(admission.get("passed"))
    if admission_passed and score >= 80:
        grade = "prospective_supported" if dimensions["prospective_tracking"]["passed"] else "research_grade"
    elif score >= 55:
        grade = "preliminary"
    else:
        grade = "illustrative"
    blockers = [name for name, item in dimensions.items() if not item["passed"]]
    claims_allowed = {
        "cost_aware_historical_oos": True,
        "positive_historical_oos_return": bool(
            selected.get("compounded_return", 0) > 0
            and selected.get("positive_fold_rate", 0) >= 0.50
        ),
        "historical_benchmark_improvement": benchmark_passed,
        "statistically_supported_historical_alpha": statistical_passed,
        "prospective_incremental_value": bool(positive_ablation),
    }
    return {
        "score": score,
        "grade": grade,
        "admission_passed": admission_passed,
        "dimensions": dimensions,
        "claims_allowed": claims_allowed,
        "blockers": blockers,
        "claim_boundary": (
            "research_grade supports a reproducible historical statement, not guaranteed future profit; "
            "prospective_supported additionally requires matured forward observations"
        ),
    }


def export_profitability_evidence(
    settings: Settings,
    asset_scope: str = "etf",
    output_dir: str | Path | None = None,
) -> dict[str, str]:
    evidence = build_evidence_summary(settings, asset_scope)
    target = Path(output_dir) if output_dir else settings.resolve("data/reports")
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "profitability-evidence-latest.json"
    markdown_path = target / "profitability-evidence-latest.md"
    json_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    assessment = evidence["profitability_assessment"]
    strategy = evidence["strategy_validation"]
    recommendation = evidence.get("evidence_first_portfolio", {})
    lines = [
        "# QuantLab 盈利能力证据报告",
        "",
        f"- 生成时间：{datetime.now(UTC).isoformat()}",
        f"- 证据等级：{assessment['grade']}",
        f"- 证据得分：{assessment['score']}/100",
        f"- 策略准入：{assessment.get('admission_passed', False)}",
        f"- 实验 ID：{strategy.get('reproducibility', {}).get('experiment_payload_sha256', 'missing')}",
        f"- 默认可投资政策：{recommendation.get('selected_policy', 'missing')}",
        (
            "- 该政策同协议成本后历史收益："
            f"{recommendation.get('selected_metrics', {}).get('total_return', recommendation.get('selected_metrics', {}).get('compounded_return', 0)):.2%}"
        ),
        "",
        "## 分项证据",
        "",
    ]
    for name, item in assessment.get("dimensions", {}).items():
        lines.append(
            f"- {name}: {item['points']}/{item['maximum']}，passed={item['passed']}"
        )
    lines.extend(
        [
            "",
            "## 可声明边界",
            "",
            *[
                f"- {name}: {allowed}"
                for name, allowed in assessment.get("claims_allowed", {}).items()
            ],
            "",
            f"> {assessment['claim_boundary']}",
        ]
    )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def _evidence_first_portfolio_recommendation(
    strategy: dict[str, Any],
    production_core: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if strategy.get("status") == "missing":
        return {
            "selected_policy": "missing",
            "positive_cost_aware_oos": False,
            "reason": "run the ETF walk-forward experiment first",
        }
    active = dict(strategy.get("selected_oos", {}))
    equal_weight = dict(strategy.get("benchmark_oos", {}).get("equal_weight_buy_hold", {}))
    production_core = production_core or {}
    production_metrics = dict(production_core.get("historical_protocol_metrics", {}))
    production_passed = bool(production_core.get("admission", {}).get("passed"))
    admission_passed = bool(strategy.get("admission", {}).get("passed"))
    if admission_passed or not equal_weight:
        selected_policy = "momentum_rotation"
        selected = active
        comparator = equal_weight
        reason = "active strategy passed every admission gate"
    else:
        selected_policy = "equal_weight_core"
        selected = production_metrics if production_passed else equal_weight
        comparator = active
        reason = (
            "active rotation did not beat the investable equal-weight OOS benchmark; use the "
            "separately validated matching production core protocol"
            if production_passed
            else "active rotation did not pass admission; use the benchmark as the portfolio core"
        )
    positive_history = bool(
        selected.get("total_return", selected.get("compounded_return", 0)) > 0
    )
    return {
        "selected_policy": selected_policy,
        "selected_metrics": selected,
        "active_metrics": active,
        "comparator_metrics": comparator,
        "positive_cost_aware_oos": bool(
            positive_history
            and (
                production_passed
                or (
                    selected.get("positive_fold_rate", 0) >= 0.50
                    and selected.get("folds", 0) >= 3
                )
            )
        ),
        "production_protocol_validated": production_passed,
        "production_protocol": production_core.get("production_core_protocol"),
        "metric_scope": (
            "production_protocol_history" if production_passed else "rolling_oos"
        ),
        "reason": reason,
        "claim_boundary": (
            "This supports a cost-aware historical profitability statement for the matching frozen "
            "production protocol, not a guarantee of future returns or active alpha."
        ),
    }


def _production_core_evidence(validation: dict | None) -> dict[str, Any]:
    if validation is None:
        return {
            "status": "missing",
            "statement": "run etf-core-validation before creating actionable core orders",
        }
    return {
        "status": validation.get("production_core_protocol", {}).get("status"),
        "validation_id": validation.get("validation_id"),
        "experiment_id": validation.get("experiment_id"),
        "requested_range": validation.get("requested_range"),
        "data_coverage": validation.get("data_coverage"),
        "historical_protocol_metrics": validation.get("historical_protocol_metrics", {}),
        "production_core_protocol": validation.get("production_core_protocol"),
        "admission": validation.get("admission", {}),
        "reproducibility": validation.get("reproducibility", {}),
        "guardrails": validation.get("guardrails", []),
        "claim_boundary": validation.get("claim_boundary"),
        "statement": "cost-aware production-matching ETF core evidence is available",
    }


def _comparison_value(item: dict[str, Any], comparison: str) -> float:
    value = item.get("comparisons", {}).get(comparison, {}).get("brier_improvement")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("-inf")
    return number if math.isfinite(number) else float("-inf")


def evaluate_probability_ablation(
    settings: Settings,
    asset_scope: str = "etf",
    horizon_days: int | None = None,
) -> list[dict[str, Any]]:
    repository = LearningRepository(settings.resolve(settings.get("system.database_path")))
    minimum_samples = int(settings.get("calibration.minimum_samples", 30))
    horizons = [horizon_days] if horizon_days else [5, 20]
    output = []
    for horizon in horizons:
        samples = repository.completed_live_samples(horizon, asset_scope)
        variants: dict[str, list[tuple[list[float], str]]] = {
            "final_ensemble": [],
            "raw_llm": [],
            "statistical": [],
        }
        for sample in samples:
            components = sample.get("context", {}).get("forecast_components", {})
            for name, key in (
                ("final_ensemble", "final"),
                ("raw_llm", "raw_llm"),
                ("statistical", "statistical"),
            ):
                probabilities = _probabilities(components.get(key))
                if probabilities is not None:
                    variants[name].append((probabilities, sample["outcome"]))
        metrics = {
            name: _probability_metrics(items, minimum_samples) for name, items in variants.items()
        }
        final_brier = metrics["final_ensemble"].get("brier_score")
        comparisons = {}
        for baseline in ("raw_llm", "statistical"):
            baseline_brier = metrics[baseline].get("brier_score")
            comparisons[f"final_vs_{baseline}"] = {
                "common_interpretation": "positive means the final ensemble has lower Brier score",
                "brier_improvement": (
                    baseline_brier - final_brier
                    if baseline_brier is not None and final_brier is not None
                    else None
                ),
            }
        completed = len(samples)
        output.append(
            {
                "asset_scope": asset_scope,
                "horizon_days": horizon,
                "completed_live_samples": completed,
                "minimum_samples": minimum_samples,
                "status": "measured" if completed >= minimum_samples else "collecting",
                "variants": metrics,
                "comparisons": comparisons,
                "statement": (
                    "prospective ablation has enough samples for reporting"
                    if completed >= minimum_samples
                    else f"need {minimum_samples - completed} more matured live samples"
                ),
            }
        )
    return output


def _strategy_evidence(validation: dict | None) -> dict[str, Any]:
    if validation is None:
        return {
            "status": "missing",
            "statement": "run ETF walk-forward validation before making strategy claims",
        }
    selected = validation.get("selected_oos", {})
    benchmarks = validation.get("benchmark_oos", {})
    return {
        "status": "benchmark_compared" if benchmarks else "strategy_only",
        "validation_id": validation.get("validation_id"),
        "requested_range": validation.get("requested_range"),
        "selected_oos": selected,
        "benchmark_oos": benchmarks,
        "relative_to_benchmarks": validation.get("relative_to_benchmarks", {}),
        "admission": validation.get("admission"),
        "robustness": validation.get("robustness", {}),
        "reproducibility": validation.get("reproducibility", {}),
        "embargo_days": validation.get("embargo_days", 0),
        "guardrails": validation.get("guardrails", []),
        "data_coverage": validation.get("data_coverage", {}),
        "research_candidates": validation.get("parameter_sensitivity", [])[:3],
        "candidate_warning": (
            "parameter-sensitivity leaders were identified with the completed OOS results "
            "and cannot be promoted without a new holdout or prospective period"
        ),
        "statement": (
            "cost-aware rolling out-of-sample strategy and references are available"
            if benchmarks
            else "strategy OOS exists, but rerun validation to add benchmark comparisons"
        ),
    }


def _adaptive_strategy_candidate_evidence(validation: dict | None) -> dict[str, Any]:
    if validation is None:
        return {
            "status": "missing",
            "statement": "run the preregistered adaptive ETF strategy lab",
        }
    holdout = validation.get("locked_holdout", {})
    return {
        "status": validation.get("status"),
        "validation_id": validation.get("validation_id"),
        "experiment_id": validation.get("experiment_id"),
        "selected_candidate": validation.get("selected_candidate"),
        "holdout": holdout,
        "research_only": validation.get("research_only", True),
        "formal_strategy_changed": validation.get("formal_strategy_changed", False),
        "claim_boundary": validation.get("claim_boundary"),
        "statement": (
            "adaptive candidate improved risk efficiency but remains research-only"
            if not holdout.get("admission", {}).get("passed")
            else "adaptive candidate passed historical holdout and awaits prospective evidence"
        ),
    }


def _adaptive_v2_diagnostic_evidence(settings: Settings) -> dict[str, Any]:
    path = settings.resolve(settings.get("system.data_dir")) / "reports" / (
        "adaptive-v2-diagnostic-latest.json"
    )
    if not path.exists():
        return {
            "status": "missing",
            "statement": "run etf-variant-research for the adaptive_v2 challenger",
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "status": payload.get("status"),
        "strategy_variant": payload.get("strategy_variant"),
        "research_only": payload.get("research_only", True),
        "period": payload.get("period"),
        "metrics": payload.get("metrics"),
        "relative_to_equal_weight": payload.get("relative_to_equal_weight"),
        "claim_boundary": payload.get("claim_boundary"),
        "statement": (
            "V2 adds correlation-aware allocation and continuous regime controls, but the saved "
            "historical comparison remains exploratory and cannot establish alpha."
        ),
    }


def _saved_strategy_report(
    settings: Settings,
    filename: str,
    missing_statement: str,
) -> dict[str, Any]:
    path = settings.resolve(settings.get("system.data_dir")) / "reports" / filename
    if not path.exists():
        return {"status": "missing", "statement": missing_statement}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        **payload,
        "statement": (
            "saved retrospective strategy diagnostic; it does not change formal admission"
        ),
    }


def _probabilities(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        probabilities = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(item) or item < 0 for item in probabilities):
        return None
    total = sum(probabilities)
    if total <= 0:
        return None
    return [item / total for item in probabilities]


def _probability_metrics(
    items: list[tuple[list[float], str]], minimum_samples: int
) -> dict[str, Any]:
    if not items:
        return {
            "samples": 0,
            "status": "unavailable",
            "brier_score": None,
            "log_loss": None,
            "accuracy": None,
            "mean_confidence": None,
            "calibration_error": None,
        }
    brier = 0.0
    log_loss = 0.0
    correct = 0
    confidences = []
    calibration_bins: dict[int, list[tuple[float, int]]] = {}
    outcomes = Counter()
    for probabilities, outcome in items:
        if outcome not in LABELS:
            continue
        target_index = LABELS.index(outcome)
        actual = [1.0 if index == target_index else 0.0 for index in range(3)]
        brier += (
            sum((probability - target) ** 2 for probability, target in zip(probabilities, actual))
            / 3
        )
        log_loss -= math.log(max(1e-12, probabilities[target_index]))
        predicted_index = max(range(3), key=probabilities.__getitem__)
        hit = int(predicted_index == target_index)
        correct += hit
        confidence = max(probabilities)
        confidences.append(confidence)
        calibration_bins.setdefault(min(4, int(confidence * 5)), []).append((confidence, hit))
        outcomes[outcome] += 1
    samples = sum(outcomes.values())
    if samples == 0:
        return _probability_metrics([], minimum_samples)
    calibration_error = sum(
        len(values)
        / samples
        * abs(
            sum(confidence for confidence, _ in values) / len(values)
            - sum(hit for _, hit in values) / len(values)
        )
        for values in calibration_bins.values()
    )
    return {
        "samples": samples,
        "status": "measured" if samples >= minimum_samples else "insufficient",
        "brier_score": brier / samples,
        "log_loss": log_loss / samples,
        "accuracy": correct / samples,
        "mean_confidence": sum(confidences) / samples,
        "calibration_error": calibration_error,
        "outcome_distribution": {label: outcomes[label] / samples for label in LABELS},
    }
