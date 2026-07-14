from __future__ import annotations

from dataclasses import dataclass

from quantlab.domain.models import Fill, OrderRequest, Side


@dataclass(frozen=True)
class CostModel:
    commission_rate: float
    minimum_commission: float
    stamp_duty_rate: float
    transfer_fee_rate: float
    slippage_bps: float
    stop_slippage_bps: float

    @classmethod
    def from_dict(cls, values: dict) -> "CostModel":
        return cls(**{key: float(values[key]) for key in cls.__dataclass_fields__})

    def fill(self, order: OrderRequest, raw_price: float, trade_date) -> Fill:
        bps = self.stop_slippage_bps if order.is_stop else self.slippage_bps
        slip_rate = bps / 10_000
        price = raw_price * (1 + slip_rate if order.side == Side.BUY else 1 - slip_rate)
        gross = price * order.quantity
        commission = max(gross * self.commission_rate, self.minimum_commission)
        stamp = gross * self.stamp_duty_rate if order.side == Side.SELL else 0.0
        transfer = gross * self.transfer_fee_rate
        slippage = abs(price - raw_price) * order.quantity
        return Fill(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=price,
            gross_value=gross,
            commission=commission,
            stamp_duty=stamp,
            transfer_fee=transfer,
            slippage=slippage,
            trade_date=trade_date,
        )

    def scaled(self, multiplier: float) -> "CostModel":
        if multiplier <= 0:
            raise ValueError("cost multiplier must be positive")
        return CostModel(
            commission_rate=self.commission_rate * multiplier,
            minimum_commission=self.minimum_commission * multiplier,
            stamp_duty_rate=self.stamp_duty_rate * multiplier,
            transfer_fee_rate=self.transfer_fee_rate * multiplier,
            slippage_bps=self.slippage_bps * multiplier,
            stop_slippage_bps=self.stop_slippage_bps * multiplier,
        )
