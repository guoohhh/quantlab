from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any


class HistoricalReplayRepository:
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
            db.executescript("""
                CREATE TABLE IF NOT EXISTS historical_replays (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    episodes INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)

    def save(
        self,
        start: date,
        end: date,
        horizon_days: int,
        episodes: int,
        status: str,
        payload: dict[str, Any],
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO historical_replays(
                    start_date,end_date,horizon_days,episodes,status,payload
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    start.isoformat(),
                    end.isoformat(),
                    horizon_days,
                    episodes,
                    status,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT id,start_date,end_date,horizon_days,episodes,status,created_at
                FROM historical_replays ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, replay_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM historical_replays WHERE id=?", (replay_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item
