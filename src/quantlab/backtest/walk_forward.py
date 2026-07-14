from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class WalkForwardFold:
    fold: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    embargo_days: int = 0

    def to_dict(self) -> dict:
        return {
            "fold": self.fold,
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "test_start": self.test_start.isoformat(),
            "test_end": self.test_end.isoformat(),
            "embargo_days": self.embargo_days,
        }


def walk_forward_splits(
    dates: Iterable[date],
    train_days: int,
    test_days: int,
    step_days: int | None = None,
    anchored: bool = False,
    embargo_days: int = 0,
) -> list[WalkForwardFold]:
    ordered = sorted(set(dates))
    if train_days <= 0 or test_days <= 0:
        raise ValueError("train_days and test_days must be positive")
    step = step_days or test_days
    if step <= 0:
        raise ValueError("step_days must be positive")
    if embargo_days < 0:
        raise ValueError("embargo_days cannot be negative")
    folds: list[WalkForwardFold] = []
    test_start_index = train_days + embargo_days
    while test_start_index + test_days <= len(ordered):
        train_end_index = test_start_index - embargo_days - 1
        train_start_index = 0 if anchored else train_end_index - train_days + 1
        folds.append(
            WalkForwardFold(
                fold=len(folds) + 1,
                train_start=ordered[train_start_index],
                train_end=ordered[train_end_index],
                test_start=ordered[test_start_index],
                test_end=ordered[test_start_index + test_days - 1],
                embargo_days=embargo_days,
            )
        )
        test_start_index += step
    return folds


def aggregate_fold_metrics(metrics: list[dict[str, float]]) -> dict[str, float]:
    if not metrics:
        return {
            "folds": 0.0,
            "compounded_return": 0.0,
            "mean_annualized_return": 0.0,
            "mean_sharpe": 0.0,
            "worst_max_drawdown": 0.0,
            "positive_fold_rate": 0.0,
            "stability_score": 0.0,
        }
    returns = np.asarray([item.get("total_return", 0.0) for item in metrics], dtype=float)
    annualized = np.asarray([item.get("annualized_return", 0.0) for item in metrics], dtype=float)
    sharpes = np.asarray([item.get("sharpe", 0.0) for item in metrics], dtype=float)
    drawdowns = np.asarray([item.get("max_drawdown", 0.0) for item in metrics], dtype=float)
    compounded = float(math.prod(1 + value for value in returns) - 1)
    dispersion = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0
    stability = float(np.mean(returns) / (dispersion + 1e-9))
    return {
        "folds": float(len(metrics)),
        "compounded_return": compounded,
        "mean_annualized_return": float(np.mean(annualized)),
        "mean_sharpe": float(np.mean(sharpes)),
        "worst_max_drawdown": float(np.min(drawdowns)),
        "positive_fold_rate": float(np.mean(returns > 0)),
        "stability_score": stability,
    }


def robust_selection_score(metrics: dict[str, float]) -> float:
    """Prefer risk-adjusted return and penalize drawdown and excessive turnover."""

    return float(
        metrics.get("sharpe", 0.0)
        + 0.50 * metrics.get("annualized_return", 0.0)
        - 2.0 * abs(metrics.get("max_drawdown", 0.0))
        - 0.0005 * metrics.get("turnover_count", 0.0)
    )
