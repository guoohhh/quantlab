from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal


ResearchOrigin = Literal[
    "user_interactive_research",
    "historical_research",
    "registered_forward_research",
    "strategy_shadow_research",
    "system_production_research",
    "demo_research",
    "test_research",
    "legacy_unclassified",
]


@dataclass(frozen=True)
class ResearchProvenance:
    """Caller provenance. Persistence derives settlement/training eligibility."""

    origin: ResearchOrigin = "user_interactive_research"
    requested_as_of: date | str | None = None
    evidence_stage: str = "research_only"
    registration_id: str | None = None


__all__ = ["ResearchOrigin", "ResearchProvenance"]
