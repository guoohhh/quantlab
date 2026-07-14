from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from quantlab.domain.models import StrategySignal
from quantlab.strategies.base import Strategy


class EtfRotationStrategy(Strategy):
    name = "etf_rotation"

    def __init__(self, lookbacks=(20, 60, 120), top_k: int = 2, defensive_symbol: str = "sh511010"):
        self.lookbacks = tuple(lookbacks)
        self.top_k = top_k
        self.defensive_symbol = defensive_symbol

    def generate(self, as_of: date, data: pd.DataFrame, **context) -> list[StrategySignal]:
        frame = data[data["date"] <= pd.Timestamp(as_of)].sort_values(["symbol", "date"])
        rows = []
        for symbol, group in frame.groupby("symbol"):
            close = group["close"].astype(float)
            if len(close) <= max(self.lookbacks):
                continue
            momenta = [
                (close.iloc[-1] / close.iloc[-lookback - 1] - 1) for lookback in self.lookbacks
            ]
            volatility = close.pct_change().tail(60).std() * np.sqrt(252)
            ma120 = close.tail(120).mean()
            trend = close.iloc[-1] / ma120 - 1
            score = np.mean(momenta) + 0.5 * trend - 0.25 * volatility
            rows.append((symbol, float(score), momenta, float(volatility)))
        rows.sort(key=lambda item: item[1], reverse=True)
        selected = rows[: self.top_k]
        if not selected or selected[0][1] <= 0:
            selected = [item for item in rows if item[0] == self.defensive_symbol][:1]
        if not selected:
            return []
        weight = 1 / len(selected)
        return [
            StrategySignal(
                strategy=self.name,
                symbol=symbol,
                as_of=as_of,
                score=float(np.tanh(score * 5)),
                target_weight=weight,
                confidence=float(min(0.9, 0.45 + abs(score) * 2)),
                reasons=[
                    f"multi-period momentum={np.mean(momenta):.2%}",
                    f"annualized volatility={vol:.2%}",
                ],
            )
            for symbol, score, momenta, vol in selected
        ]
