from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class AssetType(str, Enum):
    STOCK = "stock"
    ETF = "etf"
    CONVERTIBLE_BOND = "convertible_bond"
    INDEX = "index"
    CASH = "cash"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class MarketRegime(str, Enum):
    BULL = "bull"
    RANGE = "range"
    BEAR = "bear"
    HIGH_VOLATILITY = "high_volatility"


class Bar(BaseModel):
    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0
    prev_close: float | None = None
    adjusted_open: float | None = None
    adjusted_high: float | None = None
    adjusted_low: float | None = None
    adjusted_close: float | None = None
    suspended: bool = False
    limit_up: bool = False
    limit_down: bool = False
    is_st: bool = False
    available_at: datetime | None = None
    source: str = "unknown"

    @model_validator(mode="after")
    def validate_ohlc(self) -> "Bar":
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("invalid OHLC relationship")
        return self

    @property
    def signal_close(self) -> float:
        return self.adjusted_close if self.adjusted_close is not None else self.close


class Instrument(BaseModel):
    symbol: str
    name: str = ""
    asset_type: AssetType
    market: str = "CN"
    industry: str | None = None
    listing_date: date | None = None
    trade_lot: int = 100
    t_plus_one: bool = True


class Evidence(BaseModel):
    source: str
    title: str
    observed_at: datetime
    available_at: datetime
    value: Any = None
    credibility: float = Field(default=0.7, ge=0, le=1)
    relevance: float = Field(default=0.7, ge=0, le=1)
    recency: float = Field(default=0.7, ge=0, le=1)
    polarity: float = Field(default=0.0, ge=-1, le=1)
    degraded_from: str | None = None

    @property
    def weight(self) -> float:
        return self.credibility * self.relevance * self.recency


class StrategySignal(BaseModel):
    strategy: str
    symbol: str
    as_of: date
    score: float = Field(ge=-1, le=1)
    target_weight: float = Field(default=0.0, ge=0, le=1)
    confidence: float = Field(default=0.0, ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    valid_until: date | None = None


class OrderRequest(BaseModel):
    symbol: str
    side: Side
    quantity: int = Field(gt=0)
    signal_date: date
    reason: str = ""
    is_stop: bool = False


class Fill(BaseModel):
    symbol: str
    side: Side
    quantity: int
    price: float
    gross_value: float
    commission: float
    stamp_duty: float
    transfer_fee: float
    slippage: float
    trade_date: date
    rejected_reason: str | None = None

    @property
    def total_cost(self) -> float:
        return self.commission + self.stamp_duty + self.transfer_fee + self.slippage


class Position(BaseModel):
    symbol: str
    quantity: int = 0
    frozen_quantity: int = 0
    average_cost: float = 0.0
    market_price: float = 0.0
    industry: str | None = None

    @property
    def sellable_quantity(self) -> int:
        return max(0, self.quantity - self.frozen_quantity)

    @property
    def market_value(self) -> float:
        return self.quantity * self.market_price


class Forecast(BaseModel):
    symbol: str
    as_of: date
    horizon_days: Literal[5, 20]
    up_probability: float = Field(ge=0, le=1)
    flat_probability: float = Field(ge=0, le=1)
    down_probability: float = Field(ge=0, le=1)
    expected_return_pct: float
    lower_return_pct: float
    upper_return_pct: float
    confidence: float = Field(ge=0, le=1)
    drivers: list[str] = Field(default_factory=list)
    counter_evidence: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    model: str = "mock"
    model_provider: str = "mock"
    statistical_model_id: str | None = None
    statistical_model_version: int | None = None
    statistical_weight: float = Field(default=0.0, ge=0, le=1)
    statistical_up_probability: float | None = Field(default=None, ge=0, le=1)
    statistical_flat_probability: float | None = Field(default=None, ge=0, le=1)
    statistical_down_probability: float | None = Field(default=None, ge=0, le=1)
    raw_llm_up_probability: float | None = Field(default=None, ge=0, le=1)
    raw_llm_flat_probability: float | None = Field(default=None, ge=0, le=1)
    raw_llm_down_probability: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def probabilities_sum(self) -> "Forecast":
        total = self.up_probability + self.flat_probability + self.down_probability
        if abs(total - 1.0) > 0.02:
            raise ValueError("forecast probabilities must sum to 1")
        return self


class ForecastOutcome(BaseModel):
    run_id: str
    symbol: str
    as_of: date
    horizon_days: Literal[5, 20]
    realized_return_pct: float
    outcome: Literal["up", "flat", "down"]
    evaluated_at: date


class CalibrationReport(BaseModel):
    model: str | None = None
    horizon_days: int | None = None
    samples: int
    brier_score: float | None = None
    accuracy: float | None = None
    mean_confidence: float | None = None
    calibrated: bool = False
    minimum_samples: int = 30


class DecisionCard(BaseModel):
    symbol: str
    as_of: date
    action: Literal["buy", "add", "hold", "watch", "reduce", "sell", "review_required"]
    confidence: float = Field(ge=0, le=1)
    target_weight: float = Field(ge=0, le=1)
    entry_price: float | None = None
    stop_loss: float | None = None
    target_1: float | None = None
    target_2: float | None = None
    reasons: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    degraded_sources: list[str] = Field(default_factory=list)
    requires_human_review: bool = False
    suggested_weight_min: float = Field(default=0.0, ge=0, le=1)
    suggested_weight_max: float = Field(default=0.0, ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)
    counter_evidence_refs: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    context_id: str | None = None
    context_version: str | None = None
    context_fingerprint: str | None = None
    role_audit: list[dict[str, Any]] = Field(default_factory=list)
    trigger_codes: list[str] = Field(default_factory=list)
    policy_version: str = "legacy-unversioned"
    review_state: Literal["not_evaluated", "approved", "required", "rejected"] = (
        "not_evaluated"
    )
    signal_price_basis: str = "adjusted_close"
    execution_price_basis: str = "raw_market_price"


class AuditEvent(BaseModel):
    run_id: str
    step: str
    status: Literal["ok", "degraded", "rejected", "needs_review", "error"]
    detail: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
