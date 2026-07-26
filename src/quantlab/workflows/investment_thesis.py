from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from quantlab.config import Settings
from quantlab.domain import AnalysisContextPack
from quantlab.domain.context import EvidenceDomain, EvidenceQuality, reproducible_fingerprint
from quantlab.domain.thesis import CheckFrequency, normalize_check_frequency
from quantlab.persistence.evidence import EvidenceRepository
from quantlab.persistence.notifications import NotificationRepository
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.round8 import Round8Repository
from quantlab.persistence.round9 import Round9Repository
from quantlab.llm import await_with_provider_close, build_provider
from quantlab.llm.governance import GovernedLLMProvider, budget_from_settings
from quantlab.market import TradingCalendarService
from quantlab.workflows.experiment_recorder import ExperimentRecorder


THESIS_PROMPT_VERSION = "investment-thesis-structured-v1"
THESIS_SCHEMA_VERSION = "investment-thesis-draft-v1"


class ThesisAssumptionDraft(BaseModel):
    statement: str = Field(min_length=10, max_length=500)
    verification_metric: str = Field(min_length=3, max_length=300)
    current_evidence: list[str] = Field(default_factory=list, max_length=12)
    supporting_evidence_refs: list[str] = Field(default_factory=list, max_length=12)
    opposing_evidence_refs: list[str] = Field(default_factory=list, max_length=12)
    check_frequency: CheckFrequency
    next_check_at: str | None = None
    red_lines: list[str] = Field(default_factory=list, max_length=8)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("check_frequency", mode="before")
    @classmethod
    def normalize_frequency(cls, value: object) -> CheckFrequency:
        return normalize_check_frequency(value)


class StructuredThesisDraft(BaseModel):
    core_logic: str = Field(min_length=10, max_length=800)
    assumptions: list[ThesisAssumptionDraft] = Field(min_length=3, max_length=7)
    valuation_anchor: str = Field(min_length=1, max_length=500)
    overall_red_lines: list[str] = Field(default_factory=list, max_length=12)
    overall_invalidation_conditions: list[str] = Field(default_factory=list, max_length=12)
    data_provenance: dict[str, Any] = Field(default_factory=dict)
    needs_review: bool = True


def create_investment_thesis_from_recommendation(
    settings: Settings,
    *,
    recommendation_id: str,
    user_decision: str,
    external_trade_id: str | None = None,
) -> dict[str, Any]:
    path = settings.resolve(settings.get("system.database_path"))
    round5 = Round5Repository(path)
    lifecycle = Round8Repository(path)
    revisions = Round9Repository(path)
    recommendation = round5.recommendation(recommendation_id)
    if recommendation is None:
        raise ValueError("investor recommendation not found")
    if user_decision not in {"adopted", "partially_adopted"}:
        raise ValueError("only adopted recommendations can create an investment thesis")
    existing = lifecycle.thesis_for_recommendation(recommendation_id)
    if existing is not None:
        revised = lifecycle.revise_thesis_decision(
            recommendation_id,
            user_decision=user_decision,
            external_trade_id=external_trade_id,
        ) or existing
        history = revisions.thesis_revisions(revised["thesis_id"])
        return {
            **revised,
            "revisions": history,
            "current_revision": history[-1] if history else None,
        }

    card = recommendation.get("payload") or {}
    supporting = list(card.get("supporting_evidence") or [])
    opposing = list(card.get("opposing_evidence") or [])
    invalidation = list(card.get("invalidation_conditions") or [])
    context_id = recommendation.get("context_id")
    context_fingerprint = recommendation.get("context_fingerprint")
    research_id = card.get("research_id") or recommendation.get("research_run_id")
    parent = (
        lifecycle.run_for_link(entity_type="decision_run", entity_id=str(research_id))
        if research_id
        else None
    )
    if parent is None and context_id:
        parent = lifecycle.run_for_link(entity_type="context_pack", entity_id=str(context_id))
    recorder = ExperimentRecorder(settings)
    run = recorder.start(
        experiment_name="investment-thesis-lifecycle",
        experiment_type="investor_decision",
        run_type="thesis_creation",
        evidence_boundary="user",
        idempotency_key=f"thesis:{recommendation_id}",
        context_fingerprint=context_fingerprint,
        quote_fingerprint=card.get("quote_fingerprint"),
        parameters={"user_decision": user_decision, "symbol": recommendation["symbol"]},
        parent_run_id=parent["run_id"] if parent else None,
        workflow_version="investment-thesis-lifecycle-v2",
    )
    draft = generate_structured_thesis_draft(
        settings,
        recommendation=recommendation,
        context_id=context_id,
        context_fingerprint=context_fingerprint,
        supporting=supporting,
        opposing=opposing,
        invalidation=invalidation,
    )
    assumptions = [
        {
            "statement": item.statement,
            "verification_metric": item.verification_metric,
            "current_evidence": item.current_evidence or "unavailable",
            "status": "needs_review",
            "check_frequency": item.check_frequency,
            "next_check_at": item.next_check_at,
            "evidence_refs": [
                {"context_id": context_id, "block_id": block_id}
                for block_id in dict.fromkeys(
                    item.supporting_evidence_refs + item.opposing_evidence_refs
                )
            ],
        }
        for item in draft.assumptions
    ]
    due_dates = card.get("due_dates") or {}
    thesis = lifecycle.create_thesis(
        {
            "portfolio_id": recommendation["portfolio_id"],
            "symbol": recommendation["symbol"],
            "recommendation_id": recommendation_id,
            "research_id": research_id,
            "context_id": context_id,
            "run_id": run["run_id"],
            "initial_price": float(card.get("start_price") or 0.0),
            "core_thesis": draft.core_logic,
            "assumptions": assumptions,
            "supporting_evidence": supporting,
            "opposing_evidence": opposing,
            "red_lines": draft.overall_red_lines or invalidation,
            "invalidation_conditions": draft.overall_invalidation_conditions or invalidation,
            "valuation_anchor": draft.valuation_anchor,
            "next_check_at": due_dates.get("5"),
            "user_decision": user_decision,
            "linked_external_trade_id": external_trade_id,
            "data_provenance": {
                "context_id": context_id,
                "context_fingerprint": context_fingerprint,
                "quote_fingerprint": card.get("quote_fingerprint"),
                "as_of": recommendation.get("as_of"),
                "quality": card.get("data_reliability") or {},
                "thesis_prompt_version": THESIS_PROMPT_VERSION,
                "thesis_schema_version": THESIS_SCHEMA_VERSION,
                "needs_review": draft.needs_review,
            },
        }
    )
    revision = revisions.create_thesis_revision(
        thesis["thesis_id"],
        payload={
            "schema_version": THESIS_SCHEMA_VERSION,
            "thesis_id": thesis["thesis_id"],
            "symbol": thesis["symbol"],
            "core_logic": draft.core_logic,
            "assumptions": [item.model_dump(mode="json") for item in draft.assumptions],
            "valuation_anchor": draft.valuation_anchor,
            "overall_red_lines": draft.overall_red_lines,
            "overall_invalidation_conditions": draft.overall_invalidation_conditions,
            "data_provenance": thesis["data_provenance"],
            "needs_review": draft.needs_review,
            "user_confirmation_required": True,
        },
        source="llm_contextpack_draft" if context_id else "deterministic_needs_review_draft",
        edited_by="system",
    )
    for entity_type, entity_id, relation in (
        ("investor_recommendation", recommendation_id, "source"),
        ("investment_thesis", thesis["thesis_id"], "created"),
        ("investment_thesis_revision", revision["revision_id"], "draft"),
    ):
        recorder.link(
            run["run_id"],
            entity_type=entity_type,
            entity_id=entity_id,
            relation=relation,
        )
    if external_trade_id:
        recorder.link(
            run["run_id"],
            entity_type="external_trade",
            entity_id=external_trade_id,
            relation="linked_user_execution",
        )
    recorder.artifact(
        run["run_id"],
        artifact_type="investment_thesis_draft",
        name=revision["revision_id"],
        payload=revision,
    )
    recorder.complete(
        run["run_id"],
        result_summary={
            "thesis_id": thesis["thesis_id"],
            "revision_id": revision["revision_id"],
            "status": "draft_pending_user_confirmation",
        },
    )
    if parent:
        lifecycle.link_entity(
            parent["run_id"],
            entity_type="investment_thesis",
            entity_id=thesis["thesis_id"],
            relation="decision_lifecycle",
        )
    detail = lifecycle.thesis(thesis["thesis_id"]) or thesis
    return {
        **detail,
        "revisions": [revision],
        "current_revision": revision,
        "lifecycle_status": "draft_pending_confirmation",
    }


def generate_structured_thesis_draft(
    settings: Settings,
    *,
    recommendation: dict[str, Any],
    context_id: str | None,
    context_fingerprint: str | None,
    supporting: list[Any],
    opposing: list[Any],
    invalidation: list[Any],
) -> StructuredThesisDraft:
    path = settings.resolve(settings.get("system.database_path"))
    context_payload = EvidenceRepository(path).context(context_id) if context_id else None
    if context_payload is None:
        return _deterministic_thesis_fallback(
            recommendation,
            timezone=str(settings.get("system.timezone", "Asia/Shanghai")),
            pack=None,
            supporting=supporting,
            opposing=opposing,
            invalidation=invalidation,
        )
    pack = AnalysisContextPack.model_validate(context_payload)
    if context_fingerprint != pack.fingerprint or pack.symbol != recommendation["symbol"]:
        return _deterministic_thesis_fallback(
            recommendation,
            timezone=str(settings.get("system.timezone", "Asia/Shanghai")),
            pack=None,
            supporting=supporting,
            opposing=opposing,
            invalidation=invalidation,
        )
    provider = build_provider(settings.section("llm"))
    governed = GovernedLLMProvider(
        provider,
        EvidenceRepository(path),
        context_id=pack.context_id,
        context_fingerprint=pack.fingerprint,
        task_id=f"thesis-draft:{recommendation['recommendation_id']}:{pack.fingerprint}",
        budget=budget_from_settings(settings.section("llm")),
        prompt_version=THESIS_PROMPT_VERSION,
        schema_version=THESIS_SCHEMA_VERSION,
        governance_version="bounded-thesis-draft-v1",
    )
    system = (
        "You draft a concrete, falsifiable investment thesis using only the supplied "
        "AnalysisContextPack. Produce 3-7 assumptions with measurable verification metrics, "
        "supporting and opposing block IDs, review frequency, next check, red lines and "
        "invalidation conditions. Missing evidence must stay needs-review. Historical lessons "
        "are auxiliary hypotheses, not facts. check_frequency must be exactly one of daily, "
        "weekly, monthly, quarterly, event_driven, manual. You cannot change risk rules, "
        "orders or position limits."
    )
    prompt = json.dumps(
        {
            "recommendation": {
                "symbol": recommendation["symbol"],
                "action": recommendation["action"],
                "as_of": recommendation["as_of"],
            },
            "context": pack.llm_payload(maximum_bytes=32_000),
            "required_schema": THESIS_SCHEMA_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    try:
        draft = asyncio.run(
            await_with_provider_close(
                governed,
                governed.structured(system, prompt, StructuredThesisDraft),
            )
        )
        valid_ids = {block.block_id for block in pack.blocks}
        for assumption in draft.assumptions:
            assumption.supporting_evidence_refs = [
                item for item in assumption.supporting_evidence_refs if item in valid_ids
            ]
            assumption.opposing_evidence_refs = [
                item for item in assumption.opposing_evidence_refs if item in valid_ids
            ]
        draft.needs_review = bool(pack.review_required) or any(
            not item.supporting_evidence_refs and not item.opposing_evidence_refs
            for item in draft.assumptions
        )
        return draft
    except Exception:
        return _deterministic_thesis_fallback(
            recommendation,
            timezone=str(settings.get("system.timezone", "Asia/Shanghai")),
            pack=pack,
            supporting=supporting,
            opposing=opposing,
            invalidation=invalidation,
        )


def edit_investment_thesis_draft(
    settings: Settings,
    *,
    thesis_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    validated = StructuredThesisDraft.model_validate(payload)
    repository = Round9Repository(
        settings.resolve(settings.get("system.database_path"))
    )
    return repository.create_thesis_revision(
        thesis_id,
        payload={"schema_version": THESIS_SCHEMA_VERSION, **validated.model_dump(mode="json")},
        source="user_edit",
        edited_by="user",
    )


def freeze_investment_thesis_revision(
    settings: Settings, *, thesis_id: str, revision_id: str
) -> dict[str, Any]:
    repository = Round9Repository(
        settings.resolve(settings.get("system.database_path"))
    )
    revisions = repository.thesis_revisions(thesis_id)
    if not any(item["revision_id"] == revision_id for item in revisions):
        raise ValueError("investment thesis revision does not belong to thesis")
    revision = repository.freeze_thesis_revision(revision_id, thesis_id=thesis_id)
    lifecycle = Round8Repository(repository.path)
    thesis = lifecycle.thesis(thesis_id)
    if thesis and thesis.get("run_id"):
        lifecycle.link_entity(
            thesis["run_id"],
            entity_type="investment_thesis_revision",
            entity_id=revision_id,
            relation="frozen_by_user",
        )
    return {**revision, "thesis": lifecycle.thesis(thesis_id)}


def _deterministic_thesis_fallback(
    recommendation: dict[str, Any],
    *,
    timezone: str,
    pack: AnalysisContextPack | None,
    supporting: list[Any],
    opposing: list[Any],
    invalidation: list[Any],
) -> StructuredThesisDraft:
    block_by_domain = {
        block.domain: block
        for block in (pack.blocks if pack else [])
        if block.quality != EvidenceQuality.UNAVAILABLE
    }
    next_week = (_market_date_from_timezone(timezone) + timedelta(days=7)).isoformat()

    def refs(domain: EvidenceDomain) -> list[str]:
        block = block_by_domain.get(domain)
        return [block.block_id] if block else []

    assumptions = [
        ThesisAssumptionDraft(
            statement="最新已披露营收、利润率与现金流没有出现足以推翻投资逻辑的恶化。",
            verification_metric="最新实际披露的营收同比、利润率同比和自由现金流趋势",
            current_evidence=[str(item)[:240] for item in supporting[:2]],
            supporting_evidence_refs=refs(EvidenceDomain.FINANCIAL),
            opposing_evidence_refs=refs(EvidenceDomain.EVENT),
            check_frequency="event_driven",
            next_check_at=None,
            red_lines=["重大财务造假、审计否定或持续经营风险"],
            invalidation_conditions=["核心盈利与现金流同时持续恶化"],
        ),
        ThesisAssumptionDraft(
            statement="行业与个股资金趋势至少没有持续背离价格和基本面。",
            verification_metric="行业/个股5日与20日资金趋势、资金价格一致性和历史分位",
            current_evidence=[str(item)[:240] for item in opposing[:2]],
            supporting_evidence_refs=refs(EvidenceDomain.CAPITAL_FLOW),
            opposing_evidence_refs=refs(EvidenceDomain.CAPITAL_FLOW),
            check_frequency="weekly",
            next_check_at=next_week,
            red_lines=["连续资金恶化并伴随基本面负面事件"],
            invalidation_conditions=["资金、价格与基本面形成持续三重背离"],
        ),
        ThesisAssumptionDraft(
            statement="当前估值和价格仍位于可解释区间，且未触发监管或重大事项红线。",
            verification_metric="估值区间、初始价格锚点、监管处罚/诉讼/退市风险事件",
            current_evidence=[str(item)[:240] for item in invalidation[:3]],
            supporting_evidence_refs=refs(EvidenceDomain.VALUATION),
            opposing_evidence_refs=refs(EvidenceDomain.EVENT),
            check_frequency="monthly",
            next_check_at=next_week,
            red_lines=[str(item)[:300] for item in invalidation[:5]]
            or ["重大监管、诉讼、退市或治理风险"],
            invalidation_conditions=["估值显著超出历史区间且增长证据未同步改善"],
        ),
    ]
    start_price = (recommendation.get("payload") or {}).get("start_price")
    return StructuredThesisDraft(
        core_logic=(
            f"对{recommendation['symbol']}的{recommendation['action']}建议仅在盈利质量、"
            "资金价格一致性、估值和重大事件四类可验证条件没有失效时成立。"
        ),
        assumptions=assumptions,
        valuation_anchor=(
            f"初始原始价格锚点={start_price}; 当前估值证据必须由ContextPack复核"
            if start_price
            else "价格或估值锚点不可用，必须人工复核"
        ),
        overall_red_lines=[str(item)[:300] for item in invalidation[:8]],
        overall_invalidation_conditions=[
            "任何重大红线优先于股价上涨",
            "缺少可验证证据时不得把论文标记为强化",
        ],
        needs_review=True,
    )


def check_investment_thesis(
    settings: Settings,
    *,
    thesis_id: str,
    context_id: str | None,
    context_fingerprint: str | None,
    trigger_type: str = "manual_review",
    evidence_refs: list[dict[str, Any]],
    user_resolution: str = "confirmed",
    **legacy_untrusted_fields: Any,
) -> dict[str, Any]:
    del legacy_untrusted_fields
    if user_resolution not in {"confirmed", "ignored", "closed", "system_verified"}:
        raise ValueError("invalid thesis check user resolution")
    path = settings.resolve(settings.get("system.database_path"))
    repository = Round8Repository(path)
    thesis = repository.thesis(thesis_id)
    if thesis is None:
        raise ValueError("investment thesis not found")
    current_revision = thesis.get("current_frozen_revision")
    if current_revision is None:
        return {
            "thesis_id": thesis_id,
            "status": "waiting_for_user_confirmation",
            "lifecycle_status": thesis["status"],
            "current_frozen_revision": None,
        }
    checked_at = datetime.now(UTC)
    market_date = _market_date(settings, checked_at)
    evidence_repository = EvidenceRepository(path)
    context_payload = evidence_repository.context(context_id) if context_id else None
    if context_payload is None:
        assumption_results = [
            {
                "assumption_id": assumption["assumption_id"],
                "status": "needs_review",
                "evidence_refs": [],
            }
            for assumption in thesis["assumptions"]
        ]
        unavailable = [
            f"assumption:{item['assumption_id']}:context_evidence_unavailable"
            for item in thesis["assumptions"]
        ]
        check = repository.save_thesis_check(
            thesis_id,
            {
                "checked_at": checked_at.isoformat(),
                "context_id": context_id,
                "context_fingerprint": context_fingerprint,
                "trigger_type": trigger_type,
                "price_change_pct": None,
                "facts_changed": False,
                "red_line_triggered": False,
                "proposed_status": "unchanged",
                "final_status": thesis["status"] if user_resolution != "closed" else "closed",
                "user_resolution": user_resolution,
                "assumption_results": assumption_results,
                "evidence_refs": [],
                "unavailable_reasons": unavailable,
                "verified_evidence_snapshot": {"status": "context_unavailable"},
                "frozen_revision_id": current_revision["revision_id"],
                "frozen_revision_fingerprint": current_revision["fingerprint"],
                "schedule_status": "unavailable",
                "schedule_update_applied": False,
            },
        )
        if user_resolution == "closed":
            _resolve_closed_thesis_tasks(path, thesis_id)
        return {**check, "thesis": repository.thesis(thesis_id)}
    pack = AnalysisContextPack.model_validate(context_payload)
    if not context_fingerprint or pack.fingerprint != context_fingerprint:
        raise ValueError("ContextPack fingerprint mismatch")
    if pack.symbol != thesis["symbol"]:
        raise ValueError("ContextPack symbol does not match investment thesis")
    if pack.cutoff_at > checked_at or any(block.available_at > checked_at for block in pack.blocks):
        raise ValueError("future ContextPack evidence cannot be used for a thesis check")
    blocks = {block.block_id: block for block in pack.blocks}
    verified_refs = _verified_evidence_refs(
        evidence_refs,
        blocks=blocks,
        assumptions=thesis["assumptions"],
        checked_at=checked_at,
    )
    assumption_results = []
    unavailable = []
    for assumption in thesis["assumptions"]:
        refs = [
            item
            for item in verified_refs
            if item.get("assumption_id") == assumption["assumption_id"]
        ]
        if not refs:
            status = "needs_review"
            unavailable.append(
                f"assumption:{assumption['assumption_id']}:context_evidence_unavailable"
            )
        elif any(item["classification"] in {"contradicts", "red_line"} for item in refs):
            status = "contradicted"
        elif any(item["classification"] == "supports" for item in refs):
            status = "supported"
        else:
            status = "needs_review"
            unavailable.append(
                f"assumption:{assumption['assumption_id']}:no_effective_evidence"
            )
        assumption_results.append(
            {
                "assumption_id": assumption["assumption_id"],
                "status": status,
                "evidence_refs": [item["snapshot"] for item in refs],
            }
        )
    price_change_pct = _context_price_change(pack, float(thesis["initial_price"]))
    red_line_triggered = _red_line_triggered(thesis, verified_refs)
    facts_changed = any(
        item["classification"] in {"supports", "contradicts", "red_line"}
        and item["domain"] != EvidenceDomain.MARKET.value
        for item in verified_refs
    )
    contradicted = sum(item["status"] == "contradicted" for item in assumption_results)
    supported = sum(item["status"] == "supported" for item in assumption_results)
    if red_line_triggered:
        proposed = "broken" if contradicted else "damaged"
    elif unavailable:
        proposed = "unchanged"
    elif contradicted >= 2:
        proposed = "damaged"
    elif contradicted:
        proposed = "weakened"
    elif facts_changed and supported >= max(1, len(assumption_results) // 2):
        proposed = "strengthened"
    else:
        proposed = "unchanged"
    final_status = (
        "closed"
        if user_resolution == "closed"
        else thesis["status"]
        if user_resolution == "ignored"
        else proposed
    )
    schedule = _schedule_after_successful_check(
        settings,
        thesis=thesis,
        assumption_results=assumption_results,
        checked_at=checked_at,
        market_date=market_date,
        user_resolution=user_resolution,
        red_line_triggered=red_line_triggered,
    )
    check = repository.save_thesis_check(
        thesis_id,
        {
            "checked_at": checked_at.isoformat(),
            "context_id": context_id,
            "context_fingerprint": context_fingerprint,
            "trigger_type": trigger_type,
            "price_change_pct": price_change_pct,
            "facts_changed": facts_changed,
            "red_line_triggered": red_line_triggered,
            "proposed_status": proposed,
            "final_status": final_status,
            "user_resolution": user_resolution,
            "assumption_results": assumption_results,
            "evidence_refs": [item["snapshot"] for item in verified_refs],
            "unavailable_reasons": unavailable,
            "verified_evidence_snapshot": {
                "context_id": pack.context_id,
                "context_fingerprint": pack.fingerprint,
                "symbol": pack.symbol,
                "cutoff_at": pack.cutoff_at.isoformat(),
                "refs": [item["snapshot"] for item in verified_refs],
            },
            "frozen_revision_id": current_revision["revision_id"],
            "frozen_revision_fingerprint": current_revision["fingerprint"],
            "schedule_status": schedule["status"],
            "schedule_update_applied": schedule["applied"],
            "next_check_at": schedule["next_check_at"],
        },
    )
    if user_resolution == "closed":
        _resolve_closed_thesis_tasks(path, thesis_id)
    elif schedule["due_condition_cleared"]:
        Round9Repository(path).resolve_source_tasks(
            source_type="investment_thesis",
            source_id=thesis_id,
            task_type="investment_thesis_due_review",
            reason="thesis_check_completed",
        )
    if red_line_triggered:
        NotificationRepository(path).emit(
            event_type="investment_thesis_red_line",
            aggregate_type="investment_thesis",
            aggregate_id=thesis_id,
            payload={
                "account_id": thesis["portfolio_id"],
                "symbol": thesis["symbol"],
                "severity": "critical",
                "content": "投资论文红线已触发；即使价格上涨也必须复核基本事实。",
                "data_as_of": market_date.isoformat(),
                "action_type": "view_investment_thesis",
                "action_payload": {"thesis_id": thesis_id, "check_id": check["check_id"]},
            },
            dedup_key=f"thesis-red-line:{thesis_id}:{check['report_fingerprint']}",
        )
    elif unavailable or proposed in {"weakened", "damaged", "broken"}:
        NotificationRepository(path).emit(
            event_type="investment_thesis_review_required",
            aggregate_type="investment_thesis",
            aggregate_id=thesis_id,
            payload={
                "account_id": thesis["portfolio_id"],
                "symbol": thesis["symbol"],
                "severity": "warning",
                "content": (
                    "Investment thesis requires review because evidence is missing or "
                    "one or more frozen assumptions weakened."
                ),
                "data_as_of": market_date.isoformat(),
                "action_type": "view_investment_thesis",
                "action_payload": {"thesis_id": thesis_id, "check_id": check["check_id"]},
                "unavailable_reasons": unavailable,
            },
            dedup_key=f"thesis-review:{thesis_id}:{check['report_fingerprint']}",
        )
    return {**check, "thesis": repository.thesis(thesis_id)}


def _schedule_after_successful_check(
    settings: Settings,
    *,
    thesis: dict[str, Any],
    assumption_results: list[dict[str, Any]],
    checked_at: datetime,
    market_date: date,
    user_resolution: str,
    red_line_triggered: bool,
) -> dict[str, Any]:
    by_id = {item["assumption_id"]: item for item in thesis["assumptions"]}
    if user_resolution == "closed":
        for result in assumption_results:
            result["schedule_update_applied"] = False
            result["next_check_at"] = None
        return {
            "status": "closed",
            "applied": False,
            "next_check_at": None,
            "due_condition_cleared": True,
        }

    sessions_by_frequency = {
        "daily": 1,
        "weekly": 5,
        "monthly": 20,
        "quarterly": 60,
    }
    requires_calendar = red_line_triggered or any(
        str(by_id.get(result["assumption_id"], {}).get("check_frequency") or "").lower()
        in sessions_by_frequency
        and result.get("status") in {"supported", "contradicted"}
        for result in assumption_results
    )
    calendar = TradingCalendarService.from_settings(settings)
    next_dates: list[str] = []
    if requires_calendar:
        try:
            calendar.day(market_date, formal=True)
        except Exception:
            return {
                "status": "unavailable_trading_calendar",
                "applied": False,
                "next_check_at": thesis.get("next_check_at"),
                "due_condition_cleared": False,
            }

    applied_count = 0
    for result in assumption_results:
        assumption = by_id[result["assumption_id"]]
        current_due = assumption.get("next_check_at")
        if result.get("status") not in {"supported", "contradicted"}:
            result["schedule_update_applied"] = False
            result["next_check_at"] = current_due
            if current_due:
                next_dates.append(str(current_due))
            continue
        try:
            frequency = normalize_check_frequency(assumption.get("check_frequency"))
        except ValueError:
            result["schedule_update_applied"] = False
            result["next_check_at"] = current_due
            return {
                "status": "invalid_check_frequency",
                "applied": False,
                "next_check_at": thesis.get("next_check_at"),
                "due_condition_cleared": False,
            }
        if red_line_triggered:
            sessions = 1
        else:
            sessions = sessions_by_frequency.get(frequency)
        if sessions is None:
            next_check_at = None
        else:
            try:
                next_check_at = calendar.add_open_days(
                    market_date, sessions, formal=True
                ).isoformat()
            except Exception:
                return {
                    "status": "unavailable_trading_calendar",
                    "applied": False,
                    "next_check_at": thesis.get("next_check_at"),
                    "due_condition_cleared": False,
                }
        result["schedule_update_applied"] = True
        applied_count += 1
        result["next_check_at"] = next_check_at
        if next_check_at:
            next_dates.append(next_check_at)
    next_check_at = min(next_dates) if next_dates else None
    applied = applied_count > 0
    return {
        "status": (
            "red_line_near_term"
            if applied and red_line_triggered
            else "advanced"
            if applied
            else "not_applied"
        ),
        "applied": applied,
        "next_check_at": next_check_at if applied else thesis.get("next_check_at"),
        "due_condition_cleared": bool(
            applied and (next_check_at is None or next_check_at > market_date.isoformat())
        ),
    }


def _market_date(settings: Settings, observed_at: datetime | None = None) -> date:
    timezone = str(settings.get("system.timezone", "Asia/Shanghai"))
    return _market_date_from_timezone(timezone, observed_at)


def _market_date_from_timezone(
    timezone: str, observed_at: datetime | None = None
) -> date:
    current = observed_at or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(ZoneInfo(timezone)).date()


def _resolve_closed_thesis_tasks(path: Any, thesis_id: str) -> None:
    repository = Round9Repository(path)
    for task_type in (
        "investment_thesis_due_review",
        "thesis_weakened",
        "thesis_red_line",
    ):
        repository.resolve_source_tasks(
            source_type="investment_thesis",
            source_id=thesis_id,
            task_type=task_type,
            reason="investment_thesis_closed",
        )


def _verified_evidence_refs(
    references: list[dict[str, Any]],
    *,
    blocks: dict[str, Any],
    assumptions: list[dict[str, Any]],
    checked_at: datetime,
) -> list[dict[str, Any]]:
    assumption_ids = {item["assumption_id"] for item in assumptions}
    output: list[dict[str, Any]] = []
    for reference in references:
        block_id = str(reference.get("block_id") or reference.get("record_id") or "")
        if not block_id or block_id not in blocks:
            raise ValueError("referenced ContextPack evidence block does not exist")
        assumption_id = reference.get("assumption_id")
        if assumption_id is not None and assumption_id not in assumption_ids:
            raise ValueError("referenced thesis assumption does not exist")
        block = blocks[block_id]
        if block.available_at > checked_at:
            raise ValueError("future evidence cannot be used for a thesis check")
        classification, red_line_reason = _classify_block_for_thesis(
            block, assumption_id=assumption_id
        )
        if block.quality in {EvidenceQuality.UNAVAILABLE, EvidenceQuality.STALE}:
            classification = "missing"
        snapshot = {
            "assumption_id": assumption_id,
            "block_id": block.block_id,
            "domain": block.domain.value,
            "source": block.source,
            "as_of": block.as_of.isoformat(),
            "available_at": block.available_at.isoformat(),
            "quality": block.quality.value,
            "degraded": block.degraded,
            "missing_fields": block.missing_fields,
            "classification": classification,
            "red_line_reason": red_line_reason,
            "block_fingerprint": block.fingerprint,
        }
        snapshot["verification_fingerprint"] = reproducible_fingerprint(snapshot)
        output.append(
            {
                "assumption_id": assumption_id,
                "classification": classification,
                "domain": block.domain.value,
                "snapshot": snapshot,
            }
        )
    return output


def _classify_block_for_thesis(
    block: Any, *, assumption_id: str | None
) -> tuple[str, str | None]:
    payload = block.payload if isinstance(block.payload, dict) else {}
    evaluations = payload.get("thesis_evaluations") or payload.get("thesis_signals") or []
    if isinstance(evaluations, dict):
        evaluations = [
            {"assumption_id": key, **(value if isinstance(value, dict) else {"status": value})}
            for key, value in evaluations.items()
        ]
    for item in evaluations if isinstance(evaluations, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("assumption_id") not in {None, assumption_id}:
            continue
        if bool(item.get("red_line")):
            return "red_line", str(item.get("reason") or "structured red line")
        status = str(item.get("status") or item.get("classification") or "").lower()
        if status in {"supported", "supports", "positive"}:
            return "supports", None
        if status in {"contradicted", "contradicts", "negative"}:
            return "contradicts", None
    if bool(payload.get("red_line_triggered")):
        return "red_line", str(payload.get("red_line_reason") or "structured red line")
    if block.domain == EvidenceDomain.EVENT:
        events = payload.get("events") or []
        if any(
            str(item.get("event_type") or "").lower()
            in {"regulatory", "penalty", "litigation", "fraud", "delisting"}
            or float(item.get("impact_score") or 0) >= 0.9
            and float(item.get("sentiment") or 0) < 0
            for item in events
            if isinstance(item, dict)
        ):
            return "red_line", "material adverse event in authoritative ContextPack"
    return "neutral", None


def _red_line_triggered(
    thesis: dict[str, Any], verified_refs: list[dict[str, Any]]
) -> bool:
    del thesis
    return any(item["classification"] == "red_line" for item in verified_refs)


def _context_price_change(pack: AnalysisContextPack, initial_price: float) -> float | None:
    market = pack.block(EvidenceDomain.MARKET)
    if market is None or initial_price <= 0:
        return None
    price = market.payload.get("current_raw_price")
    if not isinstance(price, int | float) or price <= 0:
        return None
    return (float(price) / initial_price - 1.0) * 100.0


def active_investment_theses(
    settings: Settings, *, portfolio_id: str | None = None
) -> list[dict[str, Any]]:
    return Round8Repository(
        settings.resolve(settings.get("system.database_path"))
    ).theses(
        portfolio_id=portfolio_id,
        statuses=("active", "strengthened", "unchanged", "weakened", "damaged", "broken"),
    )


def _initial_assumptions(
    *,
    supporting: list[Any],
    opposing: list[Any],
    invalidation: list[Any],
    context_id: str | None,
) -> list[dict[str, Any]]:
    return [
        {
            "statement": "支持建议的核心业务或市场证据仍然成立",
            "verification_metric": "ContextPack支持证据状态",
            "current_evidence": supporting[:5] or "unavailable",
            "status": "needs_review" if not supporting else "supported_at_creation",
            "check_frequency": "monthly",
            "evidence_refs": [{"context_id": context_id, "value": item} for item in supporting[:5]],
        },
        {
            "statement": "反对证据没有恶化为决定性风险",
            "verification_metric": "ContextPack反对证据与风险等级",
            "current_evidence": opposing[:5] or "unavailable",
            "status": "needs_review",
            "check_frequency": "weekly",
            "evidence_refs": [{"context_id": context_id, "value": item} for item in opposing[:5]],
        },
        {
            "statement": "任何红线和失效条件均未触发",
            "verification_metric": "公告、财报、监管、价格和用户复评红线",
            "current_evidence": invalidation[:7] or "unavailable",
            "status": "needs_review",
            "check_frequency": "event_driven",
            "evidence_refs": [{"context_id": context_id, "value": item} for item in invalidation[:7]],
        },
    ]


__all__ = [
    "active_investment_theses",
    "check_investment_thesis",
    "create_investment_thesis_from_recommendation",
]
