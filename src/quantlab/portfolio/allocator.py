from __future__ import annotations

import math
from dataclasses import dataclass

from quantlab.domain.models import MarketRegime


def fractional_kelly(
    win_rate: float, payoff_ratio: float, fraction: float = 0.25, cap: float = 0.15
) -> float:
    if not 0 <= win_rate <= 1 or payoff_ratio <= 0:
        return 0.0
    full = win_rate - (1 - win_rate) / payoff_ratio
    return max(0.0, min(cap, full * max(0.0, min(1.0, fraction))))


@dataclass(frozen=True)
class StrategyStats:
    name: str
    sharpe_oos: float = 0.0
    calmar_oos: float = 0.0
    recent_return: float = 0.0
    max_drawdown: float = 0.0
    turnover: float = 0.0
    data_quality: float = 1.0
    calibrated: bool = False


class DynamicStrategyAllocator:
    REGIME_BIAS = {
        MarketRegime.BULL: {
            "etf_rotation": 0.25,
            "stock_reversal": 0.15,
            "convertible_bond_double_low": -0.05,
        },
        MarketRegime.RANGE: {
            "etf_rotation": 0.05,
            "stock_reversal": 0.20,
            "convertible_bond_double_low": 0.15,
        },
        MarketRegime.BEAR: {
            "etf_rotation": -0.10,
            "stock_reversal": -0.20,
            "convertible_bond_double_low": 0.20,
        },
        MarketRegime.HIGH_VOLATILITY: {
            "etf_rotation": -0.15,
            "stock_reversal": -0.25,
            "convertible_bond_double_low": 0.10,
        },
    }

    def allocate(
        self,
        stats: list[StrategyStats],
        regime: MarketRegime,
        bounds: dict[str, tuple[float, float]],
        total_budget: float = 0.80,
        temperature: float = 0.7,
    ) -> dict[str, float]:
        if not stats:
            return {}
        scores = {}
        for item in stats:
            score = (
                0.35 * item.sharpe_oos
                + 0.20 * item.calmar_oos
                + 0.15 * item.recent_return
                - 0.20 * abs(item.max_drawdown)
                - 0.05 * item.turnover
                + 0.15 * (item.data_quality - 0.5)
                + self.REGIME_BIAS[regime].get(item.name, 0.0)
            )
            scores[item.name] = score
        peak = max(scores.values())
        exp_scores = {
            name: math.exp((score - peak) / max(temperature, 0.05))
            for name, score in scores.items()
        }
        denominator = sum(exp_scores.values())
        raw = {name: total_budget * value / denominator for name, value in exp_scores.items()}
        by_name = {item.name: item for item in stats}
        clipped = {}
        for name, value in raw.items():
            lower, upper = bounds[name]
            if not by_name[name].calibrated:
                upper = min(upper, lower + 0.05)
            clipped[name] = min(upper, max(lower, value))
        total = sum(clipped.values())
        if total > total_budget:
            scale = total_budget / total
            clipped = {name: value * scale for name, value in clipped.items()}
        return clipped
