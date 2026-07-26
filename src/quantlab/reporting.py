from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from quantlab.security import sanitize_for_export


DISCLAIMER = (
    "本报告仅用于研究和辅助决策，不构成投资建议。历史数据、回测结果、统计模型与 "
    "LLM Agent 结论均不能保证未来收益；系统不会连接券商，任何交易必须由用户人工复核。"
)


def build_research_audit_package(output: dict[str, Any]) -> dict[str, Any]:
    run = output["decision_run"]
    factor_report = output["report"].model_dump(mode="json")
    financial_report = (
        output["financial_report"].model_dump(mode="json")
        if output.get("financial_report")
        else None
    )
    return _sanitize_audit(
        {
            "schema_version": "2.0",
            "report_type": "quantlab_research_audit",
            "generated_at": datetime.now(UTC).isoformat(),
            "run_id": run.run_id,
            "symbol": run.decision.symbol,
            "as_of": run.decision.as_of.isoformat(),
            "data": {
                "source": output.get("source", "unknown"),
                "bars": output.get("bars", 0),
                "price": output.get("price"),
                "effective_as_of": str(output.get("as_of", run.decision.as_of)),
                "requested_as_of": (
                    (output.get("price_history") or {}).get("requested_cutoff_date")
                ),
                "degraded_sources": list(
                    dict.fromkeys(
                        output.get("degraded_sources", [])
                        + output.get("financial_degraded_sources", [])
                        + output.get("event_degraded_sources", [])
                    )
                ),
                "event_collection": output.get("event_collection"),
            },
            "factor_report": factor_report,
            "price_history": output.get("price_history", {}),
            "financial_report": financial_report,
            "analysis_context_pack": (
                output["analysis_context_pack"].model_dump(mode="json")
                if hasattr(output.get("analysis_context_pack"), "model_dump")
                else output.get("analysis_context_pack")
            ),
            "context_committee": (
                output["context_committee"].model_dump(mode="json")
                if hasattr(output.get("context_committee"), "model_dump")
                else output.get("context_committee")
            ),
            "agent_reports": {
                name: report.model_dump(mode="json") for name, report in run.reports.items()
            },
            "forecasts": [item.model_dump(mode="json") for item in run.forecasts],
            "decision": run.decision.model_dump(mode="json"),
            "decision_trace": run.decision_trace,
            "audit_log": [item.model_dump(mode="json") for item in run.audit_log],
            "llm_audit": _sanitize_audit(run.llm_audit),
            "learning": {
                "features": run.learning_features,
                "context": run.learning_context,
            },
            "research_identity": {
                "symbol": run.decision.symbol,
                "requested_as_of": (
                    (output.get("price_history") or {}).get("requested_cutoff_date")
                ),
                "effective_as_of": str(output.get("as_of", run.decision.as_of)),
                "run_id": run.run_id,
                "origin": "user_interactive_research",
                "evidence_stage": "research_only",
            },
            "execution_boundary": "manual_orders_only",
            "disclaimer": DISCLAIMER,
        }
    )


def research_persistence_context(output: dict[str, Any]) -> dict[str, Any]:
    package = build_research_audit_package(output)
    return _sanitize_audit(
        {
            key: package[key]
            for key in (
                "schema_version",
                "report_type",
                "generated_at",
                "data",
                "factor_report",
                "price_history",
                "financial_report",
                "analysis_context_pack",
                "context_committee",
                "execution_boundary",
                "disclaimer",
                "research_identity",
            )
        }
    )


def build_stored_audit_package(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload", {})
    context = payload.get("research_context", {})
    decision = payload.get("decision", {})
    return {
        "schema_version": context.get("schema_version", "1.0"),
        "report_type": context.get("report_type", "quantlab_research_audit"),
        "generated_at": context.get("generated_at") or record.get("created_at"),
        "run_id": record.get("run_id"),
        "symbol": record.get("symbol") or decision.get("symbol"),
        "as_of": record.get("as_of") or decision.get("as_of"),
        "data": context.get("data", {}),
        "factor_report": context.get("factor_report"),
        "price_history": context.get("price_history", {}),
        "financial_report": context.get("financial_report"),
        "analysis_context_pack": context.get("analysis_context_pack"),
        "context_committee": context.get("context_committee"),
        "agent_reports": payload.get("reports", {}),
        "forecasts": payload.get("forecasts", []),
        "decision": decision,
        "decision_trace": payload.get("decision_trace", {}),
        "audit_log": payload.get("audit_log", []),
        "llm_audit": _sanitize_audit(payload.get("llm_audit", {})),
        "execution_boundary": context.get("execution_boundary", "manual_orders_only"),
        "disclaimer": context.get("disclaimer", DISCLAIMER),
        "research_identity": {
            "symbol": record.get("symbol") or decision.get("symbol"),
            "requested_as_of": record.get("requested_as_of"),
            "effective_as_of": record.get("effective_as_of") or record.get("as_of"),
            "run_id": record.get("run_id"),
            "origin": record.get("origin") or "legacy_unclassified",
            "evidence_stage": record.get("evidence_stage") or "unavailable",
            "settlement_eligible": bool(record.get("settlement_eligible")),
            "training_eligible": bool(record.get("training_eligible")),
            "registration_id": record.get("registration_id"),
            "context_id": record.get("context_id"),
            "context_fingerprint": record.get("context_fingerprint"),
        },
    }


def render_research_markdown(package: dict[str, Any]) -> str:
    package = _sanitize_audit(package)
    decision = package.get("decision", {})
    data = package.get("data", {})
    council = package.get("agent_reports", {}).get("council", {})
    reviewer = package.get("agent_reports", {}).get("reviewer", {})
    lines = [
        f"# QuantLab 研究审计报告：{package.get('symbol', '')}",
        "",
        f"- 决策日期：{package.get('as_of', '')}",
        f"- Run ID：`{package.get('run_id', '')}`",
        f"- 行情来源：{data.get('source', 'unknown')}",
        f"- 行情样本：{data.get('bars', 0)} 根",
        f"- 执行边界：{package.get('execution_boundary', 'manual_orders_only')}",
        "",
        "## 最终决策",
        "",
        f"- 动作：**{decision.get('action', 'unknown')}**",
        f"- 置信度：{_percent(decision.get('confidence'))}",
        f"- 目标权重：{_percent(decision.get('target_weight'))}",
        f"- 需要人工复核：{'是' if decision.get('requires_human_review') else '否'}",
        f"- 入场价：{_number(decision.get('entry_price'))}",
        f"- 止损价：{_number(decision.get('stop_loss'))}",
        f"- 目标价 1 / 2：{_number(decision.get('target_1'))} / {_number(decision.get('target_2'))}",
        "",
        "### 决策理由",
        "",
        *_bullet_lines(decision.get("reasons", [])),
        "",
        "### 风险与否决项",
        "",
        *_bullet_lines(decision.get("risks", [])),
        "",
        "## Agent 委员会",
        "",
        f"- 战术分：{_number(council.get('tactical_score'))}",
        f"- 战略分：{_number(council.get('strategic_score'))}",
        f"- 综合分：{_number(council.get('combined_score'))}",
        f"- 否决触发：{'是' if council.get('veto_triggered') else '否'}",
        f"- 委员会摘要：{council.get('summary', '无')}",
        "",
        "### 专家意见",
        "",
    ]
    opinions = council.get("opinions", [])
    if opinions:
        for opinion in opinions:
            lines.extend(
                [
                    f"- **{opinion.get('role', 'unknown')}**：{opinion.get('stance', 'neutral')}，"
                    f"分数 {_number(opinion.get('score'))}，置信度 {_percent(opinion.get('confidence'))}",
                ]
            )
    else:
        lines.append("- 无")
    lines.extend(["", "## 概率预测", ""])
    forecasts = package.get("forecasts", [])
    if forecasts:
        lines.extend(
            [
                "| 周期 | 上涨 | 震荡 | 下跌 | 预期收益 | 区间 | 模型 |",
                "|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for forecast in forecasts:
            interval = (
                f"{_number(forecast.get('lower_return_pct'))}% ~ "
                f"{_number(forecast.get('upper_return_pct'))}%"
            )
            lines.append(
                "| {horizon}日 | {up} | {flat} | {down} | {expected}% | {interval} | {model} |".format(
                    horizon=forecast.get("horizon_days", ""),
                    up=_percent(forecast.get("up_probability")),
                    flat=_percent(forecast.get("flat_probability")),
                    down=_percent(forecast.get("down_probability")),
                    expected=_number(forecast.get("expected_return_pct")),
                    interval=interval,
                    model=forecast.get("model", "unknown"),
                )
            )
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "## Reviewer 复核",
            "",
            f"- 状态：{reviewer.get('status', 'unknown')}",
            f"- 摘要：{reviewer.get('summary', '无')}",
            "",
            "### 复核问题",
            "",
            *_bullet_lines(reviewer.get("issues", [])),
            "",
            "## 数据质量与降级",
            "",
            *_bullet_lines(data.get("degraded_sources", []), empty="无降级项"),
            "",
            "## 审计链",
            "",
        ]
    )
    for event in package.get("audit_log", []):
        lines.append(
            f"- `{event.get('step', '')}` / {event.get('status', '')}：{event.get('detail', '')}"
        )
    lines.extend(
        [
            "",
            "## 风险声明",
            "",
            package.get("disclaimer", DISCLAIMER),
            "",
        ]
    )
    return "\n".join(lines)


def audit_package_json(package: dict[str, Any]) -> str:
    return json.dumps(_sanitize_audit(package), ensure_ascii=False, indent=2, default=str)


def prepare_historical_replay_export(replay: dict[str, Any]) -> dict[str, Any]:
    prepared = _sanitize_audit(replay)
    if "evidence_qualification" in prepared:
        return prepared
    llm_complete = bool(prepared.get("llm_validation", {}).get("live_llm_complete"))
    degraded_sources = list(prepared.get("degraded_sources", []))
    completed = int(prepared.get("completed_episodes", 0))
    prepared["evidence_qualification"] = {
        "sample_size_status": prepared.get("evidence_status", "illustrative"),
        "live_llm_complete": llm_complete,
        "clean_data_path": not degraded_sources,
        "qualified": llm_complete and not degraded_sources and completed >= 30,
        "limitations": [
            limitation
            for condition, limitation in (
                (not llm_complete, "one or more required live LLM roles are missing"),
                (bool(degraded_sources), "one or more preferred data sources degraded"),
                (completed < 30, "fewer than 30 non-overlapping episodes"),
            )
            if condition
        ],
    }
    return prepared


def render_historical_replay_markdown(replay: dict[str, Any]) -> str:
    replay = prepare_historical_replay_export(replay)
    metrics = replay["metrics"]
    llm_validation = replay.get("llm_validation", {})
    evidence_qualification = replay.get("evidence_qualification", {})
    required_outputs = int(
        llm_validation.get("required_role_outputs")
        or int(llm_validation.get("required_role_outputs_per_episode", 0))
        * int(replay["completed_episodes"])
    )
    fallback_errors = llm_validation.get("fallback_errors", [])
    missing_roles = [
        f"回合{index}:{role}x{count}"
        for index, item in enumerate(llm_validation.get("episodes", []), start=1)
        for role, count in item.get("missing_roles", {}).items()
    ]
    fallback_summary = "; ".join(
        f"{item.get('provider')}/{item.get('routing_key')}/{item.get('error_type')}"
        for item in fallback_errors
    )
    lines = [
        "# QuantLab 历史盲测回放报告",
        "",
        f"- 区间：{replay['requested_range']['start']} 至 {replay['requested_range']['end']}",
        f"- 预测周期：{replay['horizon_days']} 个交易日",
        f"- 完成回合：{replay['completed_episodes']}",
        f"- 证据等级：{replay['evidence_status']}",
        f"- 证据资格：{evidence_qualification.get('qualified', llm_validation.get('live_llm_complete', False))}",
        f"- 数据来源：{replay['source']}",
        "",
        "## 防泄漏契约",
        "",
        f"- 标的身份向 LLM 隐藏：{not replay['blinding']['actual_symbol_supplied_to_llm']}",
        f"- 真实日期向 LLM 隐藏：{not replay['blinding']['actual_date_supplied_to_llm']}",
        f"- 提供120日归一化价格路径：{replay['blinding'].get('normalized_price_path_120_supplied', False)}",
        f"- 归一化路径不含绝对价格：{not replay['blinding'].get('normalized_price_path_contains_absolute_prices', True)}",
        f"- 统计模型：{replay['statistical_model_contract']}",
        f"- 交易执行：{replay['execution_contract']}",
        f"- 抽样规则：{replay['selection_rule']}",
        "",
        "## 组合结果",
        "",
        "| 版本 | 交易数 | 参与率 | 累计收益 | 单次平均 | 胜率 | 最大回撤 | 期末权益 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, key in (
        ("纯策略", "strategy_only"),
        ("完整系统", "full_system"),
        ("沪深300同风险预算", "hs300_same_risk_budget"),
    ):
        item = metrics[key]
        win_rate = f"{item['trade_win_rate']:.1%}" if item["trade_win_rate"] is not None else "N/A"
        lines.append(
            f"| {label} | {item['trades']} | {item['participation_rate']:.1%} | "
            f"{item['total_return']:.2%} | {item['average_episode_return']:.2%} | "
            f"{win_rate} | {item['max_drawdown']:.2%} | ¥{item['final_equity']:,.2f} |"
        )
    lines.extend(
        [
            "",
            "## 单次回放",
            "",
            "| 回合 | 信号日 | 标的 | Agent 动作 | 策略收益 | 完整系统收益 | 基准收益 | 闸门效果 | 实际方向 |",
            "|---:|---|---|---|---:|---:|---:|---|---|",
        ]
    )
    for row in replay["episodes"]:
        lines.append(
            f"| {row['episode']} | {row['actual_as_of']} | {row['actual_symbol']} | "
            f"{row['decision']['action']} | {row['strategy_trade']['net_return']:.2%} | "
            f"{row['full_system_trade']['net_return']:.2%} | "
            f"{row['benchmark_trade']['net_return']:.2%} | {row['gate_effect']} | "
            f"{row['outcome']} |"
        )
    gate_metrics = metrics.get("decision_gate_counterfactuals", {})
    if gate_metrics:
        lines.extend(
            [
                "",
                "## ETF decision-gate counterfactual audit",
                "",
                "These arms reuse the same reviewed forecasts and executions. They are research-only and do not change the production gate.",
                "",
                "| Policy | Trades | Participation | Total return | Increment vs current | Increment vs strategy | Max drawdown | Screening |",
                "|---|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for name, item in gate_metrics.items():
            lines.append(
                f"| {name} | {item['trades']} | {item['participation_rate']:.1%} | "
                f"{item['total_return']:.2%} | {item['incremental_vs_current_total_return']:+.2%} | "
                f"{item['incremental_vs_strategy_total_return']:+.2%} | "
                f"{item['max_drawdown']:.2%} | {item['screening_status']} |"
            )
        gate_audit = replay.get("decision_gate_audit", {})
        lines.extend(
            [
                "",
                f"- Policy version: {gate_audit.get('policy_version', 'legacy')}",
                f"- Production policy changed: {gate_audit.get('production_policy_changed', False)}",
                f"- Promotion boundary: {gate_audit.get('promotion_boundary', 'research only')}",
            ]
        )
    lines.extend(
        [
            "",
            "## LLM 执行完整性",
            "",
            f"- 角色输出：{llm_validation.get('successful_non_mock_role_outputs', 0)}/{required_outputs}",
            f"- 端点尝试：{llm_validation.get('recorded_endpoint_attempts', 0)}",
            f"- 回退错误：{len(fallback_errors)}",
            f"- 缺失角色：{', '.join(missing_roles) if missing_roles else '无'}",
            f"- 回退明细：{fallback_summary or '无'}",
            f"- 完整执行：{llm_validation.get('live_llm_complete', False)}",
            "",
            "## 概率消融",
            "",
            "```json",
            json.dumps(metrics["forecast_ablation"], ensure_ascii=False, indent=2),
            "```",
            "",
            "## 结论边界",
            "",
            replay["claim_boundary"],
            "",
            DISCLAIMER,
            "",
        ]
    )
    return "\n".join(lines)


def _sanitize_audit(value: Any) -> Any:
    return sanitize_for_export(value)


def _bullet_lines(items: list[Any], empty: str = "无") -> list[str]:
    values = [str(item) for item in items if str(item).strip()]
    return [f"- {item}" for item in values] if values else [f"- {empty}"]


def _percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def _number(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "N/A"
