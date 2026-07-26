from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd

from quantlab.agents import MultiAgentDecisionSystem, ResearchContext
from quantlab.agents.decision_policy import (
    align_reviewer_report,
    apply_policy_result,
    evaluate_decision_policy,
)
from quantlab.agents.orchestrator import DecisionRun
from quantlab.agents.schemas import (
    AnalystReport,
    CouncilReport,
    DebateReport,
    ExpertOpinion,
    ReviewReport,
)
from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.domain import AnalysisContextPack, AssetType, CommitteeDecision
from quantlab.domain.models import AuditEvent, DecisionCard, Forecast, StrategySignal
from quantlab.factors import MomentumFactorEngine, QuantFactorReport
from quantlab.fundamentals import load_a_share_financial_report
from quantlab.llm import await_with_provider_close, build_provider
from quantlab.llm.governance import (
    GovernedLLMProvider,
    budget_for_workflow,
    workflow_plan_from_settings,
)
from quantlab.persistence import DecisionRepository, NotificationRepository
from quantlab.persistence.round9 import Round9Repository
from quantlab.workflows.capital_flow import calculate_stock_flow
from quantlab.workflows.context import (
    assemble_analysis_context_pack,
    context_repository,
    macro_evidence_from_radar,
    market_flow_block_from_radar,
)
from quantlab.learning import LearningRepository, build_predictor
from quantlab.workflows.llm_committee import (
    expected_context_committee_roles,
    run_context_committee_with_provider,
)
from quantlab.workflows.notification_rules import (
    emit_ai_view_change,
    emit_context_quality_notifications,
    emit_llm_runtime_notifications,
    evaluate_flow_notification_rules,
)
from quantlab.workflows.events import collect_all_events
from quantlab.workflows.radar import build_market_radar
from quantlab.workflows.experiment_recorder import ExperimentRecorder, checkpoint_signature
from quantlab.workflows.reflection import controlled_research_memory


def load_quant_report(
    settings: Settings,
    symbol: str,
    as_of: date | None = None,
    lookback_calendar_days: int = 500,
) -> dict:
    end = as_of or date.today()
    fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
    provider = CachedProvider(
        fallback,
        settings.resolve(settings.get("system.data_dir")) / "cache",
    )
    bars = provider.bars([symbol], end - timedelta(days=lookback_calendar_days), end)
    frame = pd.DataFrame([bar.model_dump() for bar in bars])
    if frame.empty:
        raise ValueError(f"no market data returned for {symbol}")
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame[frame["date"].dt.date <= end].sort_values("date")
    if frame.empty:
        raise ValueError(f"no market data returned for {symbol} on or before {end}")
    signal_frame = frame.copy()
    signal_frame["close"] = signal_frame["adjusted_close"].fillna(signal_frame["close"])
    effective_as_of = frame.date.max().date()
    report = MomentumFactorEngine().analyze(symbol, signal_frame, effective_as_of)
    raw_price = float(frame.iloc[-1].close)
    latest_available_at = frame.iloc[-1].get("available_at")
    if latest_available_at is not None and not pd.isna(latest_available_at):
        latest_available_at = pd.Timestamp(latest_available_at).isoformat()
    else:
        latest_available_at = datetime.combine(
            effective_as_of,
            datetime.min.time(),
            tzinfo=UTC,
        ).isoformat()
    return {
        "report": report,
        "price": raw_price,
        "as_of": effective_as_of,
        "source": provider.name,
        "degraded_sources": fallback.last_degraded_from,
        "bars": len(frame),
        "available_at": latest_available_at,
        "fetched_at": datetime.now(UTC).isoformat(),
        "price_history": build_price_history_evidence(frame, end),
    }


def analyze_symbol(
    settings: Settings,
    symbol: str,
    as_of: date | None = None,
    fundamentals: dict | None = None,
    news: list[dict] | None = None,
    asset_type: str | None = None,
    include_events: bool = False,
    account_id: str | None = None,
    include_research_memory: bool = True,
):
    quant = load_quant_report(settings, symbol, as_of)
    report: QuantFactorReport = quant["report"]
    resolved_asset_type = _asset_type(settings, symbol, asset_type)
    financial_report = None
    financial_degraded = []
    if fundamentals is None and resolved_asset_type == "stock":
        try:
            financial_report = load_a_share_financial_report(
                symbol,
                quant["as_of"],
                current_price=quant["price"],
            )
            fundamentals = financial_report.model_dump(mode="json")
        except Exception as exc:
            fundamentals = {}
            financial_degraded.append(f"financial quality data failed: {exc}")
    elif fundamentals is None:
        fundamentals = {}

    radar = None
    cross_section_factors: dict[str, float] = {}
    radar_degraded: list[str] = []
    if resolved_asset_type == "etf":
        try:
            radar = build_market_radar(settings, quant["as_of"])
            radar_row = next(
                (item for item in radar["instruments"] if item["symbol"] == symbol), None
            )
            if radar_row:
                cross_section_factors = {
                    "cross_section_momentum_20_rank": float(radar_row["momentum_20_percentile"]),
                    "cross_section_momentum_60_rank": float(radar_row["momentum_60_percentile"]),
                    "cross_section_momentum_120_rank": float(radar_row["momentum_120_percentile"]),
                    "cross_section_volatility_20_rank": max(
                        0.0,
                        min(1.0, float(radar_row["volatility_20_pct"] or 0) / 60),
                    ),
                    "cross_section_relative_return_20": float(
                        (radar_row["return_20_pct"] or 0)
                        - sum((item["return_20_pct"] or 0) for item in radar["instruments"])
                        / len(radar["instruments"])
                    )
                    / 100,
                    "cross_section_breadth_20": float(radar["breadth"]["positive_20"]),
                    "cross_section_dispersion_20": float(radar["dispersion_20_pct"]) / 100,
                    "cross_section_leadership_gap_20": float(
                        (radar_row["return_20_pct"] or 0)
                        - max((item["return_20_pct"] or 0) for item in radar["instruments"])
                    )
                    / 100,
                }
            radar_degraded.extend(radar["degraded_sources"])
        except Exception as exc:
            radar_degraded.append(f"ETF cross-section radar failed: {exc}")

    event_collection = None
    event_degraded: list[str] = []
    if news is None and include_events and resolved_asset_type == "stock":
        event_start = quant["as_of"] - timedelta(days=45)
        event_collection = collect_all_events(settings, symbol, event_start, quant["as_of"])
        event_degraded.extend(event_collection.get("degraded_sources", []))
        news = LearningRepository(
            settings.resolve(settings.get("system.database_path"))
        ).events_between(symbol, event_start.isoformat(), quant["as_of"].isoformat())
    news = news or []

    radar_row = (
        next((item for item in radar["instruments"] if item["symbol"] == symbol), None)
        if radar
        else None
    )
    signal_score = (
        max(-1.0, min(1.0, (float(radar_row["strength_score"]) - 50) / 50))
        if radar_row
        else report.composite_score
    )
    signal = StrategySignal(
        strategy="etf_rotation" if resolved_asset_type == "etf" else "factor_momentum",
        symbol=symbol,
        as_of=quant["as_of"],
        score=signal_score,
        confidence=min(0.85, 0.35 + report.regime_confidence * 0.35),
        reasons=[
            f"factor composite={report.composite_score:.3f}",
            f"MTF={report.multi_timeframe.verdict}",
            f"regime={report.regime.value}",
            *(
                [
                    f"cross-section strength={radar_row['strength_score']:.1f}",
                    f"20d rank={radar_row['rank_20']}/{len(radar['instruments'])}",
                ]
                if radar_row
                else []
            ),
        ],
    )
    all_degraded = list(
        dict.fromkeys(
            quant["degraded_sources"] + financial_degraded + radar_degraded + event_degraded
        )
    )
    portfolio = None
    if account_id:
        from quantlab.workflows.simulator import user_simulator_repository

        portfolio = user_simulator_repository(settings).overview(account_id)
    stock_flow = _stock_flow_from_quant(quant, symbol)
    valuation = _valuation_context(fundamentals or {})
    historical_lessons = (
        controlled_research_memory(settings, symbol=symbol)
        if include_research_memory
        else {
            "symbol": symbol,
            "lessons": [],
            "ablation_arm": "without_memory",
            "claim_boundary": "controlled memory disabled for this ablation arm",
        }
    )
    pack = assemble_analysis_context_pack(
        symbol=symbol,
        asset_type=AssetType(resolved_asset_type),
        as_of=quant["as_of"],
        market={
            "price": quant["price"],
            "source": quant["source"],
            "as_of": quant["as_of"],
            "available_at": quant.get("available_at"),
            "recent_raw_and_adjusted_bars_30": quant["price_history"].get(
                "recent_raw_and_adjusted_bars_30", []
            ),
        },
        technical={
            **quant["price_history"],
            "source": quant["source"],
            "available_at": quant.get("available_at"),
            "quant_factors": report.model_dump(mode="json"),
        },
        market_flow=market_flow_block_from_radar(radar, quant["as_of"]),
        industry_flow=None,
        stock_flow=stock_flow,
        financial=fundamentals or None,
        valuation=valuation,
        events=news,
        macro=macro_evidence_from_radar(radar, quant["as_of"]),
        portfolio=portfolio,
        strategy={
            "source": "quantlab_strategy_engine",
            "symbol": symbol,
            "as_of": quant["as_of"],
            "signal": signal.model_dump(mode="json"),
            "benchmark": settings.get("strategies.stock_evidence.benchmark_symbol", "sh510300"),
            "evidence_grade": "research_only",
        },
        historical_lessons=historical_lessons,
        degraded_sources=all_degraded,
        maximum_llm_payload_bytes=int(settings.get("llm.context_maximum_bytes", 48_000)),
    )
    evidence_repository = context_repository(settings)
    pack = AnalysisContextPack.model_validate(evidence_repository.save_context(pack))
    model_routing = {
        "provider": settings.get("llm.provider", "mock"),
        "model": settings.get("llm.model")
        or settings.get("llm.openai_model")
        or settings.get("llm.deepseek_model")
        or settings.get("llm.local_model"),
    }
    role_set = MultiAgentDecisionSystem.expected_llm_role_keys(
        ResearchContext(
            symbol=symbol,
            as_of=quant["as_of"],
            price=quant["price"],
            fundamentals=fundamentals or {},
            news=news,
            asset_type=resolved_asset_type,
        )
    )
    research_checkpoint_signature = checkpoint_signature(
        settings,
        workflow_structure=(
            "analyze_symbol:context_pack->primary_committee_and_reviewer"
            "->context_committee->final_decision:v2"
        ),
        model_routing=model_routing,
        prompt_version="research-committee-v1",
        context_fingerprint=pack.fingerprint,
        reasoning_effort=str(settings.get("llm.reasoning_effort", "default")),
        role_set=role_set,
        schema_version=pack.schema_version,
        key_configuration={
            "maximum_weight": settings.get("risk.max_single_position", 0.15),
            "maximum_roles": settings.get("llm.maximum_roles", 8),
            "maximum_rounds": settings.get("llm.maximum_rounds", 2),
            "include_research_memory": include_research_memory,
        },
    )
    recorder = ExperimentRecorder(settings)
    ledger_run = recorder.start(
        experiment_name="multi-agent-symbol-research",
        experiment_type="investment_research",
        run_type="multi_agent_committee",
        evidence_boundary="research_only",
        idempotency_key=f"research:{pack.fingerprint}:{research_checkpoint_signature}",
        prompt_version="research-committee-v1",
        context_fingerprint=pack.fingerprint,
        model_routing=model_routing,
        parameters={"symbol": symbol, "as_of": pack.as_of.isoformat(), "asset_type": resolved_asset_type},
        cost_budget={"maximum_task_cost_usd": settings.get("llm.maximum_task_cost_usd", 1.0)},
        workflow_version="research-decision-lifecycle-v2",
    )
    recorder.checkpointed_step(
        ledger_run["run_id"],
        step_name="context_pack_ready",
        signature=research_checkpoint_signature,
        callback=lambda: {
            "context_id": pack.context_id,
            "context_fingerprint": pack.fingerprint,
            "quality_score": pack.quality_score,
        },
    )
    used_memories = list(historical_lessons.get("lessons") or [])
    if used_memories:
        Round9Repository(
            settings.resolve(settings.get("system.database_path"))
        ).record_memory_usage(
            ledger_run["run_id"],
            used_memories,
            ablation_arm="with_memory" if include_research_memory else "without_memory",
        )
    for block in pack.blocks:
        if block.domain.value == "capital_flow":
            evidence_repository.save_flow(block)
            evaluate_flow_notification_rules(
                settings,
                block,
                account_id=account_id,
            )
    base_llm = build_provider(settings.section("llm"))
    maximum_weight = float(settings.get("risk.max_single_position", 0.15))

    research_context = ResearchContext(
        symbol=symbol,
        as_of=quant["as_of"],
        price=quant["price"],
        maximum_final_weight=maximum_weight,
        strategy_signals=[signal],
        fundamentals=fundamentals or {},
        news=news,
        quant_factors=report.model_dump(mode="json"),
        price_history=quant["price_history"],
        cross_section_factors=cross_section_factors,
        asset_type=resolved_asset_type,
        market_regime=report.regime.value,
        data_quality=pack.quality_score,
        degraded_sources=all_degraded,
        hard_vetoes=financial_report.hard_vetoes if financial_report else [],
        analysis_context_pack=pack.llm_payload(),
        context_id=pack.context_id,
        context_version=pack.schema_version,
        context_fingerprint=pack.fingerprint,
        capital_flow=stock_flow.model_dump(mode="json"),
        macro=(macro_evidence_from_radar(radar, quant["as_of"]) or {}),
        portfolio=portfolio or {},
    )
    workflow_name = f"{resolved_asset_type}_full_research"
    primary_phase_roles = MultiAgentDecisionSystem.expected_llm_phase_roles(research_context)
    context_roles = expected_context_committee_roles(
        pack,
        int(settings.get("llm.maximum_committee_roles", 6)),
    )
    workflow_plan = workflow_plan_from_settings(
        settings.section("llm"),
        workflow=workflow_name,
        phase_roles={
            **primary_phase_roles,
            "context_roles": context_roles,
            "context_synthesis": ["context_synthesis"],
        },
    )
    workflow_budget = budget_for_workflow(settings.section("llm"), workflow_name)
    llm = GovernedLLMProvider(
        base_llm,
        evidence_repository,
        context_id=pack.context_id,
        context_fingerprint=pack.fingerprint,
        task_id=f"research:{pack.fingerprint}",
        budget=workflow_budget,
        workflow_plan=workflow_plan,
    )

    async def run_primary_committee():
        llm.prepare_workflow()
        return await MultiAgentDecisionSystem(
            llm,
            build_predictor(
                settings.resolve(settings.get("system.database_path")), resolved_asset_type
            ),
        ).run(research_context)

    async def run_context_committee_step():
        llm.prepare_workflow()
        return await run_context_committee_with_provider(
            settings,
            pack=pack,
            deterministic_max_weight=maximum_weight,
            provider=llm,
        )

    async def run_checkpointed_pipeline():
        primary = await recorder.checkpointed_async_step(
            ledger_run["run_id"],
            step_name="primary_committee_and_reviewer",
            signature=research_checkpoint_signature,
            callback=run_primary_committee,
            payload_builder=_serialize_decision_run,
            result_loader=_deserialize_decision_run,
        )
        run = primary["result"]
        _rebind_decision_run_identity(run, ledger_run["run_id"])
        context_committee = await recorder.checkpointed_async_step(
            ledger_run["run_id"],
            step_name="context_committee",
            signature=research_checkpoint_signature,
            callback=run_context_committee_step,
            payload_builder=lambda value: value.model_dump(mode="json"),
            result_loader=CommitteeDecision.model_validate,
        )
        committee = context_committee["result"]

        def finalize_decision() -> dict[str, Any]:
            _apply_committee_decision(run, committee, maximum_weight)
            run.llm_audit = llm.health_snapshot()
            return {
                "decision_run": _serialize_decision_run(run),
                "committee": committee.model_dump(mode="json"),
            }

        finalized = recorder.checkpointed_step(
            ledger_run["run_id"],
            step_name="final_decision",
            signature=research_checkpoint_signature,
            callback=finalize_decision,
        )
        payload = finalized["payload"] if finalized["resumed"] else finalized["result"]
        return (
            _deserialize_decision_run(payload["decision_run"]),
            CommitteeDecision.model_validate(payload["committee"]),
        )

    try:
        run, committee = asyncio.run(
            await_with_provider_close(llm, run_checkpointed_pipeline())
        )
    except Exception as exc:
        recorder.fail(ledger_run["run_id"], error_detail=type(exc).__name__)
        raise
    recorder.link(
        ledger_run["run_id"],
        entity_type="decision_run",
        entity_id=run.run_id,
        relation="produced",
    )
    recorder.artifact(
        ledger_run["run_id"],
        artifact_type="research_decision",
        name=run.run_id,
        payload={
            "symbol": symbol,
            "as_of": pack.as_of.isoformat(),
            "decision": run.decision.model_dump(mode="json"),
            "committee": committee.model_dump(mode="json"),
        },
    )
    recorder.complete(
        ledger_run["run_id"],
        result_summary={
            "decision_run_id": run.run_id,
            "action": run.decision.action,
            "context_fingerprint": pack.fingerprint,
        },
    )
    emit_context_quality_notifications(settings, pack, account_id=account_id)
    emit_llm_runtime_notifications(
        settings,
        health=run.llm_audit,
        run_id=run.run_id,
        symbol=symbol,
        as_of=quant["as_of"].isoformat(),
        account_id=account_id,
    )
    previous = DecisionRepository(
        settings.resolve(settings.get("system.database_path"))
    ).latest_for_symbol(symbol, quant["as_of"].isoformat())
    previous_action = previous.get("action") if previous else None
    emit_ai_view_change(
        settings,
        symbol=symbol,
        current_run_id=run.run_id,
        current_action=run.decision.action,
        previous_action=previous_action,
        context_id=pack.context_id,
        as_of=quant["as_of"].isoformat(),
    )
    reviewer = run.reports.get("reviewer")
    if reviewer is not None and not reviewer.approved:
        NotificationRepository(
            settings.resolve(settings.get("system.database_path"))
        ).emit(
            event_type="reviewer_rejected",
            aggregate_type="research_run",
            aggregate_id=run.run_id,
            payload={
                "account_id": account_id,
                "symbol": symbol,
                "content": reviewer.summary,
                "data_as_of": quant["as_of"].isoformat(),
                "action_type": "view_research",
                "action_payload": {
                    "run_id": run.run_id,
                    "context_id": pack.context_id,
                    "issues": reviewer.issues,
                },
            },
            dedup_key=f"reviewer_rejected:{run.run_id}",
        )
    return {
        **quant,
        "financial_report": financial_report,
        "financial_degraded_sources": financial_degraded,
        "asset_type": resolved_asset_type,
        "market_radar": radar,
        "cross_section_factors": cross_section_factors,
        "event_collection": event_collection,
        "event_degraded_sources": event_degraded,
        "news": news,
        "analysis_context_pack": pack,
        "context_committee": committee,
        "decision_run": run,
    }


def _asset_type(settings: Settings, symbol: str, requested: str | None) -> str:
    if requested in {"stock", "etf"}:
        return requested
    universe = set(settings.get("strategies.etf_rotation.universe", []))
    return "etf" if symbol in universe else "stock"


def _stock_flow_from_quant(quant: dict[str, Any], symbol: str):
    records = []
    for item in quant["price_history"].get("recent_raw_and_adjusted_bars_30", []):
        records.append(
            {
                "symbol": symbol,
                "date": item["date"],
                "close": item.get("raw_close"),
                "amount": item.get("amount") or 0.0,
                "source": quant["source"],
                "available_at": quant.get("available_at"),
            }
        )
    return calculate_stock_flow(
        records,
        symbol=symbol,
        as_of=quant["as_of"],
        source=quant["source"],
    )


def _valuation_context(fundamentals: dict[str, Any]) -> dict[str, Any] | None:
    keys = (
        "pe_ttm",
        "pb",
        "ps_ttm",
        "market_cap",
        "net_cash",
        "free_cash_flow",
        "valuation",
    )
    values = {key: fundamentals.get(key) for key in keys if fundamentals.get(key) is not None}
    if not values:
        return None
    return {
        "source": fundamentals.get("source", "financial_provider"),
        "as_of": fundamentals.get("as_of") or fundamentals.get("report_date"),
        "available_at": fundamentals.get("available_at")
        or fundamentals.get("disclosure_date")
        or fundamentals.get("report_date"),
        **values,
    }


def _apply_committee_decision(
    run,
    committee: CommitteeDecision,
    deterministic_max_weight: float,
) -> None:
    run.reports["context_committee"] = committee
    decision = run.decision
    reviewer = run.reports.get("reviewer")
    decision.confidence = min(decision.confidence, committee.confidence)
    trace = run.decision_trace
    council = run.reports["council"]
    policy_result = evaluate_decision_policy(
        composite_score=trace["composite_score"],
        confidence=decision.confidence,
        evidence_coverage=trace["evidence_coverage"]["adjusted"],
        conflict=trace["conflict"],
        council_veto=council.veto_triggered,
        veto_roles=council.veto_roles,
        maximum_final_weight=deterministic_max_weight,
        reviewer_approved=getattr(reviewer, "approved", None),
        reviewer_issues=getattr(reviewer, "issues", ()),
        context_action=committee.action,
        context_requires_review=committee.requires_user_review,
        context_weight_max=committee.suggested_weight_max,
        price_is_executable=decision.entry_price is not None,
        signal_price_basis=decision.signal_price_basis,
    )
    apply_policy_result(decision, policy_result)
    if reviewer is not None:
        align_reviewer_report(reviewer, policy_result)
    decision.evidence_refs = committee.evidence_refs
    decision.counter_evidence_refs = committee.counter_evidence_refs
    decision.invalidation_conditions = committee.invalidation_conditions
    decision.context_id = committee.context_id
    decision.context_version = committee.context_version
    decision.context_fingerprint = committee.context_fingerprint
    decision.role_audit = [item.model_dump(mode="json") for item in committee.role_audit]
    decision.risks = list(
        dict.fromkeys(decision.risks + committee.contradictions + committee.missing_data)
    )


_REPORT_TYPES = {
    "AnalystReport": AnalystReport,
    "CouncilReport": CouncilReport,
    "DebateReport": DebateReport,
    "ExpertOpinion": ExpertOpinion,
    "ReviewReport": ReviewReport,
    "CommitteeDecision": CommitteeDecision,
}


def _serialize_structured(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return {
            "__pydantic_type__": type(value).__name__,
            "value": value.model_dump(mode="json"),
        }
    if isinstance(value, dict):
        return {str(key): _serialize_structured(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_structured(item) for item in value]
    if isinstance(value, tuple):
        return [_serialize_structured(item) for item in value]
    return value


def _deserialize_structured(value: Any) -> Any:
    if isinstance(value, dict) and "__pydantic_type__" in value:
        model = _REPORT_TYPES.get(str(value["__pydantic_type__"]))
        return model.model_validate(value["value"]) if model else value["value"]
    if isinstance(value, dict):
        return {key: _deserialize_structured(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deserialize_structured(item) for item in value]
    return value


def _serialize_decision_run(run: DecisionRun) -> dict[str, Any]:
    return {
        "schema_version": "decision-run-checkpoint-v1",
        "run_id": run.run_id,
        "reports": _serialize_structured(run.reports),
        "forecasts": [item.model_dump(mode="json") for item in run.forecasts],
        "decision": run.decision.model_dump(mode="json"),
        "decision_trace": _serialize_structured(run.decision_trace),
        "audit_log": [item.model_dump(mode="json") for item in run.audit_log],
        "learning_features": run.learning_features,
        "learning_context": _serialize_structured(run.learning_context),
        "llm_audit": _serialize_structured(run.llm_audit),
    }


def _deserialize_decision_run(payload: dict[str, Any]) -> DecisionRun:
    if payload.get("schema_version") != "decision-run-checkpoint-v1":
        raise ValueError("unsupported decision run checkpoint schema")
    return DecisionRun(
        run_id=str(payload["run_id"]),
        reports=_deserialize_structured(payload.get("reports") or {}),
        forecasts=[Forecast.model_validate(item) for item in payload.get("forecasts") or []],
        decision=DecisionCard.model_validate(payload["decision"]),
        decision_trace=_deserialize_structured(payload.get("decision_trace") or {}),
        audit_log=[AuditEvent.model_validate(item) for item in payload.get("audit_log") or []],
        learning_features={
            str(key): float(value)
            for key, value in (payload.get("learning_features") or {}).items()
        },
        learning_context=_deserialize_structured(payload.get("learning_context") or {}),
        llm_audit=_deserialize_structured(payload.get("llm_audit") or {}),
    )


def _rebind_decision_run_identity(run: DecisionRun, authoritative_run_id: str) -> None:
    """Use the unified ledger id throughout the user-visible decision lifecycle."""
    run.run_id = authoritative_run_id
    for event in run.audit_log:
        event.run_id = authoritative_run_id


def build_price_history_evidence(
    frame: pd.DataFrame,
    as_of: date | None = None,
) -> dict:
    """Build auditable price-path evidence without mixing execution and signal prices."""

    if frame.empty:
        return {}
    required = {"date", "open", "high", "low", "close"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"price history missing columns: {', '.join(missing)}")

    history = frame.copy()
    history["date"] = pd.to_datetime(history["date"], errors="coerce")
    history = history.dropna(subset=["date"])
    requested_cutoff = as_of or history["date"].max().date()
    history = history[history["date"].dt.date <= requested_cutoff]
    if history.empty:
        return {}
    if "symbol" in history.columns:
        symbols = [str(item) for item in history["symbol"].dropna().unique()]
        if len(symbols) > 1:
            raise ValueError("price history evidence requires exactly one symbol")
        symbol = symbols[0] if symbols else "unknown"
    else:
        symbol = "unknown"
    history = history.sort_values("date").drop_duplicates("date", keep="last")

    price_columns = [
        "open",
        "high",
        "low",
        "close",
        "adjusted_open",
        "adjusted_high",
        "adjusted_low",
        "adjusted_close",
        "volume",
        "amount",
    ]
    for column in price_columns:
        if column not in history.columns:
            history[column] = None
        history[column] = pd.to_numeric(history[column], errors="coerce")

    raw_close = history["close"].astype(float)
    adjusted_close = history["adjusted_close"].where(history["adjusted_close"].notna(), raw_close)
    adjusted_close = adjusted_close.astype(float)
    latest = history.iloc[-1]
    paths = {window: adjusted_close.tail(window) for window in (20, 60, 120, 250)}
    daily_returns = adjusted_close.pct_change(fill_method=None).dropna().tail(250)
    annualized_volatility = (
        float(daily_returns.std(ddof=1) * (252**0.5) * 100) if len(daily_returns) >= 2 else None
    )
    rolling_peak = paths[250].cummax()
    max_drawdown = (
        float((paths[250] / rolling_peak - 1).min() * 100)
        if not paths[250].empty
        else None
    )
    moving_averages = {
        f"ma_{window}": _window_mean(adjusted_close, window)
        for window in (5, 20, 60, 120, 250)
    }
    moving_average_relationships = {
        name: _price_level_relationship(adjusted_close.iloc[-1], average)
        for name, average in moving_averages.items()
    }

    recent_bars = []
    for _, row in history.tail(30).iterrows():
        recent_bars.append(
            {
                "date": row["date"].date().isoformat(),
                "raw_open": _finite_number(row["open"]),
                "raw_high": _finite_number(row["high"]),
                "raw_low": _finite_number(row["low"]),
                "raw_close": _finite_number(row["close"]),
                "adjusted_open": _finite_number(row["adjusted_open"]),
                "adjusted_high": _finite_number(row["adjusted_high"]),
                "adjusted_low": _finite_number(row["adjusted_low"]),
                "adjusted_close": _finite_number(row["adjusted_close"]),
                "volume": _finite_number(row["volume"], digits=2),
                "amount": _finite_number(row["amount"], digits=2),
            }
        )

    effective_cutoff = history["date"].max().date()
    return {
        "evidence_type": "market_price_history",
        "symbol": symbol,
        "requested_cutoff_date": requested_cutoff.isoformat(),
        "cutoff_date": effective_cutoff.isoformat(),
        "contains_observations_after_cutoff": False,
        "observations": int(len(history)),
        "date_range": {
            "start": history["date"].min().date().isoformat(),
            "end": effective_cutoff.isoformat(),
        },
        "price_semantics": {
            "raw_ohlc": "unadjusted market prices; use for executable price levels",
            "adjusted_ohlc": (
                "provider-supplied back-adjusted prices (AkShare uses hfq); use for returns, "
                "trend and moving averages, never as an executable quote"
            ),
            "signal_close_fallback": "adjusted_close when present, otherwise raw_close",
            "raw_and_adjusted_fields_are_separate": True,
        },
        "adjustment_availability": {
            "adjusted_close_observations": int(history["adjusted_close"].notna().sum()),
            "raw_fallback_observations": int(history["adjusted_close"].isna().sum()),
        },
        "latest": {
            "date": effective_cutoff.isoformat(),
            "raw_close": _finite_number(latest["close"]),
            "adjusted_close": _finite_number(latest["adjusted_close"]),
            "signal_close": _finite_number(adjusted_close.iloc[-1]),
            "volume": _finite_number(latest["volume"], digits=2),
            "amount": _finite_number(latest["amount"], digits=2),
        },
        "recent_raw_and_adjusted_bars_30": recent_bars,
        **{
            f"normalized_adjusted_close_path_{window}": {
                "normalization": "latest_observation=100",
                "observations": int(len(window_path)),
                "start_date": history.iloc[-len(window_path)]["date"].date().isoformat(),
                "end_date": effective_cutoff.isoformat(),
                "values": (
                    [
                        round(float(value / window_path.iloc[-1] * 100), 6)
                        for value in window_path
                    ]
                    if not window_path.empty and window_path.iloc[-1] > 0
                    else []
                ),
            }
            for window, window_path in paths.items()
        },
        "returns_adjusted_pct": {
            f"{window}_trading_days": _window_return(adjusted_close, window)
            for window in (20, 60, 120, 250)
        },
        "risk_adjusted_pct": {
            "annualized_volatility_last_250_returns": _finite_number(annualized_volatility),
            "maximum_drawdown_last_250_prices": _finite_number(max_drawdown),
            "daily_return_observations": int(len(daily_returns)),
        },
        "moving_averages_adjusted": moving_averages,
        "latest_signal_close_vs_moving_averages": moving_average_relationships,
        "raw_market_ranges": {
            f"{window}_trading_days": _raw_range(history, window)
            for window in (20, 60, 120, 250)
        },
        "average_trading_amount": {
            f"{window}_trading_days": _window_mean(history["amount"], window, digits=2)
            for window in (20, 60, 120, 250)
        },
    }


def _finite_number(value, *, digits: int = 6) -> float | None:
    if value is None or pd.isna(value):
        return None
    converted = float(value)
    if converted == float("inf") or converted == float("-inf"):
        return None
    return round(converted, digits)


def _window_return(series: pd.Series, window: int) -> float | None:
    if len(series) <= window:
        return None
    start = float(series.iloc[-window - 1])
    end = float(series.iloc[-1])
    if start <= 0:
        return None
    return round((end / start - 1) * 100, 6)


def _window_mean(series: pd.Series, window: int, *, digits: int = 6) -> float | None:
    if len(series) < window:
        return None
    values = pd.to_numeric(series.tail(window), errors="coerce").dropna()
    if len(values) < window:
        return None
    return _finite_number(values.mean(), digits=digits)


def _raw_range(history: pd.DataFrame, window: int) -> dict[str, float | None]:
    if len(history) < window:
        return {"high": None, "low": None}
    recent = history.tail(window)
    return {
        "high": _finite_number(recent["high"].max()),
        "low": _finite_number(recent["low"].min()),
    }


def _price_level_relationship(
    latest_signal_close: float,
    reference: float | None,
) -> dict[str, float | str | None]:
    latest = _finite_number(latest_signal_close)
    if latest is None or reference is None or reference <= 0:
        return {
            "latest_signal_close": latest,
            "moving_average": reference,
            "relation": "unknown",
            "distance_pct": None,
        }
    distance = (latest_signal_close / reference - 1) * 100
    relation = "equal" if abs(distance) < 1e-9 else "above" if distance > 0 else "below"
    return {
        "latest_signal_close": latest,
        "moving_average": reference,
        "relation": relation,
        "distance_pct": round(float(distance), 6),
    }
