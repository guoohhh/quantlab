from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class BudgetSmoothingPolicy:
    cash_reserve_weight: float = 0.20
    recovery_alpha: float = 0.25
    reduction_alpha: float = 0.60
    maximum_daily_weight_step: float = 0.05
    minimum_rebalance_weight: float = 0.01
    maximum_one_way_turnover: float = 0.15
    impact_bps: float = 10.0
    commission_rate: float = 0.00025
    minimum_commission: float = 5.0
    lot_size: int = 100

    def __post_init__(self) -> None:
        if not 0 <= self.cash_reserve_weight < 1:
            raise ValueError("cash reserve weight must be in [0,1)")
        if not 0 < self.recovery_alpha <= 1 or not 0 < self.reduction_alpha <= 1:
            raise ValueError("smoothing alpha must be in (0,1]")
        if self.maximum_daily_weight_step <= 0 or self.maximum_one_way_turnover <= 0:
            raise ValueError("weight and turnover limits must be positive")
        if self.lot_size <= 0:
            raise ValueError("lot size must be positive")


def plan_smoothed_rebalance(
    *,
    nav: float,
    available_cash: float,
    current_quantities: dict[str, int],
    desired_weights: dict[str, float],
    prices: dict[str, float],
    sellable_quantities: dict[str, int] | None = None,
    policy: BudgetSmoothingPolicy | None = None,
    evidence_degraded: bool = False,
) -> dict[str, Any]:
    """Create a gradual, lot-aware and cost-aware rebalance plan.

    Risk reductions are allowed to happen faster than risk increases. Buys are always clipped by
    the post-trade cash reserve; this planner never assumes that a model request is executable.
    """

    if nav <= 0 or available_cash < 0:
        raise ValueError("nav must be positive and cash must be non-negative")
    policy = policy or BudgetSmoothingPolicy()
    sellable_quantities = sellable_quantities or current_quantities
    symbols = sorted(set(current_quantities) | set(desired_weights))
    invalid_prices = [symbol for symbol in symbols if float(prices.get(symbol, 0.0)) <= 0]
    if invalid_prices:
        raise ValueError(f"positive prices are required for: {invalid_prices}")
    current_weights = {
        symbol: current_quantities.get(symbol, 0) * float(prices[symbol]) / nav
        for symbol in symbols
    }
    risk_budget = 1.0 - policy.cash_reserve_weight
    raw_desired = {symbol: max(0.0, float(desired_weights.get(symbol, 0.0))) for symbol in symbols}
    requested_total = sum(raw_desired.values())
    scale = min(1.0, risk_budget / requested_total) if requested_total else 1.0
    bounded_desired = {symbol: weight * scale for symbol, weight in raw_desired.items()}
    if evidence_degraded:
        bounded_desired = {
            symbol: min(weight, current_weights[symbol])
            for symbol, weight in bounded_desired.items()
        }
    provisional: dict[str, float] = {}
    for symbol in symbols:
        current = current_weights[symbol]
        desired = bounded_desired[symbol]
        alpha = policy.reduction_alpha if desired < current else policy.recovery_alpha
        delta = alpha * (desired - current)
        delta = max(
            -policy.maximum_daily_weight_step,
            min(policy.maximum_daily_weight_step, delta),
        )
        if abs(delta) < policy.minimum_rebalance_weight:
            delta = 0.0
        provisional[symbol] = max(0.0, current + delta)
    one_way_turnover = 0.5 * sum(
        abs(provisional[symbol] - current_weights[symbol]) for symbol in symbols
    )
    if one_way_turnover > policy.maximum_one_way_turnover:
        turnover_scale = policy.maximum_one_way_turnover / one_way_turnover
        provisional = {
            symbol: current_weights[symbol]
            + (provisional[symbol] - current_weights[symbol]) * turnover_scale
            for symbol in symbols
        }
    target_quantities = {
        symbol: _lot_floor(provisional[symbol] * nav / float(prices[symbol]), policy.lot_size)
        for symbol in symbols
    }
    orders: list[dict[str, Any]] = []
    cash = available_cash
    estimated_costs = 0.0
    # Sell first, but never more than the explicitly sellable quantity.
    for symbol in symbols:
        current_quantity = int(current_quantities.get(symbol, 0))
        target_quantity = int(target_quantities[symbol])
        if target_quantity >= current_quantity:
            continue
        requested = current_quantity - target_quantity
        quantity = min(requested, int(sellable_quantities.get(symbol, 0)))
        quantity = _lot_floor(quantity, policy.lot_size)
        if quantity <= 0:
            target_quantities[symbol] = current_quantity
            continue
        gross = quantity * float(prices[symbol])
        cost = _estimated_cost(gross, policy)
        cash += gross - cost
        estimated_costs += cost
        orders.append(_order(symbol, "sell", quantity, prices[symbol], gross, cost))
    reserve_cash = nav * policy.cash_reserve_weight
    buy_capacity = max(0.0, cash - reserve_cash)
    for symbol in symbols:
        current_quantity = int(current_quantities.get(symbol, 0))
        target_quantity = int(target_quantities[symbol])
        if target_quantity <= current_quantity:
            continue
        desired_quantity = _lot_floor(target_quantity - current_quantity, policy.lot_size)
        unit_lot_gross = policy.lot_size * float(prices[symbol])
        unit_lot_cost = _estimated_cost(unit_lot_gross, policy)
        affordable_lots = math.floor(buy_capacity / (unit_lot_gross + unit_lot_cost))
        quantity = min(desired_quantity, affordable_lots * policy.lot_size)
        quantity = _lot_floor(quantity, policy.lot_size)
        if quantity <= 0:
            target_quantities[symbol] = current_quantity
            continue
        gross = quantity * float(prices[symbol])
        cost = _estimated_cost(gross, policy)
        cash -= gross + cost
        buy_capacity = max(0.0, cash - reserve_cash)
        estimated_costs += cost
        target_quantities[symbol] = current_quantity + quantity
        orders.append(_order(symbol, "buy", quantity, prices[symbol], gross, cost))
    realized_weights = {
        symbol: target_quantities[symbol] * float(prices[symbol]) / nav for symbol in symbols
    }
    return {
        "orders": orders,
        "current_weights": current_weights,
        "requested_weights": raw_desired,
        "bounded_weights": bounded_desired,
        "smoothed_weights_before_lots": provisional,
        "target_quantities": target_quantities,
        "realized_weights_after_lots": realized_weights,
        "weight_rounding_error": {
            symbol: realized_weights[symbol] - provisional[symbol] for symbol in symbols
        },
        "ending_cash_estimate": cash,
        "cash_reserve_required": reserve_cash,
        "cash_reserve_satisfied": cash + 1e-9 >= reserve_cash,
        "estimated_costs": estimated_costs,
        "estimated_one_way_turnover": 0.5
        * sum(abs(realized_weights[symbol] - current_weights[symbol]) for symbol in symbols),
        "evidence_degraded": evidence_degraded,
        "policy": asdict(policy),
        "claim_boundary": (
            "This is a deterministic planning result. Exchange state, T+1, price limits and "
            "final order checks still govern execution."
        ),
    }


def _lot_floor(quantity: float | int, lot_size: int) -> int:
    return max(0, math.floor(float(quantity) / lot_size) * lot_size)


def _estimated_cost(gross: float, policy: BudgetSmoothingPolicy) -> float:
    commission = max(policy.minimum_commission, gross * policy.commission_rate)
    impact = gross * policy.impact_bps / 10_000.0
    return commission + impact


def _order(
    symbol: str,
    side: str,
    quantity: int,
    price: float,
    gross: float,
    cost: float,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "reference_price": float(price),
        "gross_value": gross,
        "estimated_cost": cost,
    }


__all__ = ["BudgetSmoothingPolicy", "plan_smoothed_rebalance"]
