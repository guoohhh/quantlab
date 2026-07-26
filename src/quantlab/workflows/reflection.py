from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from quantlab.config import Settings
from quantlab.persistence.round8 import Round8Repository


def record_outcome_reflection(
    settings: Settings,
    *,
    run_id: str,
    source_type: str,
    source_id: str,
    horizon_days: int,
    supporting_evidence_results: list[dict[str, Any]] | None = None,
    opposing_evidence_results: list[dict[str, Any]] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    **legacy_untrusted_fields: Any,
) -> dict[str, Any]:
    repository = Round8Repository(
        settings.resolve(settings.get("system.database_path"))
    )
    del legacy_untrusted_fields
    authority = repository.authoritative_outcome(
        run_id=run_id,
        source_type=source_type,
        source_id=source_id,
        horizon_days=horizon_days,
    )
    supporting_evidence_results = supporting_evidence_results or []
    opposing_evidence_results = opposing_evidence_results or []
    evidence_refs = evidence_refs or []
    errors = _diagnose_errors(
        raw_return_pct=float(authority["raw_return_pct"]),
        direction_correct=authority.get("direction_correct"),
        evidence_refs=evidence_refs,
        supporting=supporting_evidence_results,
        opposing=opposing_evidence_results,
    )
    lessons = _candidate_lessons(errors)
    reflection = repository.save_reflection(
        {
            "run_id": run_id,
            "source_type": source_type,
            "source_id": source_id,
            "horizon_days": horizon_days,
            "reflection": {
                "supporting_evidence_results": supporting_evidence_results,
                "opposing_evidence_results": opposing_evidence_results,
                "errors": errors,
                "policy_effect": "candidate_lessons_only",
                "automatic_strategy_change": False,
                "automatic_weight_change": False,
                "automatic_threshold_change": False,
            },
            "candidate_lessons": lessons,
            "evidence_refs": evidence_refs,
        }
    )
    memories: list[dict[str, Any]] = []
    if authority["evidence_boundary"] in {"production", "forward_shadow"}:
        memories = repository.add_memory_candidates(
            reflection["reflection_id"],
            symbol=str(authority["symbol"]),
            lessons=lessons,
            minimum_mature_samples=int(
                settings.get("learning.reflection_minimum_mature_samples", 30)
            ),
        )
    repository.link_entity(
        run_id,
        entity_type="outcome_reflection",
        entity_id=reflection["reflection_id"],
        relation="matured_result",
    )
    repository.save_artifact(
        run_id,
        artifact_type="reflection",
        name=f"{source_type}:{source_id}:{horizon_days}",
        payload=reflection,
    )
    return {**reflection, "memory_candidates": memories}


def controlled_research_memory(
    settings: Settings,
    *,
    symbol: str,
    maximum_items: int | None = None,
    maximum_bytes: int | None = None,
    lookback_days: int | None = None,
) -> dict[str, Any]:
    memories = Round8Repository(
        settings.resolve(settings.get("system.database_path"))
    ).memories(symbol)
    maximum_items = maximum_items or int(settings.get("learning.memory_maximum_items", 6))
    maximum_bytes = maximum_bytes or int(settings.get("learning.memory_maximum_bytes", 6_000))
    lookback_days = lookback_days or int(settings.get("learning.memory_lookback_days", 730))
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
    bounded: list[dict[str, Any]] = []
    used_bytes = 0
    for item in memories:
        created_at = datetime.fromisoformat(str(item["created_at"]))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if created_at < cutoff:
            continue
        classification = (
            "auxiliary"
            if item["symbol"] != symbol
            else "challenge_eligible"
            if bool(item.get("challenge_eligible"))
            else str(item.get("status") or "candidate")
        )
        entry = {
            "memory_id": item["memory_id"],
            "symbol": item["symbol"],
            "scope": item["scope"],
            "lesson": str(item["lesson"])[:1_000],
            "weight": min(float(item["weight"]), 0.25)
            if item["symbol"] != symbol
            else min(float(item["weight"]), 1.0),
            "classification": classification,
            "reflection_id": item["reflection_id"],
            "created_at": item["created_at"],
        }
        size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
        if len(bounded) >= maximum_items or used_bytes + size > maximum_bytes:
            break
        bounded.append(entry)
        used_bytes += size
    return {
        "symbol": symbol,
        "lessons": bounded,
        "same_symbol": [item for item in bounded if item["symbol"] == symbol],
        "cross_symbol_low_weight": [item for item in bounded if item["symbol"] != symbol],
        "limits": {
            "maximum_items": maximum_items,
            "maximum_bytes": maximum_bytes,
            "lookback_days": lookback_days,
            "used_items": len(bounded),
            "used_bytes": used_bytes,
        },
        "claim_boundary": (
            "Historical lessons are auxiliary hypotheses, not current facts or guarantees. Only "
            "lessons linked to matured production/forward_shadow outcomes are returned. They cannot "
            "automatically change strategy, role weights, thresholds or risk rules."
        ),
    }


def _diagnose_errors(
    *,
    raw_return_pct: float,
    direction_correct: bool | None,
    evidence_refs: list[dict[str, Any]],
    supporting: list[dict[str, Any]],
    opposing: list[dict[str, Any]],
) -> list[str]:
    errors = []
    if not evidence_refs:
        errors.append("data_missing")
    if direction_correct is False:
        errors.append("direction_reasoning_error")
    if any(item.get("available_at_after_decision") for item in evidence_refs):
        errors.append("timing_error")
    if any(float(item.get("confidence") or 0) > 0.8 and item.get("correct") is False for item in supporting):
        errors.append("overconfidence")
    if raw_return_pct < 0 and not opposing:
        errors.append("risk_omission")
    return errors or ["no_material_error_identified"]


def _candidate_lessons(errors: list[str]) -> list[str]:
    mapping = {
        "data_missing": "缺少证据时保持needs_review，不得补造事实。",
        "direction_reasoning_error": "复核方向判断中被忽略的反证和市场状态。",
        "timing_error": "研究只能使用决策时已经available的证据。",
        "overconfidence": "在相似证据冲突下压低软判断置信度。",
        "risk_omission": "下一次研究必须显式检查未覆盖的下行情景。",
        "no_material_error_identified": "保留本次结构化证据链，等待更多到期样本再挑战。",
    }
    return [mapping[item] for item in errors]


__all__ = ["controlled_research_memory", "record_outcome_reflection"]
