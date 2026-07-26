from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from math import ceil, log
from typing import Any, Callable

import numpy as np
import pandas as pd

from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.data.westock_data import WestockDataProvider
from quantlab.data.westock_tool import WestockToolProvider
from quantlab.domain.models import Bar
from quantlab.domain import ResearchProvenance
from quantlab.factors import MomentumFactorEngine
from quantlab.learning.cross_section import cross_sectional_features
from quantlab.learning.features import factor_report_features
from quantlab.persistence import DecisionRepository, TerminalRepository
from quantlab.reporting import (
    build_research_audit_package,
    research_persistence_context,
)
from quantlab.security import safe_error_detail
from quantlab.workflows.research import analyze_symbol


STOCK_DISCOVERY_STYLES = {
    "momentum_quality": {
        "label": "趋势质量",
        "expression": (
            "intersect([MA_5 > MA_10, MA_10 > MA_20, Chg20D > 0, "
            "TurnoverValue > 50000000, ROETTM > 8])"
        ),
        "orderby": "Chg20D",
        "ascending": False,
    },
    "value_quality": {
        "label": "价值质量",
        "expression": "intersect([PE_TTM > 0, PE_TTM < 20, ROETTM > 15])",
        "orderby": "ROETTM",
        "ascending": False,
    },
    "growth_quality": {
        "label": "成长质量",
        "expression": (
            "intersect([ROETTM > 12, RevenueGrowRate > 10, NetProfitGrowRate > 10, "
            "TurnoverValue > 50000000])"
        ),
        "orderby": "NetProfitGrowRate",
        "ascending": False,
    },
    "pullback_repair": {
        "label": "回撤修复",
        "expression": (
            "intersect([Chg20D < -10, Chg5D > -3, Chg5D < 8, TurnoverValue > 50000000])"
        ),
        "orderby": "Chg5D",
        "ascending": False,
    },
    "high_dividend": {
        "label": "高股息质量",
        "expression": (
            "intersect([DividendRatioTTM > 3, PE_TTM > 0, PE_TTM < 18, PB > 0, PB < 2.5])"
        ),
        "orderby": "DividendRatioTTM",
        "ascending": False,
    },
}

SCREEN_WEIGHTS = {
    "factor_composite": 0.30,
    "multi_timeframe": 0.15,
    "momentum_quality": 0.15,
    "financial_snapshot": 0.20,
    "liquidity": 0.05,
    "pullback_setup": 0.05,
    "risk_adjustment": 0.10,
}


def normalize_stock_symbol(value: str) -> str:
    text = str(value or "").strip().lower().replace(" ", "")
    if not text:
        raise ValueError("stock symbol cannot be empty")
    match = re.fullmatch(r"(sh|sz)(\d{6})", text)
    if match:
        return match.group(1) + match.group(2)
    match = re.fullmatch(r"(\d{6})\.(sh|sz)", text)
    if match:
        return match.group(2) + match.group(1)
    match = re.fullmatch(r"(\d{6})", text)
    if match:
        code = match.group(1)
        if code.startswith(("5", "6", "9")):
            return "sh" + code
        if code.startswith(("0", "1", "2", "3")):
            return "sz" + code
    raise ValueError(f"unsupported A-share symbol: {value}")


def parse_stock_symbols(values: list[str] | str, maximum: int = 20) -> list[str]:
    raw_values = (
        re.split(r"[,，;；\s]+", values.strip()) if isinstance(values, str) else list(values)
    )
    output = []
    for value in raw_values:
        if not str(value).strip():
            continue
        symbol = normalize_stock_symbol(str(value))
        if symbol not in output:
            output.append(symbol)
    if not output:
        raise ValueError("at least one stock symbol is required")
    if len(output) > maximum:
        raise ValueError(f"at most {maximum} stock symbols are allowed")
    return output


def search_stocks(
    settings: Settings,
    keyword: str,
    limit: int = 20,
    *,
    provider: WestockDataProvider | None = None,
) -> dict[str, Any]:
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("stock search keyword cannot be empty")
    if not 1 <= limit <= 50:
        raise ValueError("stock search limit must be between 1 and 50")
    source = provider or WestockDataProvider(settings.root.parent)
    payload = source.search_stocks(keyword, limit)
    results = []
    rejected = []
    for row in _records(payload):
        raw_code = row.get("code") or row.get("symbol") or row.get("证券代码")
        try:
            symbol = normalize_stock_symbol(str(raw_code))
        except ValueError:
            rejected.append(str(raw_code))
            continue
        results.append(
            {
                "symbol": symbol,
                "display_code": _display_symbol(symbol),
                "name": _text(row.get("name") or row.get("证券简称"), symbol),
                "industry": _text(
                    row.get("industry") or row.get("industryName") or row.get("所属行业"),
                    "",
                ),
                "market": "SH" if symbol.startswith("sh") else "SZ",
            }
        )
        if len(results) >= limit:
            break
    return {
        "keyword": keyword,
        "results": results,
        "rejected_codes": rejected,
        "source": source.name,
    }


def screen_selected_stocks(
    settings: Settings,
    symbols: list[str] | str,
    as_of: date | None = None,
    *,
    top_n: int = 10,
    max_correlation: float = 0.85,
    save: bool = True,
    bars: list[Bar] | list[dict[str, Any]] | None = None,
    metadata: dict[str, dict[str, Any]] | None = None,
    run_type: str = "user_selected",
) -> dict[str, Any]:
    normalized = parse_stock_symbols(symbols)
    if not 1 <= top_n <= 20:
        raise ValueError("top_n must be between 1 and 20")
    if not np.isfinite(max_correlation) or not 0 <= max_correlation <= 1:
        raise ValueError("max_correlation must be between 0 and 1")
    requested_as_of = as_of or date.today()
    degraded_sources: list[str] = []
    if bars is None:
        fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
        provider = CachedProvider(
            fallback,
            settings.resolve(settings.get("system.data_dir")) / "cache",
        )
        bars = provider.bars(
            normalized,
            requested_as_of - timedelta(days=550),
            requested_as_of,
        )
        degraded_sources.extend(fallback.last_degraded_from)
        source = provider.name
    else:
        source = "provided_bars"
    frame = _bar_frame(bars)
    if frame.empty:
        raise ValueError("stock screening returned no market history")
    effective_as_of = min(requested_as_of, frame["date"].max().date())
    candidates = _screen_frame(
        normalized,
        frame,
        effective_as_of,
        metadata or {},
    )
    ranked = sorted(
        candidates,
        key=lambda item: (-float(item.get("screen_score", 0)), item["symbol"]),
    )[: min(top_n, len(candidates))]
    for rank, item in enumerate(ranked, start=1):
        item["screen_rank"] = rank
    correlations = _correlation_matrix(frame, [item["symbol"] for item in ranked])
    diversified = _diversified_shortlist(ranked, correlations, max_correlation)
    output = {
        "as_of": effective_as_of.isoformat(),
        "requested_as_of": requested_as_of.isoformat(),
        "run_type": run_type,
        "source": source,
        "requested_symbols": normalized,
        "universe_size": len(normalized),
        "ranking_formula": SCREEN_WEIGHTS,
        "candidates": ranked,
        "diversified_shortlist": diversified,
        "correlation_matrix": correlations,
        "degraded_sources": list(dict.fromkeys(degraded_sources)),
        "recommendation_boundary": (
            "screening results are research priorities, not buy orders; full multi-agent review and "
            "portfolio constraints remain separate"
        ),
        "manual_execution_only": True,
    }
    if save:
        output["discovery_id"] = TerminalRepository(
            settings.resolve(settings.get("system.database_path"))
        ).save_stock_discovery(run_type, effective_as_of, output)
    return output


def recommend_stocks(
    settings: Settings,
    as_of: date | None = None,
    *,
    styles: list[str] | None = None,
    candidate_limit: int = 30,
    top_n: int = 10,
    max_correlation: float = 0.85,
    save: bool = True,
    screener: WestockToolProvider | None = None,
    bars: list[Bar] | list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    requested_as_of = as_of or date.today()
    if requested_as_of != date.today() and bars is None:
        raise ValueError("system stock discovery uses a current market snapshot only")
    selected_styles = styles or list(STOCK_DISCOVERY_STYLES)
    unknown = [style for style in selected_styles if style not in STOCK_DISCOVERY_STYLES]
    if unknown:
        raise ValueError(f"unknown stock discovery styles: {', '.join(unknown)}")
    if not selected_styles:
        raise ValueError("at least one stock discovery style is required")
    if not 5 <= candidate_limit <= 100:
        raise ValueError("candidate_limit must be between 5 and 100")
    if not 1 <= top_n <= min(20, candidate_limit):
        raise ValueError("top_n must be between 1 and min(20, candidate_limit)")

    provider = screener or WestockToolProvider(settings.root.parent)
    per_style = min(50, max(5, ceil(candidate_limit / len(selected_styles)) * 2))
    rows_by_style: dict[str, list[dict[str, Any]]] = {}
    degraded_sources = []

    def load(style: str):
        config = STOCK_DISCOVERY_STYLES[style]
        return provider.filter(
            config["expression"],
            limit=per_style,
            orderby=config["orderby"],
            ascending=config["ascending"],
        )

    with ThreadPoolExecutor(max_workers=min(5, len(selected_styles))) as executor:
        futures = {executor.submit(load, style): style for style in selected_styles}
        for future in as_completed(futures):
            style = futures[future]
            try:
                rows_by_style[style] = future.result()
            except Exception as exc:
                rows_by_style[style] = []
                degraded_sources.append(f"{style} stock discovery failed: {safe_error_detail(exc)}")

    merged: dict[str, dict[str, Any]] = {}
    maximum_style_rows = max(
        (len(rows_by_style.get(style, [])) for style in selected_styles), default=0
    )
    for row_index in range(maximum_style_rows):
        for style in selected_styles:
            rows = rows_by_style.get(style, [])
            if row_index >= len(rows):
                continue
            row = rows[row_index]
            raw_code = row.get("code") or row.get("symbol")
            try:
                symbol = normalize_stock_symbol(str(raw_code))
            except ValueError:
                continue
            snapshot = _safe_snapshot(row, symbol)
            item = merged.setdefault(symbol, snapshot)
            for key, value in snapshot.items():
                if item.get(key) in (None, "", []):
                    item[key] = value
            item.setdefault("style_sources", []).append(style)
            if len(merged) >= candidate_limit:
                break
        if len(merged) >= candidate_limit:
            break

    if not merged:
        output = {
            "as_of": requested_as_of.isoformat(),
            "requested_as_of": requested_as_of.isoformat(),
            "run_type": "system_recommendation",
            "source": provider.name,
            "styles": selected_styles,
            "style_labels": {
                style: STOCK_DISCOVERY_STYLES[style]["label"] for style in selected_styles
            },
            "requested_symbols": [],
            "universe_size": 0,
            "ranking_formula": SCREEN_WEIGHTS,
            "candidates": [],
            "diversified_shortlist": [],
            "correlation_matrix": {},
            "degraded_sources": degraded_sources or ["no stocks matched the selected styles"],
            "recommendation_boundary": "no candidate is manufactured when the screen is empty",
            "manual_execution_only": True,
        }
        if save:
            output["discovery_id"] = TerminalRepository(
                settings.resolve(settings.get("system.database_path"))
            ).save_stock_discovery("system_recommendation", requested_as_of, output)
        return output

    output = screen_selected_stocks(
        settings,
        list(merged),
        requested_as_of,
        top_n=top_n,
        max_correlation=max_correlation,
        save=False,
        bars=bars,
        metadata=merged,
        run_type="system_recommendation",
    )
    output.update(
        {
            "source": f"{provider.name}+{output['source']}",
            "styles": selected_styles,
            "style_labels": {
                style: STOCK_DISCOVERY_STYLES[style]["label"] for style in selected_styles
            },
            "degraded_sources": list(dict.fromkeys(degraded_sources + output["degraded_sources"])),
        }
    )
    if save:
        output["discovery_id"] = TerminalRepository(
            settings.resolve(settings.get("system.database_path"))
        ).save_stock_discovery("system_recommendation", date.fromisoformat(output["as_of"]), output)
    return output


def run_stock_research_batch(
    settings: Settings,
    symbols: list[str] | str,
    as_of: date | None = None,
    *,
    include_events: bool = True,
    save: bool = True,
    analyzer: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized = parse_stock_symbols(symbols, maximum=5)
    requested_as_of = as_of or date.today()
    analyze = analyzer or analyze_symbol
    repository = DecisionRepository(settings.resolve(settings.get("system.database_path")))
    analyses = []
    failures = []
    compact = []
    for symbol in normalized:
        try:
            research = analyze(
                settings,
                symbol,
                requested_as_of,
                asset_type="stock",
                include_events=include_events,
            )
            package = build_research_audit_package(research)
            if save:
                repository.save(
                    research["decision_run"],
                    research_persistence_context(research),
                    provenance=ResearchProvenance(
                        origin="user_interactive_research",
                        requested_as_of=requested_as_of,
                        evidence_stage="research_only",
                    ),
                )
            analyses.append(package)
            compact.append(
                {
                    "symbol": symbol,
                    "run_id": package["run_id"],
                    "as_of": package["as_of"],
                    "action": package["decision"]["action"],
                    "confidence": package["decision"]["confidence"],
                    "requires_human_review": package["decision"]["requires_human_review"],
                    "reviewer_approved": package["agent_reports"]
                    .get("reviewer", {})
                    .get("approved", False),
                }
            )
        except Exception as exc:
            failure = {"symbol": symbol, "error": safe_error_detail(exc)}
            failures.append(failure)
            compact.append(
                {
                    "symbol": symbol,
                    "action": "review_required",
                    "requires_human_review": True,
                    "status": "error",
                    "error": failure["error"],
                }
            )
    output = {
        "requested_as_of": requested_as_of.isoformat(),
        "symbols": normalized,
        "estimated_llm_role_calls": len(normalized) * (18 if include_events else 17),
        "summaries": compact,
        "analyses": analyses,
        "failures": failures,
        "manual_execution_only": True,
    }
    if save:
        output["discovery_id"] = TerminalRepository(
            settings.resolve(settings.get("system.database_path"))
        ).save_stock_discovery(
            "deep_research",
            date.fromisoformat(analyses[0]["as_of"]) if analyses else requested_as_of,
            {**output, "analyses": []},
        )
    return output


def _screen_frame(
    symbols: list[str],
    frame: pd.DataFrame,
    as_of: date,
    metadata: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    output = []
    engine = MomentumFactorEngine()
    for symbol in symbols:
        group = frame[
            (frame["symbol"] == symbol) & (frame["date"] <= pd.Timestamp(as_of))
        ].sort_values("date")
        external = metadata.get(symbol, {})
        name = _text(external.get("name"), symbol)
        if len(group) < 120:
            output.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "status": "insufficient_history",
                    "observations": len(group),
                    "screen_score": 0.0,
                    "recommendation_tier": "avoid",
                    "eligible": False,
                    "style_sources": external.get("style_sources", []),
                    "risks": ["fewer than 120 price observations"],
                }
            )
            continue
        signal_group = group.copy()
        signal_group["close"] = signal_group["signal_close"]
        report = engine.analyze(symbol, signal_group, group.iloc[-1]["date"].date())
        learning_features = factor_report_features(report)
        learning_features.update(cross_sectional_features(frame, as_of, symbol))
        factor_map = {item.name: item.score for item in report.factors}
        momentum_quality = float(
            np.mean(
                [
                    factor_map.get("momentum_20", 0.0),
                    factor_map.get("momentum_60", 0.0),
                    factor_map.get("momentum_acceleration", 0.0),
                    factor_map.get("path_quality_60", 0.0),
                ]
            )
        )
        financial_score, financial_coverage = _financial_snapshot_score(external)
        amount = group["amount"].tail(20)
        if amount.fillna(0).sum() <= 0:
            amount = group["volume"].tail(20) * group["close"].tail(20)
        average_turnover = float(amount.mean()) if len(amount) else 0.0
        liquidity_score = (
            float(np.tanh(log(max(average_turnover, 1.0) / 50_000_000)))
            if average_turnover > 0
            else -0.5
        )
        mtf_score = report.multi_timeframe.consensus / 3
        pullback_score = 2 * report.pullback_reversal.strength - 1
        return_20_pct = _return_pct(group["signal_close"], 20)
        return_60_pct = _return_pct(group["signal_close"], 60)
        return_120_pct = _return_pct(group["signal_close"], 120)
        volatility_20_pct = _volatility_pct(group["signal_close"], 20)
        volatility_penalty = float(np.tanh(max(0.0, (volatility_20_pct or 0.0) - 35.0) / 40.0))
        extension_penalty = float(np.tanh(max(0.0, (return_20_pct or 0.0) - 30.0) / 25.0))
        risk_adjustment = -float(np.clip(0.6 * volatility_penalty + 0.4 * extension_penalty, 0, 1))
        components = {
            "factor_composite": float(report.composite_score),
            "multi_timeframe": float(mtf_score),
            "momentum_quality": momentum_quality,
            "financial_snapshot": financial_score,
            "liquidity": liquidity_score,
            "pullback_setup": pullback_score,
            "risk_adjustment": risk_adjustment,
        }
        raw_score = sum(SCREEN_WEIGHTS[key] * value for key, value in components.items())
        is_st = "ST" in name.upper() or bool(external.get("is_st"))
        eligibility_penalty = 0.5 if is_st else 0.0
        screen_score = float(np.clip(50 + 50 * (raw_score - eligibility_penalty), 0, 100))
        tier = "avoid"
        if screen_score >= 65:
            tier = "research_first_high_risk" if risk_adjustment <= -0.55 else "research_first"
        elif screen_score >= 50:
            tier = "watch"
        risks = []
        if is_st:
            risks.append("ST or special-treatment name detected")
        if report.multi_timeframe.consensus < 0:
            risks.append(f"multi-timeframe trend={report.multi_timeframe.verdict}")
        if report.composite_score < 0:
            risks.append("factor composite is negative")
        if volatility_20_pct is not None and volatility_20_pct > 60:
            risks.append(f"20-day annualized volatility is high at {volatility_20_pct:.1f}%")
        if return_20_pct is not None and return_20_pct > 40:
            risks.append(f"20-day return is overextended at {return_20_pct:.1f}%")
        pe = _number(external.get("PE_TTM"))
        if pe is not None and pe > 60:
            risks.append(f"PE_TTM is elevated at {pe:.1f}")
        reasons = [
            f"factor composite={report.composite_score:.3f}",
            f"MTF consensus={report.multi_timeframe.consensus}",
            f"momentum quality={momentum_quality:.3f}",
            f"20d average turnover={average_turnover:,.0f}",
        ]
        if financial_coverage:
            reasons.append(
                f"financial snapshot={financial_score:.3f} ({financial_coverage} fields)"
            )
        output.append(
            {
                "symbol": symbol,
                "display_code": _display_symbol(symbol),
                "name": name,
                "industry": _text(external.get("industry"), ""),
                "status": "ok",
                "as_of": group.iloc[-1]["date"].date().isoformat(),
                "price": float(group.iloc[-1]["close"]),
                "observations": len(group),
                "return_20_pct": return_20_pct,
                "return_60_pct": return_60_pct,
                "return_120_pct": return_120_pct,
                "volatility_20_pct": volatility_20_pct,
                "average_turnover_20": average_turnover,
                "factor_composite": report.composite_score,
                "multi_timeframe": report.multi_timeframe.model_dump(mode="json"),
                "pullback_reversal": report.pullback_reversal.model_dump(mode="json"),
                "screen_score": round(screen_score, 4),
                "recommendation_tier": tier,
                "eligible": not is_st,
                "score_trace": components,
                "learning_features": learning_features,
                "financial_snapshot_coverage": financial_coverage,
                "snapshot": _safe_snapshot(external, symbol),
                "style_sources": list(dict.fromkeys(external.get("style_sources", []))),
                "reasons": reasons,
                "risks": risks,
                "full_research_required": True,
            }
        )
    return output


def _bar_frame(bars: list[Bar] | list[dict[str, Any]] | None) -> pd.DataFrame:
    records = [item.model_dump() if isinstance(item, Bar) else item for item in bars or []]
    frame = pd.DataFrame(records)
    if frame.empty or not {"symbol", "date", "close"}.issubset(frame.columns):
        return pd.DataFrame()
    frame["symbol"] = frame["symbol"].map(lambda item: normalize_stock_symbol(str(item)))
    frame["date"] = pd.to_datetime(frame["date"])
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["signal_close"] = frame["close"]
    if "adjusted_close" in frame.columns:
        frame["signal_close"] = pd.to_numeric(frame["adjusted_close"], errors="coerce").fillna(
            frame["close"]
        )
    for column in ("volume", "amount"):
        if column not in frame.columns:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    return (
        frame.dropna(subset=["symbol", "date", "close", "signal_close"])
        .sort_values(["symbol", "date"])
        .drop_duplicates(["symbol", "date"], keep="last")
    )


def _financial_snapshot_score(snapshot: dict[str, Any]) -> tuple[float, int]:
    readings = []
    roe = _number(snapshot.get("ROETTM"))
    if roe is not None:
        readings.append(float(np.clip((roe - 8) / 22, -1, 1)))
    pe = _number(snapshot.get("PE_TTM"))
    if pe is not None:
        readings.append(float(np.tanh((25 - pe) / 20)) if pe > 0 else -0.75)
    debt = _number(snapshot.get("DebtRatio"))
    if debt is not None:
        readings.append(float(np.tanh((60 - debt) / 35)))
    for key in ("RevenueGrowRate", "NetProfitGrowRate"):
        growth = _number(snapshot.get(key))
        if growth is not None:
            readings.append(float(np.tanh(growth / 30)))
    return (float(np.mean(readings)) if readings else 0.0, len(readings))


def _correlation_matrix(
    frame: pd.DataFrame, symbols: list[str]
) -> dict[str, dict[str, float | None]]:
    if not symbols or frame.empty:
        return {symbol: {other: None for other in symbols} for symbol in symbols}
    pivot = frame[frame["symbol"].isin(symbols)].pivot_table(
        index="date",
        columns="symbol",
        values="signal_close",
        aggfunc="last",
    )
    returns = pivot.pct_change(fill_method=None).dropna().tail(252)
    correlation = returns.corr()
    return {
        symbol: {
            other: (
                round(float(correlation.loc[symbol, other]), 6)
                if symbol in correlation.index
                and other in correlation.columns
                and pd.notna(correlation.loc[symbol, other])
                else None
            )
            for other in symbols
        }
        for symbol in symbols
    }


def _diversified_shortlist(
    candidates: list[dict[str, Any]],
    correlations: dict[str, dict[str, float | None]],
    max_correlation: float,
) -> list[dict[str, Any]]:
    selected = []
    maximum = min(5, len(candidates))
    for candidate in candidates:
        if not candidate.get("eligible") or candidate.get("recommendation_tier") == "avoid":
            candidate["diversification_status"] = "not_eligible"
            continue
        correlated = next(
            (
                item["symbol"]
                for item in selected
                if correlations.get(candidate["symbol"], {}).get(item["symbol"]) is not None
                and correlations[candidate["symbol"]][item["symbol"]] > max_correlation
            ),
            None,
        )
        if correlated:
            candidate["diversification_status"] = f"excluded_high_correlation:{correlated}"
            continue
        if len(selected) >= maximum:
            candidate["diversification_status"] = "shortlist_full"
            continue
        candidate["diversification_status"] = "selected"
        selected.append(candidate)
    return selected


def _safe_snapshot(row: dict[str, Any], symbol: str) -> dict[str, Any]:
    fields = (
        "name",
        "industry",
        "ClosePrice",
        "ChangePCT",
        "Chg5D",
        "Chg20D",
        "Chg60D",
        "TurnoverValue",
        "TotalMV",
        "PE_TTM",
        "PB",
        "ROETTM",
        "DebtRatio",
        "RevenueGrowRate",
        "NetProfitGrowRate",
        "DividendRatioTTM",
        "style_sources",
    )
    output = {key: row.get(key) for key in fields if row.get(key) is not None}
    output["name"] = _text(row.get("name"), symbol)
    output["industry"] = _text(row.get("industry") or row.get("IndustryName"), "")
    return output


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "stocks", "results"):
            if isinstance(payload.get(key), list):
                return [item for item in payload[key] if isinstance(item, dict)]
        return [payload]
    return []


def _display_symbol(symbol: str) -> str:
    return f"{symbol[2:]}.{symbol[:2].upper()}"


def _number(value: Any) -> float | None:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if np.isfinite(output) else None


def _text(value: Any, default: str) -> str:
    text = str(value or "").strip()
    return text if text and "�" not in text else default


def _return_pct(close: pd.Series, days: int) -> float | None:
    if len(close) <= days:
        return None
    return round(float((close.iloc[-1] / close.iloc[-days - 1] - 1) * 100), 4)


def _volatility_pct(close: pd.Series, days: int) -> float | None:
    returns = close.pct_change(fill_method=None).dropna().tail(days)
    if len(returns) < max(5, days // 2):
        return None
    return round(float(returns.std(ddof=0) * np.sqrt(252) * 100), 4)
