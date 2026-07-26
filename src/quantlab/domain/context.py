from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from quantlab.domain.models import AssetType


CONTEXT_SCHEMA_VERSION = "2.0"
EVIDENCE_BLOCK_VERSION = "1.0"


class EvidenceDomain(str, Enum):
    MARKET = "market"
    TECHNICAL = "technical"
    CAPITAL_FLOW = "capital_flow"
    FINANCIAL = "financial"
    VALUATION = "valuation"
    EVENT = "event"
    MACRO = "macro"
    PORTFOLIO = "portfolio"
    STRATEGY = "strategy"
    GOVERNANCE = "governance"
    MEMORY = "memory"


LLM_CONTEXT_COMPRESSION_PRIORITY = {
    EvidenceDomain.MARKET.value: 100,
    EvidenceDomain.PORTFOLIO.value: 100,
    EvidenceDomain.STRATEGY.value: 100,
    EvidenceDomain.GOVERNANCE.value: 95,
    EvidenceDomain.FINANCIAL.value: 90,
    EvidenceDomain.VALUATION.value: 85,
    EvidenceDomain.TECHNICAL.value: 80,
    EvidenceDomain.CAPITAL_FLOW.value: 75,
    EvidenceDomain.EVENT.value: 60,
    EvidenceDomain.MACRO.value: 55,
    EvidenceDomain.MEMORY.value: 40,
}


class EvidenceQuality(str, Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"


class EvidenceBlock(BaseModel):
    block_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    domain: EvidenceDomain
    title: str
    source: str
    methodology: str
    as_of: datetime
    available_at: datetime
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    freshness_seconds: int = Field(default=0, ge=0)
    freshness: Literal["fresh", "stale", "unknown"] = "unknown"
    quality: EvidenceQuality
    degraded: bool = False
    estimated: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    missing_reason: str | None = None
    version: str = EVIDENCE_BLOCK_VERSION
    payload: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str = ""

    @model_validator(mode="after")
    def finalize(self) -> "EvidenceBlock":
        if self.quality == EvidenceQuality.UNAVAILABLE and not self.missing_reason:
            raise ValueError("unavailable evidence requires a missing reason")
        if self.quality in {
            EvidenceQuality.DEGRADED,
            EvidenceQuality.STALE,
            EvidenceQuality.CONFLICT,
        }:
            self.degraded = True
        self.freshness_seconds = max(
            0,
            int((self.fetched_at - self.available_at).total_seconds()),
        )
        if not self.fingerprint:
            self.fingerprint = reproducible_fingerprint(
                {
                    "domain": self.domain.value,
                    "source": self.source,
                    "methodology": self.methodology,
                    "as_of": self.as_of.isoformat(),
                    "available_at": self.available_at.isoformat(),
                    "quality": self.quality.value,
                    "estimated": self.estimated,
                    "missing_fields": self.missing_fields,
                    "version": self.version,
                    "payload": self.payload,
                }
            )
        return self


class AnalysisContextPack(BaseModel):
    context_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: str = CONTEXT_SCHEMA_VERSION
    symbol: str
    asset_type: AssetType
    as_of: date
    cutoff_at: datetime
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    blocks: list[EvidenceBlock] = Field(default_factory=list, max_length=32)
    quality_score: float = Field(default=0.0, ge=0, le=1)
    review_required: bool = True
    critical_gaps: list[str] = Field(default_factory=list)
    deterministic_summary: dict[str, Any] = Field(default_factory=dict)
    maximum_llm_payload_bytes: int = Field(default=48_000, ge=4_000, le=200_000)
    fingerprint: str = ""
    byte_size: int = 0

    @model_validator(mode="after")
    def validate_temporal_boundary(self) -> "AnalysisContextPack":
        future = [
            block.block_id
            for block in self.blocks
            if block.available_at > self.cutoff_at or block.as_of > self.cutoff_at
        ]
        if future:
            raise ValueError(
                "evidence available after context cutoff is forbidden: " + ",".join(future)
            )
        self.critical_gaps = list(dict.fromkeys(self.critical_gaps))
        self.quality_score = calculate_context_quality(self.blocks)
        self.review_required = self.quality_score < 0.70 or bool(self.critical_gaps)
        canonical = {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "asset_type": self.asset_type.value,
            "as_of": self.as_of.isoformat(),
            "cutoff_at": self.cutoff_at.isoformat(),
            "blocks": [
                {
                    "domain": block.domain.value,
                    "title": block.title,
                    "source": block.source,
                    "methodology": block.methodology,
                    "as_of": block.as_of.isoformat(),
                    "available_at": block.available_at.isoformat(),
                    "quality": block.quality.value,
                    "estimated": block.estimated,
                    "version": block.version,
                    "fingerprint": block.fingerprint,
                }
                for block in self.blocks
            ],
            "critical_gaps": self.critical_gaps,
            "deterministic_summary": self.deterministic_summary,
        }
        self.byte_size = len(
            json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if not self.fingerprint:
            self.fingerprint = reproducible_fingerprint(canonical)
        return self

    def block(self, domain: EvidenceDomain | str) -> EvidenceBlock | None:
        value = domain.value if isinstance(domain, EvidenceDomain) else str(domain)
        return next((item for item in self.blocks if item.domain.value == value), None)

    def llm_payload(self, maximum_bytes: int | None = None) -> dict[str, Any]:
        limit = maximum_bytes or self.maximum_llm_payload_bytes
        compact_blocks: list[dict[str, Any]] = []
        for block in self.blocks:
            compact_blocks.append(
                {
                    "block_id": block.block_id,
                    "domain": block.domain.value,
                    "source": block.source,
                    "methodology": block.methodology,
                    "as_of": block.as_of.isoformat(),
                    "available_at": block.available_at.isoformat(),
                    "quality": block.quality.value,
                    "degraded": block.degraded,
                    "estimated": block.estimated,
                    "missing_fields": block.missing_fields,
                    "missing_reason": block.missing_reason,
                    "fingerprint": block.fingerprint,
                    "payload": deterministic_compress(block.payload),
                }
            )
        payload = {
            "context_id": self.context_id,
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "asset_type": self.asset_type.value,
            "as_of": self.as_of.isoformat(),
            "cutoff_at": self.cutoff_at.isoformat(),
            "quality_score": self.quality_score,
            "review_required": self.review_required,
            "critical_gaps": self.critical_gaps,
            "deterministic_summary": self.deterministic_summary,
            "blocks": compact_blocks,
            "fingerprint": self.fingerprint,
        }
        compression_order = sorted(
            range(len(payload["blocks"])),
            key=lambda index: (
                LLM_CONTEXT_COMPRESSION_PRIORITY.get(
                    str(payload["blocks"][index].get("domain")), 0
                ),
                -encoded_size(payload["blocks"][index].get("payload", {})),
            ),
        )
        for index in compression_order:
            if encoded_size(payload) <= limit:
                break
            block = payload["blocks"][index]
            block["payload"] = deterministic_compress(block["payload"], aggressive=True)
            if encoded_size(payload) > limit:
                block["payload"] = {
                    "compression": "summary_only",
                    "keys": sorted(block["payload"].keys())[:30]
                    if isinstance(block["payload"], dict)
                    else [],
                }
        payload["compressed"] = self.byte_size > encoded_size(payload)
        payload["payload_bytes"] = encoded_size(payload)
        return payload


class CommitteeRoleOpinion(BaseModel):
    role: str
    stance: Literal["bullish", "neutral", "bearish", "mixed"]
    confidence: float = Field(ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    summary: str
    evidence_refs: list[str] = Field(default_factory=list, max_length=12)
    counter_evidence_refs: list[str] = Field(default_factory=list, max_length=12)
    contradictions: list[str] = Field(default_factory=list, max_length=12)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=12)
    suggested_weight: float = Field(default=0.0, ge=0, le=1)
    missing_data: list[str] = Field(default_factory=list, max_length=20)


class CommitteeDecision(BaseModel):
    action: Literal["observe", "buy", "add", "hold", "reduce", "avoid", "review_required"]
    confidence: float = Field(ge=0, le=1)
    suggested_weight_min: float = Field(default=0.0, ge=0, le=1)
    suggested_weight_max: float = Field(default=0.0, ge=0, le=1)
    deterministic_max_weight: float = Field(default=0.0, ge=0, le=1)
    bull_scenario: list[str] = Field(default_factory=list, max_length=12)
    bear_scenario: list[str] = Field(default_factory=list, max_length=12)
    evidence_refs: list[str] = Field(default_factory=list, max_length=24)
    counter_evidence_refs: list[str] = Field(default_factory=list, max_length=24)
    contradictions: list[str] = Field(default_factory=list, max_length=20)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=20)
    missing_data: list[str] = Field(default_factory=list, max_length=30)
    requires_user_review: bool = True
    context_id: str
    context_version: str
    context_fingerprint: str
    role_audit: list[CommitteeRoleOpinion] = Field(default_factory=list)
    degraded_roles: list[str] = Field(default_factory=list)
    role_weights: dict[str, float] = Field(default_factory=dict)
    governance_version: str = "default-role-policy-v1"
    governance_market_regime: str | None = None
    governance_aggregate_score: float = Field(default=0.0, ge=-1, le=1)
    cached: bool = False

    @model_validator(mode="after")
    def enforce_weight_bounds(self) -> "CommitteeDecision":
        self.suggested_weight_max = min(
            self.suggested_weight_max,
            self.deterministic_max_weight,
        )
        self.suggested_weight_min = min(
            self.suggested_weight_min,
            self.suggested_weight_max,
        )
        if self.action not in {"buy", "add", "hold"}:
            self.suggested_weight_min = 0.0
            self.suggested_weight_max = 0.0
        return self


class ChatEvidenceAnswer(BaseModel):
    answer: str
    facts: list[str] = Field(default_factory=list, max_length=20)
    quantitative_results: list[str] = Field(default_factory=list, max_length=20)
    llm_judgments: list[str] = Field(default_factory=list, max_length=20)
    user_assumptions: list[str] = Field(default_factory=list, max_length=20)
    evidence_refs: list[str] = Field(default_factory=list, max_length=30)
    missing_data: list[str] = Field(default_factory=list, max_length=30)
    suggested_action: Literal[
        "observe", "buy", "add", "hold", "reduce", "avoid", "review_required", "none"
    ] = "none"
    suggested_weight_min: float = Field(default=0.0, ge=0, le=1)
    suggested_weight_max: float = Field(default=0.0, ge=0, le=1)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=20)
    requires_user_review: bool = True


def reproducible_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def calculate_context_quality(blocks: list[EvidenceBlock]) -> float:
    if not blocks:
        return 0.0
    scores = {
        EvidenceQuality.AVAILABLE: 1.0,
        EvidenceQuality.DEGRADED: 0.65,
        EvidenceQuality.STALE: 0.35,
        EvidenceQuality.CONFLICT: 0.30,
        EvidenceQuality.UNAVAILABLE: 0.0,
    }
    weighted = []
    for block in blocks:
        if block.domain == EvidenceDomain.MEMORY:
            continue
        if block.quality == EvidenceQuality.UNAVAILABLE:
            continue
        importance = 1.5 if block.domain in {
            EvidenceDomain.MARKET,
            EvidenceDomain.TECHNICAL,
            EvidenceDomain.PORTFOLIO,
            EvidenceDomain.STRATEGY,
        } else 1.0
        weighted.append((scores[block.quality], importance))
    denominator = sum(weight for _, weight in weighted)
    return (
        round(sum(score * weight for score, weight in weighted) / denominator, 4)
        if denominator > 0
        else 0.0
    )


def deterministic_compress(value: Any, *, aggressive: bool = False, depth: int = 0) -> Any:
    if depth >= (3 if aggressive else 5):
        return "[compressed]"
    if isinstance(value, dict):
        keys = sorted(value)[: (20 if aggressive else 50)]
        return {
            key: deterministic_compress(value[key], aggressive=aggressive, depth=depth + 1)
            for key in keys
        }
    if isinstance(value, list):
        limit = 12 if aggressive else 40
        if len(value) <= limit:
            return [
                deterministic_compress(item, aggressive=aggressive, depth=depth + 1)
                for item in value
            ]
        head = limit // 2
        tail = limit - head
        return [
            *[
                deterministic_compress(item, aggressive=aggressive, depth=depth + 1)
                for item in value[:head]
            ],
            {"compressed_items": len(value) - limit},
            *[
                deterministic_compress(item, aggressive=aggressive, depth=depth + 1)
                for item in value[-tail:]
            ],
        ]
    if isinstance(value, str):
        maximum = 400 if aggressive else 1_200
        return value if len(value) <= maximum else value[:maximum] + "…"
    return value


def encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


__all__ = [
    "AnalysisContextPack",
    "CONTEXT_SCHEMA_VERSION",
    "ChatEvidenceAnswer",
    "CommitteeDecision",
    "CommitteeRoleOpinion",
    "EvidenceBlock",
    "EvidenceDomain",
    "EvidenceQuality",
    "calculate_context_quality",
    "deterministic_compress",
    "reproducible_fingerprint",
]
