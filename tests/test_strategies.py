from datetime import date

import numpy as np
import pandas as pd
import pytest

from quantlab.strategies import (
    AdaptiveEtfRotationStrategy,
    AdaptiveEtfRotationStrategyV2,
    AdaptiveEtfRotationStrategyV3,
    ConvertibleBondDoubleLowStrategy,
    StockReversalStrategy,
)


def test_stock_reversal_selects_worst_eligible_return_with_positive_score():
    dates = pd.bdate_range("2026-01-01", periods=61)
    rows = []
    for symbol, ending_price in (("worst", 80.0), ("flat", 100.0), ("best", 120.0)):
        for day, close in zip(dates, np.linspace(100.0, ending_price, len(dates))):
            rows.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "close": close,
                    "amount": 100_000_000,
                }
            )

    signals = StockReversalStrategy(lookback_days=60, selection_count=1).generate(
        dates[-1].date(),
        pd.DataFrame(rows),
    )

    assert len(signals) == 1
    assert signals[0].symbol == "worst"
    assert signals[0].score == 1.0


def test_stock_reversal_excludes_st_new_and_illiquid_symbols():
    dates = pd.bdate_range("2026-01-01", periods=61)
    rows = []
    for symbol, amount in (
        ("eligible", 100_000_000),
        ("illiquid", 1_000_000),
        ("st", 100_000_000),
        ("new", 100_000_000),
    ):
        for day, close in zip(dates, np.linspace(100.0, 80.0, len(dates))):
            rows.append({"symbol": symbol, "date": day, "close": close, "amount": amount})

    signals = StockReversalStrategy(lookback_days=60, selection_count=5).generate(
        dates[-1].date(),
        pd.DataFrame(rows),
        metadata={
            "eligible": {},
            "illiquid": {},
            "st": {"is_st": True},
            "new": {"listing_days": 30},
        },
    )

    assert [item.symbol for item in signals] == ["eligible"]


def test_convertible_bond_strategy_ignores_future_snapshots():
    frame = pd.DataFrame(
        [
            {
                "symbol": "bond-a",
                "date": "2026-07-01",
                "price": 112.0,
                "premium_pct": 20.0,
                "remaining_size": 200_000_000,
                "redeem_risk": False,
            },
            {
                "symbol": "bond-a",
                "date": "2026-08-01",
                "price": 80.0,
                "premium_pct": 1.0,
                "remaining_size": 200_000_000,
                "redeem_risk": False,
            },
            {
                "symbol": "bond-b",
                "date": "2026-07-01",
                "price": 100.0,
                "premium_pct": 5.0,
                "remaining_size": 200_000_000,
                "redeem_risk": False,
            },
        ]
    )

    signals = ConvertibleBondDoubleLowStrategy(selection_count=1).generate(date(2026, 7, 13), frame)

    assert len(signals) == 1
    assert signals[0].symbol == "bond-b"


def test_convertible_bond_strategy_enforces_size_redeem_and_schema_filters():
    frame = pd.DataFrame(
        [
            {
                "symbol": "eligible",
                "price": 100.0,
                "premium_pct": 5.0,
                "remaining_size": 200_000_000,
                "redeem_risk": False,
            },
            {
                "symbol": "small",
                "price": 90.0,
                "premium_pct": 1.0,
                "remaining_size": 50_000_000,
                "redeem_risk": False,
            },
            {
                "symbol": "redeem",
                "price": 90.0,
                "premium_pct": 1.0,
                "remaining_size": 200_000_000,
                "redeem_risk": True,
            },
        ]
    )
    strategy = ConvertibleBondDoubleLowStrategy(selection_count=3)

    assert [item.symbol for item in strategy.generate(date(2026, 7, 13), frame)] == ["eligible"]
    assert strategy.generate(date(2026, 7, 13), frame.drop(columns="premium_pct")) == []


def _adaptive_frame():
    dates = pd.bdate_range("2025-01-01", periods=180)
    rows = []
    paths = {
        "low_vol_up": np.linspace(100, 135, len(dates)),
        "high_vol_up": np.linspace(100, 145, len(dates)) + np.sin(np.arange(len(dates))) * 8,
        "down": np.linspace(100, 75, len(dates)),
        "sh511010": np.linspace(100, 103, len(dates)),
    }
    for symbol, values in paths.items():
        for day, close in zip(dates, values):
            rows.append({"symbol": symbol, "date": day, "close": close})
    return dates, pd.DataFrame(rows)


def test_adaptive_etf_uses_absolute_momentum_and_inverse_volatility():
    dates, frame = _adaptive_frame()
    strategy = AdaptiveEtfRotationStrategy(
        {
            "top_k": 2,
            "defensive_symbol": "sh511010",
            "breadth_threshold": 0.5,
            "target_volatility": 0.5,
        }
    )

    signals = strategy.generate(dates[-1].date(), frame)

    assert {item.symbol for item in signals} == {"low_vol_up", "high_vol_up"}
    assert sum(item.target_weight for item in signals) == pytest.approx(1.0)
    weights = {item.symbol: item.target_weight for item in signals}
    assert weights["low_vol_up"] > weights["high_vol_up"]
    assert strategy.last_diagnostics["breadth"] == pytest.approx(2 / 3)
    assert strategy.last_diagnostics["regime_multiplier"] == 1.0


def test_adaptive_etf_rank_buffer_can_retain_existing_near_cutoff_holding():
    dates, frame = _adaptive_frame()
    strategy = AdaptiveEtfRotationStrategy(
        {
            "top_k": 1,
            "rank_buffer": 1,
            "defensive_symbol": "sh511010",
            "target_volatility": 0.5,
        }
    )

    signals = strategy.generate(
        dates[-1].date(), frame, current_symbols=["high_vol_up"]
    )

    assert [item.symbol for item in signals] == ["high_vol_up"]


def test_adaptive_etf_moves_to_zero_active_risk_when_no_asset_has_absolute_momentum():
    dates, frame = _adaptive_frame()
    for symbol in ("low_vol_up", "high_vol_up", "down"):
        frame.loc[frame.symbol == symbol, "close"] = np.linspace(100, 70, len(dates))
    strategy = AdaptiveEtfRotationStrategy(
        {"top_k": 2, "defensive_symbol": "sh511010", "risk_off_multiplier": 0.0}
    )

    signals = strategy.generate(dates[-1].date(), frame)

    assert signals == []
    assert strategy.last_diagnostics["risk_scale"] == 0.0


def test_adaptive_etf_rejects_mismatched_momentum_configuration():
    with pytest.raises(ValueError, match="equal length"):
        AdaptiveEtfRotationStrategy(
            {"lookbacks": (20, 60), "momentum_weights": (1.0,)}
        )


def _adaptive_v2_frame(stressed: bool = False):
    dates = pd.bdate_range("2024-01-01", periods=260)
    index = np.arange(len(dates))
    common_cycle = 0.005 * np.sin(index / 5)
    returns = {
        "equity_a": 0.0010 + common_cycle,
        "equity_b": 0.0009 + common_cycle + 0.0002 * np.cos(index / 11),
        "diversifier": 0.00075 + 0.005 * np.cos(index / 7),
        "sh511010": np.full(len(dates), 0.0001),
    }
    if stressed:
        for symbol in ("equity_a", "equity_b", "diversifier"):
            returns[symbol] = returns[symbol].copy()
            returns[symbol][-20:] -= 0.007
    rows = []
    for symbol, daily_returns in returns.items():
        prices = 100 * np.cumprod(1 + daily_returns)
        for day, close in zip(dates, prices):
            rows.append({"symbol": symbol, "date": day, "close": close})
    return dates, pd.DataFrame(rows)


def test_adaptive_etf_v2_penalizes_redundant_correlation_and_caps_weights():
    dates, frame = _adaptive_v2_frame()
    strategy = AdaptiveEtfRotationStrategyV2(
        {
            "top_k": 2,
            "correlation_penalty": 5.0,
            "maximum_satellite_weight": 0.6,
            "target_volatility": 0.5,
            "defensive_symbol": "sh511010",
        }
    )

    signals = strategy.generate(dates[-1].date(), frame)

    assert len(signals) == 2
    assert "diversifier" in {item.symbol for item in signals}
    assert {item.symbol for item in signals} != {"equity_a", "equity_b"}
    assert sum(item.target_weight for item in signals) == pytest.approx(1.0)
    assert max(item.target_weight for item in signals) <= 0.6 + 1e-9
    assert strategy.last_diagnostics["allocation_method"] == (
        "shrunk_maximum_diversification"
    )


def test_adaptive_etf_v2_continuously_reduces_risk_and_core_during_stress():
    dates, calm_frame = _adaptive_v2_frame()
    _, stressed_frame = _adaptive_v2_frame(stressed=True)
    config = {
        "top_k": 2,
        "target_volatility": 0.5,
        "defensive_symbol": "sh511010",
        "drawdown_warning": -0.03,
        "drawdown_stop": -0.12,
    }
    calm = AdaptiveEtfRotationStrategyV2(config)
    stressed = AdaptiveEtfRotationStrategyV2(config)

    calm.generate(dates[-1].date(), calm_frame)
    stressed.generate(dates[-1].date(), stressed_frame)

    assert stressed.last_diagnostics["market_drawdown"] < -0.03
    assert (
        stressed.last_diagnostics["regime_multiplier"]
        < calm.last_diagnostics["regime_multiplier"]
    )
    assert (
        stressed.last_diagnostics["effective_core_weight"]
        < calm.last_diagnostics["effective_core_weight"]
    )
    assert stressed.last_diagnostics["research_status"] == "prospective_challenger"


def test_adaptive_etf_v2_validates_portfolio_and_regime_configuration():
    with pytest.raises(ValueError, match="equal length"):
        AdaptiveEtfRotationStrategyV2(
            {"lookbacks": [20, 60], "momentum_weights": [1.0]}
        )
    with pytest.raises(ValueError, match="positive total weight"):
        AdaptiveEtfRotationStrategyV2({"momentum_weights": [0.0, 0.0, 0.0]})
    with pytest.raises(ValueError, match="top_k"):
        AdaptiveEtfRotationStrategyV2({"top_k": 0})
    with pytest.raises(ValueError, match="shrinkage"):
        AdaptiveEtfRotationStrategyV2({"covariance_shrinkage": 1.5})
    with pytest.raises(ValueError, match="too small"):
        AdaptiveEtfRotationStrategyV2(
            {"top_k": 3, "maximum_satellite_weight": 0.3}
        )
    with pytest.raises(ValueError, match="thresholds"):
        AdaptiveEtfRotationStrategyV2(
            {"breadth_risk_off": 0.8, "breadth_risk_on": 0.5}
        )
    with pytest.raises(ValueError, match="stress stop"):
        AdaptiveEtfRotationStrategyV2(
            {"volatility_stress_warning": 2.0, "volatility_stress_stop": 1.5}
        )
    with pytest.raises(ValueError, match="drawdown stop"):
        AdaptiveEtfRotationStrategyV2(
            {"drawdown_warning": -0.10, "drawdown_stop": -0.05}
        )
    with pytest.raises(ValueError, match="core weight"):
        AdaptiveEtfRotationStrategyV2(
            {"minimum_core_weight": 0.8, "maximum_core_weight": 0.5}
        )
    with pytest.raises(ValueError, match="rebalance tolerance"):
        AdaptiveEtfRotationStrategyV2({"rebalance_tolerance_weight": 0.11})


def test_adaptive_etf_v2_handles_defensive_only_and_single_risk_asset():
    dates, frame = _adaptive_v2_frame()
    strategy = AdaptiveEtfRotationStrategyV2(
        {
            "top_k": 1,
            "maximum_satellite_weight": 1.0,
            "defensive_symbol": "sh511010",
            "target_volatility": 0.5,
        }
    )

    defensive_only = frame[frame.symbol == "sh511010"]
    assert strategy.generate(dates[-1].date(), defensive_only) == []
    assert strategy.last_diagnostics["allocation_method"] == "none"

    one_asset = frame[frame.symbol.isin(["equity_a", "sh511010"])]
    signals = strategy.generate(dates[-1].date(), one_asset)
    assert [item.symbol for item in signals] == ["equity_a"]
    assert strategy.last_diagnostics["allocation_method"] == "single_asset"


def _positive_volatility_shock_frame():
    dates = pd.bdate_range("2024-01-01", periods=260)
    rows = []
    for offset, symbol in enumerate(("risk_a", "risk_b", "risk_c")):
        daily = np.full(len(dates), 0.0006 + offset * 0.00005)
        daily[-20::2] += 0.03
        prices = 100 * np.cumprod(1 + daily)
        for day, close in zip(dates, prices):
            rows.append({"symbol": symbol, "date": day, "close": close})
    defensive = 100 * np.cumprod(1 + np.full(len(dates), 0.0001))
    for day, close in zip(dates, defensive):
        rows.append({"symbol": "sh511010", "date": day, "close": close})
    return dates, pd.DataFrame(rows)


def test_adaptive_etf_v3_does_not_treat_positive_volatility_as_downside_stress():
    dates, frame = _positive_volatility_shock_frame()
    config = {
        "top_k": 3,
        "defensive_symbol": "sh511010",
        "target_volatility": 0.5,
    }
    v2 = AdaptiveEtfRotationStrategyV2(config)
    v3 = AdaptiveEtfRotationStrategyV3(config)

    v2.generate(dates[-1].date(), frame)
    signals = v3.generate(dates[-1].date(), frame)

    assert signals
    assert v2.last_diagnostics["volatility_scale"] < 0.5
    assert v3.last_diagnostics["volatility_scale"] == pytest.approx(1.0)
    assert (
        v3.last_diagnostics["regime_multiplier"]
        > v2.last_diagnostics["regime_multiplier"]
    )
    assert v3.last_diagnostics["research_status"] == "prospective_v3_challenger"


def test_adaptive_etf_v3_only_penalizes_correlation_above_threshold():
    dates = pd.bdate_range("2025-01-01", periods=60)
    base_values = np.sin(np.arange(60) / 5)
    base_values = (base_values - base_values.mean()) / base_values.std(ddof=1)
    orthogonal = np.cos(np.arange(60) / 7)
    orthogonal = orthogonal - (
        np.dot(orthogonal, base_values) / np.dot(base_values, base_values)
    ) * base_values
    orthogonal = (orthogonal - orthogonal.mean()) / orthogonal.std(ddof=1)
    base = pd.Series(base_values, index=dates)
    returns = {
        "leader": base,
        "moderate": pd.Series(
            0.7 * base_values + np.sqrt(1 - 0.7**2) * orthogonal,
            index=dates,
        ),
        "diversifier": pd.Series(orthogonal, index=dates),
    }
    eligible = [
        {"symbol": "leader", "score": 2.0},
        {"symbol": "moderate", "score": 1.9},
        {"symbol": "diversifier", "score": 1.8},
    ]
    v2 = AdaptiveEtfRotationStrategyV2(
        {"top_k": 2, "correlation_penalty": 0.5, "maximum_satellite_weight": 0.6}
    )
    v3 = AdaptiveEtfRotationStrategyV3(
        {
            "top_k": 2,
            "correlation_penalty": 0.5,
            "correlation_penalty_threshold": 0.8,
            "maximum_satellite_weight": 0.6,
        }
    )

    assert v2._diversified_ranking(eligible, returns)[1]["symbol"] == "diversifier"
    assert v3._diversified_ranking(eligible, returns)[1]["symbol"] == "moderate"
    with pytest.raises(ValueError, match="correlation penalty threshold"):
        AdaptiveEtfRotationStrategyV3({"correlation_penalty_threshold": 1.0})
