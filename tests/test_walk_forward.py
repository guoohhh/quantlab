from datetime import date, timedelta

import pytest

from quantlab.backtest import (
    aggregate_fold_metrics,
    equity_return_series,
    paired_block_bootstrap,
    selection_overfit_diagnostics,
    sharpe_significance,
    walk_forward_splits,
)
from quantlab.workflows.validation import evaluate_strategy_admission


def test_walk_forward_splits_keep_training_strictly_before_test():
    start = date(2020, 1, 1)
    dates = [start + timedelta(days=index) for index in range(30)]
    folds = walk_forward_splits(dates, train_days=10, test_days=5)

    assert len(folds) == 4
    assert all(fold.train_end < fold.test_start for fold in folds)
    assert folds[1].train_start == dates[5]
    assert folds[0].to_dict()["train_start"] == "2020-01-01"


def test_walk_forward_aggregation_compounds_independent_folds():
    output = aggregate_fold_metrics(
        [
            {"total_return": 0.10, "annualized_return": 0.10, "sharpe": 1.0, "max_drawdown": -0.1},
            {
                "total_return": -0.05,
                "annualized_return": -0.05,
                "sharpe": -0.2,
                "max_drawdown": -0.2,
            },
        ]
    )

    assert round(output["compounded_return"], 4) == 0.045
    assert output["positive_fold_rate"] == 0.5
    assert output["worst_max_drawdown"] == -0.2


def test_walk_forward_embargo_keeps_a_gap_between_training_and_test():
    start = date(2020, 1, 1)
    dates = [start + timedelta(days=index) for index in range(40)]

    folds = walk_forward_splits(dates, train_days=10, test_days=5, embargo_days=2)

    assert folds[0].train_end == dates[9]
    assert folds[0].test_start == dates[12]
    assert folds[0].embargo_days == 2


def test_equity_return_series_uses_pre_window_equity_and_stops_at_end():
    start = date(2026, 1, 2)
    curve = [
        (date(2026, 1, 1), 100.0),
        (date(2026, 1, 2), 110.0),
        (date(2026, 1, 3), 99.0),
        (date(2026, 1, 4), 120.0),
    ]

    returns = equity_return_series(curve, start, date(2026, 1, 3))

    assert [day for day, _ in returns] == [start, date(2026, 1, 3)]
    assert [value for _, value in returns] == pytest.approx([0.1, -0.1])


def test_paired_block_bootstrap_and_trials_adjustment_are_deterministic():
    benchmark = [0.0, 0.001, -0.001, 0.0] * 80
    strategy = [value + 0.001 for value in benchmark]

    inference = paired_block_bootstrap(strategy, benchmark, block_size=10, simulations=500)
    significance = sharpe_significance(strategy, tested_variants=27)

    assert inference["probability_alpha_positive"] == 1.0
    assert inference["annualized_alpha_ci"][0] > 0
    assert significance["probabilistic_sharpe"] >= significance["multiple_testing_adjusted_psr"]
    assert significance["tested_variants"] == 27


def test_statistical_helpers_reject_unpaired_or_insufficient_inputs():
    with pytest.raises(ValueError, match="paired"):
        paired_block_bootstrap([0.1], [0.1, 0.2])

    assert paired_block_bootstrap([0.1], [0.0])["status"] == "insufficient"
    assert sharpe_significance([0.0, 0.0])["status"] == "insufficient"
    assert selection_overfit_diagnostics([])["status"] == "insufficient"
    assert selection_overfit_diagnostics(
        [{"selected_key": "missing", "candidates": [{"key": "other", "test_score": 1.0}]}]
    )["status"] == "insufficient"


def test_selection_overfit_flags_training_winner_that_fails_oos():
    diagnostic = selection_overfit_diagnostics(
        [
            {
                "selected_key": "winner",
                "candidates": [
                    {"key": "winner", "train_score": 2.0, "test_score": -1.0},
                    {"key": "stable", "train_score": 1.0, "test_score": 1.0},
                    {"key": "middle", "train_score": 0.5, "test_score": 0.2},
                ],
            }
        ]
    )

    assert diagnostic["overfit_fold_rate"] == 1.0
    assert diagnostic["mean_selected_oos_rank_percentile"] == 0.0


def test_strategy_admission_rejects_positive_returns_that_lose_to_equal_weight():
    admission = evaluate_strategy_admission(
        {
            "folds": 9,
            "positive_fold_rate": 0.8,
            "mean_sharpe": 1.2,
            "worst_max_drawdown": -0.13,
        },
        {
            "equal_weight_buy_hold": {
                "compounded_return_delta": -0.08,
                "mean_sharpe_delta": -0.5,
                "worst_drawdown_delta": -0.06,
            }
        },
    )

    assert admission["absolute_gate"]["passed"] is True
    assert admission["benchmark_gate"]["passed"] is False
    assert admission["passed"] is False
