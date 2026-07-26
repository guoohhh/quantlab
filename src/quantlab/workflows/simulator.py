from __future__ import annotations

import math
import uuid
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from quantlab.config import Settings
from quantlab.domain import (
    AssetType,
    DataQuality,
    MarketQuote,
    OrderRequest,
    PreTradeCheckResult,
    Side,
)
from quantlab.execution import (
    CostModel,
    PortfolioExecutionState,
    TradeConstraints,
    TradeRuleService,
    USER_PAPER_SIMULATION_MODES,
    validate_user_paper_simulation_mode,
)
from quantlab.market import InMemoryQuoteProvider, QuoteService, TradingCalendarService
from quantlab.persistence import (
    DecisionRepository,
    NotificationRepository,
    TerminalRepository,
    UserPaperTradingRepository,
)
from quantlab.workflows.context import build_trade_context_pack
from quantlab.workflows.research_identity import validate_research_record


def user_simulator_repository(settings: Settings) -> UserPaperTradingRepository:
    return UserPaperTradingRepository(
        settings.resolve(settings.get("system.database_path"))
    )


def _market_datetime(settings: Settings, value: datetime | None = None) -> datetime:
    timezone = ZoneInfo(str(settings.get("system.timezone", "Asia/Shanghai")))
    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone)
    return resolved.astimezone(timezone)


def _market_date(settings: Settings, value: datetime | None = None) -> date:
    return _market_datetime(settings, value).date()


def create_user_paper_account(
    settings: Settings,
    *,
    name: str,
    initial_capital: float = 100_000.0,
    benchmark_symbol: str = "sh000300",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    return user_simulator_repository(settings).create_account(
        name=name,
        initial_capital=initial_capital,
        benchmark_symbol=benchmark_symbol,
        idempotency_key=idempotency_key,
        test_only=bool(settings.get("system.test_mode", False)),
    )


def load_latest_trade_quote(
    settings: Settings,
    symbol: str,
    *,
    asset_type: str | None = None,
    as_of: date | None = None,
    quote_service: QuoteService | None = None,
) -> MarketQuote:
    resolved_asset_type = _asset_type(settings, symbol, asset_type)
    resolved_as_of = as_of or _market_date(settings)
    return (quote_service or QuoteService.from_settings(settings)).get(
        symbol,
        asset_type=resolved_asset_type,
        as_of=resolved_as_of,
        require_authoritative=not bool(settings.get("system.test_mode", False)),
    )


def run_pretrade_check(
    settings: Settings,
    *,
    account_id: str,
    symbol: str,
    side: str,
    quantity: int | None = None,
    amount: float | None = None,
    quote_service: QuoteService | None = None,
    quote: MarketQuote | None = None,
    asset_type: str | None = None,
    research_run_id: str | None = None,
    requested_at: datetime | None = None,
    user_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repository = user_simulator_repository(settings)
    account = repository.account(account_id)
    if account is None:
        raise ValueError("user paper account not found")
    if account["status"] != "active":
        raise ValueError("user paper account is not active")
    if quote is not None:
        if not bool(settings.get("system.test_mode", False)):
            raise ValueError("direct quote injection is available only in explicit test mode")
        quote_service = QuoteService(InMemoryQuoteProvider([quote]))
    resolved_quote = load_latest_trade_quote(
        settings,
        symbol,
        asset_type=asset_type,
        as_of=_market_date(settings, requested_at),
        quote_service=quote_service,
    )
    if resolved_quote.symbol != symbol:
        raise ValueError("quote symbol does not match requested symbol")
    if not resolved_quote.authoritative and not bool(account.get("test_only", False)):
        raise ValueError("non-authoritative quotes are isolated to test-only accounts")
    trade_lot = resolved_quote.trade_lot
    if quantity is None:
        if amount is None or amount <= 0:
            raise ValueError("quantity or positive amount is required")
        raw_quantity = int(float(amount) / resolved_quote.raw_price)
        quantity = (
            raw_quantity // trade_lot * trade_lot
            if side == Side.BUY.value
            else raw_quantity
        )
    if quantity <= 0:
        raise ValueError("resolved order quantity must be positive")
    overview = repository.overview(account_id)
    position = next(
        (item for item in overview["positions"] if item["symbol"] == symbol),
        None,
    )
    industry_market_value = sum(
        float(item["market_value"])
        for item in overview["positions"]
        if resolved_quote.industry
        and item.get("industry") == resolved_quote.industry
    )
    state = PortfolioExecutionState(
        cash=float(overview.get("available_cash", overview["cash"])),
        equity=max(float(overview["equity"]), 0.01),
        market_value=float(overview["market_value"]),
        symbol_market_value=float(position["market_value"]) if position else 0.0,
        industry_market_value=industry_market_value,
        position_quantity=int(position["quantity"]) if position else 0,
        sellable_quantity=int(position["sellable_quantity"]) if position else 0,
    )
    cost_model = _cost_model(settings, resolved_quote.asset_type)
    order = OrderRequest(
        symbol=symbol,
        side=Side(side),
        quantity=quantity,
        signal_date=resolved_quote.as_of,
        reason="user_pretrade_check",
    )
    fill = cost_model.fill(order, resolved_quote.raw_price, resolved_quote.as_of)
    transaction_fees = fill.commission + fill.stamp_duty + fill.transfer_fee
    constraints = _constraints(settings)
    rules = TradeRuleService().validate(
        order,
        resolved_quote,
        state,
        request_date=_market_date(settings, requested_at),
        estimated_gross_value=fill.gross_value,
        estimated_transaction_fees=transaction_fees,
        constraints=constraints,
        calendar_service=TradingCalendarService.from_settings(settings),
    )
    maximum_quantity = _maximum_quantity(
        side=side,
        quote=resolved_quote,
        state=state,
        constraints=constraints,
        transaction_fees=transaction_fees,
    )
    research = _safe_research_context(
        settings,
        symbol,
        resolved_quote.as_of,
        research_run_id,
        resolved_quote.asset_type.value,
    )
    research.setdefault(
        "link_status",
        "linked" if research.get("run_id") else "unlinked",
    )
    research.setdefault("symbol", symbol if research.get("run_id") else None)
    research.setdefault("as_of", resolved_quote.as_of if research.get("run_id") else None)
    research.setdefault("asset_type", resolved_quote.asset_type.value if research.get("run_id") else None)
    research.setdefault("committee_decision", None)
    research_decision = research["decision"]
    reviewer_status = research["reviewer_status"]
    context_pack = build_trade_context_pack(
        settings,
        quote=resolved_quote,
        account=overview,
        research=research,
    )
    committee = research.get("committee_decision") or {}
    suggested_quantity = min(quantity, maximum_quantity)
    suggested_action = (
        "buy"
        if side == "buy" and not position
        else "add"
        if side == "buy"
        else "sell"
        if position and quantity >= int(position["quantity"])
        else "reduce"
    )
    soft_review = False
    if research_decision:
        action = str(research_decision.get("action") or "watch")
        if side == "buy" and action not in {"buy", "add"}:
            suggested_quantity = 0
            suggested_action = action
            soft_review = True
        elif side == "sell" and action in {"buy", "add", "hold"}:
            soft_review = True
    elif side == "buy":
        soft_review = True

    llm_weight_range = None
    if committee:
        llm_action = str(committee.get("action") or "review_required")
        llm_min_weight = max(0.0, float(committee.get("suggested_weight_min") or 0.0))
        llm_max_weight = min(
            constraints.maximum_single_weight,
            max(0.0, float(committee.get("suggested_weight_max") or 0.0)),
        )
        llm_weight_range = (llm_min_weight, llm_max_weight)
        if side == "buy":
            llm_value_budget = max(
                0.0,
                llm_max_weight * state.equity - state.symbol_market_value,
            )
            llm_quantity = int(llm_value_budget / resolved_quote.raw_price)
            llm_quantity = llm_quantity // trade_lot * trade_lot
            suggested_quantity = min(suggested_quantity, llm_quantity)
            if llm_action not in {"buy", "add"}:
                suggested_quantity = 0
                suggested_action = llm_action
                soft_review = True
        elif llm_action in {"buy", "add", "hold", "observe"}:
            soft_review = True
    else:
        llm_action = None

    check_id = str(uuid.uuid4())
    reference_time = resolved_quote.available_at or datetime.combine(
        resolved_quote.as_of,
        datetime.min.time(),
        tzinfo=UTC,
    )
    symbol_value_after = (
        max(0.0, state.symbol_market_value - fill.gross_value)
        if side == "sell"
        else state.symbol_market_value + fill.gross_value
    )
    result = PreTradeCheckResult(
        check_id=check_id,
        account_id=account_id,
        account_version=int(account["version"]),
        symbol=symbol,
        side=side,
        requested_quantity=quantity,
        suggested_action=suggested_action,
        suggested_quantity=max(0, suggested_quantity),
        suggested_quantity_range=(0, max(0, maximum_quantity)),
        reference_price=resolved_quote.raw_price,
        reference_time=reference_time,
        estimated_gross_value=fill.gross_value,
        estimated_transaction_fees=transaction_fees,
        estimated_slippage=fill.slippage,
        estimated_total_cash_effect=(
            -(fill.gross_value + transaction_fees)
            if side == "buy"
            else fill.gross_value - transaction_fees
        ),
        post_trade_cash=rules.post_trade_cash,
        post_trade_single_weight=rules.post_trade_single_weight,
        post_trade_industry_weight=rules.post_trade_industry_weight,
        post_trade_total_exposure=rules.post_trade_total_exposure,
        loss_if_symbol_down_10pct=symbol_value_after * 0.10,
        loss_if_symbol_down_15pct=symbol_value_after * 0.15,
        supporting_evidence=research["supporting_evidence"],
        opposing_evidence=research["opposing_evidence"],
        invalidation_conditions=research["invalidation_conditions"],
        data_gaps=research["data_gaps"],
        reviewer_status=reviewer_status,
        hard_risk_passed=rules.allowed,
        hard_failures=rules.hard_failures,
        warnings=list(dict.fromkeys(rules.warnings + resolved_quote.degraded_from)),
        allowed_to_submit=rules.allowed,
        requires_user_review=(
            rules.review_required
            or soft_review
            or context_pack.review_required
            or reviewer_status not in {"approved", "not_required"}
        ),
        research_run_id=research["run_id"],
        research_link_status=research["link_status"],
        research_symbol=research.get("symbol"),
        research_as_of=research.get("as_of"),
        context_id=context_pack.context_id,
        context_version=context_pack.schema_version,
        context_fingerprint=context_pack.fingerprint,
        context_quality_score=context_pack.quality_score,
        llm_suggested_action=llm_action,
        llm_suggested_weight_range=llm_weight_range,
        quote=resolved_quote,
    )
    payload = result.model_dump(mode="json")
    repository.save_pretrade_check(
        payload,
        user_request={
            "account_id": account_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "amount": amount,
            "research_run_id": research_run_id,
            "research_link_status": result.research_link_status,
            "requested_at": _market_datetime(settings, requested_at).isoformat(),
            "user_context": user_context or {},
        },
        system_suggestion={
            "action": result.suggested_action,
            "quantity": result.suggested_quantity,
            "quantity_range": result.suggested_quantity_range,
            "requires_user_review": result.requires_user_review,
        },
    )
    return payload


def submit_user_paper_order(
    settings: Settings,
    *,
    check_id: str,
    quantity: int,
    idempotency_key: str,
    quote_service: QuoteService | None = None,
    requested_at: datetime | None = None,
    expires_at: datetime | None = None,
    user_confirmation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repository = user_simulator_repository(settings)
    decision = repository.pretrade_check(check_id)
    if decision is None:
        raise ValueError("pre-trade check not found")
    check = decision["check_payload"]
    confirmation = _validated_user_confirmation(
        user_confirmation,
        check_id=check_id,
        account_id=str(decision["account_id"]),
        symbol=str(check["symbol"]),
        side=str(check["side"]),
        quantity=quantity,
    )
    existing = repository.order_by_idempotency(
        str(decision["account_id"]),
        idempotency_key,
    )
    if existing is not None:
        if (
            existing["check_id"] != check_id
            or int(existing["requested_quantity"]) != quantity
            or existing["symbol"] != check["symbol"]
            or existing["side"] != check["side"]
            or existing["user_confirmation"] != confirmation
        ):
            raise ValueError("idempotency key is already bound to a different confirmed order")
        from quantlab.persistence.wide_research import WideResearchRepository

        WideResearchRepository(repository.path).record_user_adoption(
            order=existing,
            check=check,
        )
        return existing
    request_time = _market_datetime(settings, requested_at)
    checked_quote = MarketQuote.model_validate(check["quote"])
    quote = checked_quote
    if not bool(settings.get("system.test_mode", False)) or quote_service is not None:
        quote = load_latest_trade_quote(
            settings,
            checked_quote.symbol,
            asset_type=checked_quote.asset_type.value,
            as_of=request_time.date(),
            quote_service=quote_service,
        )
        if (
            not checked_quote.quote_fingerprint
            or not quote.quote_fingerprint
            or quote.quote_fingerprint != checked_quote.quote_fingerprint
        ):
            raise ValueError(
                "market quote changed after the pre-trade check; run a new pre-trade check "
                "before confirming the order"
            )
    validate_user_paper_simulation_mode(
        quote,
        confirmation,
        allow_test_quote=bool(settings.get("system.test_mode", False)),
    )
    calendar = TradingCalendarService.from_settings(settings)
    eligible = (
        calendar.next_open_day(quote.as_of)
        if quote.session_status == "closed"
        else calendar.on_or_next_open_day(quote.as_of)
    )
    resolved_expiry = expires_at or calendar.due_at(
        eligible,
        int(settings.get("runtime.paper_order_expiry_open_sessions", 5)),
        formal=False,
    )
    order = repository.create_order(
        check_id=check_id,
        quantity=quantity,
        idempotency_key=idempotency_key,
        requested_at=request_time,
        eligible_trade_date=eligible,
        expires_at=resolved_expiry,
        user_confirmation=confirmation,
    )
    from quantlab.persistence.wide_research import WideResearchRepository

    WideResearchRepository(repository.path).record_user_adoption(order=order, check=check)
    NotificationRepository(repository.path).process_outbox()
    return order


def _validated_user_confirmation(
    value: dict[str, Any] | None,
    *,
    check_id: str,
    account_id: str,
    symbol: str,
    side: str,
    quantity: int,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("confirmed") is not True:
        raise ValueError("explicit user confirmation with confirmed=true is required")
    expected: dict[str, Any] = {
        "check_id": check_id,
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ValueError(f"user confirmation {key} does not match the pre-trade check")
    source = str(value.get("source") or "").strip()
    if not source:
        raise ValueError("user confirmation source is required")
    simulation_mode = str(value.get("simulation_mode") or "").strip()
    if simulation_mode not in USER_PAPER_SIMULATION_MODES:
        raise ValueError(
            "user confirmation simulation_mode must be intraday_simulation or "
            "next_open_simulation"
        )
    return {
        "confirmed": True,
        **expected,
        "source": source,
        "simulation_mode": simulation_mode,
        "close_reference_acknowledged": value.get("close_reference_acknowledged") is True,
    }


def settle_user_paper_order(
    settings: Settings,
    *,
    order_id: str,
    quote_service: QuoteService | None = None,
    quote: MarketQuote | None = None,
    fill_quantity: int | None = None,
    fill_key: str,
) -> dict[str, Any]:
    repository = user_simulator_repository(settings)
    order = repository.order(order_id)
    if order is None:
        raise ValueError("user paper order not found")
    if quote is not None:
        if not bool(settings.get("system.test_mode", False)):
            raise ValueError("direct quote injection is available only in explicit test mode")
        quote_service = QuoteService(InMemoryQuoteProvider([quote]))
    resolved_quote = load_latest_trade_quote(
        settings,
        order["symbol"],
        asset_type=order["asset_type"],
        as_of=quote.as_of if quote is not None else None,
        quote_service=quote_service,
    )
    account = repository.account(order["account_id"])
    if not resolved_quote.authoritative and not bool((account or {}).get("test_only", False)):
        raise ValueError("non-authoritative quotes are isolated to test-only accounts")
    calendar = TradingCalendarService.from_settings(settings)
    output = repository.settle_order(
        order_id=order_id,
        quote=resolved_quote,
        cost_model=_cost_model(settings, resolved_quote.asset_type),
        constraints=_constraints(settings),
        fill_quantity=fill_quantity,
        fill_key=fill_key,
        t_plus_one_release_date=(
            calendar.next_open_day(resolved_quote.as_of)
            if resolved_quote.t_plus_one
            else None
        ),
    )
    NotificationRepository(repository.path).process_outbox()
    return output


def cancel_user_paper_order(
    settings: Settings,
    order_id: str,
    reason: str = "cancelled_by_user",
) -> dict[str, Any]:
    repository = user_simulator_repository(settings)
    output = repository.cancel_order(order_id, reason)
    NotificationRepository(repository.path).process_outbox()
    return output


def mark_user_paper_account(
    settings: Settings,
    *,
    account_id: str,
    snapshot_date: date | None = None,
    quote_service: QuoteService | None = None,
    marks: list[MarketQuote] | None = None,
    benchmark_quote: MarketQuote | None = None,
) -> dict[str, Any]:
    repository = user_simulator_repository(settings)
    resolved_date = snapshot_date or date.today()
    account = repository.account(account_id)
    if account is None:
        raise ValueError("user paper account not found")
    if marks is not None or benchmark_quote is not None:
        if not bool(settings.get("system.test_mode", False)):
            raise ValueError("direct quote injection is available only in explicit test mode")
        quote_service = QuoteService(
            InMemoryQuoteProvider([*(marks or []), *([benchmark_quote] if benchmark_quote else [])])
        )
    marks = []
    for position in repository.positions(account_id):
        try:
            marks.append(
                load_latest_trade_quote(
                    settings,
                    position["symbol"],
                    asset_type=position["asset_type"],
                    as_of=resolved_date,
                    quote_service=quote_service,
                )
            )
        except Exception:
            marks.append(
                _stored_stale_mark(
                    position,
                    snapshot_date=resolved_date,
                    test_only=bool(account.get("test_only", False)),
                )
            )
    benchmark_quote = None
    if any(not quote.authoritative for quote in marks) and not bool(
        (account or {}).get("test_only", False)
    ):
        raise ValueError("non-authoritative quotes are isolated to test-only accounts")
    if account:
        try:
            benchmark_quote = load_latest_trade_quote(
                settings,
                account["benchmark_symbol"],
                asset_type=AssetType.INDEX.value,
                as_of=resolved_date,
                quote_service=quote_service,
            )
        except Exception:
            benchmark_quote = None
    output = repository.mark_to_market(
        account_id=account_id,
        snapshot_date=resolved_date,
        marks=marks,
        benchmark_quote=benchmark_quote,
        constraints=_constraints(settings),
    )
    _trigger_user_alerts(settings, account_id, marks, resolved_date)
    NotificationRepository(repository.path).process_outbox()
    return output


def _stored_stale_mark(
    position: dict[str, Any],
    *,
    snapshot_date: date,
    test_only: bool,
) -> MarketQuote:
    raw_time = position.get("latest_price_at")
    available_at = None
    observed_date = snapshot_date
    if raw_time:
        try:
            available_at = datetime.fromisoformat(str(raw_time))
            if available_at.tzinfo is None:
                available_at = available_at.replace(tzinfo=UTC)
            observed_date = available_at.date()
        except ValueError:
            try:
                observed_date = date.fromisoformat(str(raw_time)[:10])
            except ValueError:
                observed_date = snapshot_date
    return MarketQuote(
        symbol=position["symbol"],
        name=str(position.get("name") or ""),
        asset_type=position["asset_type"],
        raw_price=float(position.get("latest_price") or position["average_cost"]),
        as_of=min(observed_date, snapshot_date),
        available_at=available_at,
        source=str(position.get("mark_source") or "stored_last_mark"),
        provider="stored_last_mark",
        source_version="stale-position-v1",
        data_quality=DataQuality.STALE,
        degraded_from=["current_server_quote_unavailable_using_last_mark"],
        industry=position.get("industry"),
        session_status="closed",
        authoritative=not test_only,
        evidence_stage="test" if test_only else "production",
        actionable=False,
        actionability_reasons=["stored_last_mark_is_not_current_execution_data"],
    )


def _trigger_user_alerts(
    settings: Settings,
    account_id: str,
    marks: list[MarketQuote],
    snapshot_date: date,
) -> None:
    terminal = TerminalRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    notifications = NotificationRepository(terminal.path)
    quotes = {quote.symbol: quote for quote in marks}
    positions = {
        position["symbol"]: position
        for position in user_simulator_repository(settings).overview(account_id)["positions"]
    }
    for alert in terminal.list_alerts(account_id):
        if not alert["active"]:
            continue
        quote = quotes.get(alert["symbol"])
        if quote is None:
            continue
        condition = str(alert["condition_type"])
        threshold = float(alert["threshold"])
        if condition == "price_above":
            value = quote.raw_price
            triggered = value >= threshold
        elif condition == "price_below":
            value = quote.raw_price
            triggered = value <= threshold
        elif condition == "position_weight_above":
            value = float(positions.get(alert["symbol"], {}).get("weight", 0.0))
            triggered = value >= threshold
        else:
            continue
        if not triggered or not terminal.mark_alert_triggered(
            int(alert["id"]),
            value=value,
            triggered_at=snapshot_date.isoformat(),
        ):
            continue
        display_value = f"{value:.1%}" if condition == "position_weight_above" else f"{value:.2f}"
        notifications.emit(
            event_type="price_alert_triggered",
            aggregate_type="user_alert",
            aggregate_id=str(alert["id"]),
            payload={
                "account_id": account_id,
                "symbol": alert["symbol"],
                "content": f"{condition} 已触发，当前值 {display_value}",
                "data_as_of": quote.as_of.isoformat(),
                "action_type": "view_position",
                "action_payload": {
                    "account_id": account_id,
                    "symbol": alert["symbol"],
                },
            },
            dedup_key=f"price_alert_triggered:{alert['id']}",
        )


def _maximum_quantity(
    *,
    side: str,
    quote: MarketQuote,
    state: PortfolioExecutionState,
    constraints: TradeConstraints,
    transaction_fees: float,
) -> int:
    if side == Side.SELL.value:
        return state.sellable_quantity
    price = quote.raw_price
    lot = quote.trade_lot
    cash_budget = max(0.0, state.cash - transaction_fees)
    single_budget = max(
        0.0,
        constraints.maximum_single_weight * state.equity - state.symbol_market_value,
    )
    total_budget = max(
        0.0,
        constraints.maximum_total_exposure * state.equity - state.market_value,
    )
    industry_budget = max(
        0.0,
        constraints.maximum_industry_weight * state.equity - state.industry_market_value,
    )
    quantity = math.floor(
        min(cash_budget, single_budget, total_budget, industry_budget) / price
    )
    return max(0, quantity // lot * lot)


def _research_context(
    settings: Settings,
    symbol: str,
    as_of: date,
    run_id: str | None,
    asset_type: str | None = None,
) -> dict[str, Any]:
    if not run_id:
        return {
            "run_id": None,
            "symbol": None,
            "as_of": None,
            "asset_type": None,
            "link_status": "unlinked",
            "decision": None,
            "reviewer_status": "unavailable",
            "supporting_evidence": [],
            "opposing_evidence": [],
            "invalidation_conditions": [],
            "data_gaps": [
                "research is unlinked; deterministic checks completed without a research report"
            ],
            "committee_decision": None,
            "source_context_id": None,
            "source_context_fingerprint": None,
        }
    try:
        repository = DecisionRepository(
            settings.resolve(settings.get("system.database_path"))
        )
        record = repository.get(run_id)
    except Exception as exc:
        raise RuntimeError(
            "research service unavailable; explicit run_id could not be validated"
        ) from exc
    identity = validate_research_record(
        record,
        run_id=run_id,
        symbol=symbol,
        market_as_of=as_of,
        asset_type=asset_type,
    )
    assert record is not None
    payload = record.get("payload", {})
    decision = (
        payload.get("decision")
        or payload.get("decision_card")
        or payload.get("final_decision")
        or {}
    )
    reports = payload.get("reports") or payload.get("agent_reports") or {}
    reviewer = reports.get("reviewer") if isinstance(reports, dict) else None
    committee = (
        reports.get("context_committee")
        if isinstance(reports, dict)
        else None
    ) or payload.get("research_context", {}).get("context_committee")
    reviewer_status = "approved"
    if isinstance(reviewer, dict):
        reviewer_status = "approved" if reviewer.get("approved") else "rejected"
    elif decision.get("requires_human_review"):
        reviewer_status = "review_required"
    forecasts = payload.get("forecasts") or []
    invalidation: list[str] = []
    for forecast in forecasts if isinstance(forecasts, list) else []:
        if isinstance(forecast, dict):
            invalidation.extend(forecast.get("invalidation_conditions", []))
    invalidation.extend(decision.get("invalidation_conditions", []))
    return {
        "run_id": record["run_id"],
        "symbol": identity["symbol"],
        "as_of": identity["as_of"],
        "asset_type": identity["asset_type"],
        "link_status": "linked",
        "decision": decision,
        "reviewer_status": reviewer_status,
        "supporting_evidence": list(decision.get("reasons", [])),
        "opposing_evidence": list(decision.get("risks", [])),
        "invalidation_conditions": list(dict.fromkeys(invalidation)),
        "data_gaps": list(decision.get("degraded_sources", [])),
        "committee_decision": committee,
        "source_context_id": identity["context_id"],
        "source_context_fingerprint": identity["context_fingerprint"],
    }


def _safe_research_context(
    settings: Settings,
    symbol: str,
    as_of: date,
    run_id: str | None,
    asset_type: str | None = None,
) -> dict[str, Any]:
    if run_id:
        return _research_context(settings, symbol, as_of, run_id, asset_type)
    try:
        return _research_context(settings, symbol, as_of, None, asset_type)
    except Exception:
        return {
            "run_id": None,
            "symbol": None,
            "as_of": None,
            "asset_type": None,
            "link_status": "unavailable",
            "decision": None,
            "reviewer_status": "unavailable",
            "supporting_evidence": [],
            "opposing_evidence": [],
            "invalidation_conditions": [],
            "data_gaps": [
                "research service unavailable; deterministic checks still completed"
            ],
            "committee_decision": None,
            "source_context_id": None,
            "source_context_fingerprint": None,
        }


def _cost_model(settings: Settings, asset_type: AssetType) -> CostModel:
    section = (
        "stock"
        if asset_type == AssetType.STOCK
        else "etf"
        if asset_type in {AssetType.ETF, AssetType.INDEX}
        else "stock"
    )
    return CostModel.from_dict(settings.section("costs").get(section, {}))


def _constraints(settings: Settings) -> TradeConstraints:
    return TradeConstraints(
        maximum_market_data_age_business_days=1,
        maximum_total_exposure=float(settings.get("risk.max_total_exposure", 0.80)),
        maximum_single_weight=float(settings.get("risk.max_single_position", 0.15)),
        maximum_industry_weight=float(settings.get("risk.max_industry_exposure", 0.30)),
    )


def _asset_type(
    settings: Settings,
    symbol: str,
    requested: str | None,
) -> AssetType:
    if requested:
        return AssetType(requested)
    if symbol in set(settings.get("strategies.etf_rotation.universe", [])):
        return AssetType.ETF
    return AssetType.STOCK


__all__ = [
    "cancel_user_paper_order",
    "create_user_paper_account",
    "load_latest_trade_quote",
    "mark_user_paper_account",
    "run_pretrade_check",
    "settle_user_paper_order",
    "submit_user_paper_order",
    "user_simulator_repository",
]
