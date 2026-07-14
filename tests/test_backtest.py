from datetime import date

import pytest

from quantlab.backtest.engine import Account, BacktestEngine
from quantlab.config import Settings
from quantlab.data import DemoDataProvider
from quantlab.domain.models import AssetType, Bar, Instrument, OrderRequest, Position, Side
from quantlab.execution.costs import CostModel
from quantlab.strategies import AdaptiveEtfRotationStrategyV2, AdaptiveEtfRotationStrategyV3
from quantlab.workflows.etf import (
    _etf_strategy,
    _export_etf_variant_research,
    _rebalance_gap_passes,
    resolve_etf_variant_config,
    run_etf_core_protocol_backtest,
    run_etf_static_backtest,
)


def test_t_plus_one_rejects_frozen_shares():
    engine = BacktestEngine(
        {"sh600000": Instrument(symbol="sh600000", asset_type=AssetType.STOCK)},
        {AssetType.STOCK.value: CostModel(0.00025, 5, 0.0005, 0.00001, 10, 25)},
    )
    account = Account(
        cash=1000,
        positions={"sh600000": Position(symbol="sh600000", quantity=100, frozen_quantity=100)},
    )
    order = OrderRequest(
        symbol="sh600000", side=Side.SELL, quantity=100, signal_date=date(2026, 1, 1)
    )
    bar = Bar(symbol="sh600000", date=date(2026, 1, 2), open=10, high=10, low=10, close=10)
    assert engine._reject_reason(order, bar, account) == "t_plus_one_or_insufficient_position"


def test_limit_up_rejects_buy():
    engine = BacktestEngine(
        {"sh600000": Instrument(symbol="sh600000", asset_type=AssetType.STOCK)},
        {AssetType.STOCK.value: CostModel(0.00025, 5, 0.0005, 0.00001, 10, 25)},
    )
    order = OrderRequest(
        symbol="sh600000", side=Side.BUY, quantity=100, signal_date=date(2026, 1, 1)
    )
    bar = Bar(
        symbol="sh600000", date=date(2026, 1, 2), open=11, high=11, low=11, close=11, limit_up=True
    )
    assert engine._reject_reason(order, bar, Account(cash=100000)) == "limit_up"


def test_static_etf_reference_uses_next_open_and_costs(tmp_path):
    provider = DemoDataProvider()
    bars = provider.bars(["sh510300"], date(2025, 1, 1), date(2025, 3, 31))
    settings = Settings(
        values={
            "system": {"initial_capital": 100_000.0},
            "costs": {
                "etf": {
                    "commission_rate": 0.0001,
                    "minimum_commission": 5.0,
                    "stamp_duty_rate": 0.0,
                    "transfer_fee_rate": 0.0,
                    "slippage_bps": 5.0,
                    "stop_slippage_bps": 15.0,
                }
            },
        },
        root=tmp_path,
    )

    result = run_etf_static_backtest(
        settings,
        bars,
        ["sh510300"],
        {"sh510300": 0.8},
        date(2025, 1, 2),
        date(2025, 3, 31),
    )

    assert len(result.fills) == 1
    assert result.fills[0].trade_date > date(2025, 1, 2)
    assert result.fills[0].commission >= 5.0


def test_semiannual_etf_core_rebalances_with_lots_tolerance_and_costs(tmp_path):
    bars = []
    for symbol, closes in (
        ("asset-a", (10.0, 10.0, 20.0, 20.0)),
        ("asset-b", (10.0, 10.0, 10.0, 10.0)),
    ):
        for day, close in zip(
            (
                date(2025, 6, 29),
                date(2025, 6, 30),
                date(2025, 7, 1),
                date(2025, 7, 2),
            ),
            closes,
        ):
            bars.append(
                Bar(
                    symbol=symbol,
                    date=day,
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                )
            )
    settings = Settings(
        values={
            "system": {"initial_capital": 100_000.0},
            "risk": {"max_total_exposure": 0.8, "max_single_position": 0.5},
            "costs": {
                "etf": {
                    "commission_rate": 0.0001,
                    "minimum_commission": 5.0,
                    "stamp_duty_rate": 0.0,
                    "transfer_fee_rate": 0.0,
                    "slippage_bps": 5.0,
                    "stop_slippage_bps": 15.0,
                }
            },
        },
        root=tmp_path,
    )

    result = run_etf_core_protocol_backtest(
        settings,
        bars,
        ["asset-a", "asset-b"],
        date(2025, 6, 29),
        date(2025, 7, 2),
    )

    assert len(result.fills) > 2
    assert all(fill.quantity % 100 == 0 for fill in result.fills)
    assert any(fill.side == Side.SELL for fill in result.fills)
    assert all(fill.commission >= 5.0 for fill in result.fills)


def test_etf_strategy_factory_exposes_research_only_v2_variant():
    strategy = _etf_strategy(
        {
            "strategy_variant": "adaptive_v2",
            "lookbacks": [20, 60, 120],
            "top_k": 2,
            "defensive_symbol": "sh511010",
        }
    )

    assert isinstance(strategy, AdaptiveEtfRotationStrategyV2)

    v3 = _etf_strategy(
        {
            "strategy_variant": "adaptive_v3",
            "lookbacks": [20, 60, 120],
            "top_k": 2,
            "defensive_symbol": "sh511010",
        }
    )
    assert isinstance(v3, AdaptiveEtfRotationStrategyV3)


def test_v2_rebalance_tolerance_filters_small_weight_drift():
    assert _rebalance_gap_passes(100, 200, 10.0, 100_000.0, 0.02) is False
    assert _rebalance_gap_passes(100, 300, 10.0, 100_000.0, 0.02) is True


def test_etf_variant_config_merges_research_profile_without_mutating_base(tmp_path):
    settings = Settings(
        values={
            "strategies": {
                "etf_rotation": {
                    "lookbacks": [20, 60, 120],
                    "top_k": 2,
                    "universe": ["risk", "sh511010"],
                    "defensive_symbol": "sh511010",
                },
                "etf_adaptive_v2": {"top_k": 3, "correlation_penalty": 0.4},
            }
        },
        root=tmp_path,
    )

    resolved = resolve_etf_variant_config(settings, "adaptive_v2")

    assert resolved["strategy_variant"] == "adaptive_v2"
    assert resolved["top_k"] == 3
    assert resolved["universe"] == ["risk", "sh511010"]
    assert settings.get("strategies.etf_rotation.top_k") == 2
    with pytest.raises(ValueError, match="unsupported"):
        resolve_etf_variant_config(settings, "future_leaking_variant")


def test_variant_report_names_do_not_overwrite_adaptive_v2_evidence(tmp_path):
    settings = Settings(values={"system": {"data_dir": "data"}}, root=tmp_path)
    output = {
        "strategy_variant": "legacy",
        "period": {"start": "2025-01-01", "end": "2025-12-31"},
        "status": "retrospective_exploratory_only",
        "metrics": {
            "strategy": {
                "total_return": 0.1,
                "sharpe": 1.0,
                "max_drawdown": -0.1,
                "turnover_count": 2.0,
            },
            "equal_weight_buy_hold": {"total_return": 0.08},
            "two_x_cost": {"total_return": 0.09},
        },
        "relative_to_equal_weight": {
            "total_return_delta": 0.02,
            "sharpe_delta": 0.1,
            "max_drawdown_delta": 0.01,
        },
        "claim_boundary": "exploratory only",
    }

    reports = _export_etf_variant_research(settings, output)

    assert reports["json"].endswith("etf-legacy-diagnostic-latest.json")
    assert not settings.resolve("data/reports/adaptive-v2-diagnostic-latest.json").exists()
