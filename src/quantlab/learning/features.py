from __future__ import annotations

from typing import Any

import numpy as np

from quantlab.learning.cross_section import CROSS_SECTION_FEATURE_NAMES


FACTOR_NAMES = (
    "momentum_20",
    "momentum_60",
    "momentum_acceleration",
    "path_quality_60",
    "volume_asymmetry_20",
    "return_skewness_60",
    "price_position_60",
    "ma_spread_5_20",
    "rsi_14",
)

FEATURE_NAMES = (
    tuple(f"factor_{name}" for name in FACTOR_NAMES)
    + (
        "factor_composite",
        "mtf_consensus",
        "pullback_strength",
        "regime_bull",
        "regime_range",
        "regime_bear",
        "regime_high_volatility",
        "strategy_score",
        "strategy_confidence",
        "data_quality",
        "council_tactical",
        "council_strategic",
        "council_combined",
        "momentum_technical_sync",
        "council_veto",
        "financial_quality",
        "average_roe_10y_pct",
        "average_gross_margin_5y_pct",
        "average_net_margin_10y_pct",
        "latest_debt_ratio_pct",
        "average_ocf_to_profit_5y",
        "latest_revenue_growth_pct",
        "latest_profit_growth_pct",
        "quant_analyst_score",
        "fundamental_analyst_score",
        "news_analyst_score",
        "bull_confidence",
        "bear_confidence",
        "llm_up_probability",
        "llm_flat_probability",
        "llm_down_probability",
        "llm_confidence",
    )
    + CROSS_SECTION_FEATURE_NAMES
)


def extract_learning_features(context, reports: dict[str, Any]) -> dict[str, float]:
    features = empty_features()
    features.update(factor_report_features(context.quant_factors))
    features.update(context.cross_section_factors)
    regime_key = f"regime_{context.market_regime}"
    if regime_key in features:
        features[regime_key] = 1.0
    signal_weight = sum(item.confidence for item in context.strategy_signals)
    if signal_weight:
        features["strategy_score"] = (
            sum(item.score * item.confidence for item in context.strategy_signals) / signal_weight
        )
        features["strategy_confidence"] = signal_weight / len(context.strategy_signals)
    features["data_quality"] = float(context.data_quality)
    council = reports.get("council")
    if council:
        features["council_tactical"] = council.tactical_score
        features["council_strategic"] = council.strategic_score or 0.0
        features["council_combined"] = council.combined_score
        features["momentum_technical_sync"] = float(council.momentum_tech_sync)
        features["council_veto"] = float(council.veto_triggered)
    fundamentals = context.fundamentals or {}
    features["financial_quality"] = _float(fundamentals.get("quality_score"))
    metrics = fundamentals.get("metrics", {})
    for name in (
        "average_roe_10y_pct",
        "average_gross_margin_5y_pct",
        "average_net_margin_10y_pct",
        "latest_debt_ratio_pct",
        "average_ocf_to_profit_5y",
        "latest_revenue_growth_pct",
        "latest_profit_growth_pct",
    ):
        features[name] = _float(metrics.get(name))
    for report_name, feature_name in (
        ("quant", "quant_analyst_score"),
        ("fundamental", "fundamental_analyst_score"),
        ("news", "news_analyst_score"),
    ):
        report = reports.get(report_name)
        if report:
            features[feature_name] = _stance_value(report.stance) * report.confidence
    if reports.get("bull"):
        features["bull_confidence"] = reports["bull"].confidence
    if reports.get("bear"):
        features["bear_confidence"] = reports["bear"].confidence
    return sanitize_features(features)


def factor_report_features(report: dict | Any) -> dict[str, float]:
    features = empty_features()
    if not report:
        return features
    if hasattr(report, "model_dump"):
        report = report.model_dump(mode="json")
    for item in report.get("factors", []):
        key = f"factor_{item.get('name')}"
        if key in features:
            features[key] = _float(item.get("score"))
    features["factor_composite"] = _float(report.get("composite_score"))
    features["mtf_consensus"] = _float(report.get("multi_timeframe", {}).get("consensus")) / 3
    features["pullback_strength"] = _float(report.get("pullback_reversal", {}).get("strength"))
    regime = report.get("regime")
    regime_key = f"regime_{regime}"
    if regime_key in features:
        features[regime_key] = 1.0
    features["data_quality"] = min(1.0, _float(report.get("data_points")) / 252)
    return sanitize_features(features)


def with_forecast_features(features: dict[str, float], forecast) -> dict[str, float]:
    output = dict(features)
    output.update(
        {
            "llm_up_probability": (
                forecast.raw_llm_up_probability
                if forecast.raw_llm_up_probability is not None
                else forecast.up_probability
            ),
            "llm_flat_probability": (
                forecast.raw_llm_flat_probability
                if forecast.raw_llm_flat_probability is not None
                else forecast.flat_probability
            ),
            "llm_down_probability": (
                forecast.raw_llm_down_probability
                if forecast.raw_llm_down_probability is not None
                else forecast.down_probability
            ),
            "llm_confidence": forecast.confidence,
        }
    )
    return sanitize_features(output)


def empty_features() -> dict[str, float]:
    return {name: 0.0 for name in FEATURE_NAMES}


def sanitize_features(features: dict[str, float]) -> dict[str, float]:
    return {name: _float(features.get(name)) for name in FEATURE_NAMES}


def feature_vector(
    features: dict[str, float], feature_names: tuple[str, ...] | list[str] = FEATURE_NAMES
) -> np.ndarray:
    return np.asarray([_float(features.get(name)) for name in feature_names], dtype=float)


def _float(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if np.isfinite(number) else 0.0


def _stance_value(stance: str) -> float:
    return {"bullish": 1.0, "bearish": -1.0}.get(stance, 0.0)
