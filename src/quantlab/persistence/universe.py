from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any


class AShareUniverseRepository:
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
                CREATE TABLE IF NOT EXISTS a_share_master_builds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    as_of TEXT NOT NULL,
                    version_hash TEXT NOT NULL UNIQUE,
                    sources_json TEXT NOT NULL,
                    audit_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS a_share_security_master_versions (
                    version_hash TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    board TEXT NOT NULL,
                    listing_date TEXT,
                    delisting_date TEXT,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(version_hash,symbol)
                );
                CREATE TABLE IF NOT EXISTS a_share_universe_snapshots (
                    snapshot_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    source_symbol TEXT,
                    name TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    board TEXT NOT NULL,
                    trade_status INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    captured_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(snapshot_date,symbol)
                );
                CREATE TABLE IF NOT EXISTS a_share_daily_status (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    trade_status INTEGER NOT NULL,
                    is_st INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY(symbol,trade_date)
                );
                CREATE INDEX IF NOT EXISTS idx_a_share_snapshot_symbol
                  ON a_share_universe_snapshots(symbol,snapshot_date);
                CREATE INDEX IF NOT EXISTS idx_a_share_daily_status_date
                  ON a_share_daily_status(trade_date,symbol);
            """)
            self._ensure_column(db, "a_share_universe_snapshots", "source_symbol", "TEXT")

    @staticmethod
    def _ensure_column(
        db: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def save_master_build(
        self,
        *,
        as_of: date,
        version_hash: str,
        records: list[dict[str, Any]],
        sources: list[str],
        audit: dict[str, Any],
    ) -> int:
        with self.connect() as db:
            existing = db.execute(
                "SELECT id FROM a_share_master_builds WHERE version_hash=?", (version_hash,)
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            cursor = db.execute(
                """
                INSERT INTO a_share_master_builds(as_of,version_hash,sources_json,audit_json)
                VALUES(?,?,?,?)
                """,
                (
                    as_of.isoformat(),
                    version_hash,
                    json.dumps(sources, ensure_ascii=False),
                    json.dumps(audit, ensure_ascii=False),
                ),
            )
            db.executemany(
                """
                INSERT INTO a_share_security_master_versions(
                    version_hash,symbol,name,exchange,board,listing_date,delisting_date,
                    status,source,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        version_hash,
                        item["symbol"],
                        item.get("name", item["symbol"]),
                        item["exchange"],
                        item["board"],
                        _date_text(item.get("listing_date")),
                        _date_text(item.get("delisting_date")),
                        item["status"],
                        item["source"],
                        json.dumps(item.get("payload", {}), ensure_ascii=False),
                    )
                    for item in records
                ],
            )
            return int(cursor.lastrowid)

    def latest_master_build(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM a_share_master_builds ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["sources"] = json.loads(item.pop("sources_json"))
        item["audit"] = json.loads(item.pop("audit_json"))
        return item

    def master_records(self, version_hash: str | None = None) -> list[dict[str, Any]]:
        if version_hash is None:
            build = self.latest_master_build()
            if build is None:
                return []
            version_hash = build["version_hash"]
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM a_share_security_master_versions
                WHERE version_hash=? ORDER BY symbol
                """,
                (version_hash,),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            output.append(item)
        return output

    def save_snapshot(
        self, snapshot_date: date, records: list[dict[str, Any]], source: str
    ) -> None:
        with self.connect() as db:
            # A forced recapture is a full snapshot replacement. Without this delete, symbols
            # removed by a data correction or canonical-code migration remain as stale members.
            db.execute(
                "DELETE FROM a_share_universe_snapshots WHERE snapshot_date=?",
                (snapshot_date.isoformat(),),
            )
            db.executemany(
                """
                INSERT INTO a_share_universe_snapshots(
                    snapshot_date,symbol,source_symbol,name,exchange,board,trade_status,source
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(snapshot_date,symbol) DO UPDATE SET
                    source_symbol=excluded.source_symbol,name=excluded.name,
                    exchange=excluded.exchange,board=excluded.board,
                    trade_status=excluded.trade_status,source=excluded.source,
                    captured_at=CURRENT_TIMESTAMP
                """,
                [
                    (
                        snapshot_date.isoformat(),
                        item["symbol"],
                        item.get("source_symbol") or item["symbol"],
                        item.get("name", item["symbol"]),
                        item["exchange"],
                        item["board"],
                        int(bool(item.get("trade_status", True))),
                        source,
                    )
                    for item in records
                ],
            )

    def snapshot(self, snapshot_date: date) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT snapshot_date,symbol,source_symbol,name,exchange,board,trade_status,
                       source,captured_at
                FROM a_share_universe_snapshots WHERE snapshot_date=? ORDER BY symbol
                """,
                (snapshot_date.isoformat(),),
            ).fetchall()
        return [{**dict(row), "trade_status": bool(row["trade_status"])} for row in rows]

    def snapshot_dates(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT snapshot_date,COUNT(*) securities,MIN(captured_at) captured_at
                FROM a_share_universe_snapshots
                GROUP BY snapshot_date ORDER BY snapshot_date DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_daily_status(self, records: list[dict[str, Any]]) -> None:
        with self.connect() as db:
            db.executemany(
                """
                INSERT INTO a_share_daily_status(symbol,trade_date,trade_status,is_st,source)
                VALUES(?,?,?,?,?)
                ON CONFLICT(symbol,trade_date) DO UPDATE SET
                    trade_status=excluded.trade_status,is_st=excluded.is_st,source=excluded.source
                """,
                [
                    (
                        item["symbol"],
                        _date_text(item["trade_date"]),
                        int(bool(item["trade_status"])),
                        int(bool(item["is_st"])),
                        item.get("source", "unknown"),
                    )
                    for item in records
                ],
            )

    def daily_status(self, symbol: str, trade_date: date) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM a_share_daily_status WHERE symbol=? AND trade_date=?",
                (symbol, trade_date.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return {
            **dict(row),
            "trade_status": bool(row["trade_status"]),
            "is_st": bool(row["is_st"]),
        }


def _date_text(value: date | str | None) -> str | None:
    return value.isoformat() if isinstance(value, date) else value
