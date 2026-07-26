from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from quantlab.config import Settings
from quantlab.domain.context import AnalysisContextPack, EvidenceBlock, EvidenceQuality
from quantlab.persistence import EvidenceRepository, NotificationRepository


def evaluate_flow_notification_rules(
    settings: Settings,
    block: EvidenceBlock | dict[str, Any],
    *,
    account_id: str | None = None,
) -> list[dict[str, Any]]:
    evidence = block if isinstance(block, EvidenceBlock) else EvidenceBlock.model_validate(block)
    notifications = NotificationRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    repository = EvidenceRepository(notifications.path)
    scope = str(evidence.payload.get("scope") or "unknown")
    scope_key = str(evidence.payload.get("scope_key") or "unknown")
    rules = notifications.rules(account_id=account_id, enabled_only=True)
    emitted: list[dict[str, Any]] = []
    for rule in rules:
        if rule.get("symbol") and scope == "stock" and rule["symbol"] != scope_key:
            continue
        if rule.get("industry") and scope == "industry" and rule["industry"] != scope_key:
            continue
        if not _cooldown_ready(rule):
            continue
        history = repository.flows(
            scope,
            scope_key=scope_key,
            as_of=evidence.as_of.date().isoformat(),
            limit=max(20, int(rule["consecutive_periods"])),
        )
        triggered, event_type, value, formula = _evaluate_rule(rule, evidence, history)
        if not triggered:
            continue
        triggered_at = datetime.now(UTC)
        if not notifications.mark_rule_triggered(rule["rule_id"], triggered_at):
            continue
        notifications.emit(
            event_type=event_type,
            aggregate_type="notification_rule",
            aggregate_id=rule["rule_id"],
            payload={
                "account_id": rule.get("account_id") or account_id,
                "symbol": scope_key if scope == "stock" else None,
                "content": (
                    f"规则{rule['rule_type']}触发，值={value}，"
                    f"阈值={rule.get('threshold')}，来源={evidence.source}"
                ),
                "data_as_of": evidence.as_of.isoformat(),
                "action_type": "view_capital_flow",
                "action_payload": {
                    "rule_id": rule["rule_id"],
                    "scope": scope,
                    "scope_key": scope_key,
                    "trigger_value": value,
                    "threshold": rule.get("threshold"),
                    "consecutive_periods": rule["consecutive_periods"],
                    "source": evidence.source,
                    "methodology": evidence.methodology,
                    "evidence_fingerprint": evidence.fingerprint,
                    "formula": formula,
                },
            },
            dedup_key=(
                f"flow_rule:{rule['rule_id']}:{evidence.as_of.date().isoformat()}:"
                f"{evidence.fingerprint}"
            ),
        )
        emitted.append(
            {
                "rule_id": rule["rule_id"],
                "event_type": event_type,
                "trigger_value": value,
                "threshold": rule.get("threshold"),
                "formula": formula,
            }
        )
    return emitted


def emit_context_quality_notifications(
    settings: Settings,
    pack: AnalysisContextPack | dict[str, Any],
    *,
    account_id: str | None = None,
) -> list[str]:
    context = pack if isinstance(pack, AnalysisContextPack) else AnalysisContextPack.model_validate(pack)
    repository = NotificationRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    emitted: list[str] = []
    if context.critical_gaps:
        repository.emit(
            event_type="context_evidence_missing",
            aggregate_type="analysis_context",
            aggregate_id=context.context_id,
            payload={
                "account_id": account_id,
                "symbol": context.symbol,
                "content": "；".join(context.critical_gaps[:8]),
                "data_as_of": context.as_of.isoformat(),
                "action_type": "view_context_pack",
                "action_payload": {
                    "context_id": context.context_id,
                    "context_version": context.schema_version,
                    "quality_score": context.quality_score,
                    "critical_gaps": context.critical_gaps,
                },
            },
            dedup_key=f"context_missing:{context.fingerprint}",
        )
        emitted.append("context_evidence_missing")
    degraded = [
        block
        for block in context.blocks
        if block.quality in {EvidenceQuality.DEGRADED, EvidenceQuality.STALE}
    ]
    if degraded:
        repository.emit(
            event_type="data_source_degraded",
            aggregate_type="analysis_context",
            aggregate_id=context.context_id,
            payload={
                "account_id": account_id,
                "symbol": context.symbol,
                "content": "；".join(
                    f"{block.domain.value}:{block.source}:{block.quality.value}"
                    for block in degraded[:8]
                ),
                "data_as_of": context.as_of.isoformat(),
                "action_type": "view_context_pack",
                "action_payload": {
                    "context_id": context.context_id,
                    "evidence": [
                        {
                            "block_id": block.block_id,
                            "source": block.source,
                            "quality": block.quality.value,
                            "available_at": block.available_at.isoformat(),
                        }
                        for block in degraded
                    ],
                },
            },
            dedup_key=f"context_degraded:{context.fingerprint}",
        )
        emitted.append("data_source_degraded")
    conflicts = [block for block in context.blocks if block.quality == EvidenceQuality.CONFLICT]
    if conflicts:
        repository.emit(
            event_type="data_source_conflict",
            aggregate_type="analysis_context",
            aggregate_id=context.context_id,
            payload={
                "account_id": account_id,
                "symbol": context.symbol,
                "content": ";".join(
                    f"{block.domain.value}:{block.source}" for block in conflicts[:8]
                ),
                "data_as_of": context.as_of.isoformat(),
                "action_type": "view_context_pack",
                "action_payload": {
                    "context_id": context.context_id,
                    "context_version": context.schema_version,
                    "conflicting_evidence": [
                        {
                            "block_id": block.block_id,
                            "source": block.source,
                            "methodology": block.methodology,
                            "fingerprint": block.fingerprint,
                        }
                        for block in conflicts
                    ],
                },
            },
            dedup_key=f"context_conflict:{context.fingerprint}",
        )
        emitted.append("data_source_conflict")
    return emitted


def emit_llm_runtime_notifications(
    settings: Settings,
    *,
    health: dict[str, Any],
    run_id: str,
    symbol: str,
    as_of: str,
    account_id: str | None = None,
) -> list[str]:
    repository = NotificationRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    emitted: list[str] = []
    governance = health.get("governance", {})
    budget = governance.get("budget", {})
    usage = governance.get("usage", {})
    reached = (
        int(usage.get("calls", 0)) >= int(budget.get("maximum_calls", 10**9))
        or int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        >= int(budget.get("maximum_total_tokens", 10**18))
        or float(usage.get("cost_usd", 0.0))
        >= float(budget.get("maximum_cost_usd", float("inf")))
    )
    if reached:
        repository.emit(
            event_type="llm_budget_reached",
            aggregate_type="research_run",
            aggregate_id=run_id,
            payload={
                "account_id": account_id,
                "symbol": symbol,
                "research_run_id": run_id,
                "data_as_of": as_of,
                "action_type": "view_llm_audit",
                "action_payload": {"usage": usage, "budget": budget},
            },
            dedup_key=f"llm_budget_reached:{governance.get('task_id') or run_id}",
        )
        emitted.append("llm_budget_reached")
    calls = health.get("recent_call_log", [])
    errors = [item for item in calls if item.get("status") == "error"]
    successes = [item for item in calls if item.get("status") == "ok"]
    if errors and successes:
        repository.emit(
            event_type="provider_fallback",
            aggregate_type="research_run",
            aggregate_id=run_id,
            payload={
                "account_id": account_id,
                "symbol": symbol,
                "research_run_id": run_id,
                "data_as_of": as_of,
                "action_type": "view_llm_audit",
                "action_payload": {
                    "failed_endpoints": sorted(
                        {str(item.get("endpoint_id")) for item in errors}
                    ),
                    "successful_endpoints": sorted(
                        {str(item.get("endpoint_id")) for item in successes}
                    ),
                    "calls": calls[-20:],
                },
            },
            dedup_key=f"provider_fallback:{run_id}",
        )
        emitted.append("provider_fallback")
    return emitted


def emit_ai_view_change(
    settings: Settings,
    *,
    symbol: str,
    current_run_id: str,
    current_action: str,
    previous_action: str | None,
    context_id: str,
    as_of: str,
) -> bool:
    if not previous_action or previous_action == current_action:
        return False
    NotificationRepository(
        settings.resolve(settings.get("system.database_path"))
    ).emit(
        event_type="ai_view_changed",
        aggregate_type="research_run",
        aggregate_id=current_run_id,
        payload={
            "symbol": symbol,
            "content": f"AI观点由{previous_action}变为{current_action}",
            "data_as_of": as_of,
            "action_type": "view_research",
            "action_payload": {
                "run_id": current_run_id,
                "context_id": context_id,
                "previous_action": previous_action,
                "current_action": current_action,
            },
        },
        dedup_key=f"ai_view_changed:{symbol}:{previous_action}:{current_action}:{as_of}",
    )
    return True


def _evaluate_rule(
    rule: dict[str, Any],
    current: EvidenceBlock,
    history: list[dict[str, Any]],
) -> tuple[bool, str, Any, str]:
    rule_type = str(rule["rule_type"])
    threshold = float(rule["threshold"] or 0)
    periods = int(rule["consecutive_periods"])
    payload = current.payload
    if current.quality == EvidenceQuality.UNAVAILABLE:
        return (
            rule_type == "flow_data_unavailable",
            "flow_data_unavailable",
            current.quality.value,
            "current evidence quality == unavailable",
        )
    if rule_type == "market_turnover_ratio_above":
        value = payload.get("turnover", {}).get("ratio_to_20")
        return bool(value is not None and float(value) >= threshold), "market_flow_changed", value, "latest turnover / 20-day mean"
    if rule_type in {"flow_positive_streak", "flow_negative_streak"}:
        values = [
            _flow_value(item.get("payload", item))
            for item in history[:periods]
        ]
        if len(values) < periods or any(value is None for value in values):
            return False, "industry_flow_streak", values, "insufficient persisted periods"
        positive = rule_type == "flow_positive_streak"
        triggered = all(float(value) >= threshold for value in values) if positive else all(
            float(value) <= -abs(threshold) for value in values
        )
        return triggered, "industry_flow_streak", values, f"{periods} consecutive one-period flow values"
    if rule_type == "flow_price_divergence":
        value = payload.get("flow_price_relation") or payload.get("flow_price_consistency")
        return value in {"price_flow_divergence", "inflow_not_confirmed"}, "flow_price_divergence", value, "deterministic sign comparison of 5-day flow and price return"
    return False, "market_flow_changed", None, "unsupported rule type"


def _flow_value(payload: dict[str, Any]) -> float | None:
    value = payload.get("flow_trend", {}).get("1")
    return float(value) if value is not None else None


def _cooldown_ready(rule: dict[str, Any]) -> bool:
    if not rule.get("last_triggered_at"):
        return True
    last = datetime.fromisoformat(rule["last_triggered_at"])
    return datetime.now(UTC) >= last + timedelta(seconds=int(rule["cooldown_seconds"]))


__all__ = [
    "emit_ai_view_change",
    "emit_context_quality_notifications",
    "emit_llm_runtime_notifications",
    "evaluate_flow_notification_rules",
]
