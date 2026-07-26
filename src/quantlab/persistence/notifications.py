from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from quantlab.security import safe_error_detail, sanitize_for_export
from quantlab.persistence.migrations import record_component_migration


EVENT_PRESENTATION: dict[str, tuple[str, str, str]] = {
    "paper_order_submitted": ("info", "模拟委托已提交", "委托已进入等待成交状态"),
    "paper_order_waiting": ("info", "模拟委托等待成交", "委托尚未满足成交时间或条件"),
    "paper_order_partially_filled": ("info", "模拟委托部分成交", "委托已部分成交"),
    "paper_order_filled": ("info", "模拟委托全部成交", "委托已全部成交"),
    "paper_order_rejected": ("warning", "模拟委托被拒绝", "委托未通过确定性交易规则"),
    "paper_order_cancelled": ("info", "模拟委托已撤销", "未成交委托已由用户撤销"),
    "paper_order_expired": ("warning", "模拟委托已过期", "委托超过有效期且未成交"),
    "paper_t1_released": ("info", "T+1冻结已解除", "持仓已进入可卖状态"),
    "paper_cash_insufficient": ("warning", "模拟账户现金不足", "委托金额超过可用现金"),
    "paper_position_risk": ("warning", "持仓风险提醒", "账户持仓接近或超过风险限制"),
    "paper_data_stale": ("warning", "行情数据陈旧", "无法使用陈旧行情检查或成交"),
    "paper_mark_completed": ("info", "模拟账户盯市完成", "账户持仓和净值快照已更新"),
    "research_completed": ("info", "研究任务完成", "研究报告已生成"),
    "research_degraded": ("warning", "研究任务降级", "部分数据或模型不可用"),
    "chat_trade_draft": ("info", "订单草稿等待确认", "Chat已创建模拟订单草稿"),
    "price_alert_triggered": ("warning", "价格预警触发", "标的价格达到用户设置条件"),
    "market_flow_changed": ("warning", "市场资金状态变化", "市场资金和交易活跃度发生明显变化"),
    "industry_flow_streak": ("warning", "行业资金趋势持续", "行业出现连续资金流入或流出"),
    "flow_price_divergence": ("warning", "资金价格背离", "资金趋势与价格趋势出现背离"),
    "holding_flow_deteriorated": ("warning", "持仓资金趋势恶化", "持仓标的资金趋势明显恶化"),
    "watchlist_flow_anomaly": ("info", "自选股资金异动", "自选标的出现资金异动"),
    "flow_data_unavailable": ("warning", "资金流数据不可用", "无法获得可靠资金流数据"),
    "data_source_degraded": ("warning", "数据源降级", "关键数据源发生降级或回退"),
    "context_evidence_missing": ("warning", "分析上下文证据缺失", "ContextPack存在关键证据缺口"),
    "data_source_conflict": ("warning", "数据源冲突", "不同数据源出现明显冲突"),
    "reviewer_rejected": ("warning", "Reviewer拒绝", "研究结论未通过Reviewer"),
    "ai_view_changed": ("info", "AI观点发生变化", "当前AI观点与上次研究不同"),
    "invalidation_triggered": ("critical", "投资逻辑失效条件触发", "原始研究失效条件已经触发"),
    "model_challenge_completed": ("info", "模型挑战完成", "角色或模型冻结挑战已经完成"),
    "llm_budget_reached": ("warning", "LLM成本预算已达上限", "任务已停止新增付费模型调用"),
    "provider_fallback": ("warning", "LLM Provider发生回退", "模型调用已切换到备用Provider"),
}

EVENT_PRESENTATION.update(
    {
        "background_job_completed": ("info", "后台任务完成", "后台任务已完成，可查询结果。"),
        "background_job_failed": ("warning", "后台任务失败", "后台任务执行失败，需要复核。"),
    "chat_job_completed": ("info", "Chat任务完成", "长时间Chat任务已完成。"),
    "chat_job_failed": ("warning", "Chat任务失败", "长时间Chat任务执行失败。"),
    "email_delivery_test": ("info", "测试邮件已排队", "测试邮件正在等待通知 Worker 投递。"),
    "premarket_digest_ready": ("info", "开盘前摘要已生成", "开盘前结构化摘要已生成。"),
        "account_daily_report_ready": ("info", "账户日报已生成", "收盘后账户日报已生成。"),
    }
)

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
MANDATORY_NOTIFICATION_TYPES = {
    "paper_order_rejected",
    "paper_cash_insufficient",
    "paper_position_risk",
    "paper_data_stale",
    "security_event",
    "invalidation_triggered",
}
NON_COALESCING_NOTIFICATION_TYPES = {"email_delivery_test"}


def _ensure_column(
    db: sqlite3.Connection, table: str, column: str, declaration: str
) -> None:
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def ensure_notification_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            notification_id TEXT PRIMARY KEY,
            notification_type TEXT NOT NULL,
            severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            account_id TEXT,
            symbol TEXT,
            order_id TEXT,
            research_run_id TEXT,
            task_id TEXT,
            data_as_of TEXT,
            read_at TEXT,
            archived_at TEXT,
            dedup_key TEXT NOT NULL UNIQUE,
            expires_at TEXT,
            action_type TEXT,
            action_payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notification_preferences (
            notification_type TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            minimum_severity TEXT NOT NULL DEFAULT 'info',
            cooldown_seconds INTEGER NOT NULL DEFAULT 300,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS notification_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            processed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS notification_outbox (
            outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            dedup_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            available_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            processed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS notification_delivery_attempts (
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            notification_id TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'in_app',
            status TEXT NOT NULL,
            error_detail TEXT,
            attempted_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notification_rules (
            rule_id TEXT PRIMARY KEY,
            account_id TEXT,
            symbol TEXT,
            industry TEXT,
            rule_type TEXT NOT NULL,
            threshold REAL,
            consecutive_periods INTEGER NOT NULL DEFAULT 2,
            cooldown_seconds INTEGER NOT NULL DEFAULT 3600,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_source TEXT NOT NULL DEFAULT 'api',
            last_triggered_at TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_unread
          ON notifications(read_at,archived_at,created_at);
        CREATE INDEX IF NOT EXISTS idx_notification_outbox_pending
          ON notification_outbox(status,available_at,outbox_id);
        """
    )
    _ensure_column(db, "notification_outbox", "max_attempts", "INTEGER NOT NULL DEFAULT 5")
    _ensure_column(db, "notification_outbox", "last_attempt_at", "TEXT")
    _ensure_column(db, "notification_outbox", "dead_lettered_at", "TEXT")


def enqueue_outbox(
    db: sqlite3.Connection,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, Any],
    dedup_key: str,
    event_id: str | None = None,
    available_at: datetime | None = None,
) -> str:
    resolved_event_id = event_id or str(uuid.uuid4())
    now = datetime.now(UTC)
    db.execute(
        """
        INSERT OR IGNORE INTO notification_outbox(
            event_id,event_type,aggregate_type,aggregate_id,payload,dedup_key,
            available_at,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            resolved_event_id,
            event_type,
            aggregate_type,
            aggregate_id,
            json.dumps(sanitize_for_export(payload), ensure_ascii=False),
            dedup_key,
            (available_at or now).isoformat(),
            now.isoformat(),
        ),
    )
    return resolved_event_id


class NotificationRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            ensure_notification_schema(db)
        record_component_migration(
            self.path,
            component="notifications",
            version=4,
            migration_identity="round4-notifications-runtime-v1",
        )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def process_outbox(self, limit: int = 100) -> dict[str, int]:
        processed = 0
        failed = 0
        dead_lettered = 0
        now = datetime.now(UTC)
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM notification_outbox
                WHERE status='pending' AND available_at<=?
                ORDER BY outbox_id LIMIT ?
                """,
                (now.isoformat(), limit),
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                    db.execute(
                        """
                        INSERT OR IGNORE INTO notification_events(
                            event_id,event_type,payload,created_at,processed_at
                        ) VALUES(?,?,?,?,?)
                        """,
                        (
                            row["event_id"],
                            row["event_type"],
                            row["payload"],
                            row["created_at"],
                            now.isoformat(),
                        ),
                    )
                    self._create_notification_in_tx(
                        db,
                        event_type=row["event_type"],
                        payload=payload,
                        dedup_key=row["dedup_key"],
                        created_at=row["created_at"],
                    )
                    db.execute(
                        """
                        UPDATE notification_outbox
                        SET status='processed',processed_at=?,attempts=attempts+1,
                            last_attempt_at=?,last_error=NULL
                        WHERE outbox_id=?
                        """,
                        (now.isoformat(), now.isoformat(), row["outbox_id"]),
                    )
                    processed += 1
                except Exception as exc:
                    attempts = int(row["attempts"]) + 1
                    is_dead = attempts >= int(row["max_attempts"])
                    available_at = now + timedelta(seconds=min(3600, 2 ** attempts * 5))
                    db.execute(
                        """
                        UPDATE notification_outbox
                        SET attempts=?,last_attempt_at=?,last_error=?,status=?,
                            available_at=?,dead_lettered_at=?
                        WHERE outbox_id=?
                        """,
                        (
                            attempts,
                            now.isoformat(),
                            safe_error_detail(exc),
                            "dead_letter" if is_dead else "pending",
                            available_at.isoformat(),
                            now.isoformat() if is_dead else None,
                            row["outbox_id"],
                        ),
                    )
                    failed += 1
                    dead_lettered += int(is_dead)
        return {
            "processed": processed,
            "failed": failed,
            "dead_lettered": dead_lettered,
        }

    def emit(
        self,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        dedup_key: str,
        defer: bool = False,
    ) -> str:
        with self.connect() as db:
            event_id = enqueue_outbox(
                db,
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload=payload,
                dedup_key=dedup_key,
            )
        if not defer:
            self.process_outbox()
        return event_id

    def list(
        self,
        *,
        unread_only: bool = False,
        include_archived: bool = False,
        account_id: str | None = None,
        symbol: str | None = None,
        severity: str | None = None,
        notification_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if unread_only:
            clauses.append("read_at IS NULL")
        if not include_archived:
            clauses.append("archived_at IS NULL")
        if account_id:
            clauses.append("account_id=?")
            params.append(account_id)
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol)
        if severity:
            if severity not in {"info", "warning", "critical"}:
                raise ValueError("invalid notification severity")
            clauses.append("severity=?")
            params.append(severity)
        if notification_type:
            clauses.append("notification_type=?")
            params.append(notification_type)
        query = "SELECT * FROM notifications"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._notification_row(row) for row in rows]

    def get_by_dedup_key(self, dedup_key: str) -> dict[str, Any] | None:
        """Return the exact notification created for an idempotent event."""

        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM notifications WHERE dedup_key=?",
                (dedup_key,),
            ).fetchone()
        return self._notification_row(row) if row is not None else None

    def unread_count(self, account_id: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM notifications WHERE read_at IS NULL AND archived_at IS NULL"
        params: list[Any] = []
        if account_id:
            query += " AND account_id=?"
            params.append(account_id)
        with self.connect() as db:
            return int(db.execute(query, params).fetchone()[0])

    def unread_attention_count(self, account_id: str | None = None) -> int:
        """Count actionable unread notifications without materializing their payloads."""

        query = (
            "SELECT COUNT(*) FROM notifications "
            "WHERE read_at IS NULL AND archived_at IS NULL "
            "AND severity IN ('warning','critical')"
        )
        params: list[Any] = []
        if account_id:
            query += " AND account_id=?"
            params.append(account_id)
        with self.connect() as db:
            return int(db.execute(query, params).fetchone()[0])

    def mark_read(self, notification_id: str) -> bool:
        with self.connect() as db:
            result = db.execute(
                "UPDATE notifications SET read_at=COALESCE(read_at,?) WHERE notification_id=?",
                (datetime.now(UTC).isoformat(), notification_id),
            )
        return result.rowcount > 0

    def mark_all_read(self, account_id: str | None = None) -> int:
        query = "UPDATE notifications SET read_at=COALESCE(read_at,?) WHERE archived_at IS NULL"
        params: list[Any] = [datetime.now(UTC).isoformat()]
        if account_id:
            query += " AND account_id=?"
            params.append(account_id)
        with self.connect() as db:
            result = db.execute(query, params)
        return result.rowcount

    def archive(self, notification_id: str) -> bool:
        with self.connect() as db:
            result = db.execute(
                "UPDATE notifications SET archived_at=? WHERE notification_id=?",
                (datetime.now(UTC).isoformat(), notification_id),
            )
        return result.rowcount > 0

    def preferences(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM notification_preferences ORDER BY notification_type"
            ).fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def update_preferences(self, preferences: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self.connect() as db:
            for item in preferences:
                notification_type = str(item["notification_type"])
                enabled = bool(item.get("enabled", True))
                severity = str(item.get("minimum_severity", "info"))
                cooldown = max(0, int(item.get("cooldown_seconds", 300)))
                if severity not in {"info", "warning", "critical"}:
                    raise ValueError("invalid notification minimum severity")
                db.execute(
                    """
                    INSERT INTO notification_preferences(
                        notification_type,enabled,minimum_severity,cooldown_seconds
                    ) VALUES(?,?,?,?)
                    ON CONFLICT(notification_type) DO UPDATE SET
                        enabled=excluded.enabled,
                        minimum_severity=excluded.minimum_severity,
                        cooldown_seconds=excluded.cooldown_seconds,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (notification_type, int(enabled), severity, cooldown),
                )
        return self.preferences()

    def create_rule(
        self,
        *,
        rule_type: str,
        idempotency_key: str,
        account_id: str | None = None,
        symbol: str | None = None,
        industry: str | None = None,
        threshold: float | None = None,
        consecutive_periods: int = 2,
        cooldown_seconds: int = 3_600,
        enabled: bool = True,
        created_source: str = "api",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if consecutive_periods < 1:
            raise ValueError("notification rule consecutive periods must be positive")
        if cooldown_seconds < 0:
            raise ValueError("notification rule cooldown must be non-negative")
        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM notification_rules WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return self._rule_row(existing)
            rule_id = str(uuid.uuid4())
            now = _iso_now()
            db.execute(
                """
                INSERT INTO notification_rules(
                    rule_id,account_id,symbol,industry,rule_type,threshold,
                    consecutive_periods,cooldown_seconds,enabled,created_source,
                    idempotency_key,payload,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    rule_id,
                    account_id,
                    symbol,
                    industry,
                    rule_type,
                    threshold,
                    consecutive_periods,
                    cooldown_seconds,
                    int(enabled),
                    created_source,
                    idempotency_key,
                    json.dumps(sanitize_for_export(payload or {}), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM notification_rules WHERE rule_id=?",
                (rule_id,),
            ).fetchone()
        return self._rule_row(row)

    def rules(
        self,
        *,
        account_id: str | None = None,
        rule_type: str | None = None,
        enabled_only: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if account_id is not None:
            clauses.append("(account_id IS NULL OR account_id=?)")
            params.append(account_id)
        if rule_type:
            clauses.append("rule_type=?")
            params.append(rule_type)
        if enabled_only:
            clauses.append("enabled=1")
        query = "SELECT * FROM notification_rules"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._rule_row(row) for row in rows]

    def mark_rule_triggered(self, rule_id: str, triggered_at: datetime) -> bool:
        with self.connect() as db:
            result = db.execute(
                """
                UPDATE notification_rules
                SET last_triggered_at=?,updated_at=? WHERE rule_id=? AND enabled=1
                """,
                (triggered_at.isoformat(), _iso_now(), rule_id),
            )
        return result.rowcount > 0

    def _create_notification_in_tx(
        self,
        db: sqlite3.Connection,
        *,
        event_type: str,
        payload: dict[str, Any],
        dedup_key: str,
        created_at: str,
    ) -> None:
        default_severity, default_title, default_content = EVENT_PRESENTATION.get(
            event_type,
            ("info", "QuantLab通知", "系统事件已更新"),
        )
        severity = str(payload.get("severity", default_severity))
        if severity not in SEVERITY_RANK:
            severity = default_severity
        preference = db.execute(
            """
            SELECT enabled,minimum_severity,cooldown_seconds
            FROM notification_preferences WHERE notification_type=?
            """,
            (event_type,),
        ).fetchone()
        mandatory = event_type in MANDATORY_NOTIFICATION_TYPES or severity == "critical"
        if preference and not mandatory:
            if not bool(preference["enabled"]):
                return
            minimum_severity = str(preference["minimum_severity"])
            if SEVERITY_RANK[severity] < SEVERITY_RANK[minimum_severity]:
                return
        cooldown = int(preference["cooldown_seconds"]) if preference else 300
        account_id = payload.get("account_id")
        symbol = payload.get("symbol")
        if (
            cooldown > 0
            and severity != "critical"
            and event_type not in NON_COALESCING_NOTIFICATION_TYPES
        ):
            cutoff = (datetime.now(UTC) - timedelta(seconds=cooldown)).isoformat()
            recent = db.execute(
                """
                SELECT notification_id FROM notifications
                WHERE notification_type=?
                  AND COALESCE(account_id,'')=COALESCE(?,'')
                  AND COALESCE(symbol,'')=COALESCE(?,'')
                  AND created_at>=? AND archived_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (event_type, account_id, symbol, cutoff),
            ).fetchone()
            if recent is not None:
                db.execute(
                    """
                    UPDATE notifications
                    SET title=?,content=?,data_as_of=?,action_type=?,action_payload=?
                    WHERE notification_id=?
                    """,
                    (
                        payload.get("title", default_title),
                        payload.get("content", default_content),
                        payload.get("data_as_of"),
                        payload.get("action_type"),
                        json.dumps(
                            sanitize_for_export(payload.get("action_payload", {})),
                            ensure_ascii=False,
                        ),
                        recent["notification_id"],
                    ),
                )
                return
        notification_id = str(uuid.uuid4())
        result = db.execute(
            """
            INSERT OR IGNORE INTO notifications(
                notification_id,notification_type,severity,title,content,
                account_id,symbol,order_id,research_run_id,task_id,data_as_of,
                dedup_key,expires_at,action_type,action_payload,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                notification_id,
                event_type,
                severity,
                payload.get("title", default_title),
                payload.get("content", default_content),
                account_id,
                symbol,
                payload.get("order_id"),
                payload.get("research_run_id"),
                payload.get("task_id"),
                payload.get("data_as_of"),
                dedup_key,
                payload.get("expires_at"),
                payload.get("action_type"),
                json.dumps(sanitize_for_export(payload.get("action_payload", {})), ensure_ascii=False),
                created_at,
            ),
        )
        if result.rowcount > 0:
            db.execute(
                """
                INSERT INTO notification_delivery_attempts(
                    notification_id,channel,status,attempted_at
                ) VALUES(?,'in_app','delivered',?)
                """,
                (notification_id, _iso_now()),
            )

    @staticmethod
    def _notification_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["action_payload"] = json.loads(item["action_payload"])
        item["read"] = item["read_at"] is not None
        item["archived"] = item["archived_at"] is not None
        return item

    @staticmethod
    def _rule_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["payload"] = json.loads(item["payload"])
        return item


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "MANDATORY_NOTIFICATION_TYPES",
    "NotificationRepository",
    "enqueue_outbox",
    "ensure_notification_schema",
]
