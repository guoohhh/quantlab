from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd


CROSS_SECTION_FEATURE_NAMES = (
    "cross_section_momentum_20_rank",
    "cross_section_momentum_60_rank",
    "cross_section_momentum_120_rank",
    "cross_section_volatility_20_rank",
    "cross_section_relative_return_20",
    "cross_section_breadth_20",
    "cross_section_dispersion_20",
    "cross_section_leadership_gap_20",
)


def cross_sectional_features(frame: pd.DataFrame, as_of: date, symbol: str) -> dict[str, float]:
    """Build point-in-time relative ETF features using data available at ``as_of`` only."""

    output = {name: 0.0 for name in CROSS_SECTION_FEATURE_NAMES}
    if frame.empty or not {"symbol", "date", "close"}.issubset(frame.columns):
        return output
    data = frame.loc[:, ["symbol", "date", "close"]].copy()
    data["date"] = pd.to_datetime(data["date"])
    data = data[data["date"] <= pd.Timestamp(as_of)]
    if data.empty:
        return output
    pivot = (
        data.drop_duplicates(["date", "symbol"], keep="last")
        .pivot(index="date", columns="symbol", values="close")
        .sort_index()
    )
    if symbol not in pivot.columns or len(pivot.columns) < 2:
        return output
    returns = {
        lookback: pivot.pct_change(lookback, fill_method=None).iloc[-1].dropna()
        for lookback in (20, 60, 120)
        if len(pivot) > lookback
    }
    for lookback in (20, 60, 120):
        values = returns.get(lookback)
        if values is not None and symbol in values:
            output[f"cross_section_momentum_{lookback}_rank"] = _rank(values, symbol)
    daily_returns = pivot.pct_change(fill_method=None).tail(20)
    volatility = daily_returns.std(ddof=0).dropna()
    if symbol in volatility:
        output["cross_section_volatility_20_rank"] = _rank(volatility, symbol)
    return_20 = returns.get(20)
    if return_20 is not None and symbol in return_20:
        own_return = float(return_20[symbol])
        output["cross_section_relative_return_20"] = own_return - float(return_20.mean())
        output["cross_section_breadth_20"] = float((return_20 > 0).mean())
        output["cross_section_dispersion_20"] = float(return_20.std(ddof=0))
        output["cross_section_leadership_gap_20"] = own_return - float(return_20.max())
    return {name: _finite(value) for name, value in output.items()}


def _rank(values: pd.Series, symbol: str) -> float:
    return float(values.rank(method="average", pct=True).loc[symbol])


def _finite(value: float) -> float:
    number = float(value)
    return number if np.isfinite(number) else 0.0
