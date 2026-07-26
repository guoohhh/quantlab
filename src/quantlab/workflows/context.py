from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from quantlab.config import Settings
from quantlab.domain import AssetType, MarketQuote
from quantlab.domain.context import (
    AnalysisContextPack,
    EvidenceBlock,
    EvidenceDomain,
    EvidenceQuality,
    deterministic_compress,
)
from quantlab.persistence.evidence import EvidenceRepository
from quantlab.workflows.capital_flow import build_live_stock_flow, unavailable_flow_block


MARKET_TZ = ZoneInfo("Asia/Shanghai")


def context_repository(settings: Settings) -> EvidenceRepository:
    return EvidenceRepository(settings.resolve(settings.get("system.database_path")))


def assemble_analysis_context_pack(
    *,
    symbol: str,
    asset_type: AssetType | str,
    as_of: date,
    market: dict[str, Any] | None = None,
    technical: dict[str, Any] | None = None,
    market_flow: EvidenceBlock | dict[str, Any] | None = None,
    industry_flow: EvidenceBlock | dict[str, Any] | None = None,
    stock_flow: EvidenceBlock | dict[str, Any] | None = None,
    financial: dict[str, Any] | None = None,
    valuation: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    macro: dict[str, Any] | None = None,
    portfolio: dict[str, Any] | None = None,
    strategy: dict[str, Any] | None = None,
    degraded_sources: list[str] | None = None,
    historical_lessons: dict[str, Any] | None = None,
    cutoff_at: datetime | None = None,
    maximum_llm_payload_bytes: int = 48_000,
) -> AnalysisContextPack:
    resolved_asset = AssetType(asset_type)
    cutoff = cutoff_at or datetime.combine(as_of, time.max, tzinfo=MARKET_TZ)
    degraded_sources = list(dict.fromkeys(degraded_sources or []))
    blocks: list[EvidenceBlock] = []
    critical_gaps: list[str] = []

    if market:
        market_at = _available_time(market, as_of)
        if market_at <= cutoff:
            blocks.append(
                _block(
                    domain=EvidenceDomain.MARKET,
                    title="raw executable market data",
                    source=str(market.get("source") or "unknown"),
                    methodology="raw unadjusted OHLCV for executable levels",
                    as_of=_observed_time(market, as_of),
                    available_at=market_at,
                    payload={
                        "current_raw_price": market.get("price") or market.get("raw_price"),
                        "price_updated_at": market_at.isoformat(),
                        "raw_ohlcv": deterministic_compress(
                            market.get("raw_ohlcv")
                            or market.get("recent_raw_and_adjusted_bars_30")
                            or market.get("bars")
                            or []
                        ),
                        "session_status": market.get("session_status", "unknown"),
                    },
                    degraded=bool(degraded_sources),
                    missing_fields=_missing_fields(
                        market,
                        ("price", "raw_price", "raw_ohlcv", "recent_raw_and_adjusted_bars_30"),
                        alternatives=(("price", "raw_price"), ("raw_ohlcv", "recent_raw_and_adjusted_bars_30", "bars")),
                    ),
                )
            )
        else:
            critical_gaps.append("market data became available after cutoff")
            blocks.append(
                _unavailable(
                    EvidenceDomain.MARKET,
                    "market data",
                    as_of,
                    "market data became available after cutoff",
                )
            )
    else:
        critical_gaps.append("current raw market data unavailable")
        blocks.append(_unavailable(EvidenceDomain.MARKET, "market data", as_of, "market data unavailable"))

    if technical:
        technical_at = _available_time(technical, as_of)
        if technical_at <= cutoff:
            paths = {
                key: value
                for key, value in technical.items()
                if key.startswith("normalized_adjusted_close_path_")
            }
            blocks.append(
                _block(
                    domain=EvidenceDomain.TECHNICAL,
                    title="deterministic technical and path evidence",
                    source=str(technical.get("source") or market.get("source") if market else "unknown"),
                    methodology=(
                        "adjusted prices for returns and paths; raw prices remain separate for execution"
                    ),
                    as_of=_observed_time(technical, as_of),
                    available_at=technical_at,
                    payload={
                        "normalized_paths": paths,
                        "returns": technical.get("returns_adjusted_pct", {}),
                        "risk": technical.get("risk_adjusted_pct", {}),
                        "moving_averages": technical.get("moving_averages_adjusted", {}),
                        "price_vs_moving_averages": technical.get(
                            "latest_signal_close_vs_moving_averages", {}
                        ),
                        "raw_ranges": technical.get("raw_market_ranges", {}),
                        "average_trading_amount": technical.get("average_trading_amount", {}),
                        "quant_factors": deterministic_compress(technical.get("quant_factors", {})),
                    },
                    degraded=bool(technical.get("degraded")),
                    missing_fields=[
                        f"normalized_path_{window}"
                        for window in (20, 60, 120, 250)
                        if f"normalized_adjusted_close_path_{window}" not in paths
                    ],
                )
            )
        else:
            critical_gaps.append("technical evidence became available after cutoff")
            blocks.append(
                _unavailable(
                    EvidenceDomain.TECHNICAL,
                    "technical evidence",
                    as_of,
                    "technical evidence became available after cutoff",
                )
            )
    else:
        critical_gaps.append("technical evidence unavailable")
        blocks.append(_unavailable(EvidenceDomain.TECHNICAL, "technical evidence", as_of, "technical evidence unavailable"))

    flow_blocks = [
        _flow_or_unavailable(market_flow, "market", "cn_market", as_of),
        _flow_or_unavailable(industry_flow, "industry", "unknown", as_of),
        _flow_or_unavailable(stock_flow, "stock", symbol, as_of),
    ]
    for block in flow_blocks:
        if block.available_at <= cutoff:
            blocks.append(block)
        else:
            blocks.append(
                unavailable_flow_block(
                    scope=str(block.payload.get("scope") or "flow"),
                    key=str(block.payload.get("scope_key") or symbol),
                    as_of=as_of,
                    source=block.source,
                    reason="flow data became available after context cutoff",
                )
            )

    if resolved_asset == AssetType.STOCK:
        if financial:
            financial_at = _available_time(
                financial,
                as_of,
                candidates=("available_at", "disclosure_date", "report_date"),
            )
            if financial_at <= cutoff:
                missing_disclosure = not any(
                    financial.get(key) for key in ("available_at", "disclosure_date")
                )
                blocks.append(
                    _block(
                        domain=EvidenceDomain.FINANCIAL,
                        title="financial history and disclosure evidence",
                        source=str(financial.get("source") or "financial_provider"),
                        methodology="reported financial facts bounded by actual disclosure availability",
                        as_of=_observed_time(financial, as_of),
                        available_at=financial_at,
                        payload=deterministic_compress(financial),
                        degraded=missing_disclosure,
                        missing_fields=["actual_disclosure_time"] if missing_disclosure else [],
                    )
                )
            else:
                critical_gaps.append("financial report was disclosed after cutoff")
                blocks.append(_unavailable(EvidenceDomain.FINANCIAL, "financial evidence", as_of, "financial report disclosed after cutoff"))
        else:
            critical_gaps.append("financial evidence unavailable for stock")
            blocks.append(_unavailable(EvidenceDomain.FINANCIAL, "financial evidence", as_of, "financial evidence unavailable"))
    else:
        blocks.append(_unavailable(EvidenceDomain.FINANCIAL, "company financial evidence", as_of, "not applicable to this asset type"))

    if valuation:
        blocks.append(
            _block(
                domain=EvidenceDomain.VALUATION,
                title="valuation range",
                source=str(valuation.get("source") or "financial_provider"),
                methodology="point-in-time valuation fields; no missing multiple is imputed",
                as_of=_observed_time(valuation, as_of),
                available_at=_available_time(valuation, as_of),
                payload=deterministic_compress(valuation),
                degraded=bool(valuation.get("degraded")),
            )
        )
    else:
        blocks.append(_unavailable(EvidenceDomain.VALUATION, "valuation evidence", as_of, "valuation range unavailable"))

    bounded_events = _bounded_events(events or [], cutoff)
    if bounded_events:
        event_available = max(_event_available_at(item, as_of) for item in bounded_events)
        blocks.append(
            _block(
                domain=EvidenceDomain.EVENT,
                title="news, announcements and regulatory events",
                source=";".join(sorted({str(item.get("source") or "unknown") for item in bounded_events})),
                methodology="event facts are filtered by available_at and summarized; raw full text is excluded",
                as_of=max(_event_observed_at(item, as_of) for item in bounded_events),
                available_at=event_available,
                payload={
                    "events": [
                        {
                            "event_id": item.get("id") or item.get("event_id"),
                            "event_type": item.get("event_type"),
                            "title": str(item.get("title") or "")[:300],
                            "source": item.get("source"),
                            "event_date": item.get("event_date"),
                            "available_at": _event_available_at(item, as_of).isoformat(),
                            "sentiment": item.get("sentiment"),
                            "impact_score": item.get("impact_score"),
                        }
                        for item in bounded_events[-20:]
                    ],
                    "raw_text_excluded": True,
                    "event_abnormal_return_analysis": {
                        "status": "unavailable",
                        "reason": (
                            "a licensed point-in-time event panel and frozen matched control "
                            "group are not yet available"
                        ),
                    },
                },
                degraded=any(not item.get("available_at") for item in bounded_events),
                missing_fields=(
                    ["actual_event_available_at"]
                    if any(not item.get("available_at") for item in bounded_events)
                    else []
                ),
            )
        )
    else:
        blocks.append(_unavailable(EvidenceDomain.EVENT, "event evidence", as_of, "no point-in-time events are available"))

    if macro:
        macro_at = _available_time(macro, as_of)
        if macro_at <= cutoff:
            blocks.append(
                _block(
                    domain=EvidenceDomain.MACRO,
                    title="rates, currency, credit and liquidity",
                    source=str(macro.get("source") or "macro_provider"),
                    methodology="point-in-time macro series; unavailable series are left missing",
                    as_of=_observed_time(macro, as_of),
                    available_at=macro_at,
                    payload=deterministic_compress(macro),
                    degraded=bool(macro.get("degraded")),
                    missing_fields=list(macro.get("missing_fields", [])),
                )
            )
        else:
            blocks.append(_unavailable(EvidenceDomain.MACRO, "macro evidence", as_of, "macro data became available after cutoff"))
    else:
        blocks.append(_unavailable(EvidenceDomain.MACRO, "macro evidence", as_of, "macro data source unavailable"))

    if portfolio:
        blocks.append(
            _block(
                domain=EvidenceDomain.PORTFOLIO,
                title="account, holdings and deterministic portfolio risk",
                source="quantlab_user_paper_ledger",
                methodology="transactional account ledger and deterministic risk limits",
                as_of=_as_datetime(as_of),
                available_at=_as_datetime(as_of),
                payload=deterministic_compress(portfolio),
                degraded=False,
            )
        )
    else:
        blocks.append(_unavailable(EvidenceDomain.PORTFOLIO, "portfolio evidence", as_of, "no account was bound to this context"))

    if strategy:
        blocks.append(
            _block(
                domain=EvidenceDomain.STRATEGY,
                title="strategy signals, benchmark and evidence grade",
                source=str(strategy.get("source") or "quantlab_strategy_engine"),
                methodology="deterministic strategy outputs remain separate from LLM judgment",
                as_of=_observed_time(strategy, as_of),
                available_at=_available_time(strategy, as_of),
                payload=deterministic_compress(strategy),
                degraded=bool(strategy.get("degraded")),
            )
        )
    else:
        critical_gaps.append("strategy evidence unavailable")
        blocks.append(_unavailable(EvidenceDomain.STRATEGY, "strategy evidence", as_of, "strategy evidence unavailable"))

    if historical_lessons and historical_lessons.get("lessons"):
        blocks.append(
            _block(
                domain=EvidenceDomain.MEMORY,
                title="bounded historical lessons (auxiliary, not factual guarantees)",
                source="quantlab_authoritative_reflection_memory",
                methodology=(
                    "server-bounded lessons linked to matured authoritative outcomes; same-symbol "
                    "lessons are preferred and cross-symbol weight never exceeds 0.25; these lessons "
                    "cannot change strategy rules, role weights, thresholds or hard risk controls"
                ),
                as_of=_as_datetime(as_of),
                available_at=_as_datetime(as_of),
                payload=deterministic_compress(historical_lessons),
                degraded=False,
            )
        )
    else:
        blocks.append(
            _unavailable(
                EvidenceDomain.MEMORY,
                "bounded historical lessons",
                as_of,
                "no eligible authoritative matured lessons are available",
            )
        )

    critical_gaps.extend(f"degraded source: {item}" for item in degraded_sources)
    summary = _deterministic_summary(blocks, portfolio, strategy)
    return AnalysisContextPack(
        symbol=symbol,
        asset_type=resolved_asset,
        as_of=as_of,
        cutoff_at=cutoff,
        blocks=blocks,
        critical_gaps=critical_gaps,
        deterministic_summary=summary,
        maximum_llm_payload_bytes=maximum_llm_payload_bytes,
    )


def build_analysis_context_pack(
    settings: Settings,
    *,
    symbol: str,
    as_of: date | None = None,
    asset_type: str | None = None,
    market_quote: MarketQuote | None = None,
    account_id: str | None = None,
    include_events: bool = True,
    save: bool = True,
) -> dict[str, Any]:
    from quantlab.fundamentals import load_a_share_financial_report
    from quantlab.learning import LearningRepository
    from quantlab.workflows.events import collect_all_events
    from quantlab.workflows.radar import build_market_radar
    from quantlab.workflows.research import load_quant_report
    from quantlab.workflows.simulator import user_simulator_repository

    end = as_of or date.today()
    if market_quote is not None and market_quote.as_of != end:
        raise ValueError("context market quote must match the requested decision date")
    resolved_asset = _asset_type(settings, symbol, asset_type)
    quant = load_quant_report(settings, symbol, end)
    financial: dict[str, Any] | None = None
    valuation: dict[str, Any] | None = None
    degraded = list(quant.get("degraded_sources", []))
    if resolved_asset == AssetType.STOCK:
        try:
            report = load_a_share_financial_report(
                symbol,
                quant["as_of"],
                current_price=quant["price"],
            )
            financial = report.model_dump(mode="json")
            valuation = _valuation_from_financial(financial)
        except Exception:
            degraded.append("financial_provider_unavailable")
    events: list[dict[str, Any]] = []
    if include_events and resolved_asset == AssetType.STOCK:
        start = quant["as_of"] - timedelta(days=60)
        try:
            collect_all_events(settings, symbol, start, quant["as_of"])
            events = LearningRepository(
                settings.resolve(settings.get("system.database_path"))
            ).events_between(symbol, start.isoformat(), quant["as_of"].isoformat())
        except Exception:
            degraded.append("event_provider_unavailable")
    portfolio = None
    if account_id:
        portfolio = user_simulator_repository(settings).overview(account_id)
    radar = None
    try:
        radar = build_market_radar(settings, quant["as_of"])
    except Exception:
        degraded.append("market_radar_unavailable")
    try:
        stock_flow = build_live_stock_flow(settings, symbol, quant["as_of"])
    except Exception:
        stock_flow = unavailable_flow_block(
            scope="stock",
            key=symbol,
            as_of=quant["as_of"],
            source="unavailable",
            reason="stock flow calculation failed",
        )
    from quantlab.workflows.reflection import controlled_research_memory

    memory = controlled_research_memory(settings, symbol=symbol)
    pack = assemble_analysis_context_pack(
        symbol=symbol,
        asset_type=resolved_asset,
        as_of=end,
        market={
            "price": market_quote.raw_price if market_quote else quant["price"],
            "source": market_quote.source if market_quote else quant["source"],
            "as_of": market_quote.as_of if market_quote else quant["as_of"],
            "available_at": (
                market_quote.available_at if market_quote else quant.get("available_at")
            ),
            "session_status": (
                market_quote.session_status if market_quote else "unknown"
            ),
            "recent_raw_and_adjusted_bars_30": quant["price_history"].get(
                "recent_raw_and_adjusted_bars_30", []
            ),
        },
        technical={
            **quant["price_history"],
            "source": quant["source"],
            "as_of": quant["as_of"],
            "available_at": quant.get("available_at"),
            "quant_factors": quant["report"].model_dump(mode="json"),
        },
        market_flow=market_flow_block_from_radar(radar, quant["as_of"]),
        industry_flow=None,
        stock_flow=stock_flow,
        financial=financial,
        valuation=valuation,
        events=events,
        macro=macro_evidence_from_radar(radar, quant["as_of"]),
        portfolio=portfolio,
        strategy={
            "source": "quantlab_factor_engine",
            "as_of": quant["as_of"],
            "factor_report": quant["report"].model_dump(mode="json"),
            "benchmark": settings.get("strategies.stock_evidence.benchmark_symbol", "sh510300"),
            "evidence_grade": "research_only",
        },
        historical_lessons=memory,
        degraded_sources=degraded,
        cutoff_at=market_quote.available_at if market_quote else None,
        maximum_llm_payload_bytes=int(
            settings.get("llm.context_maximum_bytes", 48_000)
        ),
    )
    payload = pack.model_dump(mode="json")
    if save:
        from quantlab.workflows.notification_rules import evaluate_flow_notification_rules

        repository = context_repository(settings)
        payload = repository.save_context(pack)
        for block in pack.blocks:
            if block.domain == EvidenceDomain.CAPITAL_FLOW:
                repository.save_flow(block)
                evaluate_flow_notification_rules(
                    settings,
                    block,
                    account_id=account_id,
                )
    return payload


def build_trade_context_pack(
    settings: Settings,
    *,
    quote: MarketQuote,
    account: dict[str, Any],
    research: dict[str, Any],
) -> AnalysisContextPack:
    # A trade check is a new point-in-time decision. Reusing "latest by symbol"
    # can silently attach a different report from the explicitly selected run.
    # Build and persist a trade-specific pack whose strategy block freezes the
    # exact research identity (or the explicit unlinked/unavailable state).
    strategy = {
        "source": (
            "linked_saved_research"
            if research.get("link_status") == "linked"
            else "pretrade_without_linked_research"
        ),
        "as_of": quote.as_of,
        "available_at": quote.available_at,
        "decision": research.get("decision"),
        "reviewer_status": research.get("reviewer_status"),
        "research_run_id": research.get("run_id"),
        "research_symbol": research.get("symbol"),
        "research_as_of": research.get("as_of"),
        "research_asset_type": research.get("asset_type"),
        "research_link_status": research.get("link_status", "unlinked"),
        "source_context_id": research.get("source_context_id"),
        "source_context_fingerprint": research.get("source_context_fingerprint"),
        "data_gaps": research.get("data_gaps", []),
    }
    pack = assemble_analysis_context_pack(
        symbol=quote.symbol,
        asset_type=quote.asset_type,
        as_of=quote.as_of,
        market={
            "raw_price": quote.raw_price,
            "source": quote.source,
            "as_of": quote.as_of,
            "available_at": quote.available_at,
            "session_status": quote.session_status,
            "raw_ohlcv": [],
        },
        technical=None,
        stock_flow=None,
        financial=None,
        valuation=None,
        events=None,
        macro=None,
        portfolio=account,
        strategy=strategy,
        degraded_sources=quote.degraded_from,
    )
    saved = context_repository(settings).save_context(pack)
    return AnalysisContextPack.model_validate(saved)


def _block(
    *,
    domain: EvidenceDomain,
    title: str,
    source: str,
    methodology: str,
    as_of: datetime,
    available_at: datetime,
    payload: dict[str, Any],
    degraded: bool,
    missing_fields: list[str] | None = None,
) -> EvidenceBlock:
    missing = list(dict.fromkeys(missing_fields or []))
    return EvidenceBlock(
        domain=domain,
        title=title,
        source=source,
        methodology=methodology,
        as_of=as_of,
        available_at=available_at,
        freshness="stale" if available_at.date() < as_of.date() else "fresh",
        quality=EvidenceQuality.DEGRADED if degraded or missing else EvidenceQuality.AVAILABLE,
        degraded=degraded or bool(missing),
        missing_fields=missing,
        missing_reason="some evidence fields are unavailable" if missing else None,
        payload=payload,
    )


def _unavailable(
    domain: EvidenceDomain,
    title: str,
    as_of: date,
    reason: str,
) -> EvidenceBlock:
    at = _as_datetime(as_of)
    return EvidenceBlock(
        domain=domain,
        title=title,
        source="unavailable",
        methodology="unavailable; no value was fabricated",
        as_of=at,
        available_at=at,
        freshness="unknown",
        quality=EvidenceQuality.UNAVAILABLE,
        degraded=True,
        missing_fields=[domain.value],
        missing_reason=reason,
        payload={"status": "unavailable", "reason": reason},
    )


def _flow_or_unavailable(
    value: EvidenceBlock | dict[str, Any] | None,
    scope: str,
    key: str,
    as_of: date,
) -> EvidenceBlock:
    if isinstance(value, EvidenceBlock):
        return value
    if isinstance(value, dict):
        return EvidenceBlock.model_validate(value)
    return unavailable_flow_block(
        scope=scope,
        key=key,
        as_of=as_of,
        source="unavailable",
        reason=f"{scope} capital flow data unavailable",
    )


def _bounded_events(events: list[dict[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
    eligible = [item for item in events if _event_available_at(item, cutoff.date()) <= cutoff]
    return sorted(eligible, key=lambda item: _event_available_at(item, cutoff.date()))[-40:]


def _event_available_at(value: dict[str, Any], default_date: date) -> datetime:
    return _parse_datetime(
        value.get("available_at") or value.get("event_date") or default_date,
        default_hour=23,
    )


def _event_observed_at(value: dict[str, Any], default_date: date) -> datetime:
    return _parse_datetime(value.get("event_date") or default_date, default_hour=15)


def _available_time(
    value: dict[str, Any],
    default_date: date,
    *,
    candidates: tuple[str, ...] = ("available_at", "price_updated_at", "as_of"),
) -> datetime:
    raw = next((value.get(key) for key in candidates if value.get(key) is not None), default_date)
    return _parse_datetime(raw, default_hour=15)


def _observed_time(value: dict[str, Any], default_date: date) -> datetime:
    return _parse_datetime(value.get("as_of") or value.get("date") or default_date, default_hour=15)


def _parse_datetime(value: Any, *, default_hour: int) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=MARKET_TZ)
    if isinstance(value, date):
        return datetime.combine(value, time(default_hour, 0), tzinfo=MARKET_TZ)
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=MARKET_TZ)
    except ValueError:
        parsed_date = date.fromisoformat(text[:10])
        return datetime.combine(parsed_date, time(default_hour, 0), tzinfo=MARKET_TZ)


def _as_datetime(value: date) -> datetime:
    return datetime.combine(value, time(15, 0), tzinfo=MARKET_TZ)


def _missing_fields(
    value: dict[str, Any],
    fields: tuple[str, ...],
    *,
    alternatives: tuple[tuple[str, ...], ...] = (),
) -> list[str]:
    covered = {item for group in alternatives if any(value.get(key) is not None for key in group) for item in group}
    return [field for field in fields if field not in covered and value.get(field) is None]


def _deterministic_summary(
    blocks: list[EvidenceBlock],
    portfolio: dict[str, Any] | None,
    strategy: dict[str, Any] | None,
) -> dict[str, Any]:
    quality = {block.domain.value: block.quality.value for block in blocks}
    return {
        "evidence_quality": quality,
        "cash": portfolio.get("cash") if portfolio else None,
        "equity": portfolio.get("equity") if portfolio else None,
        "current_weight": _current_weight(portfolio, strategy) if portfolio else None,
        "deterministic_max_single_weight": None,
        "strategy_action": (strategy or {}).get("decision", {}).get("action")
        if isinstance((strategy or {}).get("decision"), dict)
        else None,
        "facts_are_separate_from_llm_judgment": True,
    }


def _current_weight(
    portfolio: dict[str, Any] | None,
    strategy: dict[str, Any] | None,
) -> float | None:
    if not portfolio:
        return None
    symbol = (strategy or {}).get("symbol")
    if not symbol:
        return None
    position = next(
        (item for item in portfolio.get("positions", []) if item.get("symbol") == symbol),
        None,
    )
    return float(position.get("weight", 0.0)) if position else 0.0


def _valuation_from_financial(financial: dict[str, Any]) -> dict[str, Any] | None:
    keys = (
        "pe_ttm",
        "pb",
        "ps_ttm",
        "market_cap",
        "net_cash",
        "free_cash_flow",
        "valuation",
    )
    values = {key: financial.get(key) for key in keys if financial.get(key) is not None}
    return {"source": financial.get("source", "financial_provider"), **values} if values else None


def market_flow_block_from_radar(
    radar: dict[str, Any] | None,
    as_of: date,
) -> EvidenceBlock | None:
    if not radar:
        return None
    at = _parse_datetime(radar.get("generated_at") or as_of, default_hour=15)
    return EvidenceBlock(
        domain=EvidenceDomain.CAPITAL_FLOW,
        title="cross-asset market activity proxy",
        source=str(radar.get("source") or "market_radar"),
        methodology="cross-asset ETF breadth and relative-strength proxy; not full-market fund flow",
        as_of=_parse_datetime(radar.get("as_of") or as_of, default_hour=15),
        available_at=at,
        freshness="fresh",
        quality=EvidenceQuality.DEGRADED,
        degraded=True,
        estimated=True,
        missing_fields=["full_a_share_turnover", "financing_balance", "etf_share_change"],
        missing_reason="market radar is a cross-asset proxy, not a licensed full-market flow feed",
        payload={
            "scope": "market",
            "scope_key": "configured_cross_asset_universe",
            "risk_appetite": radar.get("risk_appetite"),
            "breadth": radar.get("breadth"),
            "leaders": radar.get("leaders"),
            "laggards": radar.get("laggards"),
            "instruments": radar.get("instruments", []),
        },
    )


def macro_evidence_from_radar(
    radar: dict[str, Any] | None,
    as_of: date,
) -> dict[str, Any] | None:
    if not radar:
        return None
    return {
        "source": radar.get("source", "market_radar"),
        "as_of": radar.get("as_of", as_of.isoformat()),
        "available_at": radar.get("generated_at"),
        "cross_asset_relative_strength": radar.get("instruments", []),
        "market_regime": radar.get("market_regime"),
        "risk_appetite": radar.get("risk_appetite"),
        "missing_fields": ["interest_rate", "currency", "credit", "liquidity"],
        "degraded": True,
    }


def _asset_type(settings: Settings, symbol: str, requested: str | None) -> AssetType:
    if requested:
        return AssetType(requested)
    universe = set(settings.get("strategies.etf_rotation.universe", []))
    return AssetType.ETF if symbol in universe else AssetType.STOCK


__all__ = [
    "assemble_analysis_context_pack",
    "build_analysis_context_pack",
    "build_trade_context_pack",
    "context_repository",
    "macro_evidence_from_radar",
    "market_flow_block_from_radar",
]
