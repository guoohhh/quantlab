from datetime import date

from quantlab.domain.models import OrderRequest, Side
from quantlab.execution.costs import CostModel


def test_stock_cost_model_sell_has_stamp_duty():
    model = CostModel(0.00025, 5, 0.0005, 0.00001, 10, 25)
    fill = model.fill(
        OrderRequest(
            symbol="sh600000", side=Side.SELL, quantity=1000, signal_date=date(2026, 1, 1)
        ),
        10,
        date(2026, 1, 2),
    )
    assert fill.stamp_duty > 0
    assert fill.commission == 5
    assert fill.price < 10


def test_cost_model_can_be_scaled_for_stress_testing():
    base = CostModel(0.0001, 5, 0, 0, 5, 15)

    stressed = base.scaled(2.0)

    assert stressed.commission_rate == 0.0002
    assert stressed.minimum_commission == 10
    assert stressed.slippage_bps == 10
