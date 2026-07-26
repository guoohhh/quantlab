from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from quantlab.persistence.migrations import record_component_migration
from quantlab.security import sanitize_for_export


ROUND7_SCHEMA_VERSION = 1


class Round7Repository:
    """Provider health, product-effect revisions and continuous-run observations."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        record_component_migration(
            self.path,
            component="round7",
            version=ROUND7_SCHEMA_VERSION,
            migration_identity="round7-provider-health-soak-adoption-revisions-v1",
        )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _init_schema(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS trusted_provider_health (
                    provider_key TEXT NOT NULL,
                    component TEXT NOT NULL,
                    status TEXT NOT NULL,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    circuit_open_until TEXT,
                    last_attempt_at TEXT NOT NULL,
                    last_success_at TEXT,
                    latency_ms REAL,
                    source_version TEXT NOT NULL,
                    error_type TEXT,
                    error_detail TEXT,
                    detail TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(provider_key,component)
                );
                CREATE TABLE IF NOT EXISTS runtime_soak_observations (
                    observation_id TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_soak_observed
                    ON runtime_soak_observations(observed_at);
                CREATE TABLE IF NOT EXISTS trusted_industry_records (
                    namespace TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    industry TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    effective_date TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    trust_level TEXT NOT NULL,
                    manifest_id TEXT NOT NULL,
                    record_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(namespace,symbol,classification,effective_date,manifest_id)
                );
                CREATE INDEX IF NOT EXISTS idx_round7_industry_lookup
                    ON trusted_industry_records(namespace,symbol,effective_date,available_at);
                CREATE TABLE IF NOT EXISTS investor_adoption_revisions (
                    revision_id TEXT PRIMARY KEY,
                    recommendation_id TEXT NOT NULL,
                    revision_number INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    actual_quantity INTEGER,
                    actual_price REAL,
                    actual_trade_date TEXT,
                    transaction_cost REAL NOT NULL DEFAULT 0,
                    note TEXT,
                    supersedes_revision_id TEXT,
                    settled INTEGER NOT NULL DEFAULT 0,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(recommendation_id,revision_number)
                );
                CREATE INDEX IF NOT EXISTS idx_investor_adoption_revisions_recommendation
                    ON investor_adoption_revisions(recommendation_id,revision_number);
                """
            )

    def provider_available(
        self,
        provider_key: str,
        component: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        observed = (now or datetime.now(UTC)).astimezone(UTC)
        state = self.provider_state(provider_key, component)
        if not state or not state.get("circuit_open_until"):
            return True
        return datetime.fromisoformat(state["circuit_open_until"]).astimezone(UTC) <= observed

    def record_provider_attempt(
        self,
        *,
        provider_key: str,
        component: str,
        status: str,
        latency_ms: float,
        source_version: str,
        error_type: str | None = None,
        error_detail: str | None = None,
        detail: dict[str, Any] | None = None,
        failure_threshold: int = 3,
        cooldown_seconds: float = 300.0,
        attempted_at: datetime | None = None,
    ) -> dict[str, Any]:
        allowed = {"available", "partial", "unavailable", "failed", "timeout"}
        if status not in allowed:
            raise ValueError("unsupported provider health status")
        observed = (attempted_at or datetime.now(UTC)).astimezone(UTC)
        timestamp = observed.isoformat()
        success = status in {"available", "partial"}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                """SELECT * FROM trusted_provider_health
                   WHERE provider_key=? AND component=?""",
                (provider_key, component),
            ).fetchone()
            failures = 0 if success else int(existing["consecutive_failures"] if existing else 0) + 1
            last_success = timestamp if success else existing["last_success_at"] if existing else None
            circuit_open_until = None
            if not success and failures >= max(1, int(failure_threshold)):
                circuit_open_until = (
                    observed + timedelta(seconds=max(1.0, float(cooldown_seconds)))
                ).isoformat()
            db.execute(
                """
                INSERT INTO trusted_provider_health(
                    provider_key,component,status,consecutive_failures,circuit_open_until,
                    last_attempt_at,last_success_at,latency_ms,source_version,error_type,
                    error_detail,detail,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(provider_key,component) DO UPDATE SET
                    status=excluded.status,
                    consecutive_failures=excluded.consecutive_failures,
                    circuit_open_until=excluded.circuit_open_until,
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=excluded.last_success_at,
                    latency_ms=excluded.latency_ms,
                    source_version=excluded.source_version,
                    error_type=excluded.error_type,
                    error_detail=excluded.error_detail,
                    detail=excluded.detail,
                    updated_at=excluded.updated_at
                """,
                (
                    provider_key,
                    component,
                    status,
                    failures,
                    circuit_open_until,
                    timestamp,
                    last_success,
                    max(0.0, float(latency_ms)),
                    source_version,
                    error_type,
                    error_detail,
                    json.dumps(sanitize_for_export(detail or {}), ensure_ascii=False),
                    timestamp,
                ),
            )
            row = db.execute(
                """SELECT * FROM trusted_provider_health
                   WHERE provider_key=? AND component=?""",
                (provider_key, component),
            ).fetchone()
            db.commit()
        return _provider_row(row)

    def provider_state(self, provider_key: str, component: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT * FROM trusted_provider_health
                   WHERE provider_key=? AND component=?""",
                (provider_key, component),
            ).fetchone()
        return _provider_row(row) if row else None

    def provider_states(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM trusted_provider_health ORDER BY component,provider_key"
            ).fetchall()
        return [_provider_row(row) for row in rows]

    def save_soak_observation(
        self,
        payload: dict[str, Any],
        *,
        source: str = "runtime",
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
        cleaned = sanitize_for_export(payload)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"observed_at": observed.isoformat(), "source": source, "payload": cleaned},
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO runtime_soak_observations(
                       observation_id,observed_at,source,payload,fingerprint,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()),
                    observed.isoformat(),
                    source,
                    json.dumps(cleaned, ensure_ascii=False),
                    fingerprint,
                    datetime.now(UTC).isoformat(),
                ),
            )
            row = db.execute(
                "SELECT * FROM runtime_soak_observations WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
        return _soak_row(row)

    def save_industry_records(
        self,
        records: list[dict[str, Any]],
        *,
        namespace: str,
        trust_level: str,
        provider: str,
        source_version: str,
        available_at: datetime,
        manifest_id: str,
    ) -> int:
        saved = 0
        observed = available_at.astimezone(UTC).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in records:
                symbol = str(item.get("symbol") or "").strip()
                industry = str(item.get("industry") or "").strip()
                classification = str(item.get("classification") or "unknown").strip()
                effective_date = str(item.get("effective_date") or "")[:10]
                if not symbol or not industry or not effective_date:
                    continue
                fingerprint = hashlib.sha256(
                    json.dumps(
                        [
                            namespace,
                            symbol,
                            industry,
                            classification,
                            effective_date,
                            observed,
                            provider,
                            source_version,
                            manifest_id,
                        ],
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                result = db.execute(
                    """INSERT OR IGNORE INTO trusted_industry_records(
                           namespace,symbol,industry,classification,effective_date,available_at,
                           provider,source_version,trust_level,manifest_id,record_fingerprint,
                           created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        namespace,
                        symbol,
                        industry,
                        classification,
                        effective_date,
                        observed,
                        provider,
                        source_version,
                        trust_level,
                        manifest_id,
                        fingerprint,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                saved += int(result.rowcount > 0)
            db.commit()
        return saved

    def industry_records(
        self,
        *,
        as_of: str | None = None,
        limit: int = 20_000,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM trusted_industry_records WHERE namespace='production'"
        params: list[Any] = []
        if as_of:
            query += " AND effective_date<=? AND available_at<=?"
            params.extend([as_of[:10], as_of])
        query += " ORDER BY symbol,effective_date DESC,available_at DESC LIMIT ?"
        params.append(max(1, min(100_000, int(limit))))
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def soak_observations(
        self,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM runtime_soak_observations WHERE 1=1"
        params: list[Any] = []
        if start_at is not None:
            query += " AND observed_at>=?"
            params.append(start_at.astimezone(UTC).isoformat())
        if end_at is not None:
            query += " AND observed_at<=?"
            params.append(end_at.astimezone(UTC).isoformat())
        query += " ORDER BY observed_at DESC LIMIT ?"
        params.append(max(1, min(100_000, int(limit))))
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [_soak_row(row) for row in rows]

    def adoption_revisions(self, recommendation_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM investor_adoption_revisions
                   WHERE recommendation_id=? ORDER BY revision_number""",
                (recommendation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_adoption_revision(
        self,
        *,
        recommendation_id: str,
        decision: str,
        actual_quantity: int | None,
        actual_price: float | None,
        actual_trade_date: str | None,
        transaction_cost: float,
        note: str | None,
        settled: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "decision": decision,
            "actual_quantity": actual_quantity,
            "actual_price": actual_price,
            "actual_trade_date": actual_trade_date,
            "transaction_cost": float(transaction_cost),
            "note": note,
        }
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute(
                """SELECT * FROM investor_adoption_revisions
                   WHERE recommendation_id=? ORDER BY revision_number DESC LIMIT 1""",
                (recommendation_id,),
            ).fetchone()
            if latest is not None:
                latest_payload = {
                    key: latest[key]
                    for key in (
                        "decision",
                        "actual_quantity",
                        "actual_price",
                        "actual_trade_date",
                        "transaction_cost",
                        "note",
                    )
                }
                if latest_payload == payload:
                    db.commit()
                    return {**dict(latest), "idempotent": True}
                if bool(latest["settled"]):
                    raise ValueError("settled recommendation adoption cannot be revised")
            revision_id = str(uuid.uuid4())
            revision_number = int(latest["revision_number"] if latest else 0) + 1
            db.execute(
                """INSERT INTO investor_adoption_revisions(
                       revision_id,recommendation_id,revision_number,decision,
                       actual_quantity,actual_price,actual_trade_date,transaction_cost,
                       note,supersedes_revision_id,settled,recorded_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    revision_id,
                    recommendation_id,
                    revision_number,
                    decision,
                    actual_quantity,
                    actual_price,
                    actual_trade_date,
                    float(transaction_cost),
                    note,
                    latest["revision_id"] if latest else None,
                    int(settled),
                    datetime.now(UTC).isoformat(),
                ),
            )
            row = db.execute(
                "SELECT * FROM investor_adoption_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
            db.commit()
        return {**dict(row), "idempotent": False}

    def mark_adoption_revisions_settled(self, recommendation_id: str) -> int:
        with self.connect() as db:
            result = db.execute(
                """UPDATE investor_adoption_revisions SET settled=1
                   WHERE recommendation_id=? AND settled=0""",
                (recommendation_id,),
            )
        return int(result.rowcount)


def _provider_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["detail"] = json.loads(item["detail"])
    return item


def _soak_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    return item


__all__ = ["ROUND7_SCHEMA_VERSION", "Round7Repository"]
