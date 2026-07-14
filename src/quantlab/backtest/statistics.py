from __future__ import annotations

import math
from collections import Counter
from datetime import date
from typing import Iterable

import numpy as np


def equity_return_series(
    curve: Iterable[tuple[date, float]],
    start: date,
    end: date,
) -> list[tuple[date, float]]:
    """Return daily returns for a window, retaining the previous equity as the first base."""

    ordered = sorted((day, float(value)) for day, value in curve)
    output: list[tuple[date, float]] = []
    previous: float | None = None
    for day, value in ordered:
        if day < start:
            previous = value
            continue
        if day > end:
            break
        if previous is not None and previous > 0:
            output.append((day, value / previous - 1.0))
        previous = value
    return output


def paired_block_bootstrap(
    strategy_returns: Iterable[float],
    benchmark_returns: Iterable[float],
    *,
    block_size: int = 20,
    simulations: int = 2_000,
    confidence: float = 0.95,
    seed: int = 20260714,
) -> dict[str, float | int | list[float] | str]:
    """Estimate paired excess-return uncertainty while preserving short serial dependence."""

    strategy = np.asarray(list(strategy_returns), dtype=float)
    benchmark = np.asarray(list(benchmark_returns), dtype=float)
    if len(strategy) != len(benchmark):
        raise ValueError("strategy and benchmark returns must be paired")
    if len(strategy) < 2:
        return {
            "status": "insufficient",
            "observations": int(len(strategy)),
            "annualized_alpha": 0.0,
            "annualized_alpha_ci": [0.0, 0.0],
            "probability_alpha_positive": 0.0,
            "one_sided_p_value": 1.0,
            "block_size": 0,
            "simulations": 0,
        }
    excess = strategy - benchmark
    observations = len(excess)
    effective_block = max(1, min(int(block_size), observations))
    draws = max(200, int(simulations))
    rng = np.random.default_rng(seed)
    starts = np.arange(observations)
    bootstrap_means = np.empty(draws, dtype=float)
    null_means = np.empty(draws, dtype=float)
    centered = excess - excess.mean()
    blocks_needed = math.ceil(observations / effective_block)
    offsets = np.arange(effective_block)
    for index in range(draws):
        chosen = rng.choice(starts, size=blocks_needed, replace=True)
        sample_indices = (chosen[:, None] + offsets) % observations
        sample_indices = sample_indices.ravel()[:observations]
        bootstrap_means[index] = float(excess[sample_indices].mean())
        null_means[index] = float(centered[sample_indices].mean())
    tail = (1.0 - confidence) / 2.0
    lower, upper = np.quantile(bootstrap_means, [tail, 1.0 - tail])
    observed_mean = float(excess.mean())
    return {
        "status": "measured",
        "observations": observations,
        "annualized_alpha": observed_mean * 252,
        "annualized_alpha_ci": [float(lower * 252), float(upper * 252)],
        "probability_alpha_positive": float(np.mean(bootstrap_means > 0)),
        "one_sided_p_value": float((1 + np.sum(null_means >= observed_mean)) / (draws + 1)),
        "block_size": effective_block,
        "simulations": draws,
    }


def sharpe_significance(
    returns: Iterable[float],
    *,
    tested_variants: int = 1,
) -> dict[str, float | int | str]:
    """Compute probabilistic Sharpe confidence with a conservative trials correction."""

    values = np.asarray(list(returns), dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 3 or float(values.std(ddof=1)) <= 0:
        return {
            "status": "insufficient",
            "observations": int(len(values)),
            "annualized_sharpe": 0.0,
            "probabilistic_sharpe": 0.0,
            "multiple_testing_adjusted_psr": 0.0,
            "tested_variants": max(1, int(tested_variants)),
        }
    daily_sharpe = float(values.mean() / values.std(ddof=1))
    centered = values - values.mean()
    scale = float(values.std(ddof=1))
    skew = float(np.mean((centered / scale) ** 3))
    kurtosis = float(np.mean((centered / scale) ** 4))
    denominator = max(
        1e-12,
        1.0 - skew * daily_sharpe + ((kurtosis - 1.0) / 4.0) * daily_sharpe**2,
    )
    statistic = daily_sharpe * math.sqrt(len(values) - 1) / math.sqrt(denominator)
    psr = _normal_cdf(statistic)
    trials = max(1, int(tested_variants))
    adjusted = max(0.0, 1.0 - trials * (1.0 - psr))
    return {
        "status": "measured",
        "observations": int(len(values)),
        "annualized_sharpe": daily_sharpe * math.sqrt(252),
        "skewness": skew,
        "kurtosis": kurtosis,
        "probabilistic_sharpe": psr,
        "multiple_testing_adjusted_psr": adjusted,
        "tested_variants": trials,
        "correction": "Bonferroni correction across the parameter variants tested",
    }


def selection_overfit_diagnostics(folds: list[dict]) -> dict[str, float | int | dict | str]:
    """Measure whether training winners remain competitive in their untouched test folds."""

    ranks: list[float] = []
    regrets: list[float] = []
    selected_keys: list[str] = []
    for fold in folds:
        candidates = list(fold.get("candidates", []))
        selected_key = str(fold.get("selected_key", ""))
        if not candidates or not selected_key:
            continue
        ordered = sorted(candidates, key=lambda item: float(item["test_score"]), reverse=True)
        selected_index = next(
            (index for index, item in enumerate(ordered) if item["key"] == selected_key),
            None,
        )
        if selected_index is None:
            continue
        count = len(ordered)
        percentile = 1.0 if count == 1 else 1.0 - selected_index / (count - 1)
        selected_score = float(ordered[selected_index]["test_score"])
        best_score = float(ordered[0]["test_score"])
        ranks.append(percentile)
        regrets.append(best_score - selected_score)
        selected_keys.append(selected_key)
    if not ranks:
        return {
            "status": "insufficient",
            "folds": 0,
            "overfit_fold_rate": 1.0,
            "mean_selected_oos_rank_percentile": 0.0,
            "mean_selection_regret": 0.0,
            "selection_entropy": 0.0,
            "selection_frequency": {},
        }
    frequency = Counter(selected_keys)
    probabilities = np.asarray([count / len(selected_keys) for count in frequency.values()])
    entropy = float(-np.sum(probabilities * np.log(probabilities)))
    normalized_entropy = entropy / math.log(len(frequency)) if len(frequency) > 1 else 0.0
    return {
        "status": "measured",
        "folds": len(ranks),
        "overfit_fold_rate": float(np.mean(np.asarray(ranks) < 0.50)),
        "mean_selected_oos_rank_percentile": float(np.mean(ranks)),
        "mean_selection_regret": float(np.mean(regrets)),
        "selection_entropy": normalized_entropy,
        "selection_frequency": dict(frequency),
        "interpretation": (
            "a fold is flagged when its training winner lands below the median in untouched OOS"
        ),
    }


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))
