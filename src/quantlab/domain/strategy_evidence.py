from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantlab.domain.data_governance import DataNamespace, DataTrustLevel


class EvidenceStage(StrEnum):
    RESEARCH_REPLAY = "research_replay"
    FORWARD_SHADOW = "forward_shadow"
    MEASURED = "measured"
    REJECTED = "rejected"


class AblationVariant(StrEnum):
    SIMPLE_BASELINE = "simple_baseline"
    QUANT_ONLY = "quant_only"
    RAW_LLM = "raw_llm"
    STATISTICAL_MODEL = "statistical_model"
    LLM_STAT_FUSION = "llm_stat_fusion"
    LLM_TRADE_GATE = "llm_trade_gate"
    FULL_SYSTEM = "full_system"


ABLATION_VARIANTS: tuple[AblationVariant, ...] = tuple(AblationVariant)


class PointInTimeSecurity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    name: str = ""
    security_type: Literal["etf", "stock", "convertible_bond"]
    exchange: str
    listing_date: date
    delisting_date: date | None = None
    asset_class: str
    category: str
    status: str = "listed"
    source: str
    source_version: str
    available_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    namespace: DataNamespace = DataNamespace.RESEARCH
    trust_level: DataTrustLevel = DataTrustLevel.RESEARCH_EXTERNAL
    manifest_id: str | None = None


class PointInTimeTradeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    trade_date: date
    trade_status: bool
    suspended: bool = False
    is_st: bool = False
    amount: float | None = Field(default=None, ge=0)
    fund_size: float | None = Field(default=None, ge=0)
    turnover_rate: float | None = Field(default=None, ge=0)
    premium_discount_pct: float | None = None
    remaining_balance: float | None = Field(default=None, ge=0)
    redeem_status: str | None = None
    rating: str | None = None
    overseas_market_date: date | None = None
    source: str
    methodology: str
    available_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    namespace: DataNamespace = DataNamespace.RESEARCH
    trust_level: DataTrustLevel = DataTrustLevel.RESEARCH_EXTERNAL
    manifest_id: str | None = None


class PointInTimePoolMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    name: str
    asset_class: str
    category: str
    eligible: bool
    exclusion_reasons: list[str] = Field(default_factory=list)
    representative: bool = False
    representative_rank: int | None = None
    amount: float | None = None
    fund_size: float | None = None
    liquidity_score: float | None = None
    premium_discount_pct: float | None = None
    overseas_market_date: date | None = None
    source: str
    available_at: datetime
    data_quality: Literal["available", "degraded", "unavailable"] = "available"
    missing_fields: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class PointInTimePoolSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_type: Literal["etf", "a_share", "convertible_bond"]
    snapshot_date: date
    cutoff_at: datetime
    protocol_version: str
    source: str
    source_version: str
    stage: EvidenceStage = EvidenceStage.RESEARCH_REPLAY
    members: list[PointInTimePoolMember]
    fingerprint: str = ""
    known_gaps: list[str] = Field(default_factory=list)
    created_at: datetime
    namespace: DataNamespace = DataNamespace.RESEARCH
    trust_level: DataTrustLevel = DataTrustLevel.RESEARCH_EXTERNAL
    manifest_id: str | None = None

    @model_validator(mode="after")
    def validate_cutoff_and_fingerprint(self) -> "PointInTimePoolSnapshot":
        future = [item.symbol for item in self.members if item.available_at > self.cutoff_at]
        if future:
            raise ValueError(f"point-in-time pool contains future data: {future[:5]}")
        calculated = self.calculate_fingerprint()
        if self.fingerprint and self.fingerprint != calculated:
            raise ValueError("point-in-time pool fingerprint does not match its contents")
        self.fingerprint = calculated
        return self

    def calculate_fingerprint(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={"fingerprint", "created_at"},
        )
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


class VariantPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant: AblationVariant
    probabilities: dict[Literal["up", "flat", "down"], float]
    action: Literal["observe", "buy", "add", "hold", "reduce", "avoid"]
    target_weight: float = Field(ge=0, le=1)
    actually_triggered: bool = False
    data_completeness: float = Field(ge=0, le=1)
    role_completeness: float = Field(ge=0, le=1)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def probabilities_sum_to_one(self) -> "VariantPrediction":
        values = [float(self.probabilities.get(key, 0.0)) for key in ("up", "flat", "down")]
        if any(value < 0 or value > 1 for value in values) or abs(sum(values) - 1.0) > 1e-6:
            raise ValueError("variant probabilities must be in [0,1] and sum to one")
        return self


__all__ = [
    "ABLATION_VARIANTS",
    "AblationVariant",
    "EvidenceStage",
    "PointInTimePoolMember",
    "PointInTimePoolSnapshot",
    "PointInTimeSecurity",
    "PointInTimeTradeStatus",
    "VariantPrediction",
]
