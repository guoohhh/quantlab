from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from quantlab.agents.roundtable import RoundtableResult


class RoundtableRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
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
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_roundtable_source_run "
                "ON roundtable_sessions(source_run_id, created_at DESC)"
            )

    def save(self, result: RoundtableResult) -> None:
        payload = result.model_dump(mode="json")
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO roundtable_sessions(
                    session_id, source_run_id, symbol, topic, participants,
                    rounds, status, payload
                ) VALUES(?,?,?,?,?,?,?,?)
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
                ),
            )

    def get(self, session_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT session_id, source_run_id, symbol, topic, participants,
                       rounds, status, payload, created_at
                FROM roundtable_sessions WHERE session_id=?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        payload = json.loads(item.pop("payload"))
        payload["stored_at"] = item["created_at"]
        return payload

    def recent(self, limit: int = 20) -> list[dict]:
        safe_limit = max(1, min(int(limit), 100))
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT session_id, source_run_id, symbol, topic, participants,
                       rounds, status, created_at
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
