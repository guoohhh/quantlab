from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pandas as pd

from quantlab.agents import MultiAgentDecisionSystem, ResearchContext
from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.domain.models import StrategySignal
from quantlab.factors import MomentumFactorEngine, QuantFactorReport
from quantlab.fundamentals import load_a_share_financial_report
from quantlab.llm import await_with_provider_close, build_provider
from quantlab.learning import LearningRepository, build_predictor
from quantlab.workflows.events import collect_all_events
from quantlab.workflows.radar import build_market_radar


def load_quant_report(
    settings: Settings,
    symbol: str,
    as_of: date | None = None,
    lookback_calendar_days: int = 500,
) -> dict:
    end = as_of or date.today()
    fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
    provider = CachedProvider(
        fallback,
        settings.resolve(settings.get("system.data_dir")) / "cache",
    )
    bars = provider.bars([symbol], end - timedelta(days=lookback_calendar_days), end)
    frame = pd.DataFrame([bar.model_dump() for bar in bars])
    if frame.empty:
        raise ValueError(f"no market data returned for {symbol}")
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame[frame["date"].dt.date <= end].sort_values("date")
    if frame.empty:
        raise ValueError(f"no market data returned for {symbol} on or before {end}")
    signal_frame = frame.copy()
    signal_frame["close"] = signal_frame["adjusted_close"].fillna(signal_frame["close"])
    effective_as_of = frame.date.max().date()
    report = MomentumFactorEngine().analyze(symbol, signal_frame, effective_as_of)
    raw_price = float(frame.iloc[-1].close)
    return {
        "report": report,
        "price": raw_price,
        "as_of": effective_as_of,
        "source": provider.name,
        "degraded_sources": fallback.last_degraded_from,
        "bars": len(frame),
        "price_history": build_price_history_evidence(frame, end),
    }


def analyze_symbol(
    settings: Settings,
    symbol: str,
    as_of: date | None = None,
    fundamentals: dict | None = None,
    news: list[dict] | None = None,
    asset_type: str | None = None,
    include_events: bool = False,
):
    quant = load_quant_report(settings, symbol, as_of)
    report: QuantFactorReport = quant["report"]
    resolved_asset_type = _asset_type(settings, symbol, asset_type)
    financial_report = None
    financial_degraded = []
    if fundamentals is None and resolved_asset_type == "stock":
        try:
            financial_report = load_a_share_financial_report(
                symbol,
                quant["as_of"],
                current_price=quant["price"],
            )
            fundamentals = financial_report.model_dump(mode="json")
        except Exception as exc:
            fundamentals = {}
            financial_degraded.append(f"financial quality data failed: {exc}")
    elif fundamentals is None:
        fundamentals = {}

    radar = None
    cross_section_factors: dict[str, float] = {}
    radar_degraded: list[str] = []
    if resolved_asset_type == "etf":
        try:
            radar = build_market_radar(settings, quant["as_of"])
            radar_row = next(
                (item for item in radar["instruments"] if item["symbol"] == symbol), None
            )
            if radar_row:
                cross_section_factors = {
                    "cross_section_momentum_20_rank": float(radar_row["momentum_20_percentile"]),
                    "cross_section_momentum_60_rank": float(radar_row["momentum_60_percentile"]),
                    "cross_section_momentum_120_rank": float(radar_row["momentum_120_percentile"]),
                    "cross_section_volatility_20_rank": max(
                        0.0,
                        min(1.0, float(radar_row["volatility_20_pct"] or 0) / 60),
                    ),
                    "cross_section_relative_return_20": float(
                        (radar_row["return_20_pct"] or 0)
                        - sum((item["return_20_pct"] or 0) for item in radar["instruments"])
                        / len(radar["instruments"])
                    )
                    / 100,
                    "cross_section_breadth_20": float(radar["breadth"]["positive_20"]),
                    "cross_section_dispersion_20": float(radar["dispersion_20_pct"]) / 100,
                    "cross_section_leadership_gap_20": float(
                        (radar_row["return_20_pct"] or 0)
                        - max((item["return_20_pct"] or 0) for item in radar["instruments"])
                    )
                    / 100,
                }
            radar_degraded.extend(radar["degraded_sources"])
        except Exception as exc:
            radar_degraded.append(f"ETF cross-section radar failed: {exc}")

    event_collection = None
    event_degraded: list[str] = []
    if news is None and include_events and resolved_asset_type == "stock":
        event_start = quant["as_of"] - timedelta(days=45)
        event_collection = collect_all_events(settings, symbol, event_start, quant["as_of"])
        event_degraded.extend(event_collection.get("degraded_sources", []))
        news = LearningRepository(
            settings.resolve(settings.get("system.database_path"))
        ).events_between(symbol, event_start.isoformat(), quant["as_of"].isoformat())
    news = news or []

    radar_row = (
        next((item for item in radar["instruments"] if item["symbol"] == symbol), None)
        if radar
        else None
    )
    signal_score = (
        max(-1.0, min(1.0, (float(radar_row["strength_score"]) - 50) / 50))
        if radar_row
        else report.composite_score
    )
    signal = StrategySignal(
        strategy="etf_rotation" if resolved_asset_type == "etf" else "factor_momentum",
        symbol=symbol,
        as_of=quant["as_of"],
        score=signal_score,
        confidence=min(0.85, 0.35 + report.regime_confidence * 0.35),
        reasons=[
            f"factor composite={report.composite_score:.3f}",
            f"MTF={report.multi_timeframe.verdict}",
            f"regime={report.regime.value}",
            *(
                [
                    f"cross-section strength={radar_row['strength_score']:.1f}",
                    f"20d rank={radar_row['rank_20']}/{len(radar['instruments'])}",
                ]
                if radar_row
                else []
            ),
        ],
    )
    all_degraded = list(
        dict.fromkeys(
            quant["degraded_sources"] + financial_degraded + radar_degraded + event_degraded
        )
    )
    llm = build_provider(settings.section("llm"))
    run = asyncio.run(
        await_with_provider_close(
            llm,
            MultiAgentDecisionSystem(
                llm,
                build_predictor(
                    settings.resolve(settings.get("system.database_path")), resolved_asset_type
                ),
            ).run(
                ResearchContext(
                    symbol=symbol,
                    as_of=quant["as_of"],
                    price=quant["price"],
                    strategy_signals=[signal],
                    fundamentals=fundamentals or {},
                    news=news,
                    quant_factors=report.model_dump(mode="json"),
                    price_history=quant["price_history"],
                    cross_section_factors=cross_section_factors,
                    asset_type=resolved_asset_type,
                    market_regime=report.regime.value,
                    data_quality=0.8 if all_degraded else 1.0,
                    degraded_sources=all_degraded,
                    hard_vetoes=financial_report.hard_vetoes if financial_report else [],
                )
            ),
        )
    )
    return {
        **quant,
        "financial_report": financial_report,
        "financial_degraded_sources": financial_degraded,
        "asset_type": resolved_asset_type,
        "market_radar": radar,
        "cross_section_factors": cross_section_factors,
        "event_collection": event_collection,
        "event_degraded_sources": event_degraded,
        "news": news,
        "decision_run": run,
    }


def _asset_type(settings: Settings, symbol: str, requested: str | None) -> str:
    if requested in {"stock", "etf"}:
        return requested
    universe = set(settings.get("strategies.etf_rotation.universe", []))
    return "etf" if symbol in universe else "stock"


def build_price_history_evidence(
    frame: pd.DataFrame,
    as_of: date | None = None,
) -> dict:
    """Build auditable price-path evidence without mixing execution and signal prices."""

    if frame.empty:
        return {}
    required = {"date", "open", "high", "low", "close"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"price history missing columns: {', '.join(missing)}")

    history = frame.copy()
    history["date"] = pd.to_datetime(history["date"], errors="coerce")
    history = history.dropna(subset=["date"])
    requested_cutoff = as_of or history["date"].max().date()
    history = history[history["date"].dt.date <= requested_cutoff]
    if history.empty:
        return {}
    if "symbol" in history.columns:
        symbols = [str(item) for item in history["symbol"].dropna().unique()]
        if len(symbols) > 1:
            raise ValueError("price history evidence requires exactly one symbol")
        symbol = symbols[0] if symbols else "unknown"
    else:
        symbol = "unknown"
    history = history.sort_values("date").drop_duplicates("date", keep="last")

    price_columns = [
        "open",
        "high",
        "low",
        "close",
        "adjusted_open",
        "adjusted_high",
        "adjusted_low",
        "adjusted_close",
        "volume",
        "amount",
    ]
    for column in price_columns:
        if column not in history.columns:
            history[column] = None
        history[column] = pd.to_numeric(history[column], errors="coerce")

    raw_close = history["close"].astype(float)
    adjusted_close = history["adjusted_close"].where(history["adjusted_close"].notna(), raw_close)
    adjusted_close = adjusted_close.astype(float)
    latest = history.iloc[-1]
    path = adjusted_close.tail(120)
    normalized_path = (
        [round(float(value / path.iloc[-1] * 100), 6) for value in path]
        if not path.empty and path.iloc[-1] > 0
        else []
    )
    daily_returns = adjusted_close.pct_change(fill_method=None).dropna().tail(120)
    annualized_volatility = (
        float(daily_returns.std(ddof=1) * (252**0.5) * 100) if len(daily_returns) >= 2 else None
    )
    rolling_peak = path.cummax()
    max_drawdown = float((path / rolling_peak - 1).min() * 100) if not path.empty else None
    moving_averages = {
        f"ma_{window}": _window_mean(adjusted_close, window) for window in (5, 20, 60, 120)
    }
    moving_average_relationships = {
        name: _price_level_relationship(adjusted_close.iloc[-1], average)
        for name, average in moving_averages.items()
    }

    recent_bars = []
    for _, row in history.tail(30).iterrows():
        recent_bars.append(
            {
                "date": row["date"].date().isoformat(),
                "raw_open": _finite_number(row["open"]),
                "raw_high": _finite_number(row["high"]),
                "raw_low": _finite_number(row["low"]),
                "raw_close": _finite_number(row["close"]),
                "adjusted_open": _finite_number(row["adjusted_open"]),
                "adjusted_high": _finite_number(row["adjusted_high"]),
                "adjusted_low": _finite_number(row["adjusted_low"]),
                "adjusted_close": _finite_number(row["adjusted_close"]),
                "volume": _finite_number(row["volume"], digits=2),
                "amount": _finite_number(row["amount"], digits=2),
            }
        )

    effective_cutoff = history["date"].max().date()
    return {
        "evidence_type": "market_price_history",
        "symbol": symbol,
        "requested_cutoff_date": requested_cutoff.isoformat(),
        "cutoff_date": effective_cutoff.isoformat(),
        "contains_observations_after_cutoff": False,
        "observations": int(len(history)),
        "date_range": {
            "start": history["date"].min().date().isoformat(),
            "end": effective_cutoff.isoformat(),
        },
        "price_semantics": {
            "raw_ohlc": "unadjusted market prices; use for executable price levels",
            "adjusted_ohlc": (
                "provider-supplied back-adjusted prices (AkShare uses hfq); use for returns, "
                "trend and moving averages, never as an executable quote"
            ),
            "signal_close_fallback": "adjusted_close when present, otherwise raw_close",
            "raw_and_adjusted_fields_are_separate": True,
        },
        "adjustment_availability": {
            "adjusted_close_observations": int(history["adjusted_close"].notna().sum()),
            "raw_fallback_observations": int(history["adjusted_close"].isna().sum()),
        },
        "latest": {
            "date": effective_cutoff.isoformat(),
            "raw_close": _finite_number(latest["close"]),
            "adjusted_close": _finite_number(latest["adjusted_close"]),
            "signal_close": _finite_number(adjusted_close.iloc[-1]),
            "volume": _finite_number(latest["volume"], digits=2),
            "amount": _finite_number(latest["amount"], digits=2),
        },
        "recent_raw_and_adjusted_bars_30": recent_bars,
        "normalized_adjusted_close_path_120": {
            "normalization": "latest_observation=100",
            "observations": int(len(path)),
            "start_date": history.iloc[-len(path)]["date"].date().isoformat(),
            "end_date": effective_cutoff.isoformat(),
            "values": normalized_path,
        },
        "returns_adjusted_pct": {
            f"{window}_trading_days": _window_return(adjusted_close, window)
            for window in (20, 60, 120)
        },
        "risk_adjusted_pct": {
            "annualized_volatility_last_120_returns": _finite_number(annualized_volatility),
            "maximum_drawdown_last_120_prices": _finite_number(max_drawdown),
            "daily_return_observations": int(len(daily_returns)),
        },
        "moving_averages_adjusted": moving_averages,
        "latest_signal_close_vs_moving_averages": moving_average_relationships,
        "raw_market_ranges": {
            f"{window}_trading_days": _raw_range(history, window) for window in (20, 60, 120)
        },
        "average_trading_amount": {
            f"{window}_trading_days": _window_mean(history["amount"], window, digits=2)
            for window in (20, 60, 120)
        },
    }


def _finite_number(value, *, digits: int = 6) -> float | None:
    if value is None or pd.isna(value):
        return None
    converted = float(value)
    if converted == float("inf") or converted == float("-inf"):
        return None
    return round(converted, digits)


def _window_return(series: pd.Series, window: int) -> float | None:
    if len(series) <= window:
        return None
    start = float(series.iloc[-window - 1])
    end = float(series.iloc[-1])
    if start <= 0:
        return None
    return round((end / start - 1) * 100, 6)


def _window_mean(series: pd.Series, window: int, *, digits: int = 6) -> float | None:
    if len(series) < window:
        return None
    values = pd.to_numeric(series.tail(window), errors="coerce").dropna()
    if len(values) < window:
        return None
    return _finite_number(values.mean(), digits=digits)


def _raw_range(history: pd.DataFrame, window: int) -> dict[str, float | None]:
    if len(history) < window:
        return {"high": None, "low": None}
    recent = history.tail(window)
    return {
        "high": _finite_number(recent["high"].max()),
        "low": _finite_number(recent["low"].min()),
    }


def _price_level_relationship(
    latest_signal_close: float,
    reference: float | None,
) -> dict[str, float | str | None]:
    latest = _finite_number(latest_signal_close)
    if latest is None or reference is None or reference <= 0:
        return {
            "latest_signal_close": latest,
            "moving_average": reference,
            "relation": "unknown",
            "distance_pct": None,
        }
    distance = (latest_signal_close / reference - 1) * 100
    relation = "equal" if abs(distance) < 1e-9 else "above" if distance > 0 else "below"
    return {
        "latest_signal_close": latest,
        "moving_average": reference,
        "relation": relation,
        "distance_pct": round(float(distance), 6),
    }
