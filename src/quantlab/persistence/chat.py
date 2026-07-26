from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from quantlab.security import sanitize_for_export
from quantlab.persistence.migrations import record_component_migration


_CURRENT_CHAT_JOB_ID: ContextVar[str | None] = ContextVar(
    "quantlab_current_chat_job_id", default=None
)
_SCHEMA_LOCK = threading.RLock()
_SCHEMA_READY: dict[str, tuple[int, int] | None] = {}


def set_current_chat_job(job_id: str | None) -> None:
    _CURRENT_CHAT_JOB_ID.set(job_id)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _database_identity(path: Path) -> tuple[int, int] | None:
    """Identify a database file without treating normal writes as replacement."""

    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


class ChatRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _init_schema(self) -> None:
        cache_key = str(self.path.resolve()).casefold()
        identity = _database_identity(self.path)
        with _SCHEMA_LOCK:
            # Do not treat a missing database (``identity is None``) as ready.
            # A later constructor must create its schema after a file has been
            # removed or a first connection has not been established yet.
            if (
                identity is not None
                and cache_key in _SCHEMA_READY
                and _SCHEMA_READY[cache_key] == identity
            ):
                return
            with self.connect() as db:
                db.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_conversations (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    account_id TEXT,
                    symbol TEXT,
                    research_run_id TEXT,
                    page_scope TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    idempotency_key TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
                    content TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    model TEXT NOT NULL DEFAULT 'deterministic-tool-router',
                    provider TEXT NOT NULL DEFAULT 'local',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms REAL NOT NULL DEFAULT 0,
                    data_as_of TEXT,
                    context_id TEXT,
                    context_version TEXT,
                    status TEXT NOT NULL DEFAULT 'ok',
                    degraded_reason TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES chat_conversations(conversation_id)
                );
                CREATE TABLE IF NOT EXISTS chat_tool_calls (
                    tool_call_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    message_id TEXT,
                    tool_name TEXT NOT NULL,
                    permission TEXT NOT NULL,
                    arguments TEXT NOT NULL,
                    result TEXT,
                    status TEXT NOT NULL,
                    error_detail TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES chat_conversations(conversation_id)
                );
                CREATE TABLE IF NOT EXISTS chat_citations (
                    citation_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    data_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    as_of TEXT,
                    available_at TEXT,
                    research_run_id TEXT,
                    symbol TEXT,
                    data_quality TEXT,
                    degraded_status TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(message_id) REFERENCES chat_messages(message_id)
                );
                CREATE TABLE IF NOT EXISTS chat_action_drafts (
                    action_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    message_id TEXT,
                    action_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'confirmation_required',
                    account_id TEXT,
                    symbol TEXT,
                    research_run_id TEXT,
                    check_id TEXT,
                    order_id TEXT,
                    draft_payload TEXT NOT NULL,
                    result_payload TEXT NOT NULL DEFAULT '{}',
                    idempotency_key TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES chat_conversations(conversation_id)
                );
                CREATE TABLE IF NOT EXISTS chat_conversation_summaries (
                    conversation_id TEXT PRIMARY KEY,
                    message_count INTEGER NOT NULL,
                    summary_payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES chat_conversations(conversation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation
                  ON chat_messages(conversation_id,created_at);
                CREATE INDEX IF NOT EXISTS idx_chat_actions_conversation
                  ON chat_action_drafts(conversation_id,status,created_at);
                CREATE INDEX IF NOT EXISTS idx_chat_conversations_updated_at
                  ON chat_conversations(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_chat_citations_message_created
                  ON chat_citations(message_id,created_at);
                """
                )
                columns = {
                    row[1] for row in db.execute("PRAGMA table_info(chat_messages)").fetchall()
                }
                for column in ("context_id", "context_version"):
                    if column not in columns:
                        db.execute(f"ALTER TABLE chat_messages ADD COLUMN {column} TEXT")
                for column, declaration in (
                    ("job_id", "TEXT"),
                    ("idempotency_key", "TEXT"),
                    ("retention_until", "TEXT"),
                    ("deleted_at", "TEXT"),
                    ("redacted_at", "TEXT"),
                ):
                    if column not in columns:
                        db.execute(f"ALTER TABLE chat_messages ADD COLUMN {column} {declaration}")
                db.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_message_idempotency
                    ON chat_messages(idempotency_key) WHERE idempotency_key IS NOT NULL
                    """
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_chat_messages_job ON chat_messages(job_id,role)"
                )
                conversation_columns = {
                    row[1]
                    for row in db.execute("PRAGMA table_info(chat_conversations)").fetchall()
                }
                for column, declaration in (
                    ("deleted_at", "TEXT"),
                    ("retention_until", "TEXT"),
                    ("page_scope", "TEXT NOT NULL DEFAULT ''"),
                ):
                    if column not in conversation_columns:
                        db.execute(
                            f"ALTER TABLE chat_conversations ADD COLUMN {column} {declaration}"
                        )
                db.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_chat_conversations_context
                    ON chat_conversations(account_id,symbol,research_run_id,page_scope,updated_at DESC)
                    """
                )
                action_columns = {
                    row[1]
                    for row in db.execute("PRAGMA table_info(chat_action_drafts)").fetchall()
                }
                if "result_payload" not in action_columns:
                    db.execute(
                        "ALTER TABLE chat_action_drafts ADD COLUMN result_payload TEXT NOT NULL DEFAULT '{}'"
                    )
            record_component_migration(
                self.path,
                component="chat",
                version=5,
                migration_identity="round5-chat-context-scope-v1",
            )
            _SCHEMA_READY[cache_key] = _database_identity(self.path)

    def create_conversation(
        self,
        *,
        title: str,
        account_id: str | None = None,
        symbol: str | None = None,
        research_run_id: str | None = None,
        page_scope: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        key = idempotency_key or str(uuid.uuid4())
        normalized_scope = _normalize_page_scope(page_scope)
        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM chat_conversations WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing is not None:
                identity = {
                    "account_id": account_id,
                    "symbol": symbol,
                    "research_run_id": research_run_id,
                    "page_scope": normalized_scope,
                }
                if any(existing[field] != value for field, value in identity.items()):
                    raise ValueError(
                        "chat conversation idempotency key is bound to a different identity"
                    )
                return dict(existing)
            conversation_id = str(uuid.uuid4())
            now = _now()
            db.execute(
                """
                INSERT INTO chat_conversations(
                    conversation_id,title,account_id,symbol,research_run_id,page_scope,
                    idempotency_key,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    conversation_id,
                    title.strip() or "QuantLab会话",
                    account_id,
                    symbol,
                    research_run_id,
                    normalized_scope,
                    key,
                    now,
                    now,
                ),
            )
            return dict(
                db.execute(
                    "SELECT * FROM chat_conversations WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()
            )

    def conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM chat_conversations
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def conversations_for_context(
        self,
        *,
        account_id: str | None,
        symbol: str | None,
        research_run_id: str | None,
        page_scope: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return only conversations with an exact, persisted UI identity."""

        normalized_scope = _normalize_page_scope(page_scope)
        clauses = ["status='active'", "deleted_at IS NULL", "page_scope=?"]
        params: list[Any] = [normalized_scope]
        for field, value in (
            ("account_id", account_id),
            ("symbol", symbol),
            ("research_run_id", research_run_id),
        ):
            if value is None:
                clauses.append(f"{field} IS NULL")
            else:
                clauses.append(f"{field}=?")
                params.append(value)
        params.append(max(1, min(int(limit), 200)))
        query = (
            "SELECT * FROM chat_conversations WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC LIMIT ?"
        )
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM chat_conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def add_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        payload: dict[str, Any] | None = None,
        model: str = "deterministic-tool-router",
        provider: str = "local",
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
        data_as_of: str | None = None,
        context_id: str | None = None,
        context_version: str | None = None,
        status: str = "ok",
        degraded_reason: str | None = None,
        job_id: str | None = None,
        idempotency_key: str | None = None,
        retention_days: int = 365,
    ) -> dict[str, Any]:
        if retention_days < 1:
            raise ValueError("chat message retention must be positive")
        resolved_job_id = job_id or _CURRENT_CHAT_JOB_ID.get()
        resolved_idempotency_key = idempotency_key
        if resolved_job_id and role == "assistant" and resolved_idempotency_key is None:
            resolved_idempotency_key = f"chat-job:{resolved_job_id}:assistant"
        message_id = str(uuid.uuid4())
        now = _now()
        retention_until = (datetime.now(UTC) + timedelta(days=retention_days)).isoformat()
        with self.connect() as db:
            if resolved_idempotency_key:
                existing = db.execute(
                    "SELECT * FROM chat_messages WHERE idempotency_key=?",
                    (resolved_idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return self._message_row(existing)
            if db.execute(
                """
                SELECT 1 FROM chat_conversations
                WHERE conversation_id=? AND status='active' AND deleted_at IS NULL
                """,
                (conversation_id,),
            ).fetchone() is None:
                raise ValueError("chat conversation not found")
            db.execute(
                """
                INSERT INTO chat_messages(
                    message_id,conversation_id,role,content,payload,model,provider,
                    input_tokens,output_tokens,latency_ms,data_as_of,status,
                    context_id,context_version,degraded_reason,created_at,
                    job_id,idempotency_key,retention_until
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    message_id,
                    conversation_id,
                    role,
                    content,
                    json.dumps(sanitize_for_export(payload or {}), ensure_ascii=False),
                    model,
                    provider,
                    max(0, input_tokens),
                    max(0, output_tokens),
                    max(0.0, latency_ms),
                    data_as_of,
                    status,
                    context_id,
                    context_version,
                    degraded_reason,
                    now,
                    resolved_job_id,
                    resolved_idempotency_key,
                    retention_until,
                ),
            )
            db.execute(
                """
                UPDATE chat_conversations SET updated_at=? WHERE conversation_id=?
                """,
                (now, conversation_id),
            )
            row = db.execute(
                "SELECT * FROM chat_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
        self.refresh_summary(conversation_id)
        return self._message_row(row)

    def messages(self, conversation_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM chat_messages WHERE conversation_id=?
                  AND deleted_at IS NULL
                ORDER BY created_at DESC LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [self._message_row(row) for row in reversed(rows)]

    def message_by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM chat_messages WHERE idempotency_key=? AND deleted_at IS NULL",
                (idempotency_key,),
            ).fetchone()
        return self._message_row(row) if row else None

    def message(self, message_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM chat_messages WHERE message_id=? AND deleted_at IS NULL",
                (message_id,),
            ).fetchone()
        return self._message_row(row) if row else None

    def messages_for_job(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM chat_messages WHERE job_id=? AND deleted_at IS NULL
                ORDER BY created_at
                """,
                (job_id,),
            ).fetchall()
        return [self._message_row(row) for row in rows]

    def attach_message_job(self, message_id: str, job_id: str) -> dict[str, Any]:
        with self.connect() as db:
            db.execute(
                "UPDATE chat_messages SET job_id=? WHERE message_id=?",
                (job_id, message_id),
            )
            row = db.execute(
                "SELECT * FROM chat_messages WHERE message_id=?", (message_id,)
            ).fetchone()
        if row is None:
            raise ValueError("chat message not found")
        return self._message_row(row)

    def update_message_status(
        self,
        message_id: str,
        *,
        status: str,
        degraded_reason: str | None = None,
    ) -> dict[str, Any]:
        with self.connect() as db:
            db.execute(
                """
                UPDATE chat_messages SET status=?,degraded_reason=? WHERE message_id=?
                """,
                (status, degraded_reason, message_id),
            )
            row = db.execute(
                "SELECT * FROM chat_messages WHERE message_id=?", (message_id,)
            ).fetchone()
        if row is None:
            raise ValueError("chat message not found")
        return self._message_row(row)

    def delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        now = _now()
        with self.connect() as db:
            exists = db.execute(
                "SELECT * FROM chat_conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if exists is None:
                raise ValueError("chat conversation not found")
            db.execute(
                """
                UPDATE chat_conversations SET status='deleted',deleted_at=?,updated_at=?
                WHERE conversation_id=?
                """,
                (now, now, conversation_id),
            )
            db.execute(
                """
                UPDATE chat_messages SET content='[deleted by user]',payload='{}',
                    deleted_at=?,redacted_at=? WHERE conversation_id=? AND deleted_at IS NULL
                """,
                (now, now, conversation_id),
            )
        return {"conversation_id": conversation_id, "status": "deleted", "deleted_at": now}

    def purge_expired(self, as_of: datetime | None = None) -> dict[str, int]:
        cutoff = (as_of or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connect() as db:
            message_ids = [
                row[0]
                for row in db.execute(
                    """
                    SELECT message_id FROM chat_messages
                    WHERE retention_until IS NOT NULL AND retention_until<=?
                    """,
                    (cutoff,),
                ).fetchall()
            ]
            if message_ids:
                placeholders = ",".join("?" for _ in message_ids)
                citations = db.execute(
                    f"DELETE FROM chat_citations WHERE message_id IN ({placeholders})",
                    message_ids,
                ).rowcount
                db.execute(
                    f"UPDATE chat_action_drafts SET message_id=NULL WHERE message_id IN ({placeholders})",
                    message_ids,
                )
                messages = db.execute(
                    f"DELETE FROM chat_messages WHERE message_id IN ({placeholders})",
                    message_ids,
                ).rowcount
            else:
                citations = 0
                messages = 0
        return {"messages_deleted": messages, "citations_deleted": citations}

    def record_tool_call(
        self,
        *,
        conversation_id: str,
        message_id: str | None,
        tool_name: str,
        permission: str,
        arguments: dict[str, Any],
        result: dict[str, Any] | None,
        status: str,
        error_detail: str | None = None,
    ) -> dict[str, Any]:
        tool_call_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO chat_tool_calls(
                    tool_call_id,conversation_id,message_id,tool_name,permission,
                    arguments,result,status,error_detail,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    tool_call_id,
                    conversation_id,
                    message_id,
                    tool_name,
                    permission,
                    json.dumps(sanitize_for_export(arguments), ensure_ascii=False),
                    json.dumps(sanitize_for_export(result), ensure_ascii=False)
                    if result is not None
                    else None,
                    status,
                    error_detail,
                    _now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM chat_tool_calls WHERE tool_call_id=?",
                (tool_call_id,),
            ).fetchone()
        return self._tool_row(row)

    def add_citations(
        self,
        message_id: str,
        citations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        output = []
        with self.connect() as db:
            for citation in citations:
                citation_id = str(uuid.uuid4())
                db.execute(
                    """
                    INSERT INTO chat_citations(
                        citation_id,message_id,data_type,source,as_of,available_at,
                        research_run_id,symbol,data_quality,degraded_status,payload,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        citation_id,
                        message_id,
                        citation["data_type"],
                        citation["source"],
                        citation.get("as_of"),
                        citation.get("available_at"),
                        citation.get("research_run_id"),
                        citation.get("symbol"),
                        citation.get("data_quality"),
                        citation.get("degraded_status"),
                        json.dumps(
                            sanitize_for_export(citation.get("payload", {})),
                            ensure_ascii=False,
                        ),
                        _now(),
                    ),
                )
                output.append(
                    self._citation_row(
                        db.execute(
                            "SELECT * FROM chat_citations WHERE citation_id=?",
                            (citation_id,),
                        ).fetchone()
                    )
                )
        return output

    def citations(self, message_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM chat_citations WHERE message_id=? ORDER BY created_at,rowid",
                (message_id,),
            ).fetchall()
        return [self._citation_row(row) for row in rows]

    def citations_for_messages(
        self,
        message_ids: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Load citations for many rendered messages without per-message queries.

        Conversation views usually show a small, bounded transcript.  Keeping the
        rows grouped by message ID lets callers render those messages without an
        N+1 ``citations()`` loop.  Empty requested IDs are retained in the result
        so a caller can distinguish "no citations" from "not requested".
        """

        unique_ids = list(dict.fromkeys(item.strip() for item in message_ids if item.strip()))
        if not unique_ids:
            return {}
        grouped = {message_id: [] for message_id in unique_ids}
        # SQLite accepts a finite number of bind parameters.  Chunking preserves
        # the same API for exports or future transcript views larger than 999 rows.
        with self.connect() as db:
            for start in range(0, len(unique_ids), 500):
                batch = unique_ids[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = db.execute(
                    f"""
                    SELECT * FROM chat_citations
                    WHERE message_id IN ({placeholders})
                    ORDER BY message_id,created_at,rowid
                    """,
                    batch,
                ).fetchall()
                for row in rows:
                    citation = self._citation_row(row)
                    grouped[citation["message_id"]].append(citation)
        return grouped

    def create_action(
        self,
        *,
        conversation_id: str,
        message_id: str | None,
        action_type: str,
        account_id: str,
        symbol: str,
        research_run_id: str | None,
        check_id: str,
        draft_payload: dict[str, Any],
        idempotency_key: str,
        expires_at: datetime,
    ) -> dict[str, Any]:
        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM chat_action_drafts WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                existing_action = self._action_row(existing)
                expected_identity = {
                    "conversation_id": conversation_id,
                    "action_type": action_type,
                    "account_id": account_id,
                    "symbol": symbol,
                    "research_run_id": research_run_id,
                    "check_id": check_id,
                }
                if any(
                    existing_action[field] != value
                    for field, value in expected_identity.items()
                ) or existing_action["draft_payload"] != sanitize_for_export(draft_payload):
                    raise ValueError(
                        "chat action idempotency key is bound to a different identity"
                    )
                return existing_action
            action_id = str(uuid.uuid4())
            now = _now()
            db.execute(
                """
                INSERT INTO chat_action_drafts(
                    action_id,conversation_id,message_id,action_type,account_id,
                    symbol,research_run_id,check_id,draft_payload,idempotency_key,
                    expires_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    action_id,
                    conversation_id,
                    message_id,
                    action_type,
                    account_id,
                    symbol,
                    research_run_id,
                    check_id,
                    json.dumps(sanitize_for_export(draft_payload), ensure_ascii=False),
                    idempotency_key,
                    expires_at.isoformat(),
                    now,
                    now,
                ),
            )
            return self._action_row(
                db.execute(
                    "SELECT * FROM chat_action_drafts WHERE action_id=?",
                    (action_id,),
                ).fetchone()
            )

    def action(self, action_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM chat_action_drafts WHERE action_id=?",
                (action_id,),
            ).fetchone()
        return self._action_row(row) if row else None

    def actions(self, conversation_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM chat_action_drafts WHERE conversation_id=?
                ORDER BY created_at DESC LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [self._action_row(row) for row in rows]

    def update_action(
        self,
        action_id: str,
        *,
        status: str,
        order_id: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.connect() as db:
            db.execute(
                """
                UPDATE chat_action_drafts
                SET status=?,order_id=COALESCE(?,order_id),
                    result_payload=CASE WHEN ? IS NULL THEN result_payload ELSE ? END,
                    updated_at=?
                WHERE action_id=?
                """,
                (
                    status,
                    order_id,
                    None if result is None else 1,
                    json.dumps(sanitize_for_export(result or {}), ensure_ascii=False),
                    _now(),
                    action_id,
                ),
            )
            row = db.execute(
                "SELECT * FROM chat_action_drafts WHERE action_id=?",
                (action_id,),
            ).fetchone()
        if row is None:
            raise ValueError("chat action not found")
        return self._action_row(row)

    def refresh_summary(self, conversation_id: str) -> dict[str, Any] | None:
        messages = self.messages(conversation_id, 200)
        if len(messages) < 20:
            return None
        conversation = self.conversation(conversation_id)
        actions = self.actions(conversation_id, 20)
        summary = {
            "current_question": next(
                (item["content"] for item in reversed(messages) if item["role"] == "user"),
                "",
            ),
            "account_id": conversation.get("account_id") if conversation else None,
            "symbol": conversation.get("symbol") if conversation else None,
            "research_run_id": conversation.get("research_run_id") if conversation else None,
            "unresolved_actions": [
                {
                    "action_id": item["action_id"],
                    "action_type": item["action_type"],
                    "status": item["status"],
                }
                for item in actions
                if item["status"] == "confirmation_required"
            ],
            "memory_boundary": (
                "chat history is not a permanent investment preference and cannot relax risk gates"
            ),
        }
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO chat_conversation_summaries(
                    conversation_id,message_count,summary_payload,updated_at
                ) VALUES(?,?,?,?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    message_count=excluded.message_count,
                    summary_payload=excluded.summary_payload,
                    updated_at=excluded.updated_at
                """,
                (
                    conversation_id,
                    len(messages),
                    json.dumps(summary, ensure_ascii=False),
                    _now(),
                ),
            )
        return summary

    @staticmethod
    def _message_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _tool_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["arguments"] = json.loads(item["arguments"])
        item["result"] = json.loads(item["result"]) if item["result"] else None
        return item

    @staticmethod
    def _citation_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _action_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["draft_payload"] = json.loads(item["draft_payload"])
        item["result_payload"] = json.loads(item.get("result_payload") or "{}")
        return item


def _normalize_page_scope(value: str | None) -> str:
    scope = str(value or "").strip()
    if "\n" in scope or "\r" in scope:
        raise ValueError("chat page scope cannot contain newlines")
    if len(scope) > 160:
        raise ValueError("chat page scope is too long")
    return scope


__all__ = ["ChatRepository", "set_current_chat_job"]
