from datetime import date, timedelta

from quantlab.execution import CostModel
from quantlab.workflows.stock_discovery import _bar_frame
from quantlab.workflows.stock_strategy_lab_v2 import (
    CANDIDATE_POLICIES,
    _evaluate_v2_policy,
    _validation_admission,
)


def _features(rank: int) -> dict[str, float]:
    value = rank / 11
    return {
        "cross_section_volatility_20_rank": value,
        "cross_section_momentum_20_rank": value,
        "cross_section_momentum_60_rank": value,
        "cross_section_momentum_120_rank": value,
        "factor_path_quality_60": value,
        "factor_rsi_14": value,
        "factor_volume_asymmetry_20": value,
        "factor_composite": value,
        "pullback_strength": 1 - value,
    }


def test_v2_execution_uses_top_four_full_cost_portfolio():
    start = date(2020, 1, 1)
    benchmark_dates = [start + timedelta(days=offset) for offset in range(180)]
    bars = [
        {
            "symbol": "sh000300",
            "date": day,
            "open": 100 + offset * 0.01,
            "close": 100 + offset * 0.01,
            "adjusted_open": 100 + offset * 0.01,
            "adjusted_close": 100 + offset * 0.01,
        }
        for offset, day in enumerate(benchmark_dates)
    ]
    groups = {}
    for episode, signal_offset in enumerate((10, 45, 80, 115)):
        signal = benchmark_dates[signal_offset]
        target = benchmark_dates[signal_offset + 20]
        items = []
        for rank in range(12):
            symbol = f"sh6{episode:02d}{rank:03d}"
            items.append(
                {
                    "symbol": symbol,
                    "as_of": signal.isoformat(),
                    "features": _features(rank),
                    "realized_return_pct": 0.0,
                }
            )
            exit_price = 11.0 if rank < 4 else 9.5
            bars.extend(
                [
                    {"symbol": symbol, "date": signal, "open": 10.0, "close": 10.0},
                    {
                        "symbol": symbol,
                        "date": signal + timedelta(days=1),
                        "open": 10.0,
                        "close": 10.0,
                    },
                    {"symbol": symbol, "date": target, "open": exit_price, "close": exit_price},
                ]
            )
        groups[signal] = items

    result = _evaluate_v2_policy(
        groups,
        _bar_frame(bars),
        CANDIDATE_POLICIES["broad_contrarian_four"],
        holding_horizon_days=20,
        cost_model=CostModel(
            commission_rate=0.00025,
            minimum_commission=5.0,
            stamp_duty_rate=0.0005,
            transfer_fee_rate=0.00001,
            slippage_bps=10.0,
            stop_slippage_bps=25.0,
        ),
    )

    assert result["top_k"] == 4
    assert result["total_exposure"] == 0.40
    assert result["leg_fill_rate"] == 1.0
    assert result["total_return"] > result["benchmark_total_return"]
    assert result["mean_rank_ic"] > 0.8


def test_v2_validation_gate_rejects_weak_bootstrap_evidence():
    result = {
        "total_return": 0.05,
        "benchmark_total_return": 0.02,
        "mean_rank_ic": 0.10,
        "max_drawdown": -0.05,
        "participation_rate": 1.0,
        "leg_fill_rate": 1.0,
        "annual": {
            "2023": {"strategy_return": 0.02, "excess_return": 0.01},
            "2024": {"strategy_return": 0.02, "excess_return": 0.01},
            "2025": {"strategy_return": 0.01, "excess_return": 0.01},
        },
        "paired_comparison": {
            "mean_excess_return": 0.001,
            "probability_mean_excess_positive": 0.89,
            "bootstrap_90pct_interval": [-0.0005, 0.002],
        },
    }

    admission = _validation_admission(result)

    assert admission["passed"] is False
    assert admission["checks"]["bootstrap_probability_at_least_90pct"] is False
