from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AnalystReport(BaseModel):
    stance: Literal["bullish", "neutral", "bearish", "mixed"]
    confidence: float = Field(ge=0, le=1)
    summary: str
    evidence: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)


class DebateReport(BaseModel):
    stance: Literal["bullish", "neutral", "bearish"]
    confidence: float = Field(ge=0, le=1)
    thesis: list[str]
    rebuttals: list[str]


class ReviewReport(BaseModel):
    approved: bool
    status: Literal["approved", "needs_review", "rejected"]
    issues: list[str] = Field(default_factory=list)
    summary: str
    model_summary: str | None = None
    policy_action: str | None = None
    policy_trigger_codes: list[str] = Field(default_factory=list)
    policy_version: str = "legacy-unversioned"


class ExpertOpinion(BaseModel):
    role: str
    perspective: str
    stance: Literal["bullish", "neutral", "bearish"]
    score: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    weight: float = Field(default=1.0, ge=0)
    mode: Literal["vote", "veto_only", "strategic"] = "vote"
    veto: bool = False
    thesis: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)


class CouncilReport(BaseModel):
    tactical_score: float = Field(ge=-1, le=1)
    strategic_score: float | None = Field(default=None, ge=-1, le=1)
    combined_score: float = Field(ge=-1, le=1)
    veto_triggered: bool = False
    veto_roles: list[str] = Field(default_factory=list)
    momentum_tech_sync: bool = False
    market_regime: str
    opinions: list[ExpertOpinion]
    summary: str
