from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from quantlab.domain.models import AssetType, MarketRegime, Position, StrategySignal
from quantlab.portfolio.etf_core import rebalance_period
from quantlab.risk.filters import assess_instrument_risk


class ManualOrderLine(BaseModel):
    symbol: str
    name: str = ""
    strategy: str
    side: Literal["buy", "sell", "review"]
    status: Literal["actionable", "blocked", "review_required", "below_minimum"]
    quantity: int = Field(ge=0)
    current_quantity: int = Field(ge=0)
    target_quantity: int = Field(ge=0)
    reference_price: float = Field(ge=0)
    estimated_gross_value: float = Field(ge=0)
    target_weight: float = Field(ge=0, le=1)
    stop_loss: float | None = Field(default=None, gt=0)
    maximum_loss_amount: float | None = Field(default=None, ge=0)
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PortfolioPlan(BaseModel):
    as_of: date
    market_regime: MarketRegime
    equity: float = Field(gt=0)
    cash_before: float
    cash_reserve_target: float
    max_total_exposure: float = Field(ge=0, le=1)
    max_industry_exposure: float = Field(ge=0, le=1)
    strategy_budgets: dict[str, float]
    target_weights: dict[str, float]
    orders: list[ManualOrderLine]
    blocked_candidates: list[ManualOrderLine]
    degraded_sources: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    manual_execution_only: bool = True


def build_manual_portfolio_plan(
    *,
    as_of: date,
    market_regime: MarketRegime,
    equity: float,
    cash: float,
    positions: dict[str, Position],
    signals: list[StrategySignal],
    strategy_budgets: dict[str, float],
    market_data: dict[str, dict[str, Any]],
    previous_targets: dict[str, dict[str, Any]] | None = None,
    previous_plan_as_of: date | None = None,
    max_total_exposure: float = 0.80,
    max_single_position: float = 0.15,
    max_industry_exposure: float = 0.30,
    minimum_order_value: float = 1_000.0,
    degraded_sources: list[str] | None = None,
) -> PortfolioPlan:
    if equity <= 0:
        raise ValueError("portfolio equity must be positive")
    previous_targets = previous_targets or {}
    degraded_sources = list(degraded_sources or [])
    warnings: list[str] = []
    grouped: dict[str, list[tuple[StrategySignal, dict[str, Any]]]] = {}
    blocked: list[ManualOrderLine] = []
    candidate_symbols = {signal.symbol for signal in signals}

    for signal in signals:
        metadata = market_data.get(signal.symbol, {})
        price = _positive_price(metadata.get("price"))
        asset_type = AssetType(metadata.get("asset_type", AssetType.STOCK.value))
        risk = assess_instrument_risk(asset_type, metadata, as_of)
        metadata["_pretrade_checks"] = list(risk.checks)
        metadata["_pretrade_warnings"] = list(risk.warnings)
        status = "blocked" if risk.blocked else "review_required" if risk.review_required else None
        risk_warnings = list(risk.warnings)
        if bool(metadata.get("_research_only")):
            status = status or "review_required"
            risk_warnings.append(
                str(
                    metadata.get("_research_only_reason")
                    or "research-only strategy cannot create a new actionable order"
                )
            )
        agent_action = metadata.get("_agent_decision_action")
        current_quantity = positions.get(signal.symbol, Position(symbol=signal.symbol)).quantity
        if metadata.get("_agent_requires_human_review"):
            risk_warnings.append("same-day multi-agent decision requires human review")
            if current_quantity == 0:
                status = status or "review_required"
        elif current_quantity == 0 and agent_action in {
            "hold",
            "watch",
            "reduce",
            "sell",
            "review_required",
        }:
            status = status or "review_required"
            risk_warnings.append(
                f"same-day multi-agent action is {agent_action}; new position is not allowed"
            )
        if price is None:
            status = "blocked"
            risk_warnings.append("valid reference price is unavailable")
        if status:
            blocked.append(
                ManualOrderLine(
                    symbol=signal.symbol,
                    name=str(metadata.get("name") or ""),
                    strategy=signal.strategy,
                    side="review",
                    status=status,
                    quantity=0,
                    current_quantity=positions.get(
                        signal.symbol, Position(symbol=signal.symbol)
                    ).quantity,
                    target_quantity=0,
                    reference_price=price or 0.0,
                    estimated_gross_value=0.0,
                    target_weight=0.0,
                    reasons=list(signal.reasons) + risk.checks + risk.hard_vetoes,
                    warnings=risk_warnings,
                )
            )
            continue
        grouped.setdefault(signal.strategy, []).append((signal, metadata))

    managed_symbols = candidate_symbols | set(previous_targets)
    unmanaged_value = sum(
        position.market_value
        for symbol, position in positions.items()
        if symbol not in managed_symbols
    )
    unmanaged_weight = unmanaged_value / equity
    available_managed_weight = max(0.0, max_total_exposure - unmanaged_weight)
    if unmanaged_weight > max_total_exposure:
        warnings.append("existing unmanaged holdings already exceed the total exposure limit")

    raw_targets: dict[str, float] = {}
    selected_by_symbol: dict[str, tuple[StrategySignal, dict[str, Any]]] = {}
    for strategy, candidates in grouped.items():
        budget = max(0.0, strategy_budgets.get(strategy, 0.0))
        convictions = [
            max(signal.target_weight, 1e-6)
            * max(0.10, signal.confidence)
            * (0.50 + 0.50 * max(0.0, signal.score))
            for signal, _ in candidates
        ]
        denominator = sum(convictions) or 1.0
        for (signal, metadata), conviction in zip(candidates, convictions):
            raw_targets[signal.symbol] = raw_targets.get(signal.symbol, 0.0) + (
                budget * conviction / denominator
            )
            agent_cap = metadata.get("_agent_target_cap")
            if agent_cap is not None:
                raw_targets[signal.symbol] = min(
                    raw_targets[signal.symbol], max(0.0, float(agent_cap))
                )
            selected_by_symbol[signal.symbol] = (signal, metadata)

    raw_total = sum(raw_targets.values())
    if raw_total > available_managed_weight and raw_total > 0:
        scale = available_managed_weight / raw_total
        raw_targets = {symbol: weight * scale for symbol, weight in raw_targets.items()}
        warnings.append("strategy targets were scaled to respect existing portfolio exposure")
    target_weights = {
        symbol: min(max_single_position, max(0.0, weight)) for symbol, weight in raw_targets.items()
    }
    if sum(target_weights.values()) < sum(raw_targets.values()) - 1e-9:
        warnings.append("single-position caps reduced one or more targets")
    baseline_industry_weights: dict[str, float] = {}
    for symbol, position in positions.items():
        if symbol in managed_symbols:
            continue
        industry = position.industry or market_data.get(symbol, {}).get("industry")
        if industry:
            baseline_industry_weights[str(industry)] = (
                baseline_industry_weights.get(str(industry), 0.0) + position.market_value / equity
            )
    target_symbols_by_industry: dict[str, list[str]] = {}
    for symbol in target_weights:
        industry = market_data.get(symbol, {}).get("industry")
        if industry:
            target_symbols_by_industry.setdefault(str(industry), []).append(symbol)
    for industry, industry_symbols in target_symbols_by_industry.items():
        requested = sum(target_weights[symbol] for symbol in industry_symbols)
        available = max(
            0.0,
            max_industry_exposure - baseline_industry_weights.get(industry, 0.0),
        )
        if requested <= available + 1e-12:
            continue
        scale = available / requested if requested > 0 else 0.0
        for symbol in industry_symbols:
            target_weights[symbol] *= scale
        warnings.append(
            f"industry cap reduced {industry} targets to {max_industry_exposure:.1%} exposure"
        )

    order_lines: list[ManualOrderLine] = []
    for symbol in sorted(set(target_weights) | set(previous_targets)):
        selected = selected_by_symbol.get(symbol)
        previous = previous_targets.get(symbol, {})
        metadata = selected[1] if selected else market_data.get(symbol, previous)
        signal = selected[0] if selected else None
        strategy = (
            signal.strategy if signal else str(previous.get("strategy") or "previous_plan_exit")
        )
        price = _positive_price(metadata.get("price"))
        position = positions.get(symbol, Position(symbol=symbol))
        current_quantity = position.quantity
        if price is None:
            if current_quantity:
                blocked.append(
                    ManualOrderLine(
                        symbol=symbol,
                        name=str(metadata.get("name") or ""),
                        strategy=strategy,
                        side="review",
                        status="review_required",
                        quantity=0,
                        current_quantity=current_quantity,
                        target_quantity=current_quantity,
                        reference_price=0.0,
                        estimated_gross_value=0.0,
                        target_weight=0.0,
                        reasons=["previously managed position has no current reference price"],
                    )
                )
            continue
        lot = max(1, int(metadata.get("trade_lot") or previous.get("trade_lot") or 100))
        target_weight = target_weights.get(symbol, 0.0)
        target_quantity = int(target_weight * equity / price / lot) * lot
        delta = target_quantity - current_quantity
        if target_weight > 0 and current_quantity == 0 and target_quantity == 0:
            order_lines.append(
                ManualOrderLine(
                    symbol=symbol,
                    name=str(metadata.get("name") or ""),
                    strategy=strategy,
                    side="buy",
                    status="below_minimum",
                    quantity=0,
                    current_quantity=0,
                    target_quantity=0,
                    reference_price=price,
                    estimated_gross_value=0.0,
                    target_weight=target_weight,
                    reasons=(list(signal.reasons) if signal else []),
                    warnings=["target budget cannot purchase one trading lot"],
                )
            )
            continue
        if delta == 0:
            continue
        if signal is not None and target_weight > 0 and current_quantity > 0:
            frequency = str(metadata.get("rebalance_frequency") or "").lower()
            tolerance = max(0.0, float(metadata.get("rebalance_tolerance_weight") or 0.0))
            current_weight = current_quantity * price / equity
            if frequency and not _rebalance_due(previous_plan_as_of, as_of, frequency):
                message = (
                    f"{metadata.get('execution_protocol') or frequency} rebalance is not due; "
                    "existing ETF core positions are held"
                )
                if message not in warnings:
                    warnings.append(message)
                continue
            if tolerance > 0 and abs(target_weight - current_weight) < tolerance:
                message = (
                    f"rebalance drift below {tolerance:.1%}; existing ETF core positions are held"
                )
                if message not in warnings:
                    warnings.append(message)
                continue
        side: Literal["buy", "sell", "review"] = "buy" if delta > 0 else "sell"
        quantity = abs(delta)
        if side == "sell" and target_quantity == 0:
            quantity = current_quantity
        elif side == "sell":
            quantity = quantity // lot * lot
        if quantity <= 0:
            continue
        gross = quantity * price
        status = "actionable" if gross >= minimum_order_value else "below_minimum"
        stop_loss = _positive_price(metadata.get("stop_loss")) if side == "buy" else None
        maximum_loss = max(0.0, price - stop_loss) * quantity if stop_loss is not None else None
        order_lines.append(
            ManualOrderLine(
                symbol=symbol,
                name=str(metadata.get("name") or ""),
                strategy=strategy,
                side=side,
                status=status,
                quantity=quantity,
                current_quantity=current_quantity,
                target_quantity=target_quantity,
                reference_price=price,
                estimated_gross_value=gross,
                target_weight=target_weight,
                stop_loss=stop_loss,
                maximum_loss_amount=maximum_loss,
                reasons=(
                    list(signal.reasons) + list(metadata.get("_pretrade_checks", []))
                    if signal
                    else ["symbol left the latest managed target set"]
                ),
                warnings=(
                    list(metadata.get("_pretrade_warnings", []))
                    + (
                        [f"risk reference: {metadata.get('risk_method')}"]
                        if stop_loss is not None and metadata.get("risk_method")
                        else []
                    )
                    + (
                        []
                        if status == "actionable"
                        else ["trade value is below the configured minimum"]
                    )
                ),
            )
        )

    order_lines.sort(key=lambda item: (0 if item.side == "sell" else 1, item.strategy, item.symbol))
    sell_value = sum(
        item.estimated_gross_value
        for item in order_lines
        if item.side == "sell" and item.status == "actionable"
    )
    buy_value = sum(
        item.estimated_gross_value
        for item in order_lines
        if item.side == "buy" and item.status == "actionable"
    )
    reserve_target = equity * (1 - max_total_exposure)
    if cash + sell_value - buy_value < reserve_target - 1e-6:
        warnings.append(
            "estimated orders may breach the cash reserve after fees; execute sells first"
        )
    warnings.append(
        "reference prices can move; recheck limit status, suspension and quantities before entry"
    )

    return PortfolioPlan(
        as_of=as_of,
        market_regime=market_regime,
        equity=equity,
        cash_before=cash,
        cash_reserve_target=reserve_target,
        max_total_exposure=max_total_exposure,
        max_industry_exposure=max_industry_exposure,
        strategy_budgets=strategy_budgets,
        target_weights=target_weights,
        orders=order_lines,
        blocked_candidates=blocked,
        degraded_sources=degraded_sources,
        warnings=warnings,
    )


def _positive_price(value: Any) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _rebalance_due(previous: date | None, current: date, frequency: str) -> bool:
    if previous is None or previous >= current:
        return previous is None
    try:
        return rebalance_period(previous, frequency) != rebalance_period(current, frequency)
    except ValueError:
        return True
