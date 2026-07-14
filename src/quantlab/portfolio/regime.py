from __future__ import annotations

import numpy as np
import pandas as pd

from quantlab.domain.models import MarketRegime


def detect_regime(close: pd.Series) -> tuple[MarketRegime, float, dict[str, float]]:
    close = close.dropna().astype(float)
    if len(close) < 200:
        return MarketRegime.RANGE, 0.3, {"reason": 0.0}
    ma60 = close.tail(60).mean()
    ma200 = close.tail(200).mean()
    slope60 = ma60 / close.iloc[-120:-60].mean() - 1 if len(close) >= 120 else 0.0
    vol20 = close.pct_change().tail(20).std() * np.sqrt(252)
    current = close.iloc[-1]
    if vol20 > 0.35:
        regime = MarketRegime.HIGH_VOLATILITY
    elif current > ma60 > ma200 and slope60 > 0:
        regime = MarketRegime.BULL
    elif current < ma200:
        regime = MarketRegime.BEAR
    else:
        regime = MarketRegime.RANGE
    distance = min(1.0, abs(current / ma200 - 1) * 5 + abs(slope60) * 3)
    return (
        regime,
        max(0.35, distance),
        {"ma60": ma60, "ma200": ma200, "slope60": slope60, "vol20": vol20},
    )
