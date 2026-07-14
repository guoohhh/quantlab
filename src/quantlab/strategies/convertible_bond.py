from __future__ import annotations

from datetime import date

import pandas as pd

from quantlab.domain.models import StrategySignal
from quantlab.strategies.base import Strategy


class ConvertibleBondDoubleLowStrategy(Strategy):
    name = "convertible_bond_double_low"

    def __init__(self, selection_count=10, maximum_price=115.0, maximum_premium_pct=30.0):
        self.selection_count = selection_count
        self.maximum_price = maximum_price
        self.maximum_premium_pct = maximum_premium_pct

    def generate(self, as_of: date, data: pd.DataFrame, **context) -> list[StrategySignal]:
        frame = data.copy()
        required = {"symbol", "price", "premium_pct"}
        if not required.issubset(frame.columns):
            return []
        if "date" in frame:
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
            frame = (
                frame[frame["date"] <= pd.Timestamp(as_of)]
                .sort_values(["symbol", "date"])
                .drop_duplicates("symbol", keep="last")
            )
        frame = frame[
            (frame.price < self.maximum_price) & (frame.premium_pct < self.maximum_premium_pct)
        ]
        if "remaining_size" in frame:
            frame = frame[frame.remaining_size >= 100_000_000]
        if "redeem_risk" in frame:
            frame = frame[~frame.redeem_risk.astype(bool)]
        if frame.empty:
            return []
        price_z = (frame.price - frame.price.mean()) / (frame.price.std(ddof=0) or 1)
        premium_z = (frame.premium_pct - frame.premium_pct.mean()) / (
            frame.premium_pct.std(ddof=0) or 1
        )
        frame["double_low"] = price_z + premium_z
        selected = frame.nsmallest(self.selection_count, "double_low")
        weight = 1 / len(selected)
        return [
            StrategySignal(
                strategy=self.name,
                symbol=row.symbol,
                as_of=as_of,
                score=float(max(-1, min(1, -row.double_low / 3))),
                target_weight=weight,
                confidence=0.6,
                reasons=[f"price={row.price:.2f}", f"premium={row.premium_pct:.2f}%"],
            )
            for row in selected.itertuples()
        ]
