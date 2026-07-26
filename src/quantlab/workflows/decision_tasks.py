from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from quantlab.config import Settings
from quantlab.domain.context import reproducible_fingerprint
from quantlab.persistence.round9 import Round9Repository


def refresh_decision_tasks(settings: Settings) -> dict[str, Any]:
    path = settings.resolve(settings.get("system.database_path"))
    repository = Round9Repository(path)
    candidates: list[dict[str, Any]] = []
    managed_task_types = {
        "pending_order",
        "partial_order",
        "thesis_red_line",
        "thesis_weakened",
        "data_degraded",
        "chat_draft_confirmation",
        "formal_experiment_anomaly",
    }
    with sqlite3.connect(path, timeout=30) as db:
        db.row_factory = sqlite3.Row
        for row in _rows_if_table(
            db,
            "user_paper_orders",
            """SELECT order_id,account_id,symbol,status,side,requested_quantity,
                      filled_quantity,expires_at,research_run_id
               FROM user_paper_orders
               WHERE status IN ('pending','partially_filled')""",
        ):
            partial = row["status"] == "partially_filled" or int(row["filled_quantity"] or 0) > 0
            condition = reproducible_fingerprint(
                {
                    "status": row["status"],
                    "filled_quantity": int(row["filled_quantity"] or 0),
                    "requested_quantity": int(row["requested_quantity"]),
                }
            )
            candidates.append(
                _system_task(
                    {
                    "category": "immediate_action" if partial else "needs_review",
                    "task_type": "partial_order" if partial else "pending_order",
                    "severity": "warning" if partial else "info",
                    "title": "模拟订单部分成交" if partial else "模拟订单等待处理",
                    "user_summary": (
                        f"{row['symbol']} {row['side']}订单已成交{row['filled_quantity']}/"
                        f"{row['requested_quantity']}股。"
                        if partial
                        else f"{row['symbol']} 模拟订单仍为待处理状态，请复核最新行情。"
                    ),
                    "diagnostic_detail": f"order_status={row['status']}",
                    "account_id": row["account_id"],
                    "symbol": row["symbol"],
                    "decision_run_id": row["research_run_id"],
                    "source_type": "user_paper_order",
                    "source_id": row["order_id"],
                    "dedup_key": f"decision-order:{row['order_id']}:{condition}",
                    "condition_fingerprint": condition,
                    "payload": dict(row),
                    }
                )
            )
        for row in _rows_if_table(
            db,
            "thesis_checks",
            """SELECT c.check_id,c.thesis_id,c.final_status,c.red_line_triggered,
                      c.checked_at,t.portfolio_id,t.symbol,t.run_id
               FROM thesis_checks c JOIN investment_theses t ON t.thesis_id=c.thesis_id
               WHERE t.status<>'closed' AND c.checked_at=(
                   SELECT MAX(latest.checked_at) FROM thesis_checks latest
                   WHERE latest.thesis_id=c.thesis_id
               ) AND (c.red_line_triggered=1 OR c.final_status IN ('weakened','damaged','broken'))""",
        ):
            condition = reproducible_fingerprint(
                {
                    "check_id": row["check_id"],
                    "final_status": row["final_status"],
                    "red_line_triggered": bool(row["red_line_triggered"]),
                }
            )
            candidates.append(
                _system_task(
                    {
                    "category": "immediate_action" if row["red_line_triggered"] else "needs_review",
                    "task_type": "thesis_red_line" if row["red_line_triggered"] else "thesis_weakened",
                    "severity": "critical" if row["red_line_triggered"] else "warning",
                    "title": "投资论文红线触发" if row["red_line_triggered"] else "投资论文需要复核",
                    "user_summary": (
                        f"{row['symbol']} 的投资逻辑出现重大反证，请先复核，不会自动交易。"
                    ),
                    "diagnostic_detail": f"thesis_status={row['final_status']}",
                    "account_id": row["portfolio_id"],
                    "symbol": row["symbol"],
                    "decision_run_id": row["run_id"],
                    "source_type": "investment_thesis",
                    "source_id": row["thesis_id"],
                    "dedup_key": f"decision-thesis-check:{row['thesis_id']}:{condition}",
                    "condition_fingerprint": condition,
                    "payload": dict(row),
                    }
                )
            )
        for row in _rows_if_table(
            db,
            "trusted_data_source_state",
            """SELECT batch_type,status,minimum_ready,consecutive_failures,
                      last_attempt_at,last_success_at,detail
               FROM trusted_data_source_state
               WHERE minimum_ready=0 OR status NOT IN ('completed','partial')""",
        ):
            condition = reproducible_fingerprint(
                {
                    "batch_type": row["batch_type"],
                    "status": row["status"],
                    "last_success_at": row["last_success_at"],
                }
            )
            candidates.append(
                _system_task(
                    {
                    "category": "system_data_issue",
                    "task_type": "data_degraded",
                    "severity": "warning",
                    "title": "部分投资数据暂不可用",
                    "user_summary": "系统已降低结论权限；数据恢复前不会把缺失值当成真实证据。",
                    "diagnostic_detail": f"{row['batch_type']} status={row['status']}",
                    "source_type": "data_source_state",
                    "source_id": row["batch_type"],
                    "dedup_key": f"decision-data:{row['batch_type']}:{condition}",
                    "condition_fingerprint": condition,
                    "payload": {
                        **dict(row),
                        "detail": json.loads(row["detail"] or "{}"),
                    },
                    }
                )
            )
        for row in _rows_if_table(
            db,
            "chat_action_drafts",
            """SELECT action_id,conversation_id,message_id,action_type,status,
                      draft_payload,account_id,symbol,research_run_id,created_at
               FROM chat_action_drafts WHERE status='confirmation_required'""",
        ):
            condition = reproducible_fingerprint(
                {"action_id": row["action_id"], "status": row["status"]}
            )
            candidates.append(
                _system_task(
                    {
                    "category": "immediate_action",
                    "task_type": "chat_draft_confirmation",
                    "severity": "info",
                    "title": "Chat草稿等待确认",
                    "user_summary": "AI只创建了草稿；需要你单独确认后才会继续。",
                    "source_type": "chat_action_draft",
                    "account_id": row["account_id"],
                    "symbol": row["symbol"],
                    "decision_run_id": row["research_run_id"],
                    "source_id": row["action_id"],
                    "dedup_key": f"decision-chat-draft:{row['action_id']}",
                    "condition_fingerprint": condition,
                    "payload": {
                        **dict(row),
                        "draft_payload": json.loads(row["draft_payload"] or "{}"),
                    },
                    }
                )
            )
        for row in _rows_if_table(
            db,
            "runtime_failures",
            """SELECT failure_id,source_type,source_id,severity,error_detail,created_at
               FROM runtime_failures WHERE acknowledged_at IS NULL""",
        ):
            condition = reproducible_fingerprint(
                {
                    "failure_id": row["failure_id"],
                    "severity": row["severity"],
                    "error_detail": row["error_detail"],
                }
            )
            candidates.append(
                _system_task(
                    {
                    "category": "system_data_issue",
                    "task_type": "formal_experiment_anomaly",
                    "severity": row["severity"],
                    "title": "正式实验或运行任务异常",
                    "user_summary": "系统已阻断受影响的正式流程；可在高级模式查看诊断。",
                    "diagnostic_detail": row["error_detail"],
                    "source_type": "runtime_failure",
                    "source_id": row["failure_id"],
                    "dedup_key": f"decision-runtime-failure:{row['failure_id']}",
                    "condition_fingerprint": condition,
                    "payload": dict(row),
                    }
                )
            )
    saved = [repository.upsert_decision_task(item) for item in candidates]
    active_dedup_keys = {str(item["dedup_key"]) for item in candidates}
    resolved = repository.reconcile_system_tasks(
        active_dedup_keys=active_dedup_keys,
        task_types=managed_task_types,
    )
    return {
        "refreshed_at": datetime.now(UTC).isoformat(),
        "candidate_count": len(candidates),
        "tasks": saved,
        "auto_resolved": resolved,
    }


def _system_task(payload: dict[str, Any]) -> dict[str, Any]:
    return {"management_source": "system_managed", **payload}


def _rows_if_table(
    db: sqlite3.Connection, table: str, query: str
) -> list[sqlite3.Row]:
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone():
        return []
    return list(db.execute(query).fetchall())


__all__ = ["refresh_decision_tasks"]
