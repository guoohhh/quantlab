from __future__ import annotations

import json
import os
import smtplib
import sqlite3
import ssl
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from email.message import EmailMessage
from typing import Any, Protocol
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from quantlab.config import Settings
from quantlab.persistence.notifications import NotificationRepository, ensure_notification_schema
from quantlab.security import safe_error_detail, sanitize_for_export


class NotificationChannelAdapter(Protocol):
    channel: str

    def send(self, notification: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]: ...


class ChannelDisabled(RuntimeError):
    pass


class EmailChannelAdapter:
    channel = "email"

    def send(self, notification: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        required = {"smtp_host", "from_address", "to_address"}
        missing = sorted(required - set(config))
        if missing:
            raise ChannelDisabled(f"email channel missing configuration: {missing}")
        message = EmailMessage()
        message["Subject"] = str(notification["title"])
        message["From"] = str(config["from_address"])
        message["To"] = str(config["to_address"])
        message.set_content(str(notification["content"]))
        host = str(config["smtp_host"])
        port = int(config.get("smtp_port", 465))
        username = config.get("username")
        password = os.getenv(str(config.get("password_env", ""))) if config.get("password_env") else None
        if username and not password:
            raise ChannelDisabled("email password environment variable is not configured")
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=10) as smtp:
            if username:
                smtp.login(str(username), str(password))
            smtp.send_message(message)
        return {"accepted": True, "recipient": str(config["to_address"])}


class FeishuChannelAdapter:
    channel = "feishu"

    def send(self, notification: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        webhook_env = str(config.get("webhook_env", ""))
        webhook = os.getenv(webhook_env) if webhook_env else None
        if not webhook:
            raise ChannelDisabled("Feishu webhook environment variable is not configured")
        body = json.dumps(
            {
                "msg_type": "text",
                "content": {
                    "text": f"{notification['title']}\n{notification['content']}"
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(webhook, data=body, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:  # noqa: S310 - configured webhook only
            response_body = response.read(4_096).decode("utf-8", errors="replace")
            if response.status >= 300:
                raise RuntimeError(f"Feishu returned HTTP {response.status}")
        return {"accepted": True, "response": response_body[:500]}


class DesktopChannelAdapter:
    channel = "desktop"

    def send(self, notification: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        try:
            from plyer import notification as desktop_notification  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ChannelDisabled("desktop adapter requires the optional plyer package") from exc
        desktop_notification.notify(
            title=str(notification["title"]),
            message=str(notification["content"]),
            app_name=str(config.get("app_name", "QuantLab")),
            timeout=int(config.get("timeout_seconds", 10)),
        )
        return {"accepted": True}


@dataclass
class MemoryChannelAdapter:
    channel: str
    delivered: list[dict[str, Any]]
    fail_count: int = 0

    def send(self, notification: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        if self.fail_count > 0:
            self.fail_count -= 1
            raise RuntimeError("injected delivery failure")
        self.delivered.append({"notification": notification, "config": config})
        return {"accepted": True, "memory_index": len(self.delivered) - 1}


class NotificationDeliveryWorker:
    def __init__(
        self,
        settings: Settings,
        *,
        worker_id: str,
        adapters: dict[str, NotificationChannelAdapter] | None = None,
    ):
        self.settings = settings
        self.path = settings.resolve(settings.get("system.database_path"))
        self.worker_id = worker_id
        self.repository = NotificationRepository(self.path)
        self.adapters = adapters or {
            "email": EmailChannelAdapter(),
            "feishu": FeishuChannelAdapter(),
            "desktop": DesktopChannelAdapter(),
        }
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _init_schema(self) -> None:
        with self.connect() as db:
            ensure_notification_schema(db)
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS notification_channel_preferences (
                    preference_id TEXT PRIMARY KEY,
                    account_id TEXT,
                    channel TEXT NOT NULL CHECK(channel IN ('in_app','email','feishu','desktop')),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    quiet_start TEXT,
                    quiet_end TEXT,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                    daily_maximum INTEGER NOT NULL DEFAULT 50,
                    config TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_id,channel)
                );
                CREATE TABLE IF NOT EXISTS notification_channel_outbox (
                    delivery_id TEXT PRIMARY KEY,
                    notification_id TEXT NOT NULL,
                    account_id TEXT,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    available_at TEXT NOT NULL,
                    claimed_by TEXT,
                    claimed_at TEXT,
                    last_error TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    dead_lettered_at TEXT,
                    FOREIGN KEY(notification_id) REFERENCES notifications(notification_id)
                );
                CREATE TABLE IF NOT EXISTS notification_delivery_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL,
                    notification_id TEXT NOT NULL,
                    account_id TEXT,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_payload TEXT NOT NULL DEFAULT '{}',
                    error_detail TEXT,
                    attempted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_fanout_log (
                    notification_id TEXT PRIMARY KEY,
                    channel_count INTEGER NOT NULL,
                    processed_at TEXT NOT NULL,
                    FOREIGN KEY(notification_id) REFERENCES notifications(notification_id)
                );
                CREATE INDEX IF NOT EXISTS idx_channel_outbox_claim
                  ON notification_channel_outbox(status,available_at,created_at);
                CREATE INDEX IF NOT EXISTS idx_delivery_history_daily
                  ON notification_delivery_history(account_id,channel,attempted_at,status);
                """
            )

    def configure_channel(
        self,
        *,
        channel: str,
        enabled: bool,
        account_id: str | None = None,
        quiet_start: str | None = None,
        quiet_end: str | None = None,
        timezone: str = "Asia/Shanghai",
        daily_maximum: int = 50,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if channel not in {"in_app", "email", "feishu", "desktop"}:
            raise ValueError("unsupported notification channel")
        ZoneInfo(timezone)
        if (quiet_start is None) != (quiet_end is None):
            raise ValueError("quiet_start and quiet_end must be configured together")
        if quiet_start:
            time.fromisoformat(quiet_start)
            time.fromisoformat(quiet_end or "")
        if daily_maximum < 1:
            raise ValueError("daily notification limit must be positive")
        resolved_config = sanitize_for_export(config or {})
        _validate_enabled_config(channel, enabled, resolved_config)
        now = _now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO notification_channel_preferences(
                    preference_id,account_id,channel,enabled,quiet_start,quiet_end,
                    timezone,daily_maximum,config,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account_id,channel) DO UPDATE SET
                    enabled=excluded.enabled,quiet_start=excluded.quiet_start,
                    quiet_end=excluded.quiet_end,timezone=excluded.timezone,
                    daily_maximum=excluded.daily_maximum,config=excluded.config,
                    updated_at=excluded.updated_at
                """,
                (
                    str(uuid.uuid4()),
                    account_id,
                    channel,
                    int(enabled),
                    quiet_start,
                    quiet_end,
                    timezone,
                    daily_maximum,
                    json.dumps(resolved_config, ensure_ascii=False),
                    now,
                ),
            )
            row = db.execute(
                """
                SELECT * FROM notification_channel_preferences
                WHERE COALESCE(account_id,'')=COALESCE(?,'') AND channel=?
                """,
                (account_id, channel),
            ).fetchone()
        return _preference_row(row)

    def preferences(self, account_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM notification_channel_preferences
                WHERE account_id IS NULL OR account_id=? ORDER BY account_id,channel
                """,
                (account_id,),
            ).fetchall()
        return [_preference_row(row) for row in rows]

    def channel_status(
        self,
        channel: str,
        *,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        """Return a redacted, user-facing delivery status for one channel."""

        if channel not in {"in_app", "email", "feishu", "desktop"}:
            raise ValueError("unsupported notification channel")
        with self.connect() as db:
            if account_id is None:
                preference = db.execute(
                    """
                    SELECT * FROM notification_channel_preferences
                    WHERE account_id IS NULL AND channel=?
                    """,
                    (channel,),
                ).fetchone()
                delivery = db.execute(
                    """
                    SELECT d.*, h.status AS last_history_status,
                           h.attempted_at AS last_attempted_at
                    FROM notification_channel_outbox d
                    LEFT JOIN notification_delivery_history h ON h.history_id=(
                        SELECT history_id FROM notification_delivery_history
                        WHERE delivery_id=d.delivery_id ORDER BY history_id DESC LIMIT 1
                    )
                    WHERE d.channel=? AND d.account_id IS NULL
                    ORDER BY d.created_at DESC LIMIT 1
                    """,
                    (channel,),
                ).fetchone()
            else:
                preference = db.execute(
                    """
                    SELECT * FROM notification_channel_preferences
                    WHERE channel=? AND (account_id=? OR account_id IS NULL)
                    ORDER BY CASE WHEN account_id IS NULL THEN 1 ELSE 0 END LIMIT 1
                    """,
                    (channel, account_id),
                ).fetchone()
                delivery = db.execute(
                    """
                    SELECT d.*, h.status AS last_history_status,
                           h.attempted_at AS last_attempted_at
                    FROM notification_channel_outbox d
                    LEFT JOIN notification_delivery_history h ON h.history_id=(
                        SELECT history_id FROM notification_delivery_history
                        WHERE delivery_id=d.delivery_id ORDER BY history_id DESC LIMIT 1
                    )
                    WHERE d.channel=? AND d.account_id=?
                    ORDER BY d.created_at DESC LIMIT 1
                    """,
                    (channel, account_id),
                ).fetchone()
        preference_item = _preference_row(preference) if preference is not None else None
        config = dict((preference_item or {}).get("config") or {})
        issues = _channel_configuration_issues(channel, config)
        configured = preference_item is not None
        enabled = bool((preference_item or {}).get("enabled"))
        configuration_ready = configured and not issues
        latest_delivery = _delivery_status_row(delivery) if delivery is not None else None
        if not configured:
            state = "not_configured"
        elif not configuration_ready:
            state = "not_ready"
        elif not enabled:
            state = "disabled"
        else:
            state = _delivery_state(delivery) if delivery is not None else "ready"
        return {
            "channel": channel,
            "configured": configured,
            "enabled": enabled,
            "configuration_ready": configuration_ready,
            "recipient_configured": bool(config.get("to_address")),
            "ready": bool(configuration_ready and enabled),
            "state": state,
            "latest_delivery": latest_delivery,
        }

    def queue_email_test(self, *, account_id: str | None = None) -> dict[str, Any]:
        """Queue a test email through the normal outbox and Worker path."""

        status = self.channel_status("email", account_id=account_id)
        if not status["ready"]:
            raise ValueError("email channel is not ready for a test delivery")
        dedup_key = f"email-delivery-test:{uuid.uuid4()}"
        event_id = self.repository.emit(
            event_type="email_delivery_test",
            aggregate_type="notification_channel",
            aggregate_id=account_id or "global-email",
            payload={
                "account_id": account_id,
                "title": "QuantLab 测试邮件",
                "content": "这是一封通过通知队列和 Worker 投递的测试邮件。",
            },
            dedup_key=dedup_key,
            # This only turns the transactional notification event into a
            # notification row.  It does not touch SMTP; the external delivery
            # remains queued for the notification Worker below.
            defer=False,
        )
        notification = self.repository.get_by_dedup_key(dedup_key)
        if notification is None:
            raise RuntimeError("test email notification was not created")
        self._fan_out_notification(notification["notification_id"])
        return {
            "event_id": event_id,
            "notification_id": notification["notification_id"],
            "status": "queued",
        }

    def run_once(self, limit: int = 100) -> dict[str, Any]:
        in_app = self.repository.process_outbox(limit=limit)
        fanned_out = self._fan_out(limit=limit * 4)
        delivered = 0
        failed = 0
        dead_lettered = 0
        deferred = 0
        for _ in range(limit):
            delivery = self._claim()
            if delivery is None:
                break
            outcome = self._deliver(delivery)
            delivered += int(outcome == "delivered")
            failed += int(outcome == "retry")
            dead_lettered += int(outcome == "dead_letter")
            deferred += int(outcome in {"quiet_deferred", "daily_limit_deferred"})
        return {
            "in_app": in_app,
            "external_fanned_out": fanned_out,
            "external_delivered": delivered,
            "external_failed": failed,
            "external_dead_lettered": dead_lettered,
            "external_deferred": deferred,
        }

    def cleanup(self, *, message_retention_days: int = 365) -> dict[str, int]:
        cutoff = (datetime.now(UTC) - timedelta(days=message_retention_days)).isoformat()
        with self.connect() as db:
            attempts = db.execute(
                "DELETE FROM notification_delivery_history WHERE attempted_at<?", (cutoff,)
            ).rowcount
            outbox = db.execute(
                """
                DELETE FROM notification_channel_outbox
                WHERE created_at<? AND status IN ('delivered','dead_letter','suppressed')
                """,
                (cutoff,),
            ).rowcount
        return {"delivery_history_deleted": attempts, "outbox_deleted": outbox}

    def _fan_out(self, limit: int) -> int:
        with self.connect() as db:
            notifications = db.execute(
                """
                SELECT n.* FROM notifications n
                WHERE NOT EXISTS (
                    SELECT 1 FROM notification_fanout_log f
                    WHERE f.notification_id=n.notification_id
                )
                ORDER BY n.created_at LIMIT ?
                """,
                (limit,),
            ).fetchall()
            notification_ids = [str(item["notification_id"]) for item in notifications]
        return sum(self._fan_out_notification(notification_id) for notification_id in notification_ids)

    def _fan_out_notification(self, notification_id: str) -> int:
        """Queue one already-created notification without sending it inline."""

        created = 0
        with self.connect() as db:
            notification = db.execute(
                "SELECT * FROM notifications WHERE notification_id=?",
                (notification_id,),
            ).fetchone()
            if notification is None:
                raise ValueError("notification not found")
            already_fanned_out = db.execute(
                "SELECT 1 FROM notification_fanout_log WHERE notification_id=?",
                (notification_id,),
            ).fetchone()
            if already_fanned_out is not None:
                return 0
            preferences = db.execute(
                """
                SELECT * FROM notification_channel_preferences
                WHERE enabled=1 AND channel!='in_app'
                  AND (account_id IS NULL OR account_id=?)
                ORDER BY CASE WHEN account_id IS NULL THEN 1 ELSE 0 END
                """,
                (notification["account_id"],),
            ).fetchall()
            seen_channels: set[str] = set()
            for preference in preferences:
                channel = preference["channel"]
                if channel in seen_channels:
                    continue
                seen_channels.add(channel)
                result = db.execute(
                    """
                    INSERT OR IGNORE INTO notification_channel_outbox(
                        delivery_id,notification_id,account_id,channel,available_at,
                        idempotency_key,created_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        str(uuid.uuid4()),
                        notification["notification_id"],
                        notification["account_id"],
                        channel,
                        _now(),
                        f"{notification['notification_id']}:{channel}",
                        _now(),
                    ),
                )
                created += int(result.rowcount > 0)
            db.execute(
                """
                INSERT OR IGNORE INTO notification_fanout_log(
                    notification_id,channel_count,processed_at
                ) VALUES(?,?,?)
                """,
                (notification["notification_id"], len(seen_channels), _now()),
            )
        return created

    def _claim(self) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # A crashed delivery worker releases its lease after five minutes.
            db.execute(
                """
                UPDATE notification_channel_outbox
                SET status='pending',claimed_by=NULL,claimed_at=NULL
                WHERE status='sending' AND claimed_at<?
                """,
                ((now - timedelta(minutes=5)).isoformat(),),
            )
            row = db.execute(
                """
                SELECT * FROM notification_channel_outbox
                WHERE status='pending' AND available_at<=?
                ORDER BY created_at LIMIT 1
                """,
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                db.commit()
                return None
            db.execute(
                """
                UPDATE notification_channel_outbox
                SET status='sending',claimed_by=?,claimed_at=? WHERE delivery_id=?
                """,
                (self.worker_id, now.isoformat(), row["delivery_id"]),
            )
            claimed = db.execute(
                "SELECT * FROM notification_channel_outbox WHERE delivery_id=?",
                (row["delivery_id"],),
            ).fetchone()
            db.commit()
        return dict(claimed)

    def _deliver(self, delivery: dict[str, Any]) -> str:
        now = datetime.now(UTC)
        with self.connect() as db:
            notification = db.execute(
                "SELECT * FROM notifications WHERE notification_id=?",
                (delivery["notification_id"],),
            ).fetchone()
            preference = db.execute(
                """
                SELECT * FROM notification_channel_preferences
                WHERE channel=? AND enabled=1
                  AND (account_id=? OR account_id IS NULL)
                ORDER BY CASE WHEN account_id IS NULL THEN 1 ELSE 0 END LIMIT 1
                """,
                (delivery["channel"], delivery["account_id"]),
            ).fetchone()
        if notification is None or preference is None:
            return self._finish(delivery, "suppressed", error="channel disabled or notification missing")
        preference_item = _preference_row(preference)
        quiet_until = _quiet_until(now, preference_item)
        if quiet_until:
            self._reschedule(delivery, quiet_until, "quiet hours")
            return "quiet_deferred"
        if self._daily_count(delivery, preference_item, now) >= int(
            preference_item["daily_maximum"]
        ):
            local = now.astimezone(ZoneInfo(preference_item["timezone"]))
            tomorrow = datetime.combine(
                local.date() + timedelta(days=1), time(0, 5), tzinfo=local.tzinfo
            ).astimezone(UTC)
            self._reschedule(delivery, tomorrow, "daily delivery limit reached")
            return "daily_limit_deferred"
        adapter = self.adapters.get(delivery["channel"])
        if adapter is None:
            return self._finish(
                delivery,
                "dead_letter",
                error=f"no adapter configured for {delivery['channel']}",
            )
        notification_item = dict(notification)
        notification_item["action_payload"] = json.loads(notification_item["action_payload"])
        try:
            response = adapter.send(notification_item, preference_item["config"])
            return self._finish(delivery, "delivered", response=response)
        except ChannelDisabled as exc:
            return self._finish(delivery, "dead_letter", error=safe_error_detail(exc))
        except Exception as exc:
            attempts = int(delivery["attempts"]) + 1
            if attempts >= int(delivery["max_attempts"]):
                return self._finish(delivery, "dead_letter", error=safe_error_detail(exc))
            retry_at = now + timedelta(seconds=min(3600, 2 ** attempts * 10))
            self._reschedule(delivery, retry_at, safe_error_detail(exc), attempts=attempts)
            return "retry"

    def _finish(
        self,
        delivery: dict[str, Any],
        status: str,
        *,
        response: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> str:
        now = _now()
        with self.connect() as db:
            db.execute(
                """
                UPDATE notification_channel_outbox SET status=?,attempts=attempts+1,
                    last_error=?,delivered_at=?,dead_lettered_at=?,claimed_by=NULL,
                    claimed_at=NULL WHERE delivery_id=?
                """,
                (
                    status,
                    error,
                    now if status == "delivered" else None,
                    now if status == "dead_letter" else None,
                    delivery["delivery_id"],
                ),
            )
            self._history(db, delivery, status, response or {}, error)
            db.execute(
                """
                INSERT INTO notification_delivery_attempts(
                    notification_id,channel,status,error_detail,attempted_at
                ) VALUES(?,?,?,?,?)
                """,
                (
                    delivery["notification_id"],
                    delivery["channel"],
                    status,
                    error,
                    now,
                ),
            )
        return status

    def _reschedule(
        self,
        delivery: dict[str, Any],
        available_at: datetime,
        reason: str,
        *,
        attempts: int | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE notification_channel_outbox SET status='pending',available_at=?,
                    attempts=?,last_error=?,claimed_by=NULL,claimed_at=NULL
                WHERE delivery_id=?
                """,
                (
                    available_at.astimezone(UTC).isoformat(),
                    int(delivery["attempts"]) if attempts is None else attempts,
                    reason,
                    delivery["delivery_id"],
                ),
            )
            self._history(db, delivery, "deferred", {}, reason)

    def _daily_count(
        self,
        delivery: dict[str, Any],
        preference: dict[str, Any],
        now: datetime,
    ) -> int:
        zone = ZoneInfo(preference["timezone"])
        local = now.astimezone(zone)
        start = datetime.combine(local.date(), time.min, tzinfo=zone).astimezone(UTC)
        with self.connect() as db:
            return int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM notification_delivery_history
                    WHERE COALESCE(account_id,'')=COALESCE(?,'') AND channel=?
                      AND status='delivered' AND attempted_at>=?
                    """,
                    (delivery["account_id"], delivery["channel"], start.isoformat()),
                ).fetchone()[0]
            )

    @staticmethod
    def _history(
        db: sqlite3.Connection,
        delivery: dict[str, Any],
        status: str,
        response: dict[str, Any],
        error: str | None,
    ) -> None:
        db.execute(
            """
            INSERT INTO notification_delivery_history(
                delivery_id,notification_id,account_id,channel,status,
                response_payload,error_detail,attempted_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                delivery["delivery_id"],
                delivery["notification_id"],
                delivery["account_id"],
                delivery["channel"],
                status,
                json.dumps(sanitize_for_export(response), ensure_ascii=False),
                error,
                _now(),
            ),
        )


def _channel_configuration_issues(channel: str, config: dict[str, Any]) -> tuple[str, ...]:
    if channel == "in_app":
        return ()
    required = {
        "email": {"smtp_host", "from_address", "to_address"},
        "feishu": {"webhook_env"},
        "desktop": set(),
    }[channel]
    issues = [f"missing:{name}" for name in sorted(required - set(config))]
    if channel == "email":
        try:
            port = int(config.get("smtp_port", 465))
        except (TypeError, ValueError):
            port = 0
        if not 1 <= port <= 65_535:
            issues.append("invalid:smtp_port")
        username = str(config.get("username") or "").strip()
        password_env = str(config.get("password_env") or "").strip()
        if username and (not password_env or not os.getenv(password_env)):
            issues.append("missing:email_password")
    return tuple(issues)


def _delivery_status_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "delivery_id": str(row["delivery_id"]),
        "status": str(row["status"]),
        "attempts": int(row["attempts"]),
        "created_at": row["created_at"],
        "attempted_at": row["last_attempted_at"] or row["delivered_at"] or row["created_at"],
        "delivered_at": row["delivered_at"],
    }


def _delivery_state(row: sqlite3.Row) -> str:
    status = str(row["status"])
    if status == "delivered":
        return "delivered"
    if status == "sending":
        return "sending"
    if status == "dead_letter":
        return "failed"
    if status == "suppressed":
        return "disabled"
    if status == "pending":
        reason = str(row["last_error"] or "")
        if reason == "quiet hours":
            return "quiet_deferred"
        if reason == "daily delivery limit reached":
            return "daily_limit_deferred"
        if int(row["attempts"] or 0) > 0:
            return "retrying"
        return "queued"
    return "queued"


def _validate_enabled_config(channel: str, enabled: bool, config: dict[str, Any]) -> None:
    if not enabled or channel == "in_app":
        return
    issues = _channel_configuration_issues(channel, config)
    if issues:
        raise ValueError(f"cannot enable {channel}; configuration is incomplete")


def _quiet_until(now: datetime, preference: dict[str, Any]) -> datetime | None:
    if not preference.get("quiet_start"):
        return None
    zone = ZoneInfo(preference["timezone"])
    local = now.astimezone(zone)
    start = time.fromisoformat(preference["quiet_start"])
    end = time.fromisoformat(preference["quiet_end"])
    current = local.timetz().replace(tzinfo=None)
    in_quiet = start <= current < end if start < end else current >= start or current < end
    if not in_quiet:
        return None
    end_date = local.date()
    if start >= end and current >= start:
        end_date += timedelta(days=1)
    return datetime.combine(end_date, end, tzinfo=zone).astimezone(UTC)


def _preference_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    item["config"] = json.loads(item["config"])
    return item


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "ChannelDisabled",
    "DesktopChannelAdapter",
    "EmailChannelAdapter",
    "FeishuChannelAdapter",
    "MemoryChannelAdapter",
    "NotificationChannelAdapter",
    "NotificationDeliveryWorker",
]
