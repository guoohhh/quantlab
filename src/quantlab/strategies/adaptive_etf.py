from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

from quantlab.domain.models import StrategySignal
from quantlab.strategies.base import Strategy


class AdaptiveEtfRotationStrategy(Strategy):
    """Risk-aware core-satellite ETF rotation candidate registered for research only."""

    name = "adaptive_etf_rotation"

    def __init__(self, config: dict):
        self.lookbacks = tuple(int(value) for value in config.get("lookbacks", (20, 60, 120)))
        self.momentum_weights = tuple(
            float(value) for value in config.get("momentum_weights", (0.50, 0.30, 0.20))
        )
        if len(self.lookbacks) != len(self.momentum_weights):
            raise ValueError("lookbacks and momentum_weights must have equal length")
        if sum(self.momentum_weights) <= 0:
            raise ValueError("momentum_weights must sum to a positive value")
        total = sum(self.momentum_weights)
        self.momentum_weights = tuple(value / total for value in self.momentum_weights)
        self.top_k = int(config.get("top_k", 2))
        self.defensive_symbol = str(config.get("defensive_symbol", "sh511010"))
        self.volatility_lookback = int(config.get("volatility_lookback", 60))
        self.trend_lookback = int(config.get("trend_lookback", 120))
        self.breadth_threshold = float(config.get("breadth_threshold", 0.50))
        self.risk_off_multiplier = float(config.get("risk_off_multiplier", 0.25))
        self.target_volatility = float(config.get("target_volatility", 0.12))
        self.rank_buffer = int(config.get("rank_buffer", 1))
        self.last_diagnostics: dict[str, float | int | list[str]] = {}

    def generate(self, as_of: date, data: pd.DataFrame, **context) -> list[StrategySignal]:
        frame = data[data["date"] <= pd.Timestamp(as_of)].sort_values(["symbol", "date"])
        rows = []
        maximum_history = max(
            max(self.lookbacks), self.volatility_lookback, self.trend_lookback
        )
        for symbol, group in frame.groupby("symbol"):
            if symbol == self.defensive_symbol:
                continue
            close = pd.to_numeric(group["close"], errors="coerce").dropna()
            if len(close) <= maximum_history:
                continue
            daily_returns = close.pct_change().dropna()
            volatility = float(
                daily_returns.tail(self.volatility_lookback).std(ddof=1) * math.sqrt(252)
            )
            if not math.isfinite(volatility) or volatility <= 1e-6:
                continue
            risk_adjusted = []
            raw_returns = []
            for lookback in self.lookbacks:
                period_return = float(close.iloc[-1] / close.iloc[-lookback - 1] - 1.0)
                horizon_risk = volatility * math.sqrt(lookback / 252)
                risk_adjusted.append(period_return / max(horizon_risk, 1e-6))
                raw_returns.append(period_return)
            weighted_momentum = float(
                sum(
                    weight * value
                    for weight, value in zip(self.momentum_weights, risk_adjusted)
                )
            )
            moving_average = float(close.tail(self.trend_lookback).mean())
            trend_return = float(close.iloc[-1] / moving_average - 1.0)
            trend_risk = volatility * math.sqrt(self.trend_lookback / 252)
            trend_score = trend_return / max(trend_risk, 1e-6)
            path = close.tail(self.volatility_lookback)
            path_quality = float(
                (path.iloc[-1] - path.iloc[0])
                / max(float(path.diff().abs().sum()), 1e-9)
            )
            absolute_return = raw_returns[-1]
            eligible = absolute_return > 0 and close.iloc[-1] > moving_average
            score = 0.65 * weighted_momentum + 0.25 * trend_score + 0.10 * path_quality
            rows.append(
                {
                    "symbol": symbol,
                    "score": score,
                    "volatility": volatility,
                    "eligible": eligible,
                    "absolute_return": absolute_return,
                    "trend_return": trend_return,
                }
            )
        eligible = sorted(
            (item for item in rows if item["eligible"]),
            key=lambda item: (-float(item["score"]), str(item["symbol"])),
        )
        breadth = len(eligible) / len(rows) if rows else 0.0
        current_symbols = set(context.get("current_symbols") or []) - {self.defensive_symbol}
        buffered = [
            item
            for rank, item in enumerate(eligible)
            if item["symbol"] in current_symbols and rank < self.top_k + self.rank_buffer
        ]
        selected = buffered[: self.top_k]
        selected_symbols = {item["symbol"] for item in selected}
        for item in eligible:
            if len(selected) >= self.top_k:
                break
            if item["symbol"] not in selected_symbols:
                selected.append(item)
                selected_symbols.add(item["symbol"])
        regime_multiplier = 1.0 if breadth >= self.breadth_threshold else self.risk_off_multiplier
        if not selected:
            self.last_diagnostics = {
                "breadth": breadth,
                "regime_multiplier": regime_multiplier,
                "risk_scale": 0.0,
                "selected": [],
            }
            return []
        inverse_volatility = np.asarray(
            [1.0 / float(item["volatility"]) for item in selected], dtype=float
        )
        weights = inverse_volatility / inverse_volatility.sum()
        estimated_volatility = float(
            sum(weight * float(item["volatility"]) for weight, item in zip(weights, selected))
        )
        risk_scale = min(1.0, self.target_volatility / max(estimated_volatility, 1e-6))
        self.last_diagnostics = {
            "breadth": breadth,
            "regime_multiplier": regime_multiplier,
            "risk_scale": risk_scale,
            "estimated_selected_volatility": estimated_volatility,
            "selected": [str(item["symbol"]) for item in selected],
        }
        return [
            StrategySignal(
                strategy=self.name,
                symbol=str(item["symbol"]),
                as_of=as_of,
                score=float(np.tanh(float(item["score"]))),
                target_weight=float(weight),
                confidence=float(
                    min(
                        0.95,
                        0.45
                        + 0.25 * breadth
                        + 0.20 * min(1.0, abs(float(item["score"]))),
                    )
                ),
                reasons=[
                    f"risk_adjusted_score={float(item['score']):.3f}",
                    f"absolute_momentum={float(item['absolute_return']):.2%}",
                    f"trend={float(item['trend_return']):.2%}",
                    f"annualized_volatility={float(item['volatility']):.2%}",
                    f"market_breadth={breadth:.2%}",
                ],
            )
            for weight, item in zip(weights, selected)
        ]
