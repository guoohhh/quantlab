from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.domain import Bar
from quantlab.domain.context import (
    EvidenceBlock,
    EvidenceDomain,
    EvidenceQuality,
)


MARKET_TZ = ZoneInfo("Asia/Shanghai")
SIGNED_TURNOVER_METHODOLOGY = (
    "estimated signed-turnover proxy: sign(daily raw close return) × transaction amount; "
    "it is not confirmed institutional or main-force capital"
)


def calculate_market_flow(
    records: Iterable[Bar | dict[str, Any]],
    *,
    as_of: date,
    source: str,
    methodology: str = SIGNED_TURNOVER_METHODOLOGY,
    supplemental: dict[str, Any] | None = None,
) -> EvidenceBlock:
    frame = _prepare_frame(records, as_of)
    if frame.empty:
        return unavailable_flow_block(
            scope="market",
            key="cn_market",
            as_of=as_of,
            source=source,
            reason="no point-in-time market records are available",
        )
    _validate_methodology_isolation(frame, source, methodology)
    if "amount" not in frame:
        frame["amount"] = 0.0
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce").fillna(0.0)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["prev_close"] = pd.to_numeric(frame.get("prev_close"), errors="coerce")
    frame = frame.sort_values(["symbol", "date"])
    derived_previous = frame.groupby("symbol")["close"].shift(1)
    frame["previous"] = frame["prev_close"].where(frame["prev_close"].notna(), derived_previous)
    frame["return"] = frame["close"] / frame["previous"] - 1
    frame["signed_turnover"] = np.sign(frame["return"].fillna(0.0)) * frame["amount"]

    daily = frame.groupby("date", as_index=False).agg(
        turnover=("amount", "sum"),
        signed_turnover_proxy=("signed_turnover", "sum"),
    )
    latest_date = daily["date"].max()
    latest_rows = frame[frame["date"] == latest_date]
    returns = latest_rows["return"].fillna(0.0)
    up = int((returns > 1e-12).sum())
    down = int((returns < -1e-12).sum())
    flat = int(len(returns) - up - down)
    latest_turnover = float(daily.iloc[-1]["turnover"])
    avg_5 = _tail_mean(daily["turnover"], 5)
    avg_20 = _tail_mean(daily["turnover"], 20)
    limit_up = _count_flag_or_threshold(latest_rows, "limit_up", lower=0.095)
    limit_down = _count_flag_or_threshold(latest_rows, "limit_down", upper=-0.095)
    breadth = up / len(latest_rows) if len(latest_rows) else None
    supplemental = supplemental or {}
    relative_strength = _asset_class_relative_strength(frame)
    missing = [
        key
        for key in ("financing_balance", "etf_share_change", "northbound_flow")
        if supplemental.get(key) is None
    ]
    payload = {
        "scope": "market",
        "scope_key": "cn_market",
        "coverage": {
            "symbols": int(latest_rows["symbol"].nunique()),
            "latest_date": latest_date.isoformat(),
            "scope_note": supplemental.get(
                "scope_note",
                "coverage is limited to supplied point-in-time instruments",
            ),
        },
        "turnover": {
            "latest": round(latest_turnover, 2),
            "average_5": _round(avg_5, 2),
            "average_20": _round(avg_20, 2),
            "ratio_to_5": _ratio(latest_turnover, avg_5),
            "ratio_to_20": _ratio(latest_turnover, avg_20),
        },
        "breadth": {
            "up": up,
            "down": down,
            "flat": flat,
            "positive_share": _round(breadth, 4),
            "limit_up": limit_up,
            "limit_down": limit_down,
        },
        "signed_turnover_proxy": {
            "today": _round(float(daily.iloc[-1]["signed_turnover_proxy"]), 2),
            "sum_5": _round(float(daily.tail(5)["signed_turnover_proxy"].sum()), 2),
            "sum_20": _round(float(daily.tail(20)["signed_turnover_proxy"].sum()), 2),
            "estimated": True,
        },
        "financing_balance": _optional_metric(supplemental.get("financing_balance")),
        "etf_share_change": _optional_metric(supplemental.get("etf_share_change")),
        "northbound_flow": _optional_metric(supplemental.get("northbound_flow")),
        "asset_relative_strength": relative_strength,
        "recomputable_summary": _market_summary(latest_turnover, avg_5, breadth, relative_strength),
        "claim_boundary": (
            "turnover and signed-turnover are transaction-activity estimates; they must not be "
            "described as confirmed institutional holdings"
        ),
    }
    return EvidenceBlock(
        domain=EvidenceDomain.CAPITAL_FLOW,
        title="market capital and transaction activity",
        source=source,
        methodology=methodology,
        as_of=_as_datetime(latest_date),
        available_at=_available_at(frame, latest_date),
        fetched_at=datetime.now(UTC),
        freshness="fresh" if latest_date == as_of else "stale",
        quality=EvidenceQuality.DEGRADED if missing or latest_date < as_of else EvidenceQuality.AVAILABLE,
        degraded=bool(missing or latest_date < as_of),
        estimated=True,
        missing_fields=missing,
        missing_reason="optional licensed capital fields are unavailable" if missing else None,
        payload=payload,
    )


def calculate_industry_flow(
    records: Iterable[dict[str, Any]],
    *,
    as_of: date,
    source: str,
    methodology: str,
) -> list[EvidenceBlock]:
    frame = _prepare_frame(records, as_of)
    if frame.empty or "industry" not in frame:
        return [
            unavailable_flow_block(
                scope="industry",
                key="all",
                as_of=as_of,
                source=source,
                reason="industry point-in-time history is unavailable",
            )
        ]
    _validate_methodology_isolation(frame, source, methodology)
    output: list[EvidenceBlock] = []
    for industry, group in frame.groupby("industry"):
        group = group.sort_values(["date", "symbol"])
        if group.empty:
            continue
        prepared, estimated = _with_flow_metric(group)
        daily = prepared.groupby("date", as_index=False).agg(
            net_flow=("flow_metric", "sum"),
            turnover=("amount", "sum"),
            up_count=("is_up", "sum"),
            members=("symbol", "nunique"),
            price_index=("close", "mean"),
        )
        latest_date = daily["date"].max()
        latest = prepared[prepared["date"] == latest_date]
        flow_windows = {
            str(window): _round(float(daily.tail(window)["net_flow"].sum()), 2)
            for window in (1, 3, 5, 20)
        }
        price_return_5 = _window_return(daily["price_index"], 5)
        consistency = _flow_price_relation(flow_windows["5"], price_return_5)
        concentration = _concentration(latest, "flow_metric", 3)
        turnover_percentile = _percentile_of_latest(daily["turnover"])
        breadth = (
            float(daily.iloc[-1]["up_count"]) / float(daily.iloc[-1]["members"])
            if float(daily.iloc[-1]["members"]) > 0
            else None
        )
        representatives = (
            latest.assign(abs_flow=latest["flow_metric"].abs())
            .sort_values("abs_flow", ascending=False)["symbol"]
            .head(5)
            .astype(str)
            .tolist()
        )
        payload = {
            "scope": "industry",
            "scope_key": str(industry),
            "flow_trend": flow_windows,
            "turnover": _round(float(daily.iloc[-1]["turnover"]), 2),
            "turnover_percentile": turnover_percentile,
            "up_share": _round(breadth, 4),
            "price_return_5_pct": price_return_5,
            "flow_price_consistency": consistency,
            "leader_concentration_top3": concentration,
            "crowding": _crowding_label(turnover_percentile, concentration),
            "overextended": bool(price_return_5 is not None and abs(price_return_5) >= 12),
            "representative_stocks": representatives,
            "representative_etfs": [],
            "estimated": estimated,
            "claim_boundary": (
                "estimated flow is a transaction proxy and not confirmed institutional ownership"
                if estimated
                else "vendor flow follows the named source methodology only"
            ),
        }
        output.append(
            EvidenceBlock(
                domain=EvidenceDomain.CAPITAL_FLOW,
                title=f"industry flow: {industry}",
                source=source,
                methodology=methodology,
                as_of=_as_datetime(latest_date),
                available_at=_available_at(prepared, latest_date),
                freshness="fresh" if latest_date == as_of else "stale",
                quality=EvidenceQuality.DEGRADED if estimated else EvidenceQuality.AVAILABLE,
                degraded=estimated,
                estimated=estimated,
                missing_fields=["representative_etfs"] if not payload["representative_etfs"] else [],
                missing_reason="industry ETF mapping is unavailable" if not payload["representative_etfs"] else None,
                payload=payload,
            )
        )
    return output or [
        unavailable_flow_block(
            scope="industry",
            key="all",
            as_of=as_of,
            source=source,
            reason="no usable industry groups are available",
        )
    ]


def calculate_stock_flow(
    records: Iterable[Bar | dict[str, Any]],
    *,
    symbol: str,
    as_of: date,
    source: str,
    methodology: str = SIGNED_TURNOVER_METHODOLOGY,
    industry_rank: dict[str, Any] | None = None,
) -> EvidenceBlock:
    frame = _prepare_frame(records, as_of)
    frame = frame[frame["symbol"] == symbol] if not frame.empty else frame
    if frame.empty:
        return unavailable_flow_block(
            scope="stock",
            key=symbol,
            as_of=as_of,
            source=source,
            reason="stock flow history is unavailable",
        )
    _validate_methodology_isolation(frame, source, methodology)
    prepared, estimated = _with_flow_metric(frame.sort_values("date"))
    latest_date = prepared["date"].max()
    flow_windows = {
        str(window): _round(float(prepared.tail(window)["flow_metric"].sum()), 2)
        for window in (1, 3, 5, 20)
    }
    price_return_5 = _window_return(prepared["close"], 5)
    components = {
        key: _optional_metric(prepared.iloc[-1].get(key))
        for key in ("large_order_flow", "medium_order_flow", "small_order_flow")
    }
    missing = [key for key, value in components.items() if value["status"] == "unavailable"]
    payload = {
        "scope": "stock",
        "scope_key": symbol,
        "flow_trend": flow_windows,
        "order_size_structure": components,
        "turnover_amount": _round(float(prepared.iloc[-1]["amount"]), 2),
        "turnover_rate": _round(prepared.iloc[-1].get("turnover_rate"), 4),
        "price_return_5_pct": price_return_5,
        "flow_price_relation": _flow_price_relation(flow_windows["5"], price_return_5),
        "historical_flow_percentile": _percentile_of_latest(prepared["flow_metric"]),
        "industry_relative_rank": industry_rank or {"status": "unavailable"},
        "estimated": estimated,
        "claim_boundary": (
            "signed-turnover proxy is not confirmed main-force or institutional flow"
            if estimated
            else "vendor order-size fields retain the source methodology and are estimates"
        ),
    }
    return EvidenceBlock(
        domain=EvidenceDomain.CAPITAL_FLOW,
        title=f"stock flow: {symbol}",
        source=source,
        methodology=methodology,
        as_of=_as_datetime(latest_date),
        available_at=_available_at(prepared, latest_date),
        freshness="fresh" if latest_date == as_of else "stale",
        quality=EvidenceQuality.DEGRADED if estimated or missing else EvidenceQuality.AVAILABLE,
        degraded=estimated or bool(missing),
        estimated=estimated,
        missing_fields=missing + ([] if industry_rank else ["industry_relative_rank"]),
        missing_reason="order-size or industry comparison data is unavailable" if missing or not industry_rank else None,
        payload=payload,
    )


def build_live_stock_flow(
    settings: Settings,
    symbol: str,
    as_of: date | None = None,
) -> EvidenceBlock:
    end = as_of or date.today()
    fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
    provider = CachedProvider(
        fallback,
        settings.resolve(settings.get("system.data_dir")) / "cache",
    )
    bars = provider.bars([symbol], end - timedelta(days=180), end)
    block = calculate_stock_flow(
        bars,
        symbol=symbol,
        as_of=end,
        source=provider.name,
    )
    if fallback.last_degraded_from:
        block.degraded = True
        block.quality = EvidenceQuality.DEGRADED
        block.missing_fields = list(
            dict.fromkeys(block.missing_fields + list(fallback.last_degraded_from))
        )
    return block


def industry_flow_blocks_from_radar(
    radar: dict[str, Any],
    *,
    as_of: date,
) -> list[EvidenceBlock]:
    """Convert a real-time sector snapshot into explicitly degraded flow evidence."""
    sectors = list(radar.get("sectors") or [])
    if not sectors:
        return [
            unavailable_flow_block(
                scope="industry",
                key="all",
                as_of=as_of,
                source=str(radar.get("source") or "unavailable"),
                reason="industry snapshot source returned no usable records",
            )
        ]
    available_at = _parse_available_at(radar.get("generated_at"), as_of)
    output: list[EvidenceBlock] = []
    for sector in sectors:
        name = str(sector.get("name") or "unknown")
        up = sector.get("up_count")
        down = sector.get("down_count")
        denominator = float(up or 0) + float(down or 0)
        output.append(
            EvidenceBlock(
                domain=EvidenceDomain.CAPITAL_FLOW,
                title=f"industry activity snapshot: {name}",
                source=str(radar.get("source") or "market_radar"),
                methodology=(
                    "real-time industry price/turnover participation snapshot; capital-flow "
                    "windows are unavailable and no net inflow is inferred"
                ),
                as_of=_as_datetime(as_of),
                available_at=available_at,
                fetched_at=datetime.now(UTC),
                freshness="fresh",
                quality=EvidenceQuality.DEGRADED,
                degraded=True,
                estimated=False,
                missing_fields=[
                    "flow_trend_1d",
                    "flow_trend_3d",
                    "flow_trend_5d",
                    "flow_trend_20d",
                    "leader_concentration",
                    "representative_etfs",
                ],
                missing_reason=(
                    "the free sector endpoint is a current activity snapshot, not a "
                    "point-in-time net-flow history"
                ),
                payload={
                    "scope": "industry",
                    "scope_key": name,
                    "status": "degraded",
                    "flow_trend": {
                        key: {"status": "unavailable", "value": None}
                        for key in ("1", "3", "5", "20")
                    },
                    "price_change_pct": sector.get("change_pct"),
                    "turnover_rate": sector.get("turnover_pct"),
                    "up_share": round(float(up or 0) / denominator, 4)
                    if denominator
                    else None,
                    "leader": sector.get("leader") or None,
                    "heat_score": sector.get("heat_score"),
                    "signed_turnover_proxy": {
                        "status": "unavailable",
                        "value": None,
                    },
                    "claim_boundary": (
                        "This snapshot describes industry price and trading activity only; "
                        "it is not actual net capital inflow."
                    ),
                },
            )
        )
    return output


def unavailable_flow_block(
    *,
    scope: str,
    key: str,
    as_of: date,
    source: str,
    reason: str,
    last_success_at: str | None = None,
) -> EvidenceBlock:
    at = _as_datetime(as_of)
    return EvidenceBlock(
        domain=EvidenceDomain.CAPITAL_FLOW,
        title=f"{scope} flow: {key}",
        source=source,
        methodology="unavailable; no substitute data was fabricated",
        as_of=at,
        available_at=at,
        freshness="unknown",
        quality=EvidenceQuality.UNAVAILABLE,
        degraded=True,
        estimated=False,
        missing_fields=["capital_flow"],
        missing_reason=reason,
        payload={
            "scope": scope,
            "scope_key": key,
            "status": "unavailable",
            "reason": reason,
            "last_success_at": last_success_at,
        },
    )


def _prepare_frame(records: Iterable[Bar | dict[str, Any]], as_of: date) -> pd.DataFrame:
    rows = [item.model_dump(mode="json") if isinstance(item, Bar) else dict(item) for item in records]
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    required = {"symbol", "date", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"capital flow records missing columns: {sorted(missing)}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    frame = frame.dropna(subset=["date", "symbol", "close"])
    frame = frame[frame["date"] <= as_of]
    cutoff = _cutoff(as_of)
    if "available_at" in frame:
        available = pd.to_datetime(frame["available_at"], utc=True, errors="coerce")
        frame = frame[available.isna() | (available <= cutoff.astimezone(UTC))]
        frame["available_at"] = available
    return frame.sort_values(["date", "symbol"]).reset_index(drop=True)


def _parse_available_at(value: Any, as_of: date) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=MARKET_TZ)
    if value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=MARKET_TZ)
    return _as_datetime(as_of)


def _validate_methodology_isolation(
    frame: pd.DataFrame,
    source: str,
    methodology: str,
) -> None:
    sources = set(frame.get("source", pd.Series(dtype=str)).dropna().astype(str))
    if len(sources) > 1 or (sources and source not in sources):
        raise ValueError("capital flow sources must be normalized separately before aggregation")
    methods = set(frame.get("methodology", pd.Series(dtype=str)).dropna().astype(str))
    if len(methods) > 1 or (methods and methodology not in methods):
        raise ValueError("capital flow methodologies cannot be mixed in one snapshot")


def _with_flow_metric(frame: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    output = frame.copy()
    output["close"] = pd.to_numeric(output["close"], errors="coerce")
    if "amount" not in output:
        output["amount"] = 0.0
    output["amount"] = pd.to_numeric(output["amount"], errors="coerce").fillna(0.0)
    output["previous"] = output.groupby("symbol")["close"].shift(1)
    output["return"] = output["close"] / output["previous"] - 1
    output["is_up"] = (output["return"] > 0).astype(int)
    if "net_flow" in output and pd.to_numeric(output["net_flow"], errors="coerce").notna().any():
        output["flow_metric"] = pd.to_numeric(output["net_flow"], errors="coerce").fillna(0.0)
        estimated = bool(output.get("estimated", False).any()) if "estimated" in output else True
    else:
        output["flow_metric"] = np.sign(output["return"].fillna(0.0)) * output["amount"]
        estimated = True
    return output, estimated


def _asset_class_relative_strength(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if "asset_class" not in frame:
        return []
    output = []
    for asset_class, group in frame.groupby("asset_class"):
        index = group.sort_values("date").groupby("date")["close"].mean()
        output.append(
            {
                "asset_class": str(asset_class),
                "return_20_pct": _window_return(index, 20),
            }
        )
    return sorted(
        output,
        key=lambda item: item["return_20_pct"] if item["return_20_pct"] is not None else -1e9,
        reverse=True,
    )


def _market_summary(
    turnover: float,
    average_5: float | None,
    breadth: float | None,
    relative_strength: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "turnover_state": (
            "expanded"
            if average_5 and turnover > average_5 * 1.1
            else "contracted"
            if average_5 and turnover < average_5 * 0.9
            else "normal"
        ),
        "breadth_state": (
            "broad" if breadth is not None and breadth >= 0.6 else "narrow" if breadth is not None and breadth <= 0.4 else "balanced"
        ),
        "strongest_asset_class": relative_strength[0]["asset_class"] if relative_strength else None,
        "formula": "turnover ratios and positive-share breadth are directly recomputable",
    }


def _optional_metric(value: Any) -> dict[str, Any]:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return {"status": "unavailable", "value": None}
    if isinstance(value, dict):
        return {"status": value.get("status", "available"), **value}
    return {"status": "available", "value": _round(value, 6)}


def _flow_price_relation(flow: float | None, price_return_pct: float | None) -> str:
    if flow is None or price_return_pct is None:
        return "insufficient_data"
    if flow > 0 and price_return_pct > 0:
        return "flow_price_confirmation"
    if flow > 0 and price_return_pct <= 0:
        return "inflow_not_confirmed"
    if flow < 0 and price_return_pct > 0:
        return "price_flow_divergence"
    if flow < 0 and price_return_pct < 0:
        return "outflow_price_confirmation"
    return "neutral"


def _concentration(frame: pd.DataFrame, field: str, top_k: int) -> float | None:
    values = pd.to_numeric(frame[field], errors="coerce").abs().dropna()
    total = float(values.sum())
    return round(float(values.nlargest(top_k).sum()) / total, 4) if total > 0 else None


def _crowding_label(turnover_percentile: float | None, concentration: float | None) -> str:
    if turnover_percentile is None or concentration is None:
        return "insufficient_data"
    if turnover_percentile >= 0.9 and concentration >= 0.7:
        return "high"
    if turnover_percentile >= 0.7 or concentration >= 0.5:
        return "elevated"
    return "normal"


def _percentile_of_latest(series: pd.Series) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    return round(float((values <= values.iloc[-1]).mean()), 4)


def _window_return(series: pd.Series, window: int) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if len(values) <= window or float(values.iloc[-window - 1]) <= 0:
        return None
    return round((float(values.iloc[-1]) / float(values.iloc[-window - 1]) - 1) * 100, 4)


def _tail_mean(series: pd.Series, window: int) -> float | None:
    values = pd.to_numeric(series.tail(window), errors="coerce").dropna()
    return float(values.mean()) if len(values) >= min(window, 2) else None


def _ratio(value: float, denominator: float | None) -> float | None:
    return round(value / denominator, 4) if denominator and denominator > 0 else None


def _count_flag_or_threshold(
    frame: pd.DataFrame,
    field: str,
    *,
    lower: float | None = None,
    upper: float | None = None,
) -> int:
    if field in frame and frame[field].notna().any():
        return int(frame[field].fillna(False).astype(bool).sum())
    if lower is not None:
        return int((frame["return"] >= lower).sum())
    return int((frame["return"] <= float(upper)).sum())


def _available_at(frame: pd.DataFrame, latest_date: date) -> datetime:
    if "available_at" in frame:
        values = pd.to_datetime(
            frame.loc[frame["date"] == latest_date, "available_at"],
            utc=True,
            errors="coerce",
        ).dropna()
        if not values.empty:
            return values.max().to_pydatetime()
    return _as_datetime(latest_date)


def _as_datetime(value: date) -> datetime:
    return datetime.combine(value, time(15, 0), tzinfo=MARKET_TZ).astimezone(UTC)


def _cutoff(value: date) -> datetime:
    return datetime.combine(value, time.max, tzinfo=MARKET_TZ)


def _round(value: Any, digits: int) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


__all__ = [
    "SIGNED_TURNOVER_METHODOLOGY",
    "build_live_stock_flow",
    "industry_flow_blocks_from_radar",
    "calculate_industry_flow",
    "calculate_market_flow",
    "calculate_stock_flow",
    "unavailable_flow_block",
]
