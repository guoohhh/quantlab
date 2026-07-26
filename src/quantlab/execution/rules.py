from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from quantlab.market import TradingCalendarService

from quantlab.domain.models import Instrument, OrderRequest, Side
from quantlab.domain.trading import DataQuality, MarketQuote
from quantlab.risk.filters import assess_instrument_risk


@dataclass(frozen=True)
class PortfolioExecutionState:
    cash: float
    equity: float
    market_value: float
    symbol_market_value: float
    industry_market_value: float
    position_quantity: int
    sellable_quantity: int


@dataclass(frozen=True)
class TradeConstraints:
    maximum_market_data_age_business_days: int = 1
    maximum_total_exposure: float = 0.80
    maximum_single_weight: float = 0.15
    maximum_industry_weight: float = 0.30


@dataclass
class TradeRuleResult:
    allowed: bool = True
    review_required: bool = False
    hard_failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    post_trade_cash: float = 0.0
    post_trade_single_weight: float = 0.0
    post_trade_industry_weight: float = 0.0
    post_trade_total_exposure: float = 0.0


class TradeRuleService:
    """Shared deterministic execution gates for backtests, shadow and user paper accounts."""

    @staticmethod
    def quote_from_bar(
        instrument: Instrument,
        *,
        raw_price: float,
        trade_date: date,
        suspended: bool = False,
        limit_up: bool = False,
        limit_down: bool = False,
        is_st: bool = False,
        source: str = "backtest",
    ) -> MarketQuote:
        return MarketQuote(
            symbol=instrument.symbol,
            name=instrument.name,
            asset_type=instrument.asset_type,
            raw_price=raw_price,
            as_of=trade_date,
            source=source,
            suspended=suspended,
            limit_up=limit_up,
            limit_down=limit_down,
            is_st=is_st,
            industry=instrument.industry,
            trade_lot=instrument.trade_lot,
            t_plus_one=instrument.t_plus_one,
            session_status="closed",
        )

    def validate(
        self,
        order: OrderRequest,
        quote: MarketQuote | None,
        state: PortfolioExecutionState,
        *,
        request_date: date,
        estimated_gross_value: float = 0.0,
        estimated_transaction_fees: float = 0.0,
        constraints: TradeConstraints | None = None,
        apply_instrument_risk: bool = True,
        calendar_service: "TradingCalendarService | None" = None,
    ) -> TradeRuleResult:
        limits = constraints or TradeConstraints()
        result = TradeRuleResult(post_trade_cash=state.cash)
        if quote is None:
            return self._fail(result, "suspended_or_missing")
        if quote.data_quality == DataQuality.MISSING:
            return self._fail(result, "market_data_missing")
        age = business_day_age(quote.as_of, request_date, calendar_service=calendar_service)
        result.checks.append(f"market_data_business_day_age={age}")
        if quote.data_quality == DataQuality.STALE or (
            age > max(0, limits.maximum_market_data_age_business_days)
        ):
            self._fail(result, "market_data_stale")
        if quote.suspended:
            self._fail(result, "suspended_or_missing")
        if quote.session_status == "unknown":
            self._fail(result, "session_status_unknown")
        if quote.actionability_reasons:
            result.review_required = True
            result.warnings.extend(quote.actionability_reasons)
        if order.side == Side.BUY and quote.limit_up:
            self._fail(result, "limit_up")
        if order.side == Side.SELL and quote.limit_down:
            self._fail(result, "limit_down")
        if order.side == Side.BUY and order.quantity % quote.trade_lot != 0:
            self._fail(result, "invalid_trade_lot")
        if order.side == Side.SELL and order.quantity > state.sellable_quantity:
            self._fail(result, "t_plus_one_or_insufficient_position")

        if order.side == Side.BUY and apply_instrument_risk:
            metadata: dict[str, Any] = {
                **quote.risk_metadata,
                "name": quote.name,
                "is_st": quote.is_st,
                "suspended": quote.suspended,
                "limit_up": quote.limit_up,
                "market_data_freshness_required": True,
                "market_data_as_of": quote.as_of,
                "maximum_market_data_age_business_days": (
                    limits.maximum_market_data_age_business_days
                ),
                "market_data_business_day_age": age,
            }
            risk = assess_instrument_risk(quote.asset_type, metadata, request_date)
            result.review_required = risk.review_required
            result.hard_failures.extend(risk.hard_vetoes)
            result.warnings.extend(risk.warnings)
            result.checks.extend(risk.checks)

        cash_effect = estimated_gross_value + estimated_transaction_fees
        if order.side == Side.BUY:
            result.post_trade_cash = state.cash - cash_effect
            if result.post_trade_cash < -1e-6:
                self._fail(result, "insufficient_cash")
            post_market_value = state.market_value + estimated_gross_value
            post_symbol_value = state.symbol_market_value + estimated_gross_value
            post_industry_value = state.industry_market_value + estimated_gross_value
        else:
            result.post_trade_cash = (
                state.cash + estimated_gross_value - estimated_transaction_fees
            )
            post_market_value = max(0.0, state.market_value - estimated_gross_value)
            post_symbol_value = max(0.0, state.symbol_market_value - estimated_gross_value)
            post_industry_value = max(0.0, state.industry_market_value - estimated_gross_value)

        post_equity = max(
            0.01,
            result.post_trade_cash + post_market_value,
        )
        result.post_trade_total_exposure = post_market_value / post_equity
        result.post_trade_single_weight = post_symbol_value / post_equity
        result.post_trade_industry_weight = post_industry_value / post_equity
        if order.side == Side.BUY and estimated_gross_value > 0:
            if result.post_trade_total_exposure > limits.maximum_total_exposure + 1e-9:
                self._fail(result, "maximum_total_exposure_exceeded")
            if result.post_trade_single_weight > limits.maximum_single_weight + 1e-9:
                self._fail(result, "maximum_single_weight_exceeded")
            if result.post_trade_industry_weight > limits.maximum_industry_weight + 1e-9:
                self._fail(result, "maximum_industry_weight_exceeded")

        if result.hard_failures:
            result.allowed = False
            result.hard_failures = list(dict.fromkeys(result.hard_failures))
        result.warnings = list(dict.fromkeys(result.warnings))
        result.checks = list(dict.fromkeys(result.checks))
        return result

    @staticmethod
    def _fail(result: TradeRuleResult, reason: str) -> TradeRuleResult:
        result.allowed = False
        result.hard_failures.append(reason)
        return result


def business_day_age(
    observed: date,
    current: date,
    *,
    calendar_service: "TradingCalendarService | None" = None,
) -> int:
    if observed >= current:
        return 0
    if calendar_service is not None:
        return calendar_service.business_day_age(observed, current, formal=False)
    return (current - observed).days


__all__ = [
    "PortfolioExecutionState",
    "TradeConstraints",
    "TradeRuleResult",
    "TradeRuleService",
    "business_day_age",
]
