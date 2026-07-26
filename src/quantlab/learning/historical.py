from __future__ import annotations

from datetime import date

import pandas as pd

from quantlab.factors import MomentumFactorEngine
from quantlab.learning.cross_section import cross_sectional_features
from quantlab.learning.features import factor_report_features
from quantlab.learning.repository import LearningRepository


def generate_historical_samples(
    frame: pd.DataFrame,
    repository: LearningRepository,
    horizons: tuple[int, ...] = (5, 20),
    step: int = 5,
    minimum_history: int = 200,
    flat_threshold_pct: float = 1.0,
    sample_start: date | None = None,
    asset_scope: str = "etf",
    training_eligible: bool = True,
    universe_provenance: str = "configured_universe",
) -> dict:
    data = frame.copy()
    data["date"] = pd.to_datetime(data["date"])
    engine = MomentumFactorEngine()
    counts = {horizon: 0 for horizon in horizons}
    for symbol, group in data.sort_values(["symbol", "date"]).groupby("symbol"):
        group = group.reset_index(drop=True)
        max_horizon = max(horizons)
        for index in range(minimum_history - 1, len(group) - max_horizon, step):
            as_of = group.iloc[index].date.date()
            if sample_start and as_of < sample_start:
                continue
            report = engine.analyze(symbol, group.iloc[: index + 1], as_of)
            features = factor_report_features(report)
            features.update(cross_sectional_features(data, as_of, symbol))
            start_price = float(group.iloc[index].close)
            for horizon in horizons:
                target = group.iloc[index + horizon]
                realized = (float(target.close) / start_price - 1) * 100
                outcome = _outcome(realized, flat_threshold_pct)
                repository.upsert_sample(
                    sample_key=f"historical:{symbol}:{as_of.isoformat()}:{horizon}",
                    run_id=None,
                    source="historical_factor",
                    asset_scope=asset_scope,
                    symbol=symbol,
                    as_of=as_of,
                    horizon_days=horizon,
                    features=features,
                    expected_return_pct=None,
                    outcome=outcome,
                    realized_return_pct=realized,
                    evaluated_at=target.date.date(),
                    context={
                        "feature_availability": "factor_plus_cross_section",
                        "training_eligible": training_eligible,
                        "universe_provenance": universe_provenance,
                    },
                    origin="historical_research",
                    evidence_stage="historical_training",
                    training_eligible=training_eligible,
                )
                counts[horizon] += 1
    return {"generated": counts, "symbols": int(data.symbol.nunique()), "step": step}


def _outcome(realized_return_pct: float, threshold: float) -> str:
    if realized_return_pct > threshold:
        return "up"
    if realized_return_pct < -threshold:
        return "down"
    return "flat"
