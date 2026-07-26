from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable

import numpy as np

from quantlab.domain.models import Bar, Fill, Instrument, OrderRequest, Position, Side
from quantlab.execution.costs import CostModel
from quantlab.execution.rules import PortfolioExecutionState, TradeRuleService


@dataclass
class BacktestResult:
    initial_capital: float
    final_equity: float
    equity_curve: list[tuple[date, float]]
    fills: list[Fill]
    rejected_orders: list[tuple[OrderRequest, str]]
    metrics: dict[str, float]


@dataclass
class Account:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    def equity(self) -> float:
        return self.cash + sum(position.market_value for position in self.positions.values())

    def roll_settlement(self) -> None:
        for position in self.positions.values():
            position.frozen_quantity = 0


SignalFunction = Callable[[date, dict[str, Bar], Account], Iterable[OrderRequest]]


class BacktestEngine:
    """Small event-driven engine with explicit A-share execution constraints."""

    def __init__(
        self,
        instruments: dict[str, Instrument],
        cost_models: dict[str, CostModel],
        initial_capital: float = 100_000.0,
    ) -> None:
        self.instruments = instruments
        self.cost_models = cost_models
        self.initial_capital = initial_capital

    def run(self, bars: Iterable[Bar], signal_fn: SignalFunction) -> BacktestResult:
        grouped: dict[date, dict[str, Bar]] = defaultdict(dict)
        for bar in bars:
            grouped[bar.date][bar.symbol] = bar
        account = Account(cash=self.initial_capital)
        pending: list[OrderRequest] = []
        fills: list[Fill] = []
        rejected: list[tuple[OrderRequest, str]] = []
        curve: list[tuple[date, float]] = []

        for trade_date in sorted(grouped):
            day_bars = grouped[trade_date]
            account.roll_settlement()
            for symbol, bar in day_bars.items():
                if symbol in account.positions:
                    account.positions[symbol].market_price = bar.open

            for order in pending:
                bar = day_bars.get(order.symbol)
                reason = self._reject_reason(order, bar, account)
                if reason:
                    rejected.append((order, reason))
                    continue
                assert bar is not None
                instrument = self.instruments[order.symbol]
                cost = self.cost_models[instrument.asset_type.value]
                fill = cost.fill(order, bar.open, trade_date)
                if order.side == Side.BUY:
                    total = fill.gross_value + fill.commission + fill.transfer_fee
                    if total > account.cash:
                        rejected.append((order, "insufficient_cash"))
                        continue
                    account.cash -= total
                    position = account.positions.setdefault(
                        order.symbol, Position(symbol=order.symbol)
                    )
                    old_cost = position.average_cost * position.quantity
                    position.quantity += order.quantity
                    position.average_cost = (old_cost + total) / position.quantity
                    position.market_price = fill.price
                    position.industry = instrument.industry
                    if instrument.t_plus_one:
                        position.frozen_quantity += order.quantity
                else:
                    position = account.positions[order.symbol]
                    proceeds = (
                        fill.gross_value - fill.commission - fill.stamp_duty - fill.transfer_fee
                    )
                    account.cash += proceeds
                    position.quantity -= order.quantity
                    position.market_price = fill.price
                    if position.quantity == 0:
                        del account.positions[order.symbol]
                fills.append(fill)

            for symbol, bar in day_bars.items():
                if symbol in account.positions:
                    account.positions[symbol].market_price = bar.close
            pending = list(signal_fn(trade_date, day_bars, account))
            curve.append((trade_date, account.equity()))

        metrics = calculate_equity_metrics(curve, len(fills))
        final_equity = curve[-1][1] if curve else self.initial_capital
        return BacktestResult(self.initial_capital, final_equity, curve, fills, rejected, metrics)

    def _reject_reason(self, order: OrderRequest, bar: Bar | None, account: Account) -> str | None:
        if bar is None:
            return "suspended_or_missing"
        instrument = self.instruments[order.symbol]
        position = account.positions.get(order.symbol)
        quote = TradeRuleService.quote_from_bar(
            instrument,
            raw_price=bar.open,
            trade_date=bar.date,
            suspended=bar.suspended,
            limit_up=bar.limit_up,
            limit_down=bar.limit_down,
            is_st=bar.is_st,
            source=bar.source,
        )
        result = TradeRuleService().validate(
            order,
            quote,
            PortfolioExecutionState(
                cash=account.cash,
                equity=max(account.equity(), 0.01),
                market_value=sum(item.market_value for item in account.positions.values()),
                symbol_market_value=position.market_value if position else 0.0,
                industry_market_value=sum(
                    item.market_value
                    for item in account.positions.values()
                    if item.industry == instrument.industry
                ),
                position_quantity=position.quantity if position else 0,
                sellable_quantity=position.sellable_quantity if position else 0,
            ),
            request_date=bar.date,
            apply_instrument_risk=False,
        )
        return result.hard_failures[0] if result.hard_failures else None

    @staticmethod
    def _metrics(curve: list[tuple[date, float]], fills: list[Fill]) -> dict[str, float]:
        return calculate_equity_metrics(curve, len(fills))


def calculate_equity_metrics(
    curve: list[tuple[date, float]], turnover_count: int = 0
) -> dict[str, float]:
    if len(curve) < 2:
        return {
            "total_return": 0.0,
            "annualized_return": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "turnover_count": float(turnover_count),
        }
    values = np.asarray([value for _, value in curve], dtype=float)
    returns = values[1:] / values[:-1] - 1
    total = values[-1] / values[0] - 1
    years = max(len(values) / 252, 1 / 252)
    annualized = (values[-1] / values[0]) ** (1 / years) - 1
    volatility = returns.std(ddof=1) * math.sqrt(252) if len(returns) > 1 else 0.0
    sharpe = returns.mean() * 252 / volatility if volatility > 0 else 0.0
    peaks = np.maximum.accumulate(values)
    max_drawdown = float(np.min(values / peaks - 1))
    return {
        "total_return": float(total),
        "annualized_return": float(annualized),
        "annualized_volatility": float(volatility),
        "sharpe": float(sharpe),
        "max_drawdown": max_drawdown,
        "turnover_count": float(turnover_count),
    }
