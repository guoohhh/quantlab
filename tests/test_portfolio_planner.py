from datetime import date

import pytest

from quantlab.domain.models import AssetType, MarketRegime, Position, StrategySignal
from quantlab.portfolio import build_manual_portfolio_plan
from quantlab.risk import assess_instrument_risk
from quantlab.workflows.portfolio import select_evidence_first_etf_policy


def _signal(symbol="sh600000", strategy="stock_reversal"):
    return StrategySignal(
        strategy=strategy,
        symbol=symbol,
        as_of=date(2026, 7, 13),
        score=0.8,
        target_weight=1.0,
        confidence=0.6,
        reasons=["test signal"],
    )


def test_manual_plan_rejects_non_positive_equity():
    with pytest.raises(ValueError, match="equity must be positive"):
        build_manual_portfolio_plan(
            as_of=date(2026, 7, 13),
            market_regime=MarketRegime.RANGE,
            equity=0,
            cash=0,
            positions={},
            signals=[],
            strategy_budgets={},
            market_data={},
        )


def test_manual_plan_rounds_stock_buys_to_lots_and_never_executes():
    plan = build_manual_portfolio_plan(
        as_of=date(2026, 7, 13),
        market_regime=MarketRegime.RANGE,
        equity=100_000,
        cash=100_000,
        positions={},
        signals=[_signal()],
        strategy_budgets={"stock_reversal": 0.10},
        market_data={
            "sh600000": {
                "price": 11.0,
                "asset_type": AssetType.STOCK.value,
                "trade_lot": 100,
                "risk_check_complete": True,
                "financial_check_complete": True,
            }
        },
    )

    assert plan.manual_execution_only is True
    assert plan.orders[0].side == "buy"
    assert plan.orders[0].quantity == 900
    assert plan.orders[0].status == "actionable"


def test_stock_hard_veto_prevents_order_creation():
    plan = build_manual_portfolio_plan(
        as_of=date(2026, 7, 13),
        market_regime=MarketRegime.RANGE,
        equity=100_000,
        cash=100_000,
        positions={},
        signals=[_signal()],
        strategy_budgets={"stock_reversal": 0.10},
        market_data={
            "sh600000": {
                "price": 10,
                "asset_type": "stock",
                "is_st": True,
                "risk_check_complete": True,
                "financial_check_complete": True,
            }
        },
    )

    assert plan.orders == []
    assert plan.blocked_candidates[0].status == "blocked"
    assert "ST/*ST" in plan.blocked_candidates[0].reasons[-1]


def test_previous_managed_symbol_gets_sell_line_when_it_leaves_target_set():
    plan = build_manual_portfolio_plan(
        as_of=date(2026, 7, 13),
        market_regime=MarketRegime.BEAR,
        equity=100_000,
        cash=90_000,
        positions={"sh600000": Position(symbol="sh600000", quantity=1000, market_price=10)},
        signals=[],
        strategy_budgets={},
        market_data={"sh600000": {"price": 10, "trade_lot": 100}},
        previous_targets={"sh600000": {"strategy": "stock_reversal", "price": 10}},
    )

    assert plan.orders[0].side == "sell"
    assert plan.orders[0].quantity == 1000
    assert plan.orders[0].target_quantity == 0


def test_convertible_bond_missing_critical_fields_requires_review():
    risk = assess_instrument_risk(
        AssetType.CONVERTIBLE_BOND,
        {"rating": "AA", "maturity_date": "2028-01-01", "redeem_risk": False},
        date(2026, 7, 13),
    )

    assert risk.blocked is False
    assert risk.review_required is True
    assert "remaining issue size is unavailable" in risk.warnings


def test_negative_cashflow_financial_veto_blocks_stock():
    risk = assess_instrument_risk(
        AssetType.STOCK,
        {
            "risk_check_complete": True,
            "financial_check_complete": True,
            "financial_quality_score": 0.6,
            "financial_hard_vetoes": ["five-year cumulative free cash flow is negative"],
        },
        date(2026, 7, 13),
    )

    assert risk.blocked is True
    assert "five-year cumulative free cash flow is negative" in risk.hard_vetoes


def test_same_day_agent_watch_can_only_tighten_new_position():
    plan = build_manual_portfolio_plan(
        as_of=date(2026, 7, 13),
        market_regime=MarketRegime.RANGE,
        equity=100_000,
        cash=100_000,
        positions={},
        signals=[_signal("sh510300", "etf_rotation")],
        strategy_budgets={"etf_rotation": 0.25},
        market_data={
            "sh510300": {
                "price": 4.0,
                "asset_type": "etf",
                "trade_lot": 100,
                "risk_check_complete": True,
                "_agent_decision_action": "watch",
                "_agent_target_cap": 0.0,
            }
        },
    )

    assert plan.orders == []
    assert plan.blocked_candidates[0].status == "review_required"
    assert "new position is not allowed" in plan.blocked_candidates[0].warnings[-1]


def test_same_day_agent_buy_caps_but_never_increases_strategy_target():
    plan = build_manual_portfolio_plan(
        as_of=date(2026, 7, 13),
        market_regime=MarketRegime.RANGE,
        equity=100_000,
        cash=100_000,
        positions={},
        signals=[_signal("sh510300", "etf_rotation")],
        strategy_budgets={"etf_rotation": 0.25},
        market_data={
            "sh510300": {
                "price": 4.0,
                "asset_type": "etf",
                "trade_lot": 100,
                "risk_check_complete": True,
                "_agent_decision_action": "buy",
                "_agent_target_cap": 0.05,
                "stop_loss": 3.8,
                "risk_method": "test ATR",
            }
        },
    )

    assert plan.target_weights["sh510300"] == 0.05
    assert plan.orders[0].quantity == 1_200
    assert plan.orders[0].maximum_loss_amount == pytest.approx(240.0)
    assert plan.orders[0].stop_loss == 3.8


def test_manual_plan_scales_new_targets_to_industry_cap():
    signals = [_signal("tech-a"), _signal("tech-b")]
    plan = build_manual_portfolio_plan(
        as_of=date(2026, 7, 13),
        market_regime=MarketRegime.RANGE,
        equity=100_000,
        cash=90_000,
        positions={
            "existing-tech": Position(
                symbol="existing-tech",
                quantity=1_000,
                market_price=10,
                industry="technology",
            )
        },
        signals=signals,
        strategy_budgets={"stock_reversal": 0.40},
        market_data={
            symbol: {
                "price": 10,
                "industry": "technology",
                "asset_type": "stock",
                "risk_check_complete": True,
                "financial_check_complete": True,
            }
            for symbol in ("tech-a", "tech-b")
        },
        max_single_position=0.50,
        max_industry_exposure=0.25,
    )

    assert sum(plan.target_weights.values()) == pytest.approx(0.15)
    assert plan.max_industry_exposure == 0.25
    assert any("industry cap reduced technology" in warning for warning in plan.warnings)


def test_evidence_first_policy_uses_investable_oos_winner():
    validation = {
        "admission": {"passed": False},
        "benchmark_oos": {
            "equal_weight_buy_hold": {
                "folds": 18,
                "compounded_return": 0.84,
                "mean_sharpe": 0.93,
            }
        },
    }

    assert select_evidence_first_etf_policy(validation) == "equal_weight_core"
    assert select_evidence_first_etf_policy(None) == "equal_weight_core"
    assert (
        select_evidence_first_etf_policy({**validation, "admission": {"passed": True}})
        == "equal_weight_core"
    )
    assert (
        select_evidence_first_etf_policy(
            {
                **validation,
                "admission": {"passed": True},
                "production_deployment": {
                    "status": "admitted",
                    "policy": "momentum_rotation",
                    "config": {"lookbacks": [20, 60, 120], "top_k": 2},
                },
            }
        )
        == "momentum_rotation"
    )


def test_stale_market_data_hard_blocks_new_order():
    risk = assess_instrument_risk(
        AssetType.ETF,
        {
            "market_data_freshness_required": True,
            "market_data_as_of": "2026-06-30",
            "maximum_market_data_age_business_days": 1,
        },
        date(2026, 7, 15),
    )

    assert risk.blocked is True
    assert "market data is stale" in risk.hard_vetoes[0]


def test_previous_business_day_market_data_remains_tradeable():
    risk = assess_instrument_risk(
        AssetType.ETF,
        {
            "market_data_freshness_required": True,
            "market_data_as_of": "2026-07-10",
            "maximum_market_data_age_business_days": 1,
        },
        date(2026, 7, 13),
    )

    assert risk.blocked is False
    assert any("business_day_age=1" in item for item in risk.checks)


def test_research_only_strategy_cannot_create_actionable_buy():
    plan = build_manual_portfolio_plan(
        as_of=date(2026, 7, 13),
        market_regime=MarketRegime.RANGE,
        equity=100_000,
        cash=100_000,
        positions={},
        signals=[_signal("sh510300", "etf_rotation")],
        strategy_budgets={"etf_rotation": 0.25},
        market_data={
            "sh510300": {
                "price": 4.0,
                "asset_type": "etf",
                "trade_lot": 100,
                "_research_only": True,
                "_research_only_reason": "failed production admission",
            }
        },
    )

    assert plan.orders == []
    assert plan.blocked_candidates[0].status == "review_required"
    assert "failed production admission" in plan.blocked_candidates[0].warnings


def test_semiannual_core_does_not_rebalance_twice_in_same_half_year():
    plan = build_manual_portfolio_plan(
        as_of=date(2026, 7, 15),
        market_regime=MarketRegime.RANGE,
        equity=100_000,
        cash=85_000,
        positions={
            "sh510300": Position(symbol="sh510300", quantity=2_600, market_price=5.0)
        },
        signals=[_signal("sh510300", "etf_rotation")],
        strategy_budgets={"etf_rotation": 0.15},
        market_data={
            "sh510300": {
                "price": 5.0,
                "asset_type": "etf",
                "trade_lot": 100,
                "execution_protocol": "semiannual_equal_weight_core_v1",
                "rebalance_frequency": "semiannual",
                "rebalance_tolerance_weight": 0.02,
            }
        },
        previous_plan_as_of=date(2026, 7, 1),
    )

    assert plan.orders == []
    assert any("rebalance is not due" in warning for warning in plan.warnings)
