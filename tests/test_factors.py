from datetime import date, timedelta

import numpy as np
import pandas as pd

from quantlab.factors import MomentumFactorEngine, calculate_factor_ic


def _trend_frame(days=260):
    dates = [date(2025, 1, 1) + timedelta(days=index) for index in range(days)]
    close = np.linspace(100, 160, days) + np.sin(np.arange(days) / 8)
    return pd.DataFrame(
        {
            "symbol": "sh510300",
            "date": dates,
            "close": close,
            "volume": np.linspace(1_000_000, 1_300_000, days),
        }
    )


def test_momentum_factor_report_contains_ironq_layers():
    frame = _trend_frame()
    report = MomentumFactorEngine().analyze("sh510300", frame, frame.date.iloc[-1])

    names = {item.name for item in report.factors}
    assert {"momentum_acceleration", "path_quality_60", "volume_asymmetry_20"} <= names
    assert report.multi_timeframe.consensus > 0
    assert report.composite_score > 0
    assert report.data_points == len(frame)


def test_factor_ic_detects_positive_cross_sectional_signal():
    rows = []
    start = date(2025, 1, 1)
    for symbol_index in range(6):
        symbol = f"s{symbol_index}"
        for day in range(40):
            factor = float(symbol_index)
            rows.append(
                {
                    "date": start + timedelta(days=day),
                    "symbol": symbol,
                    "close": 100 + day * (symbol_index + 1),
                    "factor": factor,
                }
            )
    result = calculate_factor_ic(pd.DataFrame(rows), ["factor"], horizon_days=5)[0]

    assert result.observations > 0
    assert result.rank_ic is not None and result.rank_ic > 0.9
    assert result.direction == "long"
