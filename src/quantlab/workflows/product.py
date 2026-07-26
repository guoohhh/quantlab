from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from quantlab.config import Settings
from quantlab.persistence import DecisionRepository, NotificationRepository
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.round6 import Round6Repository
from quantlab.runtime.readiness import primary_start_readiness
from quantlab.workflows.simulator import user_simulator_repository


PRODUCT_ENTRYPOINTS = (
    "今日",
    "市场与发现",
    "研究台",
    "组合与交易",
    "决策复盘",
    "专业空间",
    "帮助中心",
    # Kept as read-compatible analytics identities for records emitted by the
    # pre-vNext Streamlit shell. They are not rendered as current navigation.
    "首页",
    "行情与发现",
    "AI研究",
    "模拟交易",
    "我的",
)


def build_product_home(
    settings: Settings,
    *,
    readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the product-home snapshot without changing any decision authority.

    ``readiness`` is an optional display snapshot supplied by the Streamlit
    shell.  It is deliberately an input rather than a cache inside this
    workflow: scheduler, worker, and order paths continue to call
    ``primary_start_readiness`` directly every time they need authoritative
    admission evidence.
    """

    path = settings.resolve(settings.get("system.database_path"))
    simulator = user_simulator_repository(settings)
    accounts = simulator.accounts(include_closed=False)
    account = simulator.overview(accounts[0]["account_id"]) if accounts else None
    round5 = Round5Repository(path)
    portfolios = round5.investor_portfolios()
    investor = round5.investor_overview(portfolios[0]["portfolio_id"]) if portfolios else None
    readiness = readiness or primary_start_readiness(settings, require_runtime=False)
    notifications = NotificationRepository(path)
    recent_decisions = DecisionRepository(path).recent(5)
    pending_orders = (
        [
            item
            for item in simulator.orders(account["account_id"], limit=100)
            if item["status"] in {"pending", "partially_filled"}
        ]
        if account
        else []
    )
    risks = []
    if account:
        maximum_single = float(settings.get("risk.max_single_position", 0.15))
        risks.extend(
            {
                "type": "position_concentration",
                "symbol": item["symbol"],
                "message": f"{item['symbol']} 仓位 {item['weight']:.1%} 接近或超过单股上限",
            }
            for item in account["positions"]
            if float(item["weight"]) >= maximum_single * 0.90
        )
    if readiness["blockers"]:
        risks.append(
            {
                "type": "data_readiness",
                "message": "生产数据或运行条件尚未满足，正式前瞻实验保持关闭",
                "blockers": readiness["blockers"],
            }
        )
    if not accounts and not portfolios:
        state = "no_account"
    elif not any(readiness["data"]["source_states"].values()):
        state = "no_data"
    elif readiness["blockers"]:
        state = "degraded"
    else:
        state = "normal"
    todos = []
    if not accounts:
        todos.append({"type": "create_simulator", "message": "创建模拟账户，开始验证决策"})
    if pending_orders:
        todos.append(
            {"type": "pending_orders", "message": f"有 {len(pending_orders)} 笔模拟委托待处理"}
        )
    if notifications.unread_count(account["account_id"] if account else None):
        todos.append({"type": "notifications", "message": "查看未读通知和风险变化"})
    if readiness["blockers"]:
        todos.append({"type": "data_status", "message": "复核数据源和运行 readiness"})
    return {
        "state": state,
        "generated_at": datetime.now(UTC).isoformat(),
        "account": account,
        "investor_portfolio": investor,
        "risk_items": risks,
        "data_status": readiness,
        "todos": todos,
        "latest_ai_suggestions": recent_decisions,
        "pending_orders": pending_orders,
        "unread_notifications": notifications.unread_count(
            account["account_id"] if account else None
        ),
    }


def record_product_usage(
    settings: Settings,
    *,
    event_type: str,
    entrypoint: str | None = None,
    account_id: str | None = None,
    portfolio_id: str | None = None,
    symbol: str | None = None,
    reference_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if entrypoint is not None and entrypoint not in PRODUCT_ENTRYPOINTS:
        raise ValueError("unknown product entrypoint")
    return Round6Repository(
        settings.resolve(settings.get("system.database_path"))
    ).record_product_event(
        event_type=event_type,
        entrypoint=entrypoint,
        account_id=account_id,
        portfolio_id=portfolio_id,
        symbol=symbol,
        reference_id=reference_id,
        payload={
            **(payload or {}),
            "usage_only": True,
            "training_eligible": False,
            "forward_scorecard_eligible": False,
        },
    )


__all__ = ["PRODUCT_ENTRYPOINTS", "build_product_home", "record_product_usage"]
