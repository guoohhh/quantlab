from quantlab.domain.models import MarketRegime
from quantlab.portfolio.allocator import DynamicStrategyAllocator, StrategyStats


def test_uncalibrated_strategy_budget_is_capped_near_minimum():
    weights = DynamicStrategyAllocator().allocate(
        [
            StrategyStats("etf_rotation", sharpe_oos=5.0, calibrated=False),
            StrategyStats("stock_reversal", sharpe_oos=-5.0, calibrated=True),
        ],
        MarketRegime.BULL,
        {"etf_rotation": (0.20, 0.65), "stock_reversal": (0.10, 0.55)},
    )

    assert weights["etf_rotation"] <= 0.25 + 1e-12
