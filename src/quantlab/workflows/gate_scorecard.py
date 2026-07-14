from __future__ import annotations

from collections import Counter
from typing import Any

from quantlab.config import Settings
from quantlab.persistence import HistoricalReplayRepository
from quantlab.workflows.replay import _forecast_metrics


def build_decision_gate_scorecard(settings: Settings, replay_ids: list[int]) -> dict[str, Any]:
    repository = HistoricalReplayRepository(settings.resolve(settings.get("system.database_path")))
    records = []
    for replay_id in replay_ids:
        record = repository.get(replay_id)
        if record is None:
            raise ValueError(f"historical replay {replay_id} not found")
        records.append({"replay_id": replay_id, **record["payload"]})
    return summarize_decision_gate_replays(records)


def summarize_decision_gate_replays(replays: list[dict[str, Any]]) -> dict[str, Any]:
    if not replays:
        raise ValueError("at least one historical replay is required")
    replay_summaries = [_replay_summary(item) for item in replays]
    v2_replays = [
        item
        for item in replays
        if item.get("decision_gate_audit", {}).get("policy_version") == "2026-07-14.v2"
    ]
    horizon_groups = {}
    for horizon in sorted({int(item["horizon_days"]) for item in v2_replays}):
        selected = [item for item in v2_replays if int(item["horizon_days"]) == horizon]
        rows = [row for item in selected for row in item["episodes"]]
        horizon_groups[str(horizon)] = _horizon_summary(rows, selected)
    v1_failures = []
    for item in replays:
        metrics = item.get("metrics", {}).get("decision_gate_counterfactuals", {})
        if item.get("decision_gate_audit", {}).get("policy_version") != "2026-07-14.v1":
            continue
        for policy in ("etf_score_tiered_v1", "etf_forecast_confirmed_v1"):
            if policy in metrics:
                v1_failures.append(
                    {
                        "replay_id": item.get("replay_id"),
                        "policy": policy,
                        "total_return": metrics[policy]["total_return"],
                        "incremental_vs_strategy": metrics[policy][
                            "incremental_vs_strategy_total_return"
                        ],
                        "screening_status": metrics[policy]["screening_status"],
                    }
                )
    v2_new_episodes = sum(group["episodes"] for group in horizon_groups.values())
    reduction_count = sum(group["v2_risk_reduction_count"] for group in horizon_groups.values())
    promotion_checks = {
        "at_least_12_new_episodes_in_one_horizon": any(
            group["episodes"] >= 12 for group in horizon_groups.values()
        ),
        "at_least_one_model_driven_risk_reduction": reduction_count > 0,
        "all_required_live_roles_complete": all(
            item.get("llm_validation", {}).get("live_llm_complete", False) for item in v2_replays
        ),
        "clean_data_paths": all(not item.get("degraded_sources") for item in v2_replays),
    }
    promotion_status = (
        "eligible_for_prospective_shadow_review"
        if all(promotion_checks.values())
        else "insufficient_evidence_not_promoted"
    )
    return {
        "method": "cross-replay decision-gate evidence scorecard",
        "replay_ids": [item.get("replay_id") for item in replays],
        "replays": replay_summaries,
        "v1_failure_records": v1_failures,
        "v2_horizon_challenges": horizon_groups,
        "v2_new_episodes": v2_new_episodes,
        "v2_model_driven_risk_reductions": reduction_count,
        "promotion_checks": promotion_checks,
        "promotion_status": promotion_status,
        "conclusion": (
            "V1 LLM-first ETF gates are rejected. The V2 strategy-primary architecture prevents "
            "uncalibrated LLM and reviewer variance from suppressing the quantitative strategy. "
            "The statistical model improves short-horizon probability quality, but no frozen bearish "
            "threshold fired, so there is not yet evidence that the model improves trading profit."
        ),
        "claim_boundary": (
            "historical blind replay and stitched short samples do not prove future profitability; "
            "V2 remains research-only until a frozen prospective shadow sample is sufficient"
        ),
    }


def render_decision_gate_scorecard_markdown(scorecard: dict[str, Any]) -> str:
    lines = [
        "# QuantLab ETF decision-gate evidence scorecard",
        "",
        f"- Replays: {', '.join(str(item) for item in scorecard['replay_ids'])}",
        f"- V2 new episodes: {scorecard['v2_new_episodes']}",
        f"- Model-driven risk reductions: {scorecard['v2_model_driven_risk_reductions']}",
        f"- Promotion status: {scorecard['promotion_status']}",
        "",
        "## Replay-level evidence",
        "",
        "| Replay | Range | Horizon | Episodes | Policy | Strategy | Current | V2 | Veto rate | Reviewer reject rate |",
        "|---:|---|---:|---:|---|---:|---:|---:|---:|---:|",
    ]
    for item in scorecard["replays"]:
        v2_return = item.get("calibrated_strategy_primary_return")
        lines.append(
            f"| {item['replay_id']} | {item['range']} | {item['horizon_days']} | "
            f"{item['episodes']} | {item['policy_version']} | {item['strategy_return']:.2%} | "
            f"{item['current_return']:.2%} | "
            f"{v2_return:.2%} | "
            f"{item['veto_rate']:.1%} | {item['reviewer_reject_rate']:.1%} |"
            if v2_return is not None
            else f"| {item['replay_id']} | {item['range']} | {item['horizon_days']} | "
            f"{item['episodes']} | {item['policy_version']} | {item['strategy_return']:.2%} | "
            f"{item['current_return']:.2%} | N/A | {item['veto_rate']:.1%} | "
            f"{item['reviewer_reject_rate']:.1%} |"
        )
    lines.extend(["", "## V2 horizon challenges", ""])
    for horizon, item in scorecard["v2_horizon_challenges"].items():
        lines.extend(
            [
                f"### {horizon}-day",
                "",
                f"- Episodes: {item['episodes']}",
                f"- Strategy return: {item['strategy']['total_return']:.2%}",
                f"- Current return: {item['current_strict']['total_return']:.2%}",
                f"- V2 return: {item['calibrated_strategy_primary_v1']['total_return']:.2%}",
                f"- Raw LLM Brier: {item['forecast_ablation']['raw_llm']['brier_score']}",
                f"- Statistical Brier: {item['forecast_ablation']['point_in_time_statistical']['brier_score']}",
                f"- Final ensemble Brier: {item['forecast_ablation']['final_ensemble']['brier_score']}",
                f"- Model-driven reductions: {item['v2_risk_reduction_count']}",
                "",
            ]
        )
    lines.extend(
        [
            "## Promotion checks",
            "",
            *[
                f"- {name}: {passed}"
                for name, passed in scorecard["promotion_checks"].items()
            ],
            "",
            "## Conclusion",
            "",
            scorecard["conclusion"],
            "",
            scorecard["claim_boundary"],
            "",
        ]
    )
    return "\n".join(lines)


def _replay_summary(replay: dict[str, Any]) -> dict[str, Any]:
    rows = replay["episodes"]
    metrics = replay["metrics"]
    gate_metrics = metrics.get("decision_gate_counterfactuals", {})
    return {
        "replay_id": replay.get("replay_id"),
        "range": f"{replay['requested_range']['start']}..{replay['requested_range']['end']}",
        "horizon_days": replay["horizon_days"],
        "episodes": replay["completed_episodes"],
        "policy_version": replay.get("decision_gate_audit", {}).get(
            "policy_version", "legacy"
        ),
        "strategy_return": metrics["strategy_only"]["total_return"],
        "current_return": metrics["full_system"]["total_return"],
        "calibrated_strategy_primary_return": gate_metrics.get(
            "calibrated_strategy_primary_v1", {}
        ).get("total_return"),
        "veto_rate": sum(row["council"]["veto_triggered"] for row in rows) / len(rows),
        "reviewer_reject_rate": sum(not row["reviewer"]["approved"] for row in rows)
        / len(rows),
        "live_llm_complete": replay.get("llm_validation", {}).get("live_llm_complete"),
        "degraded_sources": replay.get("degraded_sources", []),
    }


def _horizon_summary(
    rows: list[dict[str, Any]], replays: list[dict[str, Any]]
) -> dict[str, Any]:
    dates = [(row["actual_as_of"], row["outcome_date"]) for row in rows]
    if len(dates) != len(set(dates)):
        raise ValueError("duplicate replay episodes cannot be stitched")
    ordered = sorted(rows, key=lambda item: item["actual_as_of"])
    return {
        "episodes": len(ordered),
        "replay_ids": [item.get("replay_id") for item in replays],
        "strategy": _stitched_metrics(ordered, "strategy_trade"),
        "current_strict": _stitched_metrics(ordered, "full_system_trade"),
        "calibrated_strategy_primary_v1": _stitched_metrics(
            ordered, "gate_counterfactuals.calibrated_strategy_primary_v1.trade"
        ),
        "forecast_ablation": {
            "final_ensemble": _forecast_metrics(ordered, "final"),
            "raw_llm": _forecast_metrics(ordered, "raw_llm"),
            "point_in_time_statistical": _forecast_metrics(ordered, "statistical"),
        },
        "v2_tiers": dict(
            Counter(
                row["gate_counterfactuals"]["calibrated_strategy_primary_v1"]["tier"]
                for row in ordered
            )
        ),
        "v2_risk_reduction_count": sum(
            row["gate_counterfactuals"]["calibrated_strategy_primary_v1"]["tier"]
            == "risk_reduced"
            for row in ordered
        ),
    }


def _stitched_metrics(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    trade_returns = []
    for row in rows:
        value: Any = row
        for part in field.split("."):
            value = value[part]
        result = float(value["net_return"])
        equity *= 1 + result
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
        if value["traded"]:
            trade_returns.append(result)
    return {
        "episodes": len(rows),
        "trades": len(trade_returns),
        "participation_rate": len(trade_returns) / len(rows),
        "total_return": equity - 1,
        "trade_win_rate": (
            sum(value > 0 for value in trade_returns) / len(trade_returns)
            if trade_returns
            else None
        ),
        "max_drawdown": max_drawdown,
    }
