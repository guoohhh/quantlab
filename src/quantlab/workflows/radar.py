from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from math import tanh
from typing import Any

import numpy as np
import pandas as pd

from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.domain.models import Bar
from quantlab.portfolio.regime import detect_regime


ETF_METADATA = {
    "sh510300": {"name": "沪深300ETF", "category": "A股宽基", "risk_bucket": "risk_on"},
    "sz159915": {"name": "创业板ETF", "category": "A股成长", "risk_bucket": "risk_on"},
    "sh510880": {"name": "红利ETF", "category": "A股红利", "risk_bucket": "balanced"},
    "sh518880": {"name": "黄金ETF", "category": "黄金", "risk_bucket": "defensive"},
    "sh513100": {"name": "纳指ETF", "category": "海外权益", "risk_bucket": "risk_on"},
    "sh511010": {"name": "国债ETF", "category": "债券", "risk_bucket": "defensive"},
}


def build_market_radar(
    settings: Settings,
    as_of: date | None = None,
    include_sectors: bool = False,
    sector_limit: int = 15,
) -> dict[str, Any]:
    requested_as_of = as_of or date.today()
    symbols = list(settings.get("strategies.etf_rotation.universe", ETF_METADATA))
    fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
    provider = CachedProvider(
        fallback,
        settings.resolve(settings.get("system.data_dir")) / "cache",
    )
    bars = provider.bars(symbols, requested_as_of - timedelta(days=550), requested_as_of)
    output = calculate_market_radar(
        bars,
        symbols=symbols,
        requested_as_of=requested_as_of,
        source=provider.name,
        degraded_sources=list(fallback.last_degraded_from),
    )
    if include_sectors:
        try:
            output["sectors"] = load_sector_snapshot(sector_limit)
        except Exception as exc:
            output["sectors"] = []
            output["degraded_sources"].append(f"industry snapshot failed: {exc}")
    return output


def calculate_market_radar(
    bars: list[Bar] | list[dict[str, Any]],
    symbols: list[str] | None = None,
    requested_as_of: date | None = None,
    source: str = "unknown",
    degraded_sources: list[str] | None = None,
) -> dict[str, Any]:
    records = [item.model_dump() if isinstance(item, Bar) else item for item in bars]
    frame = pd.DataFrame(records)
    if frame.empty:
        raise ValueError("market radar data returned no bars")
    required = {"symbol", "date", "close"}
    if not required.issubset(frame.columns):
        raise ValueError(f"market radar data is missing columns: {sorted(required - set(frame))}")
    frame["date"] = pd.to_datetime(frame["date"])
    if "adjusted_close" in frame.columns:
        frame["signal_close"] = frame["adjusted_close"].fillna(frame["close"])
    else:
        frame["signal_close"] = frame["close"]
    frame = frame.sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"], keep="last")
    effective_as_of = frame["date"].max().date()
    requested_symbols = symbols or sorted(frame["symbol"].unique())

    rows: list[dict[str, Any]] = []
    for symbol in requested_symbols:
        group = frame[frame["symbol"] == symbol].sort_values("date")
        if group.empty:
            continue
        close = group["signal_close"].astype(float).dropna()
        raw_close = group.loc[close.index, "close"].astype(float)
        metadata = ETF_METADATA.get(
            symbol,
            {"name": symbol, "category": "其他", "risk_bucket": "balanced"},
        )
        row = {
            "symbol": symbol,
            **metadata,
            "as_of": group.iloc[-1]["date"].date().isoformat(),
            "price": _round(raw_close.iloc[-1], 4),
            "return_20_pct": _return_pct(close, 20),
            "return_60_pct": _return_pct(close, 60),
            "return_120_pct": _return_pct(close, 120),
            "volatility_20_pct": _annualized_volatility(close, 20),
            "distance_ma60_pct": _distance_to_average(close, 60),
            "distance_ma120_pct": _distance_to_average(close, 120),
            "observations": int(len(close)),
            "source": str(group.iloc[-1].get("source") or source),
        }
        rows.append(row)
    if not rows:
        raise ValueError("market radar has no usable instruments")

    table = pd.DataFrame(rows)
    for lookback in (20, 60, 120):
        column = f"return_{lookback}_pct"
        percentile = table[column].rank(pct=True, method="average")
        table[f"momentum_{lookback}_percentile"] = percentile.fillna(0.5)
        table[f"rank_{lookback}"] = (
            table[column].rank(ascending=False, method="min", na_option="bottom").astype(int)
        )
    volatility_percentile = table["volatility_20_pct"].rank(pct=True, method="average")
    table["strength_score"] = (
        100
        * (
            0.35 * table["momentum_20_percentile"]
            + 0.35 * table["momentum_60_percentile"]
            + 0.20 * table["momentum_120_percentile"]
            + 0.10 * (1 - volatility_percentile.fillna(0.5))
        )
    ).round(1)
    table["trend"] = table.apply(_trend_label, axis=1)
    table = table.sort_values(["strength_score", "return_20_pct"], ascending=False)

    breadth_20 = _positive_share(table["return_20_pct"])
    breadth_60 = _positive_share(table["return_60_pct"])
    above_ma60 = _positive_share(table["distance_ma60_pct"])
    above_ma120 = _positive_share(table["distance_ma120_pct"])
    dispersion_20 = _safe_float(table["return_20_pct"].std(ddof=0)) or 0.0
    risk_spread_20 = _risk_spread(table, "return_20_pct")
    appetite_score = float(
        np.clip(
            0.35 * (2 * breadth_20 - 1)
            + 0.25 * (2 * breadth_60 - 1)
            + 0.20 * (2 * above_ma60 - 1)
            + 0.20 * tanh(risk_spread_20 / 5.0),
            -1,
            1,
        )
    )
    if appetite_score >= 0.25:
        appetite = "risk_on"
    elif appetite_score <= -0.25:
        appetite = "risk_off"
    else:
        appetite = "neutral"

    benchmark = frame[frame["symbol"] == "sh510300"].sort_values("date")["signal_close"]
    regime, regime_confidence, regime_features = detect_regime(benchmark)
    leaders = table.head(3)["symbol"].tolist()
    laggards = table.tail(3).sort_values("strength_score")["symbol"].tolist()
    output_rows = table.replace({np.nan: None}).to_dict("records")
    return {
        "requested_as_of": (requested_as_of or effective_as_of).isoformat(),
        "as_of": effective_as_of.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "source": source,
        "coverage": {"requested": len(requested_symbols), "available": len(output_rows)},
        "market_regime": regime.value,
        "regime_confidence": round(float(regime_confidence), 4),
        "regime_features": {key: _round(value, 6) for key, value in regime_features.items()},
        "risk_appetite": appetite,
        "risk_appetite_score": round(appetite_score, 4),
        "breadth": {
            "positive_20": round(breadth_20, 4),
            "positive_60": round(breadth_60, 4),
            "above_ma60": round(above_ma60, 4),
            "above_ma120": round(above_ma120, 4),
        },
        "dispersion_20_pct": round(dispersion_20, 4),
        "risk_spread_20_pct": round(risk_spread_20, 4),
        "leaders": leaders,
        "laggards": laggards,
        "instruments": output_rows,
        "sectors": [],
        "degraded_sources": list(dict.fromkeys(degraded_sources or [])),
        "methodology": [
            "returns use adjusted closes when available and never use data after as_of",
            "strength combines 20/60/120-day cross-sectional momentum and a volatility penalty",
            "risk appetite combines breadth, moving-average participation and risk-on/defensive spread",
            "sector data is an optional real-time snapshot and is not treated as historical evidence",
        ],
    }


def load_sector_snapshot(limit: int = 15) -> list[dict[str, Any]]:
    import akshare as ak

    frame = ak.stock_board_industry_name_em()
    if frame.empty:
        raise ValueError("AkShare returned an empty industry snapshot")
    name_col = _find_column(frame, ("板块名称", "行业名称", "名称"))
    change_col = _find_column(frame, ("涨跌幅", "涨跌幅%"))
    turnover_col = _find_column(frame, ("换手率",), required=False)
    up_col = _find_column(frame, ("上涨家数",), required=False)
    down_col = _find_column(frame, ("下跌家数",), required=False)
    leader_col = _find_column(frame, ("领涨股票", "领涨股"), required=False)
    snapshot = pd.DataFrame(
        {
            "name": frame[name_col].astype(str),
            "change_pct": pd.to_numeric(frame[change_col], errors="coerce"),
            "turnover_pct": (
                pd.to_numeric(frame[turnover_col], errors="coerce") if turnover_col else np.nan
            ),
            "up_count": pd.to_numeric(frame[up_col], errors="coerce") if up_col else np.nan,
            "down_count": (pd.to_numeric(frame[down_col], errors="coerce") if down_col else np.nan),
            "leader": frame[leader_col].astype(str) if leader_col else "",
        }
    ).dropna(subset=["change_pct"])
    participation = snapshot["up_count"] / (snapshot["up_count"] + snapshot["down_count"])
    change_rank = snapshot["change_pct"].rank(pct=True)
    turnover_rank = snapshot["turnover_pct"].rank(pct=True).fillna(0.5)
    snapshot["heat_score"] = (
        100 * (0.65 * change_rank + 0.20 * turnover_rank + 0.15 * participation.fillna(0.5))
    ).round(1)
    snapshot = snapshot.sort_values(["heat_score", "change_pct"], ascending=False).head(
        max(1, min(int(limit), 50))
    )
    return snapshot.replace({np.nan: None}).to_dict("records")


def _return_pct(close: pd.Series, periods: int) -> float | None:
    if len(close) <= periods:
        return None
    return _round((close.iloc[-1] / close.iloc[-periods - 1] - 1) * 100, 4)


def _annualized_volatility(close: pd.Series, periods: int) -> float | None:
    returns = close.pct_change(fill_method=None).dropna().tail(periods)
    if len(returns) < periods:
        return None
    return _round(returns.std(ddof=0) * np.sqrt(252) * 100, 4)


def _distance_to_average(close: pd.Series, periods: int) -> float | None:
    if len(close) < periods:
        return None
    return _round((close.iloc[-1] / close.tail(periods).mean() - 1) * 100, 4)


def _positive_share(values: pd.Series) -> float:
    known = pd.to_numeric(values, errors="coerce").dropna()
    return float((known > 0).mean()) if len(known) else 0.0


def _risk_spread(table: pd.DataFrame, column: str) -> float:
    risk_on = pd.to_numeric(
        table.loc[table["risk_bucket"] == "risk_on", column], errors="coerce"
    ).dropna()
    defensive = pd.to_numeric(
        table.loc[table["risk_bucket"] == "defensive", column], errors="coerce"
    ).dropna()
    if risk_on.empty or defensive.empty:
        return 0.0
    return float(risk_on.mean() - defensive.mean())


def _trend_label(row: pd.Series) -> str:
    distance_60 = row.get("distance_ma60_pct")
    distance_120 = row.get("distance_ma120_pct")
    return_20 = row.get("return_20_pct")
    if pd.notna(distance_60) and pd.notna(distance_120) and pd.notna(return_20):
        if distance_60 > 0 and distance_120 > 0 and return_20 > 0:
            return "strong_up"
        if distance_60 < 0 and distance_120 < 0 and return_20 < 0:
            return "strong_down"
    if pd.notna(distance_60) and distance_60 > 0:
        return "up"
    if pd.notna(distance_60) and distance_60 < 0:
        return "down"
    return "range"


def _find_column(
    frame: pd.DataFrame, candidates: tuple[str, ...], required: bool = True
) -> str | None:
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
    for column in frame.columns:
        if any(candidate in str(column) for candidate in candidates):
            return str(column)
    if required:
        raise ValueError(f"industry snapshot missing one of columns: {candidates}")
    return None


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _round(value: Any, digits: int) -> float | None:
    number = _safe_float(value)
    return round(number, digits) if number is not None else None
