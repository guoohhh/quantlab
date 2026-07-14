from __future__ import annotations

import asyncio
from typing import Any

from quantlab.agents.roundtable import ExpertRoundtable, roundtable_participant_catalog
from quantlab.config import Settings
from quantlab.llm import build_provider
from quantlab.llm.providers import LLMProvider
from quantlab.persistence.roundtable import RoundtableRepository
from quantlab.persistence.sqlite import DecisionRepository


def run_expert_roundtable(
    settings: Settings,
    source_run_id: str,
    participants: list[str],
    topic: str,
    *,
    rounds: int = 2,
    save: bool = True,
    llm: LLMProvider | None = None,
) -> dict[str, Any]:
    database_path = settings.resolve(settings.get("system.database_path"))
    source_record = DecisionRepository(database_path).get(source_run_id)
    if source_record is None:
        raise ValueError(f"research run not found: {source_run_id}")

    owns_provider = llm is None
    provider = llm or build_provider(settings.section("llm"))

    async def _run():
        try:
            return await ExpertRoundtable(provider).run(
                source_run_id=source_run_id,
                source_record=source_record,
                participants=participants,
                topic=topic,
                rounds=rounds,
            )
        finally:
            if owns_provider:
                await provider.aclose()

    result = asyncio.run(_run())
    if save:
        RoundtableRepository(database_path).save(result)
    return result.model_dump(mode="json")


__all__ = ["roundtable_participant_catalog", "run_expert_roundtable"]
