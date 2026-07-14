from __future__ import annotations

from datetime import date

import pandas as pd

from quantlab.domain.models import StrategySignal
from quantlab.strategies.base import Strategy, rank_to_unit


class StockReversalStrategy(Strategy):
    name = "stock_reversal"

    def __init__(self, lookback_days: int = 60, selection_count: int = 20):
        self.lookback_days = lookback_days
        self.selection_count = selection_count

    def generate(self, as_of: date, data: pd.DataFrame, **context) -> list[StrategySignal]:
        eligible = data[data["date"] <= pd.Timestamp(as_of)].sort_values(["symbol", "date"])
        records = []
        metadata = context.get("metadata", {})
        for symbol, group in eligible.groupby("symbol"):
            meta = metadata.get(symbol, {})
            if meta.get("is_st") or meta.get("listing_days", 9999) < 180:
                continue
            close = group["close"].astype(float)
            amount = group.get("amount", pd.Series(dtype=float)).astype(float)
            if len(close) <= self.lookback_days or (
                len(amount) and amount.tail(20).mean() < 50_000_000
            ):
                continue
            ret = close.iloc[-1] / close.iloc[-self.lookback_days - 1] - 1
            records.append(
                {"symbol": symbol, "return": ret, "industry": meta.get("industry", "unknown")}
            )
        if not records:
            return []
        frame = pd.DataFrame(records)
        frame["score"] = rank_to_unit(frame["return"], ascending=False)
        selected = frame.sort_values("return").head(self.selection_count)
        weight = 1 / len(selected)
        signals = []
        for row in selected.to_dict("records"):
            signals.append(
                StrategySignal(
                    strategy=self.name,
                    symbol=row["symbol"],
                    as_of=as_of,
                    score=float(row["score"]),
                    target_weight=weight,
                    confidence=0.55,
                    reasons=[
                        f"{self.lookback_days}d return={row['return']:.2%}",
                        "contrarian selection",
                    ],
                )
            )
        return signals
