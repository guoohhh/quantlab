from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from quantlab.domain.models import MarketRegime
from quantlab.portfolio.regime import detect_regime


class FactorReading(BaseModel):
    name: str
    value: float
    score: float = Field(ge=-1, le=1)
    horizon_days: int
    direction: str
    description: str


class MultiTimeframeTrend(BaseModel):
    daily: int = Field(ge=-1, le=1)
    weekly: int = Field(ge=-1, le=1)
    monthly: int = Field(ge=-1, le=1)
    consensus: int = Field(ge=-3, le=3)
    verdict: str


class PullbackReversalSignal(BaseModel):
    triggered: bool
    strength: float = Field(ge=0, le=1)
    conditions: dict[str, bool]


class QuantFactorReport(BaseModel):
    symbol: str
    as_of: date
    regime: MarketRegime
    regime_confidence: float = Field(ge=0, le=1)
    factors: list[FactorReading]
    composite_score: float = Field(ge=-1, le=1)
    multi_timeframe: MultiTimeframeTrend
    pullback_reversal: PullbackReversalSignal
    data_points: int
    warnings: list[str] = Field(default_factory=list)


class FactorIC(BaseModel):
    factor: str
    horizon_days: int
    observations: int
    rank_ic: float | None = None
    pearson_ic: float | None = None
    direction: str = "insufficient"


class MomentumFactorEngine:
    """Auditable momentum factor engine inspired by IronQ's disclosed factor layers."""

    WEIGHTS = {
        "momentum_20": 0.16,
        "momentum_60": 0.14,
        "momentum_acceleration": 0.13,
        "path_quality_60": 0.13,
        "volume_asymmetry_20": 0.10,
        "return_skewness_60": 0.08,
        "price_position_60": 0.09,
        "ma_spread_5_20": 0.09,
        "rsi_14": 0.08,
    }

    def analyze(self, symbol: str, data: pd.DataFrame, as_of: date) -> QuantFactorReport:
        frame = data.copy()
        if "symbol" in frame:
            frame = frame[frame.symbol == symbol]
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame[frame.date <= pd.Timestamp(as_of)].sort_values("date").drop_duplicates("date")
        if len(frame) < 120:
            raise ValueError("momentum factor engine requires at least 120 observations")
        close = frame["close"].astype(float)
        volume = frame.get("volume", pd.Series(0.0, index=frame.index)).astype(float)
        returns = close.pct_change()

        mom20 = close.iloc[-1] / close.iloc[-21] - 1
        mom60 = close.iloc[-1] / close.iloc[-61] - 1
        speed20 = np.log(close.iloc[-1] / close.iloc[-21]) / 20
        speed60 = np.log(close.iloc[-1] / close.iloc[-61]) / 60
        acceleration = speed20 - speed60
        path_quality = _path_quality(returns.tail(60), mom60)
        volume_asymmetry = _volume_asymmetry(returns.tail(20), volume.tail(20))
        skewness = float(returns.tail(60).skew()) if returns.tail(60).notna().sum() > 2 else 0.0
        price_range = close.tail(60).max() - close.tail(60).min()
        price_position = (
            (close.iloc[-1] - close.tail(60).min()) / price_range if price_range > 0 else 0.5
        )
        ma5 = close.tail(5).mean()
        ma20 = close.tail(20).mean()
        ma_spread = ma5 / ma20 - 1
        rsi = _rsi(close, 14)

        factors = [
            _reading("momentum_20", mom20, np.tanh(mom20 * 5), 20, "recent return"),
            _reading("momentum_60", mom60, np.tanh(mom60 * 3), 60, "medium return"),
            _reading(
                "momentum_acceleration",
                acceleration,
                np.tanh(acceleration * 250),
                20,
                "20-day log-return speed minus 60-day speed",
            ),
            _reading(
                "path_quality_60",
                path_quality,
                path_quality,
                60,
                "signed net move divided by total daily path length",
            ),
            _reading(
                "volume_asymmetry_20",
                volume_asymmetry,
                np.tanh(volume_asymmetry),
                20,
                "log ratio of average up-day volume to down-day volume",
            ),
            _reading(
                "return_skewness_60",
                skewness,
                np.tanh(skewness / 2),
                60,
                "daily return skewness",
            ),
            _reading(
                "price_position_60",
                price_position,
                2 * price_position - 1,
                60,
                "position inside the 60-day close range",
            ),
            _reading(
                "ma_spread_5_20",
                ma_spread,
                np.tanh(ma_spread * 12),
                20,
                "5-day average relative to 20-day average",
            ),
            _reading(
                "rsi_14",
                rsi,
                np.clip((rsi - 50) / 30, -1, 1),
                14,
                "relative strength index centered at 50",
            ),
        ]
        composite = sum(self.WEIGHTS[item.name] * item.score for item in factors)
        regime, regime_confidence, _ = detect_regime(close)
        mtf = _multi_timeframe(frame)
        pullback = _pullback_reversal(close, volume, rsi)
        warnings = []
        if len(frame) < 200:
            warnings.append("fewer than 200 observations; regime confidence is limited")
        if volume.fillna(0).tail(60).sum() <= 0:
            warnings.append("volume unavailable; volume factors are neutral")
        return QuantFactorReport(
            symbol=symbol,
            as_of=as_of,
            regime=regime,
            regime_confidence=regime_confidence,
            factors=factors,
            composite_score=float(np.clip(composite, -1, 1)),
            multi_timeframe=mtf,
            pullback_reversal=pullback,
            data_points=len(frame),
            warnings=warnings,
        )


def calculate_factor_ic(
    panel: pd.DataFrame,
    factor_columns: list[str],
    horizon_days: int = 20,
    minimum_cross_section: int = 5,
) -> list[FactorIC]:
    """Calculate time-series averages of daily cross-sectional IC values."""
    required = {"date", "symbol", "close", *factor_columns}
    if not required.issubset(panel.columns):
        missing = sorted(required - set(panel.columns))
        raise ValueError(f"missing IC columns: {', '.join(missing)}")
    frame = panel.copy().sort_values(["symbol", "date"])
    frame["forward_return"] = (
        frame.groupby("symbol")["close"].shift(-horizon_days) / frame["close"] - 1
    )
    output = []
    for factor in factor_columns:
        daily_rank = []
        daily_pearson = []
        observations = 0
        for _, group in frame[["date", factor, "forward_return"]].dropna().groupby("date"):
            if len(group) < minimum_cross_section:
                continue
            if group[factor].nunique() < 2 or group.forward_return.nunique() < 2:
                continue
            rank_ic = group[factor].rank().corr(group.forward_return.rank(), method="pearson")
            pearson_ic = group[factor].corr(group.forward_return, method="pearson")
            if pd.notna(rank_ic):
                daily_rank.append(float(rank_ic))
            if pd.notna(pearson_ic):
                daily_pearson.append(float(pearson_ic))
            observations += len(group)
        rank_mean = float(np.mean(daily_rank)) if daily_rank else None
        pearson_mean = float(np.mean(daily_pearson)) if daily_pearson else None
        reference = rank_mean if rank_mean is not None else pearson_mean
        direction = "long" if reference is not None and reference > 0 else "short"
        if reference is None:
            direction = "insufficient"
        output.append(
            FactorIC(
                factor=factor,
                horizon_days=horizon_days,
                observations=observations,
                rank_ic=rank_mean,
                pearson_ic=pearson_mean,
                direction=direction,
            )
        )
    return output


def _reading(name: str, value: float, score: float, horizon: int, description: str):
    clipped = float(np.clip(score, -1, 1))
    direction = "long" if clipped > 0.05 else "short" if clipped < -0.05 else "neutral"
    return FactorReading(
        name=name,
        value=float(value),
        score=clipped,
        horizon_days=horizon,
        direction=direction,
        description=description,
    )


def _path_quality(returns: pd.Series, total_return: float) -> float:
    path_length = float(returns.abs().sum())
    if path_length <= 0:
        return 0.0
    return float(np.clip(total_return / path_length, -1, 1))


def _volume_asymmetry(returns: pd.Series, volume: pd.Series) -> float:
    valid = pd.DataFrame({"return": returns, "volume": volume}).dropna()
    up = valid.loc[valid["return"] > 0, "volume"]
    down = valid.loc[valid["return"] < 0, "volume"]
    if up.empty or down.empty or up.mean() <= 0 or down.mean() <= 0:
        return 0.0
    return float(np.log(up.mean() / down.mean()))


def _rsi(close: pd.Series, period: int) -> float:
    delta = close.diff().tail(period)
    gains = delta.clip(lower=0).mean()
    losses = -delta.clip(upper=0).mean()
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    relative_strength = gains / losses
    return float(100 - 100 / (1 + relative_strength))


def _trend_score(close: pd.Series) -> int:
    if len(close) < 21:
        return 0
    ema8 = close.ewm(span=8, adjust=False).mean().iloc[-1]
    ema21 = close.ewm(span=21, adjust=False).mean().iloc[-1]
    current = close.iloc[-1]
    if current > ema8 > ema21:
        return 1
    if current < ema8 < ema21:
        return -1
    return 0


def _multi_timeframe(frame: pd.DataFrame) -> MultiTimeframeTrend:
    indexed = frame.set_index("date")["close"].astype(float)
    daily = _trend_score(indexed)
    weekly = _trend_score(indexed.resample("W-FRI").last().dropna())
    monthly = _trend_score(indexed.resample("ME").last().dropna())
    consensus = daily + weekly + monthly
    verdicts = {
        3: "strong_uptrend",
        2: "uptrend",
        1: "weak_uptrend",
        0: "mixed",
        -1: "weak_downtrend",
        -2: "downtrend",
        -3: "strong_downtrend",
    }
    return MultiTimeframeTrend(
        daily=daily,
        weekly=weekly,
        monthly=monthly,
        consensus=consensus,
        verdict=verdicts[consensus],
    )


def _pullback_reversal(close: pd.Series, volume: pd.Series, rsi: float) -> PullbackReversalSignal:
    ma60 = close.tail(60).mean()
    ma120 = close.tail(120).mean()
    daily_returns = close.pct_change()
    conditions = {
        "uptrend": bool(close.iloc[-1] > ma60 > ma120),
        "consecutive_pullback": bool((daily_returns.tail(3) < 0).all()),
        "rsi_oversold": bool(rsi < 40),
        "volume_contraction": bool(
            volume.tail(3).mean() < volume.tail(20).mean() * 0.8
            if volume.tail(20).mean() > 0
            else False
        ),
    }
    strength = sum(conditions.values()) / len(conditions)
    return PullbackReversalSignal(
        triggered=all(conditions.values()),
        strength=strength,
        conditions=conditions,
    )
