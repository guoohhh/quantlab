from datetime import date

from quantlab.config import Settings
from quantlab.learning import LearningRepository
from quantlab.workflows.evidence import (
    _evidence_first_portfolio_recommendation,
    assess_profitability_evidence,
    evaluate_probability_ablation,
)


def test_probability_ablation_compares_only_matured_live_forecasts(tmp_path):
    settings = Settings(
        values={
            "system": {"database_path": "quantlab.db"},
            "calibration": {"minimum_samples": 2},
        },
        root=tmp_path,
    )
    repository = LearningRepository(tmp_path / "quantlab.db")
    samples = [
        (
            "up",
            [0.70, 0.20, 0.10],
            [0.45, 0.30, 0.25],
            [0.80, 0.10, 0.10],
        ),
        (
            "down",
            [0.10, 0.20, 0.70],
            [0.40, 0.30, 0.30],
            [0.10, 0.10, 0.80],
        ),
    ]
    for index, (outcome, final, raw, statistical) in enumerate(samples):
        repository.upsert_sample(
            sample_key=f"live-{index}",
            run_id=f"run-{index}",
            source="live_decision",
            asset_scope="etf",
            symbol="sh510300",
            as_of=date(2026, 1, 2 + index),
            horizon_days=5,
            outcome=outcome,
            realized_return_pct=2.0 if outcome == "up" else -2.0,
            evaluated_at=date(2026, 1, 10 + index),
            features={},
            context={
                "forecast_components": {
                    "final": final,
                    "raw_llm": raw,
                    "statistical": statistical,
                }
            },
            origin="system_production_research",
            evidence_stage="registered_forward",
            settlement_eligible=True,
            training_eligible=True,
        )

    result = evaluate_probability_ablation(settings, "etf", 5)[0]

    assert result["status"] == "measured"
    assert result["variants"]["final_ensemble"]["samples"] == 2
    assert result["comparisons"]["final_vs_raw_llm"]["brier_improvement"] > 0
    assert result["variants"]["statistical"]["accuracy"] == 1.0


def test_profitability_grade_requires_statistical_and_stress_admission():
    strategy = {
        "status": "benchmark_compared",
        "statement": "cost-aware OOS",
        "selected_oos": {"folds": 6},
        "embargo_days": 1,
        "reproducibility": {"experiment_payload_sha256": "abc"},
        "robustness": {"oos_daily_observations": 800},
        "admission": {
            "passed": True,
            "benchmark_gate": {"passed": True},
            "statistical_gate": {"passed": True},
            "cost_stress_gate": {"passed": True},
            "data_coverage_gate": {"passed": True},
        },
        "guardrails": ["next-open execution"],
    }

    result = assess_profitability_evidence(
        strategy,
        probability_ablation=[],
        tournament_scorecard={"horizons": {}},
        paper_scorecard={"accounts": []},
    )

    assert result["score"] == 95
    assert result["grade"] == "research_grade"
    assert result["claims_allowed"]["statistically_supported_historical_alpha"] is True


def test_measured_ablation_with_missing_component_does_not_create_increment_claim():
    result = assess_profitability_evidence(
        {
            "status": "benchmark_compared",
            "selected_oos": {"folds": 1},
            "admission": {},
        },
        probability_ablation=[
            {
                "status": "measured",
                "comparisons": {
                    "final_vs_raw_llm": {"brier_improvement": None},
                    "final_vs_statistical": {"brier_improvement": None},
                },
            }
        ],
        tournament_scorecard={"horizons": {}},
        paper_scorecard={"accounts": []},
    )

    assert result["claims_allowed"]["prospective_incremental_value"] is False


def test_evidence_first_recommendation_selects_profitable_investable_benchmark():
    recommendation = _evidence_first_portfolio_recommendation(
        {
            "status": "benchmark_compared",
            "selected_oos": {
                "folds": 18,
                "compounded_return": 0.65,
                "positive_fold_rate": 0.78,
            },
            "benchmark_oos": {
                "equal_weight_buy_hold": {
                    "folds": 18,
                    "compounded_return": 0.84,
                    "positive_fold_rate": 0.78,
                }
            },
            "admission": {"passed": False},
        }
    )

    assert recommendation["selected_policy"] == "equal_weight_core"
    assert recommendation["positive_cost_aware_oos"] is True
