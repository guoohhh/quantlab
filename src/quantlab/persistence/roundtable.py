from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantlab.agents.roundtable import RoundtableResult


_SCHEMA_LOCK = threading.RLock()
_SCHEMA_READY: dict[str, tuple[int, int] | None] = {}


class RoundtableRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _init_schema(self) -> None:
        cache_key = str(self.path.resolve()).casefold()
        identity = _database_identity(self.path)
        with _SCHEMA_LOCK:
            if _SCHEMA_READY.get(cache_key) == identity:
                return
            with self.connect() as db:
                db.execute(
                """
                CREATE TABLE IF NOT EXISTS roundtable_sessions (
                    session_id TEXT PRIMARY KEY,
                    source_run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    participants TEXT NOT NULL,
                    rounds INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    job_id TEXT,
                    progress REAL NOT NULL DEFAULT 0,
                    progress_message TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT
                )
                """
                )
                db.execute(
                "CREATE INDEX IF NOT EXISTS idx_roundtable_source_run "
                "ON roundtable_sessions(source_run_id, created_at DESC)"
                )
                present = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(roundtable_sessions)").fetchall()
                }
                additions = {
                "job_id": "TEXT",
                "progress": "REAL NOT NULL DEFAULT 0",
                "progress_message": "TEXT",
                "updated_at": "TEXT",
                }
                added_updated_at = False
                for column, definition in additions.items():
                    if column not in present:
                        db.execute(f"ALTER TABLE roundtable_sessions ADD COLUMN {column} {definition}")
                        added_updated_at = added_updated_at or column == "updated_at"
                if added_updated_at:
                    db.execute(
                        "UPDATE roundtable_sessions SET updated_at=created_at WHERE updated_at IS NULL"
                    )
            _SCHEMA_READY[cache_key] = _database_identity(self.path)

    def save(self, result: RoundtableResult, *, job_id: str | None = None) -> None:
        payload = result.model_dump(mode="json")
        now = _now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO roundtable_sessions(
                    session_id, source_run_id, symbol, topic, participants,
                    rounds, status, payload, job_id, progress, progress_message, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                    source_run_id=excluded.source_run_id,
                    symbol=excluded.symbol,
                    topic=excluded.topic,
                    participants=excluded.participants,
                    rounds=excluded.rounds,
                    status=excluded.status,
                    payload=excluded.payload,
                    job_id=COALESCE(excluded.job_id,roundtable_sessions.job_id),
                    progress=excluded.progress,
                    progress_message=excluded.progress_message,
                    updated_at=excluded.updated_at
                """,
                (
                    result.session_id,
                    result.source_run_id,
                    result.symbol,
                    result.topic,
                    json.dumps(result.participants, ensure_ascii=False),
                    result.rounds,
                    result.status,
                    json.dumps(payload, ensure_ascii=False),
                    job_id,
                    1.0,
                    "讨论已完成",
                    now,
                ),
            )

    def create_pending(
        self,
        *,
        source_run_id: str,
        symbol: str,
        as_of: str,
        topic: str,
        participants: list[str],
        participant_labels: dict[str, str],
        rounds: int,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist a recoverable roundtable before a worker starts it."""

        normalized_session_id = session_id or uuid.uuid4().hex
        payload: dict[str, Any] = {
            "session_id": normalized_session_id,
            "source_run_id": source_run_id,
            "symbol": symbol,
            "as_of": as_of,
            "topic": topic,
            "participants": list(participants),
            "participant_labels": dict(participant_labels),
            "rounds": int(rounds),
            "status": "queued",
            "source_snapshot": {},
            "turns": [],
            "synthesis": {},
            "audit_log": [],
            "execution_boundary": (
                "exploratory_research_only; does_not_modify_formal_decisions_positions_or_orders"
            ),
        }
        now = _now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO roundtable_sessions(
                    session_id,source_run_id,symbol,topic,participants,rounds,status,payload,
                    progress,progress_message,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    normalized_session_id,
                    source_run_id,
                    symbol,
                    topic,
                    json.dumps(participants, ensure_ascii=False),
                    int(rounds),
                    "queued",
                    json.dumps(payload, ensure_ascii=False),
                    0.0,
                    "正在等待后台任务",
                    now,
                    now,
                ),
            )
        return self.get(normalized_session_id) or payload

    def attach_job(self, session_id: str, job_id: str) -> dict[str, Any]:
        return self._update_payload(
            session_id,
            status="queued",
            progress=0.02,
            message="已提交到后台讨论队列",
            job_id=job_id,
        )

    def record_progress(
        self,
        session_id: str,
        *,
        status: str,
        progress: float,
        message: str,
        turn: dict[str, Any] | None = None,
        audit_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._update_payload(
            session_id,
            status=status,
            progress=progress,
            message=message,
            turn=turn,
            audit_event=audit_event,
        )

    def mark_failed(self, session_id: str, message: str) -> dict[str, Any]:
        return self._update_payload(
            session_id,
            status="failed",
            progress=1.0,
            message=message,
        )

    def _update_payload(
        self,
        session_id: str,
        *,
        status: str,
        progress: float,
        message: str,
        job_id: str | None = None,
        turn: dict[str, Any] | None = None,
        audit_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self.connect() as db:
            row = db.execute(
                "SELECT payload,job_id FROM roundtable_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ValueError("roundtable session not found")
            payload = json.loads(row["payload"])
            payload["status"] = status
            if turn is not None:
                turns = list(payload.get("turns") or [])
                identity = (turn.get("round_number"), turn.get("participant"))
                turns = [
                    item
                    for item in turns
                    if (item.get("round_number"), item.get("participant")) != identity
                ]
                turns.append(turn)
                payload["turns"] = turns
            if audit_event is not None:
                payload["audit_log"] = [*(payload.get("audit_log") or []), audit_event]
            db.execute(
                """
                UPDATE roundtable_sessions
                SET status=?,payload=?,job_id=COALESCE(?,job_id),progress=?,
                    progress_message=?,updated_at=?
                WHERE session_id=?
                """,
                (
                    status,
                    json.dumps(payload, ensure_ascii=False),
                    job_id,
                    max(0.0, min(1.0, float(progress))),
                    message,
                    now,
                    session_id,
                ),
            )
        return self.get(session_id) or payload

    def get(self, session_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT session_id, source_run_id, symbol, topic, participants,
                       rounds, status, payload, job_id, progress, progress_message,
                       created_at, updated_at
                FROM roundtable_sessions WHERE session_id=?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        payload = json.loads(item.pop("payload"))
        payload["stored_at"] = item["created_at"]
        payload["updated_at"] = item.get("updated_at")
        payload["job_id"] = item.get("job_id")
        payload["progress"] = float(item.get("progress") or 0.0)
        payload["progress_message"] = item.get("progress_message")
        return payload

    def recent(self, limit: int = 20) -> list[dict]:
        safe_limit = max(1, min(int(limit), 100))
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT session_id, source_run_id, symbol, topic, participants,
                       rounds, status, job_id, progress, progress_message, created_at, updated_at
                FROM roundtable_sessions
                ORDER BY created_at DESC, rowid DESC LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [
            {
                **dict(row),
                "participants": json.loads(row["participants"]),
            }
            for row in rows
        ]

    def sessions_for_source(self, source_run_id: str, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT session_id,source_run_id,symbol,topic,participants,rounds,status,
                       job_id,progress,progress_message,created_at,updated_at
                FROM roundtable_sessions WHERE source_run_id=?
                ORDER BY created_at DESC,rowid DESC LIMIT ?
                """,
                (source_run_id, safe_limit),
            ).fetchall()
        return [
            {**dict(row), "participants": json.loads(row["participants"])} for row in rows
        ]

    def latest_for_source(self, source_run_id: str) -> dict[str, Any] | None:
        sessions = self.sessions_for_source(source_run_id, limit=1)
        return self.get(str(sessions[0]["session_id"])) if sessions else None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _database_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino
