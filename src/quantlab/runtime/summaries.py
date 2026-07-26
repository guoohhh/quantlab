from __future__ import annotations

from datetime import date
from typing import Any

from quantlab.config import Settings
from quantlab.persistence import EvidenceRepository, NotificationRepository
from quantlab.workflows.simulator import user_simulator_repository


def generate_premarket_digest(
    settings: Settings,
    *,
    report_date: date,
    account_id: str | None = None,
) -> dict[str, Any]:
    simulator = user_simulator_repository(settings)
    notifications = NotificationRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    accounts = (
        [simulator.account(account_id)] if account_id else simulator.accounts(include_closed=False)
    )
    account_summaries = []
    symbols: set[str] = set()
    for account in accounts:
        if account is None:
            continue
        overview = simulator.overview(account["account_id"])
        account_symbols = [item["symbol"] for item in overview["positions"]]
        symbols.update(account_symbols)
        account_summaries.append(
            {
                "account_id": account["account_id"],
                "equity": overview["equity"],
                "cash": overview["cash"],
                "position_count": len(overview["positions"]),
                "symbols": account_symbols,
                "pending_orders": len(
                    simulator.orders(account["account_id"], status="pending", limit=500)
                ),
            }
        )
    evidence = EvidenceRepository(settings.resolve(settings.get("system.database_path")))
    context_status = []
    for symbol in sorted(symbols):
        context = evidence.latest_context(symbol, as_of=report_date.isoformat())
        context_status.append(
            {
                "symbol": symbol,
                "context_id": context.get("context_id") if context else None,
                "quality_score": context.get("quality_score") if context else None,
                "review_required": context.get("review_required", True) if context else True,
                "known_gaps": context.get("known_gaps", ["context_unavailable"])
                if context
                else ["context_unavailable"],
            }
        )
    important = notifications.list(
        unread_only=True,
        account_id=account_id,
        limit=20,
    )
    summary = {
        "report_type": "premarket_digest",
        "report_date": report_date.isoformat(),
        "accounts": account_summaries,
        "held_symbol_contexts": context_status,
        "important_notifications": [
            {
                "notification_id": item["notification_id"],
                "severity": item["severity"],
                "title": item["title"],
            }
            for item in important
            if item["severity"] in {"warning", "critical"}
        ],
        "review_items": [
            item["symbol"] for item in context_status if item["review_required"]
        ],
        "data_boundary": "Uses only persisted QuantLab data available before generation.",
    }
    notifications.emit(
        event_type="premarket_digest_ready",
        aggregate_type="daily_summary",
        aggregate_id=f"premarket:{account_id or 'all'}:{report_date.isoformat()}",
        payload={
            "account_id": account_id,
            "data_as_of": report_date.isoformat(),
            "action_type": "view_structured_summary",
            "action_payload": summary,
        },
        dedup_key=f"premarket_digest:{account_id or 'all'}:{report_date.isoformat()}",
    )
    return summary


def generate_account_daily_report(
    settings: Settings,
    *,
    report_date: date,
    account_id: str | None = None,
) -> dict[str, Any]:
    simulator = user_simulator_repository(settings)
    notifications = NotificationRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    accounts = (
        [simulator.account(account_id)] if account_id else simulator.accounts(include_closed=False)
    )
    reports: list[dict[str, Any]] = []
    for account in accounts:
        if account is None:
            continue
        resolved_id = account["account_id"]
        overview = simulator.overview(resolved_id)
        fills = [
            item
            for item in simulator.fills(resolved_id, 10_000)
            if str(item["trade_date"])[:10] == report_date.isoformat()
        ]
        curve = simulator.snapshots(resolved_id, 10_000)
        latest = curve[-1] if curve else None
        previous = curve[-2] if len(curve) >= 2 else None
        today_pnl = (
            float(latest["equity"]) - float(previous["equity"])
            if latest and previous
            else 0.0
        )
        positions = overview["positions"]
        ranked = sorted(positions, key=lambda item: float(item.get("unrealized_pnl", 0.0)))
        important = notifications.list(account_id=resolved_id, limit=50)
        data_degraded = [
            item
            for item in important
            if item["notification_type"]
            in {
                "paper_data_stale",
                "data_source_degraded",
                "context_evidence_missing",
                "flow_data_unavailable",
            }
            and str(item["created_at"])[:10] == report_date.isoformat()
        ]
        report = {
            "account_id": resolved_id,
            "total_equity": overview["equity"],
            "today_pnl": today_pnl,
            "cash": overview["cash"],
            "market_value": overview["market_value"],
            "exposure": overview["market_value"] / overview["equity"]
            if overview["equity"]
            else 0.0,
            "top_profit_sources": [
                {"symbol": item["symbol"], "unrealized_pnl": item.get("unrealized_pnl", 0.0)}
                for item in reversed(ranked[-3:])
            ],
            "top_loss_sources": [
                {"symbol": item["symbol"], "unrealized_pnl": item.get("unrealized_pnl", 0.0)}
                for item in ranked[:3]
            ],
            "position_risk_change": {
                "maximum_drawdown": latest.get("maximum_drawdown") if latest else None,
                "previous_maximum_drawdown": previous.get("maximum_drawdown")
                if previous
                else None,
            },
            "today_fills": len(fills),
            "today_fees": sum(float(item.get("fees", 0.0)) for item in fills),
            "important_notifications": [
                item["notification_id"]
                for item in important
                if item["severity"] in {"warning", "critical"}
                and str(item["created_at"])[:10] == report_date.isoformat()
            ],
            "data_degradation": [item["notification_id"] for item in data_degraded],
            "review_required": bool(data_degraded)
            or any(item["severity"] == "critical" for item in important),
        }
        reports.append(report)
        notifications.emit(
            event_type="account_daily_report_ready",
            aggregate_type="account_daily_report",
            aggregate_id=f"{resolved_id}:{report_date.isoformat()}",
            payload={
                "account_id": resolved_id,
                "data_as_of": report_date.isoformat(),
                "action_type": "view_structured_summary",
                "action_payload": report,
            },
            dedup_key=f"account_daily_report:{resolved_id}:{report_date.isoformat()}",
        )
    return {
        "report_type": "account_daily_report",
        "report_date": report_date.isoformat(),
        "reports": reports,
        "data_boundary": "Uses persisted account, fill, risk and notification records.",
    }


__all__ = ["generate_account_daily_report", "generate_premarket_digest"]
