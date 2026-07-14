from __future__ import annotations

import math
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from quantlab.domain.models import StrategySignal
from quantlab.strategies.base import Strategy


class AdaptiveEtfRotationStrategyV2(Strategy):
    """Prospective ETF challenger with continuous regime and covariance controls."""

    name = "adaptive_etf_rotation_v2"

    def __init__(self, config: dict[str, Any]):
        self.lookbacks = tuple(int(value) for value in config.get("lookbacks", (20, 60, 120)))
        self.momentum_weights = tuple(
            float(value) for value in config.get("momentum_weights", (0.50, 0.30, 0.20))
        )
        if len(self.lookbacks) != len(self.momentum_weights):
            raise ValueError("lookbacks and momentum_weights must have equal length")
        if not self.lookbacks or sum(self.momentum_weights) <= 0:
            raise ValueError("momentum configuration must contain positive total weight")
        total_momentum_weight = sum(self.momentum_weights)
        self.momentum_weights = tuple(
            value / total_momentum_weight for value in self.momentum_weights
        )

        self.top_k = int(config.get("top_k", 3))
        if self.top_k <= 0:
            raise ValueError("top_k must be positive")
        self.defensive_symbol = str(config.get("defensive_symbol", "sh511010"))
        self.volatility_lookback = int(config.get("volatility_lookback", 60))
        self.trend_lookback = int(config.get("trend_lookback", 120))
        self.covariance_lookback = int(config.get("covariance_lookback", 60))
        self.covariance_shrinkage = float(config.get("covariance_shrinkage", 0.50))
        self.correlation_penalty = float(config.get("correlation_penalty", 0.35))
        self.maximum_satellite_weight = float(config.get("maximum_satellite_weight", 0.60))
        if not 0 <= self.covariance_shrinkage <= 1:
            raise ValueError("covariance_shrinkage must be between zero and one")
        if self.maximum_satellite_weight * self.top_k < 1 - 1e-9:
            raise ValueError("maximum_satellite_weight is too small for top_k")

        self.rank_buffer = int(config.get("rank_buffer", 1))
        self.target_volatility = float(config.get("target_volatility", 0.12))
        self.breadth_risk_off = float(config.get("breadth_risk_off", 0.25))
        self.breadth_risk_on = float(config.get("breadth_risk_on", 0.75))
        if not 0 <= self.breadth_risk_off < self.breadth_risk_on <= 1:
            raise ValueError("breadth regime thresholds must be ordered within zero and one")

        self.regime_short_volatility_lookback = int(
            config.get("regime_short_volatility_lookback", 20)
        )
        self.regime_long_volatility_lookback = int(
            config.get("regime_long_volatility_lookback", 120)
        )
        self.volatility_stress_warning = float(config.get("volatility_stress_warning", 1.25))
        self.volatility_stress_stop = float(config.get("volatility_stress_stop", 2.0))
        if self.volatility_stress_stop <= self.volatility_stress_warning:
            raise ValueError("volatility stress stop must exceed warning")

        self.drawdown_lookback = int(config.get("drawdown_lookback", 60))
        self.drawdown_warning = float(config.get("drawdown_warning", -0.05))
        self.drawdown_stop = float(config.get("drawdown_stop", -0.15))
        if not self.drawdown_stop < self.drawdown_warning <= 0:
            raise ValueError("drawdown stop must be below warning")

        self.minimum_core_weight = float(config.get("minimum_core_weight", 0.35))
        self.maximum_core_weight = float(config.get("maximum_core_weight", 0.75))
        if not 0 <= self.minimum_core_weight <= self.maximum_core_weight <= 1:
            raise ValueError("core weight bounds must be ordered within zero and one")
        self.rebalance_tolerance_weight = float(
            config.get("rebalance_tolerance_weight", 0.02)
        )
        if not 0 <= self.rebalance_tolerance_weight <= 0.10:
            raise ValueError("rebalance tolerance must be between zero and ten percent")
        self.last_diagnostics: dict[str, Any] = {}

    def generate(self, as_of: date, data: pd.DataFrame, **context) -> list[StrategySignal]:
        frame = data[data["date"] <= pd.Timestamp(as_of)].sort_values(["symbol", "date"])
        maximum_history = max(
            max(self.lookbacks),
            self.volatility_lookback,
            self.trend_lookback,
            self.covariance_lookback,
            self.regime_long_volatility_lookback,
            self.drawdown_lookback,
        )
        rows: list[dict[str, Any]] = []
        return_series: dict[str, pd.Series] = {}
        for symbol, group in frame.groupby("symbol"):
            if symbol == self.defensive_symbol:
                continue
            close = pd.to_numeric(group["close"], errors="coerce")
            dated_close = pd.Series(
                close.to_numpy(dtype=float),
                index=pd.to_datetime(group["date"]).to_numpy(),
            ).dropna()
            if len(dated_close) <= maximum_history:
                continue
            daily_returns = dated_close.pct_change().dropna()
            volatility = float(
                daily_returns.tail(self.volatility_lookback).std(ddof=1) * math.sqrt(252)
            )
            if not math.isfinite(volatility) or volatility <= 1e-6:
                continue
            return_series[str(symbol)] = daily_returns

            risk_adjusted = []
            raw_returns = []
            for lookback in self.lookbacks:
                period_return = float(
                    dated_close.iloc[-1] / dated_close.iloc[-lookback - 1] - 1.0
                )
                horizon_risk = volatility * math.sqrt(lookback / 252)
                risk_adjusted.append(period_return / max(horizon_risk, 1e-6))
                raw_returns.append(period_return)
            weighted_momentum = float(
                sum(
                    weight * value
                    for weight, value in zip(self.momentum_weights, risk_adjusted)
                )
            )
            moving_average = float(dated_close.tail(self.trend_lookback).mean())
            trend_return = float(dated_close.iloc[-1] / moving_average - 1.0)
            trend_risk = volatility * math.sqrt(self.trend_lookback / 252)
            trend_score = trend_return / max(trend_risk, 1e-6)
            path = dated_close.tail(self.volatility_lookback)
            path_quality = float(
                (path.iloc[-1] - path.iloc[0])
                / max(float(path.diff().abs().sum()), 1e-9)
            )
            absolute_return = raw_returns[-1]
            eligible = absolute_return > 0 and dated_close.iloc[-1] > moving_average
            score = 0.65 * weighted_momentum + 0.25 * trend_score + 0.10 * path_quality
            rows.append(
                {
                    "symbol": str(symbol),
                    "score": score,
                    "volatility": volatility,
                    "eligible": eligible,
                    "absolute_return": absolute_return,
                    "trend_return": trend_return,
                }
            )

        eligible = [item for item in rows if item["eligible"]]
        breadth = len(eligible) / len(rows) if rows else 0.0
        regime = self._regime_diagnostics(breadth, return_series)
        diversified_ranking = self._diversified_ranking(eligible, return_series)
        current_symbols = set(context.get("current_symbols") or []) - {self.defensive_symbol}
        buffered = [
            item
            for rank, item in enumerate(diversified_ranking)
            if item["symbol"] in current_symbols and rank < self.top_k + self.rank_buffer
        ]
        selected = buffered[: self.top_k]
        selected_symbols = {item["symbol"] for item in selected}
        for item in diversified_ranking:
            if len(selected) >= self.top_k:
                break
            if item["symbol"] not in selected_symbols:
                selected.append(item)
                selected_symbols.add(item["symbol"])

        effective_core_weight = self.minimum_core_weight + (
            self.maximum_core_weight - self.minimum_core_weight
        ) * float(regime["regime_multiplier"])
        if not selected:
            self.last_diagnostics = {
                **regime,
                "breadth": breadth,
                "risk_scale": 0.0,
                "effective_core_weight": effective_core_weight,
                "selected": [],
                "allocation_method": "none",
                "rebalance_tolerance_weight": self.rebalance_tolerance_weight,
                "research_status": "prospective_challenger",
            }
            return []

        weights, estimated_volatility, allocation_method = self._portfolio_weights(
            selected, return_series
        )
        risk_scale = min(1.0, self.target_volatility / max(estimated_volatility, 1e-6))
        self.last_diagnostics = {
            **regime,
            "breadth": breadth,
            "risk_scale": risk_scale,
            "effective_core_weight": effective_core_weight,
            "estimated_selected_volatility": estimated_volatility,
            "selected": [str(item["symbol"]) for item in selected],
            "allocation_method": allocation_method,
            "rebalance_tolerance_weight": self.rebalance_tolerance_weight,
            "selection_correlations": {
                str(item["symbol"]): float(item.get("maximum_selected_correlation", 0.0))
                for item in selected
            },
            "research_status": "prospective_challenger",
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
                        0.40
                        + 0.20 * breadth
                        + 0.20 * float(regime["regime_multiplier"])
                        + 0.15 * min(1.0, abs(float(item["score"]))),
                    )
                ),
                reasons=[
                    f"risk_adjusted_score={float(item['score']):.3f}",
                    f"absolute_momentum={float(item['absolute_return']):.2%}",
                    f"trend={float(item['trend_return']):.2%}",
                    f"annualized_volatility={float(item['volatility']):.2%}",
                    f"market_breadth={breadth:.2%}",
                    f"regime_multiplier={float(regime['regime_multiplier']):.2f}",
                    f"max_selected_correlation={float(item.get('maximum_selected_correlation', 0)):.2f}",
                ],
            )
            for weight, item in zip(weights, selected)
        ]

    def _regime_diagnostics(
        self, breadth: float, return_series: dict[str, pd.Series]
    ) -> dict[str, float]:
        breadth_position = (breadth - self.breadth_risk_off) / (
            self.breadth_risk_on - self.breadth_risk_off
        )
        breadth_scale = _smoothstep(float(np.clip(breadth_position, 0.0, 1.0)))
        if not return_series:
            return {
                "breadth_scale": breadth_scale,
                "volatility_stress_ratio": 1.0,
                "volatility_scale": 1.0,
                "market_drawdown": 0.0,
                "drawdown_scale": 1.0,
                "regime_multiplier": 0.0,
            }

        aligned = pd.concat(return_series, axis=1).sort_index()
        market_returns = aligned.mean(axis=1, skipna=True).dropna()
        recent_volatility = float(
            market_returns.tail(self.regime_short_volatility_lookback).std(ddof=1)
        )
        long_volatility = float(
            market_returns.tail(self.regime_long_volatility_lookback).std(ddof=1)
        )
        volatility_stress_ratio = 1.0
        if (
            math.isfinite(recent_volatility)
            and math.isfinite(long_volatility)
            and long_volatility > 1e-9
        ):
            volatility_stress_ratio = recent_volatility / long_volatility
        volatility_scale = _descending_scale(
            volatility_stress_ratio,
            self.volatility_stress_warning,
            self.volatility_stress_stop,
        )

        market_curve = (1.0 + market_returns).cumprod()
        recent_curve = market_curve.tail(self.drawdown_lookback)
        market_drawdown = (
            float(recent_curve.iloc[-1] / recent_curve.max() - 1.0)
            if not recent_curve.empty
            else 0.0
        )
        drawdown_scale = _ascending_scale(
            market_drawdown,
            self.drawdown_stop,
            self.drawdown_warning,
        )
        return {
            "breadth_scale": breadth_scale,
            "volatility_stress_ratio": volatility_stress_ratio,
            "volatility_scale": volatility_scale,
            "market_drawdown": market_drawdown,
            "drawdown_scale": drawdown_scale,
            "regime_multiplier": min(breadth_scale, volatility_scale, drawdown_scale),
        }

    def _diversified_ranking(
        self,
        eligible: list[dict[str, Any]],
        return_series: dict[str, pd.Series],
    ) -> list[dict[str, Any]]:
        remaining = [dict(item) for item in eligible]
        ranking: list[dict[str, Any]] = []
        while remaining:
            scored = []
            for item in remaining:
                correlations = [
                    _pairwise_correlation(
                        return_series[item["symbol"]],
                        return_series[selected["symbol"]],
                        self.covariance_lookback,
                    )
                    for selected in ranking
                ]
                maximum_correlation = max([0.0, *correlations])
                diversified_score = float(item["score"]) - (
                    self.correlation_penalty * maximum_correlation
                )
                scored.append((diversified_score, maximum_correlation, item))
            diversified_score, maximum_correlation, winner = sorted(
                scored,
                key=lambda value: (-value[0], -float(value[2]["score"]), value[2]["symbol"]),
            )[0]
            winner["diversified_score"] = diversified_score
            winner["maximum_selected_correlation"] = maximum_correlation
            ranking.append(winner)
            remaining = [item for item in remaining if item["symbol"] != winner["symbol"]]
        return ranking

    def _portfolio_weights(
        self,
        selected: list[dict[str, Any]],
        return_series: dict[str, pd.Series],
    ) -> tuple[np.ndarray, float, str]:
        inverse_volatility = np.asarray(
            [1.0 / float(item["volatility"]) for item in selected], dtype=float
        )
        fallback_weights = inverse_volatility / inverse_volatility.sum()
        fallback_volatility = float(
            sum(
                weight * float(item["volatility"])
                for weight, item in zip(fallback_weights, selected)
            )
        )
        if len(selected) == 1:
            return fallback_weights, fallback_volatility, "single_asset"

        aligned = pd.concat(
            {item["symbol"]: return_series[item["symbol"]] for item in selected},
            axis=1,
        ).tail(self.covariance_lookback)
        aligned = aligned.dropna(how="any")
        if len(aligned) < max(20, len(selected) * 5):
            return fallback_weights, fallback_volatility, "inverse_volatility_fallback"

        covariance = aligned.cov().to_numpy(dtype=float) * 252
        diagonal = np.diag(np.diag(covariance))
        shrunk_covariance = (
            (1.0 - self.covariance_shrinkage) * covariance
            + self.covariance_shrinkage * diagonal
        )
        asset_volatility = np.sqrt(np.clip(np.diag(shrunk_covariance), 1e-12, None))
        try:
            raw_weights = np.linalg.pinv(shrunk_covariance) @ asset_volatility
        except np.linalg.LinAlgError:
            return fallback_weights, fallback_volatility, "inverse_volatility_fallback"
        raw_weights = np.clip(raw_weights, 0.0, None)
        if not np.all(np.isfinite(raw_weights)) or raw_weights.sum() <= 1e-9:
            return fallback_weights, fallback_volatility, "inverse_volatility_fallback"
        feasible_cap = max(self.maximum_satellite_weight, 1.0 / len(raw_weights))
        weights = _capped_normalize(raw_weights, feasible_cap)
        portfolio_variance = float(weights @ shrunk_covariance @ weights)
        portfolio_volatility = math.sqrt(max(portfolio_variance, 0.0))
        return weights, portfolio_volatility, "shrunk_maximum_diversification"


def _pairwise_correlation(first: pd.Series, second: pd.Series, lookback: int) -> float:
    paired = pd.concat([first, second], axis=1).tail(lookback).dropna()
    if len(paired) < 20:
        return 0.0
    correlation = float(paired.iloc[:, 0].corr(paired.iloc[:, 1]))
    return correlation if math.isfinite(correlation) else 0.0


def _smoothstep(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    bounded = float(np.clip(value, 0.0, 1.0))
    return bounded * bounded * (3.0 - 2.0 * bounded)


def _descending_scale(value: float, warning: float, stop: float) -> float:
    if value <= warning:
        return 1.0
    if value >= stop:
        return 0.0
    return _smoothstep((stop - value) / (stop - warning))


def _ascending_scale(value: float, stop: float, warning: float) -> float:
    if value <= stop:
        return 0.0
    if value >= warning:
        return 1.0
    return _smoothstep((value - stop) / (warning - stop))


def _capped_normalize(raw_weights: np.ndarray, cap: float) -> np.ndarray:
    if len(raw_weights) == 1:
        return np.asarray([1.0])
    weights = np.asarray(raw_weights, dtype=float)
    weights = np.clip(weights, 1e-12, None)
    weights = weights / weights.sum()
    result = np.zeros_like(weights)
    active = set(range(len(weights)))
    remaining = 1.0
    while active:
        active_total = sum(weights[index] for index in active)
        proposed = {
            index: remaining * weights[index] / active_total for index in active
        }
        capped = [index for index, value in proposed.items() if value > cap + 1e-12]
        if not capped:
            for index, value in proposed.items():
                result[index] = value
            break
        for index in capped:
            result[index] = cap
            remaining -= cap
            active.remove(index)
    return result / result.sum()
