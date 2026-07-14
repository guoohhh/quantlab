from __future__ import annotations

import math
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from quantlab.strategies.adaptive_etf_v2 import (
    AdaptiveEtfRotationStrategyV2,
    _descending_scale,
    _pairwise_correlation,
)


class AdaptiveEtfRotationStrategyV3(AdaptiveEtfRotationStrategyV2):
    """Prospective V3 challenger using downside stress and thresholded correlation."""

    name = "adaptive_etf_rotation_v3"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.correlation_penalty_threshold = float(
            config.get("correlation_penalty_threshold", 0.80)
        )
        if not 0 <= self.correlation_penalty_threshold < 1:
            raise ValueError("correlation penalty threshold must be within zero and one")

    def generate(self, as_of: date, data: pd.DataFrame, **context):
        signals = super().generate(as_of, data, **context)
        self.last_diagnostics["research_status"] = "prospective_v3_challenger"
        self.last_diagnostics["strategy_version"] = "adaptive_v3"
        return signals

    def _regime_diagnostics(
        self, breadth: float, return_series: dict[str, pd.Series]
    ) -> dict[str, float]:
        diagnostics = super()._regime_diagnostics(breadth, return_series)
        if not return_series:
            diagnostics["downside_volatility_stress_ratio"] = 1.0
            return diagnostics

        aligned = pd.concat(return_series, axis=1).sort_index()
        market_returns = aligned.mean(axis=1, skipna=True).dropna()
        short_downside = _downside_semideviation(
            market_returns.tail(self.regime_short_volatility_lookback)
        )
        long_downside = _downside_semideviation(
            market_returns.tail(self.regime_long_volatility_lookback)
        )
        downside_ratio = (
            short_downside / long_downside
            if math.isfinite(long_downside) and long_downside > 1e-9
            else 1.0
        )
        downside_scale = _descending_scale(
            downside_ratio,
            self.volatility_stress_warning,
            self.volatility_stress_stop,
        )
        diagnostics["total_volatility_stress_ratio"] = diagnostics[
            "volatility_stress_ratio"
        ]
        diagnostics["downside_volatility_stress_ratio"] = downside_ratio
        diagnostics["volatility_stress_ratio"] = downside_ratio
        diagnostics["volatility_scale"] = downside_scale
        diagnostics["regime_multiplier"] = min(
            float(diagnostics["breadth_scale"]),
            downside_scale,
            float(diagnostics["drawdown_scale"]),
        )
        return diagnostics

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
                excess_correlation = max(
                    0.0,
                    (maximum_correlation - self.correlation_penalty_threshold)
                    / (1.0 - self.correlation_penalty_threshold),
                )
                diversified_score = float(item["score"]) - (
                    self.correlation_penalty * excess_correlation
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


def _downside_semideviation(returns: pd.Series) -> float:
    values = pd.to_numeric(returns, errors="coerce").dropna().to_numpy(dtype=float)
    if len(values) < 2:
        return 0.0
    downside = np.minimum(values, 0.0)
    return float(math.sqrt(float(np.mean(np.square(downside)))))
