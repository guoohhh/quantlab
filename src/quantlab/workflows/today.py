from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from quantlab.config import Settings
from quantlab.persistence import DecisionRepository, PaperTradingRepository, TerminalRepository
from quantlab.workflows.evidence import build_evidence_summary
from quantlab.workflows.radar import build_market_radar


def build_today_brief(settings: Settings, as_of: date | None = None) -> dict[str, Any]:
    radar = build_market_radar(settings, as_of, include_sectors=False)
    effective_as_of = radar["as_of"]
    database_path = settings.resolve(settings.get("system.database_path"))
    decisions = DecisionRepository(database_path)
    terminal = TerminalRepository(database_path)
    paper = PaperTradingRepository(database_path)

    latest_by_symbol = {}
    for item in decisions.recent(100):
        if item["as_of"] > effective_as_of or item["symbol"] in latest_by_symbol:
            continue
        record = decisions.get(item["run_id"])
        if record:
            latest_by_symbol[item["symbol"]] = record
    capital = float(settings.get("system.initial_capital"))
    decision_cards = [_decision_summary(item, capital) for item in latest_by_symbol.values()]
    action_counts = Counter(item["action"] for item in decision_cards)

    latest_plan = terminal.latest_portfolio_plan()
    plan_is_current = bool(
        latest_plan and latest_plan.get("plan", {}).get("as_of") == effective_as_of
    )
    if plan_is_current:
        plan = latest_plan["plan"]
        suggested_exposure = sum(float(value) for value in plan["target_weights"].values())
        plan_orders = plan.get("orders", [])
        new_buy_count = sum(
            1
            for item in plan_orders
            if item.get("side") == "buy" and item.get("status") == "actionable"
        )
        reduce_count = sum(
            1
            for item in plan_orders
            if item.get("side") == "sell" and item.get("status") == "actionable"
        )
        review_count = sum(
            1
            for item in plan.get("blocked_candidates", [])
            if item.get("status") == "review_required"
        )
    else:
        plan = None
        suggested_exposure = _regime_exposure(
            radar["risk_appetite"], float(settings.get("risk.max_total_exposure"))
        )
        plan_orders = []
        new_buy_count = action_counts["buy"] + action_counts["add"]
        reduce_count = action_counts["reduce"] + action_counts["sell"]
        review_count = action_counts["review_required"]

    full_account = next(
        (
            item
            for item in paper.scorecard().get("accounts", [])
            if item["account_id"] == "full_system"
        ),
        None,
    )
    leader = radar["instruments"][0]
    leader_decision = latest_by_symbol.get(leader["symbol"])
    next_actions = []
    if leader_decision is None or leader_decision["as_of"] != effective_as_of:
        next_actions.append(f"运行 {leader['symbol']} 的当日多 Agent 研究")
    if not plan_is_current:
        next_actions.append("生成当日三策略组合计划")
    latest_paper_run = paper.latest_run()
    if latest_paper_run is None or latest_paper_run["as_of"] != effective_as_of:
        next_actions.append("运行当日模拟盘周期，冻结信号并生成次日待成交单")
    if radar["degraded_sources"]:
        next_actions.append("复核降级数据源后再执行任何新增仓位")

    planned_buys = [
        item
        for item in plan_orders
        if item.get("side") == "buy" and item.get("status") == "actionable"
    ]
    planned_losses = [item.get("maximum_loss_amount") for item in planned_buys]
    risk_estimate_available = not planned_buys or all(value is not None for value in planned_losses)
    risk_budget = (
        sum(float(value) for value in planned_losses if value is not None)
        if risk_estimate_available
        else 0.0
    )
    if not risk_estimate_available:
        next_actions.append("为所有新增手工订单补充止损或最大可承受亏损后再下单")
    return {
        "as_of": effective_as_of,
        "status": "degraded" if radar["degraded_sources"] else "ready",
        "headline": {
            "market_regime": radar["market_regime"],
            "risk_appetite": radar["risk_appetite"],
            "suggested_total_exposure": suggested_exposure,
            "new_buy_count": new_buy_count,
            "reduce_count": reduce_count,
            "review_count": review_count,
            "estimated_maximum_loss_amount": (risk_budget if risk_estimate_available else None),
            "risk_estimate_status": (
                "available" if risk_estimate_available else "missing_for_new_orders"
            ),
        },
        "top_opportunities": radar["instruments"][:3],
        "decision_cards": sorted(
            decision_cards, key=lambda item: (item["as_of"], item["confidence"]), reverse=True
        ),
        "current_plan": {
            "available": plan_is_current,
            "plan_id": latest_plan.get("plan_id") if latest_plan else None,
            "orders": plan_orders,
            "warnings": plan.get("warnings", []) if plan else ["no current-date portfolio plan"],
        },
        "paper_portfolio": full_account,
        "evidence": build_evidence_summary(settings),
        "data_quality": {
            "source": radar["source"],
            "coverage": radar["coverage"],
            "degraded_sources": radar["degraded_sources"],
        },
        "next_actions": next_actions or ["复核待成交价格并保持当前计划"],
        "execution_boundary": "manual_orders_only",
    }


def _decision_summary(record: dict[str, Any], capital: float) -> dict[str, Any]:
    decision = record["payload"].get("decision", {})
    entry = _number(decision.get("entry_price"))
    stop = _number(decision.get("stop_loss"))
    target_weight = float(decision.get("target_weight") or 0.0)
    maximum_loss_rate = max(0.0, (entry - stop) / entry) if entry and stop and entry > stop else 0.0
    return {
        "run_id": record["run_id"],
        "symbol": record["symbol"],
        "as_of": record["as_of"],
        "action": decision.get("action", record["action"]),
        "confidence": float(decision.get("confidence", record["confidence"])),
        "target_weight": target_weight,
        "entry_price": entry,
        "stop_loss": stop,
        "maximum_loss_rate": maximum_loss_rate,
        "maximum_loss_amount": capital * target_weight * maximum_loss_rate,
        "requires_human_review": bool(decision.get("requires_human_review")),
        "reasons": decision.get("reasons", []),
        "risks": decision.get("risks", []),
    }


def _regime_exposure(risk_appetite: str, configured_maximum: float) -> float:
    target = {"risk_on": 0.75, "neutral": 0.55, "risk_off": 0.30}.get(risk_appetite, 0.45)
    return min(configured_maximum, target)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
