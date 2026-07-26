from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from quantlab.domain.models import AssetType
from quantlab.domain.data_governance import DataTrustLevel


class AccountType(str, Enum):
    SYSTEM_SHADOW = "system_shadow"
    USER_PAPER = "user_paper"
    MANUAL_REAL_LEDGER = "manual_real_ledger"


class AccountStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    ARCHIVED = "archived"


class UserOrderStatus(str, Enum):
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class DataQuality(str, Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    STALE = "stale"
    MISSING = "missing"


class MarketQuote(BaseModel):
    symbol: str
    name: str = ""
    asset_type: AssetType
    raw_price: float = Field(gt=0)
    as_of: date
    available_at: datetime | None = None
    source: str
    provider: str | None = None
    source_version: str = "unknown"
    data_quality: DataQuality = DataQuality.AVAILABLE
    degraded_from: list[str] = Field(default_factory=list)
    suspended: bool = False
    limit_up: bool = False
    limit_down: bool = False
    is_st: bool = False
    industry: str | None = None
    trade_lot: int = Field(default=100, gt=0)
    t_plus_one: bool = True
    session_status: Literal["open", "closed", "unknown"] = "unknown"
    quote_kind: Literal[
        "realtime", "delayed", "current_close", "previous_close", "unavailable"
    ] = "unavailable"
    delay_seconds: float | None = Field(default=None, ge=0)
    observed_at: datetime | None = None
    price_deviation_bps: float | None = Field(default=None, ge=0)
    provider_health: dict[str, Any] = Field(default_factory=dict)
    risk_metadata: dict[str, Any] = Field(default_factory=dict)
    authoritative: bool = True
    evidence_stage: Literal["production", "test", "research_only"] = "production"
    quote_fingerprint: str = ""
    trust_level: DataTrustLevel = DataTrustLevel.SERVER_OBSERVED
    license_status: str = "unknown"
    endpoint: str = "unknown"
    raw_payload_fingerprint: str = ""
    actionable: bool = False
    actionability_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_available_at(self) -> "MarketQuote":
        if self.available_at is None:
            self.available_at = datetime.combine(self.as_of, datetime.min.time(), tzinfo=UTC)
        if self.observed_at is None:
            self.observed_at = self.available_at
        return self


class PreTradeCheckResult(BaseModel):
    check_id: str
    account_id: str
    account_version: int
    symbol: str
    side: Literal["buy", "sell"]
    requested_quantity: int = Field(gt=0)
    suggested_action: str
    suggested_quantity: int = Field(ge=0)
    suggested_quantity_range: tuple[int, int]
    reference_price: float
    reference_time: datetime
    estimated_gross_value: float
    estimated_transaction_fees: float
    estimated_slippage: float
    estimated_total_cash_effect: float
    post_trade_cash: float
    post_trade_single_weight: float
    post_trade_industry_weight: float
    post_trade_total_exposure: float
    loss_if_symbol_down_10pct: float
    loss_if_symbol_down_15pct: float
    supporting_evidence: list[str] = Field(default_factory=list)
    opposing_evidence: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    reviewer_status: str = "unavailable"
    hard_risk_passed: bool
    hard_failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    allowed_to_submit: bool
    requires_user_review: bool
    research_run_id: str | None = None
    research_link_status: Literal["linked", "unlinked", "unavailable"] = "unlinked"
    research_symbol: str | None = None
    research_as_of: date | None = None
    context_id: str | None = None
    context_version: str | None = None
    context_fingerprint: str | None = None
    context_quality_score: float | None = Field(default=None, ge=0, le=1)
    llm_suggested_action: str | None = None
    llm_suggested_weight_range: tuple[float, float] | None = None
    quote: MarketQuote
    deterministic: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


__all__ = [
    "AccountStatus",
    "AccountType",
    "DataQuality",
    "MarketQuote",
    "PreTradeCheckResult",
    "UserOrderStatus",
]
