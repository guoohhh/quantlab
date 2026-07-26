from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from quantlab.backtest.statistics import paired_block_bootstrap
from quantlab.domain.strategy_evidence import (
    ABLATION_VARIANTS,
    AblationVariant,
    EvidenceStage,
    PointInTimePoolSnapshot,
    PointInTimeSecurity,
    PointInTimeTradeStatus,
    VariantPrediction,
)
from quantlab.security import sanitize_for_export
from quantlab.persistence.migrations import record_component_migration
from quantlab.domain.data_governance import DataNamespace, DataTrustLevel, trust_rank


class StrategyEvidenceRepository:
    """Append-only storage for point-in-time pools and forward evidence.

    Research replays and forward samples deliberately use different tables. This prevents a
    historical rerun from being counted as evidence that was genuinely unknown at prediction
    time.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        record_component_migration(
            self.path,
            component="strategy_evidence",
            version=5,
            migration_identity="round5-pit-trust-forward-registration-origin-v1",
        )
        record_component_migration(
            self.path,
            component="strategy_evidence",
            version=6,
            migration_identity="formal-evidence-immutable-temporal-audit-exceptions-v1",
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
                CREATE TABLE IF NOT EXISTS strategy_protocols (
                    protocol_version TEXT PRIMARY KEY,
                    protocol_type TEXT NOT NULL,
                    protocol_hash TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    frozen_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pit_security_master (
                    master_version TEXT NOT NULL,
                    security_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    listing_date TEXT NOT NULL,
                    delisting_date TEXT,
                    asset_class TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    record_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    namespace TEXT NOT NULL DEFAULT 'research',
                    trust_level TEXT NOT NULL DEFAULT 'research_external',
                    trust_rank INTEGER NOT NULL DEFAULT 2,
                    manifest_id TEXT,
                    PRIMARY KEY(master_version,security_type,symbol)
                );
                CREATE TABLE IF NOT EXISTS pit_trade_status (
                    security_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    trade_status INTEGER NOT NULL,
                    suspended INTEGER NOT NULL,
                    is_st INTEGER NOT NULL,
                    amount REAL,
                    fund_size REAL,
                    turnover_rate REAL,
                    premium_discount_pct REAL,
                    remaining_balance REAL,
                    redeem_status TEXT,
                    rating TEXT,
                    overseas_market_date TEXT,
                    source TEXT NOT NULL,
                    methodology TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    record_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    namespace TEXT NOT NULL DEFAULT 'research',
                    trust_level TEXT NOT NULL DEFAULT 'research_external',
                    trust_rank INTEGER NOT NULL DEFAULT 2,
                    manifest_id TEXT,
                    PRIMARY KEY(security_type,symbol,trade_date,source,methodology)
                );
                CREATE TABLE IF NOT EXISTS pit_pool_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    snapshot_type TEXT NOT NULL,
                    snapshot_date TEXT NOT NULL,
                    cutoff_at TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    evidence_stage TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    known_gaps TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                    ,namespace TEXT NOT NULL DEFAULT 'research'
                    ,trust_level TEXT NOT NULL DEFAULT 'research_external'
                    ,trust_rank INTEGER NOT NULL DEFAULT 2
                    ,manifest_id TEXT
                    ,refresh_id TEXT
                );
                CREATE TABLE IF NOT EXISTS pit_pool_members (
                    snapshot_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    eligible INTEGER NOT NULL,
                    representative INTEGER NOT NULL,
                    representative_rank INTEGER,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id,symbol),
                    FOREIGN KEY(snapshot_id) REFERENCES pit_pool_snapshots(snapshot_id)
                );
                CREATE TABLE IF NOT EXISTS formal_evidence_audit_exceptions (
                    exception_id TEXT PRIMARY KEY,
                    exception_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status='recorded'),
                    experiment_id TEXT NOT NULL,
                    registration_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    market_date TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_formal_audit_exception_identity
                  ON formal_evidence_audit_exceptions(
                    experiment_id,registration_id,snapshot_id,created_at
                  );
                CREATE TRIGGER IF NOT EXISTS trg_formal_audit_exception_no_update
                BEFORE UPDATE ON formal_evidence_audit_exceptions
                BEGIN
                    SELECT RAISE(ABORT, 'formal evidence audit exceptions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_formal_audit_exception_no_delete
                BEFORE DELETE ON formal_evidence_audit_exceptions
                BEGIN
                    SELECT RAISE(ABORT, 'formal evidence audit exceptions are immutable');
                END;
                CREATE TABLE IF NOT EXISTS strategy_research_runs (
                    run_id TEXT PRIMARY KEY,
                    protocol_version TEXT NOT NULL,
                    strategy_type TEXT NOT NULL,
                    evidence_stage TEXT NOT NULL CHECK(evidence_stage='research_replay'),
                    requested_range TEXT NOT NULL,
                    status TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forward_ablation_cohorts (
                    cohort_id TEXT PRIMARY KEY,
                    protocol_version TEXT NOT NULL,
                    protocol_hash TEXT NOT NULL,
                    frozen_at TEXT NOT NULL,
                    minimum_matured_samples INTEGER NOT NULL DEFAULT 30,
                    status TEXT NOT NULL DEFAULT 'forward_shadow',
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(protocol_version,protocol_hash)
                );
                CREATE TABLE IF NOT EXISTS forward_ablation_predictions (
                    prediction_id TEXT PRIMARY KEY,
                    cohort_id TEXT NOT NULL,
                    sample_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    account_id TEXT,
                    as_of TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL CHECK(horizon_days IN (5,20)),
                    variant TEXT NOT NULL,
                    probabilities TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_weight REAL NOT NULL,
                    actually_triggered INTEGER NOT NULL,
                    data_completeness REAL NOT NULL,
                    role_completeness REAL NOT NULL,
                    context_fingerprint TEXT NOT NULL,
                    start_price REAL NOT NULL DEFAULT 0,
                    quote_source TEXT NOT NULL DEFAULT 'unknown',
                    quote_provider TEXT NOT NULL DEFAULT 'unknown',
                    quote_version TEXT NOT NULL DEFAULT 'unknown',
                    quote_fingerprint TEXT NOT NULL DEFAULT '',
                    strategy_version TEXT NOT NULL DEFAULT 'unknown',
                    prompt_version TEXT NOT NULL DEFAULT 'unknown',
                    governance_version TEXT NOT NULL DEFAULT 'unknown',
                    prediction_fingerprint TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '{}',
                    evidence_stage TEXT NOT NULL DEFAULT 'forward_shadow',
                    created_at TEXT NOT NULL,
                    registration_origin TEXT NOT NULL DEFAULT 'internal',
                    UNIQUE(cohort_id,sample_key,horizon_days,variant),
                    FOREIGN KEY(cohort_id) REFERENCES forward_ablation_cohorts(cohort_id)
                );
                CREATE TABLE IF NOT EXISTS forward_ablation_outcomes (
                    prediction_id TEXT PRIMARY KEY,
                    realized_direction TEXT NOT NULL,
                    realized_return_pct REAL NOT NULL,
                    portfolio_return_pct REAL NOT NULL,
                    turnover REAL NOT NULL DEFAULT 0,
                    drawdown REAL NOT NULL DEFAULT 0,
                    transaction_cost_pct REAL NOT NULL DEFAULT 0,
                    outcome_source TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(prediction_id) REFERENCES forward_ablation_predictions(prediction_id)
                );
                CREATE TABLE IF NOT EXISTS forward_settlement_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    cohort_id TEXT NOT NULL,
                    sample_key TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    quote_fingerprint TEXT,
                    attempted_at TEXT NOT NULL,
                    UNIQUE(cohort_id,sample_key,horizon_days,attempted_at)
                );
                CREATE INDEX IF NOT EXISTS idx_pit_status_lookup
                  ON pit_trade_status(security_type,trade_date,symbol,available_at);
                CREATE INDEX IF NOT EXISTS idx_pit_snapshot_lookup
                  ON pit_pool_snapshots(snapshot_type,snapshot_date,created_at);
                CREATE INDEX IF NOT EXISTS idx_pit_pool_member_eligibility
                  ON pit_pool_members(snapshot_id,eligible);
                CREATE INDEX IF NOT EXISTS idx_forward_due
                  ON forward_ablation_predictions(due_at,evidence_stage,cohort_id);
                CREATE INDEX IF NOT EXISTS idx_forward_scorecard
                  ON forward_ablation_predictions(cohort_id,variant,horizon_days,as_of);
                """
            )
            existing = {
                row[1]
                for row in db.execute(
                    "PRAGMA table_info(forward_ablation_predictions)"
                ).fetchall()
            }
            for column, declaration in (
                ("start_price", "REAL NOT NULL DEFAULT 0"),
                ("quote_source", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("quote_provider", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("quote_version", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("quote_fingerprint", "TEXT NOT NULL DEFAULT ''"),
                ("strategy_version", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("prompt_version", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("governance_version", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("prediction_fingerprint", "TEXT NOT NULL DEFAULT ''"),
                ("registration_origin", "TEXT NOT NULL DEFAULT 'internal'"),
            ):
                if column not in existing:
                    db.execute(
                        f"ALTER TABLE forward_ablation_predictions ADD COLUMN {column} {declaration}"
                    )
            for table in ("pit_security_master", "pit_trade_status", "pit_pool_snapshots"):
                columns = {
                    row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for column, declaration in (
                    ("namespace", "TEXT NOT NULL DEFAULT 'research'"),
                    ("trust_level", "TEXT NOT NULL DEFAULT 'research_external'"),
                    ("trust_rank", "INTEGER NOT NULL DEFAULT 2"),
                    ("manifest_id", "TEXT"),
                ):
                    if column not in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def record_formal_audit_exception(
        self,
        *,
        exception_type: str,
        severity: str,
        experiment_id: str,
        registration_id: str,
        snapshot_id: str,
        market_date: date,
        summary: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = sanitize_for_export(payload)
        identity = {
            "exception_type": exception_type,
            "severity": severity,
            "experiment_id": experiment_id,
            "registration_id": registration_id,
            "snapshot_id": snapshot_id,
            "market_date": market_date.isoformat(),
            "summary": summary,
            "payload": normalized,
        }
        fingerprint = _fingerprint(identity)
        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM formal_evidence_audit_exceptions WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if existing is None:
                exception_id = str(uuid.uuid4())
                db.execute(
                    """INSERT INTO formal_evidence_audit_exceptions(
                           exception_id,exception_type,severity,status,experiment_id,
                           registration_id,snapshot_id,market_date,summary,payload,
                           fingerprint,created_at
                       ) VALUES(?,?,?,'recorded',?,?,?,?,?,?,?,?)""",
                    (
                        exception_id,
                        exception_type,
                        severity,
                        experiment_id,
                        registration_id,
                        snapshot_id,
                        market_date.isoformat(),
                        summary,
                        json.dumps(normalized, ensure_ascii=False, sort_keys=True),
                        fingerprint,
                        _now(),
                    ),
                )
                existing = db.execute(
                    "SELECT * FROM formal_evidence_audit_exceptions WHERE exception_id=?",
                    (exception_id,),
                ).fetchone()
        item = dict(existing)
        item["payload"] = json.loads(item["payload"])
        return item

    def formal_audit_exceptions(
        self,
        *,
        registration_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM formal_evidence_audit_exceptions"
        params: tuple[Any, ...] = ()
        if registration_id:
            query += " WHERE registration_id=?"
            params = (registration_id,)
        query += " ORDER BY created_at,exception_id"
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            output.append(item)
        return output

    def register_protocol(
        self,
        *,
        protocol_version: str,
        protocol_type: str,
        payload: dict[str, Any],
        frozen_at: datetime,
    ) -> dict[str, Any]:
        normalized = json.dumps(
            sanitize_for_export(payload), sort_keys=True, separators=(",", ":")
        )
        protocol_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        now = _now()
        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM strategy_protocols WHERE protocol_version=?",
                (protocol_version,),
            ).fetchone()
            if existing is not None:
                if existing["protocol_hash"] != protocol_hash:
                    raise ValueError("a frozen protocol version cannot be modified")
                return _protocol_row(existing)
            db.execute(
                """
                INSERT INTO strategy_protocols(
                    protocol_version,protocol_type,protocol_hash,payload,frozen_at,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    protocol_version,
                    protocol_type,
                    protocol_hash,
                    normalized,
                    frozen_at.astimezone(UTC).isoformat(),
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM strategy_protocols WHERE protocol_version=?",
                (protocol_version,),
            ).fetchone()
        return _protocol_row(row)

    def protocol(self, protocol_version: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM strategy_protocols WHERE protocol_version=?",
                (protocol_version,),
            ).fetchone()
        return _protocol_row(row) if row else None

    def save_security_master(
        self,
        *,
        master_version: str,
        records: list[PointInTimeSecurity],
    ) -> int:
        now = _now()
        with self.connect() as db:
            for record in records:
                payload = record.model_dump(mode="json")
                stable_payload = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"available_at", "manifest_id"}
                }
                fingerprint = _fingerprint(stable_payload)
                existing = db.execute(
                    """
                    SELECT record_fingerprint FROM pit_security_master
                    WHERE master_version=? AND security_type=? AND symbol=?
                    """,
                    (master_version, record.security_type, record.symbol),
                ).fetchone()
                if existing is not None and existing["record_fingerprint"] != fingerprint:
                    raise ValueError("point-in-time security master versions are immutable")
                db.execute(
                    """
                    INSERT OR IGNORE INTO pit_security_master(
                        master_version,security_type,symbol,name,exchange,listing_date,
                        delisting_date,asset_class,category,status,source,source_version,
                        available_at,payload,record_fingerprint,created_at
                        ,namespace,trust_level,trust_rank,manifest_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        master_version,
                        record.security_type,
                        record.symbol,
                        record.name or record.symbol,
                        record.exchange,
                        record.listing_date.isoformat(),
                        record.delisting_date.isoformat() if record.delisting_date else None,
                        record.asset_class,
                        record.category,
                        record.status,
                        record.source,
                        record.source_version,
                        record.available_at.astimezone(UTC).isoformat(),
                        json.dumps(sanitize_for_export(record.payload), ensure_ascii=False),
                        fingerprint,
                        now,
                        record.namespace.value,
                        record.trust_level.value,
                        trust_rank(record.trust_level),
                        record.manifest_id,
                    ),
                )
        return len(records)

    def security_master(
        self,
        *,
        security_type: str,
        master_version: str,
        cutoff_at: datetime,
    ) -> list[PointInTimeSecurity]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM pit_security_master
                WHERE security_type=? AND master_version=? AND available_at<=?
                ORDER BY symbol
                """,
                (security_type, master_version, cutoff_at.astimezone(UTC).isoformat()),
            ).fetchall()
        return [
            PointInTimeSecurity(
                symbol=row["symbol"],
                name=row["name"],
                security_type=row["security_type"],
                exchange=row["exchange"],
                listing_date=row["listing_date"],
                delisting_date=row["delisting_date"],
                asset_class=row["asset_class"],
                category=row["category"],
                status=row["status"],
                source=row["source"],
                source_version=row["source_version"],
                available_at=row["available_at"],
                payload=json.loads(row["payload"]),
                namespace=row["namespace"],
                trust_level=row["trust_level"],
                manifest_id=row["manifest_id"],
            )
            for row in rows
        ]

    def latest_security_record(
        self,
        security_type: str,
        symbol: str,
        *,
        as_of: date,
        namespace: DataNamespace | str | None = None,
        minimum_trust: DataTrustLevel | str | None = None,
    ) -> dict[str, Any] | None:
        """Return the latest master record knowable on the requested date."""
        with self.connect() as db:
            clauses = [
                "security_type=?",
                "symbol=?",
                "listing_date<=?",
                "(delisting_date IS NULL OR delisting_date>=?)",
                "substr(available_at,1,10)<=?",
            ]
            params: list[Any] = [
                security_type,
                symbol,
                as_of.isoformat(),
                as_of.isoformat(),
                as_of.isoformat(),
            ]
            if namespace is not None:
                clauses.append("namespace=?")
                params.append(DataNamespace(namespace).value)
            if minimum_trust is not None:
                clauses.append("trust_rank>=?")
                params.append(trust_rank(minimum_trust))
            row = db.execute(
                f"""
                SELECT * FROM pit_security_master
                WHERE {' AND '.join(clauses)}
                ORDER BY trust_rank DESC,available_at DESC,created_at DESC LIMIT 1
                """,
                params,
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def save_trade_status(
        self,
        *,
        security_type: str,
        records: list[PointInTimeTradeStatus],
    ) -> int:
        now = _now()
        with self.connect() as db:
            for record in records:
                payload = record.model_dump(mode="json")
                fingerprint = _fingerprint(payload)
                key = (
                    security_type,
                    record.symbol,
                    record.trade_date.isoformat(),
                    record.source,
                    record.methodology,
                )
                existing = db.execute(
                    """
                    SELECT record_fingerprint FROM pit_trade_status
                    WHERE security_type=? AND symbol=? AND trade_date=?
                      AND source=? AND methodology=?
                    """,
                    key,
                ).fetchone()
                if existing is not None and existing["record_fingerprint"] != fingerprint:
                    raise ValueError("point-in-time trade status records are immutable")
                db.execute(
                    """
                    INSERT OR IGNORE INTO pit_trade_status(
                        security_type,symbol,trade_date,trade_status,suspended,is_st,amount,
                        fund_size,turnover_rate,premium_discount_pct,remaining_balance,
                        redeem_status,rating,overseas_market_date,source,methodology,
                        available_at,payload,record_fingerprint,created_at
                        ,namespace,trust_level,trust_rank,manifest_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        *key[:3],
                        int(record.trade_status),
                        int(record.suspended),
                        int(record.is_st),
                        record.amount,
                        record.fund_size,
                        record.turnover_rate,
                        record.premium_discount_pct,
                        record.remaining_balance,
                        record.redeem_status,
                        record.rating,
                        record.overseas_market_date.isoformat()
                        if record.overseas_market_date
                        else None,
                        key[3],
                        key[4],
                        record.available_at.astimezone(UTC).isoformat(),
                        json.dumps(sanitize_for_export(record.payload), ensure_ascii=False),
                        fingerprint,
                        now,
                        record.namespace.value,
                        record.trust_level.value,
                        trust_rank(record.trust_level),
                        record.manifest_id,
                    ),
                )
        return len(records)

    def trade_statuses(
        self,
        *,
        security_type: str,
        trade_date: date,
        cutoff_at: datetime,
        source: str | None = None,
        methodology: str | None = None,
    ) -> list[PointInTimeTradeStatus]:
        clauses = ["security_type=?", "trade_date=?", "available_at<=?"]
        params: list[Any] = [
            security_type,
            trade_date.isoformat(),
            cutoff_at.astimezone(UTC).isoformat(),
        ]
        if source:
            clauses.append("source=?")
            params.append(source)
        if methodology:
            clauses.append("methodology=?")
            params.append(methodology)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM pit_trade_status WHERE {' AND '.join(clauses)} ORDER BY symbol",
                params,
            ).fetchall()
        return [
            PointInTimeTradeStatus(
                symbol=row["symbol"],
                trade_date=row["trade_date"],
                trade_status=bool(row["trade_status"]),
                suspended=bool(row["suspended"]),
                is_st=bool(row["is_st"]),
                amount=row["amount"],
                fund_size=row["fund_size"],
                turnover_rate=row["turnover_rate"],
                premium_discount_pct=row["premium_discount_pct"],
                remaining_balance=row["remaining_balance"],
                redeem_status=row["redeem_status"],
                rating=row["rating"],
                overseas_market_date=row["overseas_market_date"],
                source=row["source"],
                methodology=row["methodology"],
                available_at=row["available_at"],
                payload=json.loads(row["payload"]),
                namespace=row["namespace"],
                trust_level=row["trust_level"],
                manifest_id=row["manifest_id"],
            )
            for row in rows
        ]

    def save_pool_snapshot(self, snapshot: PointInTimePoolSnapshot) -> dict[str, Any]:
        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM pit_pool_snapshots WHERE fingerprint=?",
                (snapshot.fingerprint,),
            ).fetchone()
            if existing is not None:
                return self.pool_snapshot(existing["snapshot_id"])
            snapshot_id = str(uuid.uuid4())
            db.execute(
                """
                INSERT INTO pit_pool_snapshots(
                    snapshot_id,snapshot_type,snapshot_date,cutoff_at,protocol_version,
                    source,source_version,evidence_stage,fingerprint,known_gaps,created_at
                    ,namespace,trust_level,trust_rank,manifest_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id,
                    snapshot.snapshot_type,
                    snapshot.snapshot_date.isoformat(),
                    snapshot.cutoff_at.astimezone(UTC).isoformat(),
                    snapshot.protocol_version,
                    snapshot.source,
                    snapshot.source_version,
                    snapshot.stage.value,
                    snapshot.fingerprint,
                    json.dumps(snapshot.known_gaps, ensure_ascii=False),
                    snapshot.created_at.astimezone(UTC).isoformat(),
                    snapshot.namespace.value,
                    snapshot.trust_level.value,
                    trust_rank(snapshot.trust_level),
                    snapshot.manifest_id,
                ),
            )
            db.executemany(
                """
                INSERT INTO pit_pool_members(
                    snapshot_id,symbol,eligible,representative,representative_rank,payload
                ) VALUES(?,?,?,?,?,?)
                """,
                [
                    (
                        snapshot_id,
                        member.symbol,
                        int(member.eligible),
                        int(member.representative),
                        member.representative_rank,
                        json.dumps(member.model_dump(mode="json"), ensure_ascii=False),
                    )
                    for member in snapshot.members
                ],
            )
        return self.pool_snapshot(snapshot_id)

    def pool_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM pit_pool_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                return None
            members = db.execute(
                "SELECT payload FROM pit_pool_members WHERE snapshot_id=? ORDER BY symbol",
                (snapshot_id,),
            ).fetchall()
        item = dict(row)
        item["known_gaps"] = json.loads(item["known_gaps"])
        item["members"] = [json.loads(member["payload"]) for member in members]
        return item

    def latest_pool_snapshot(
        self,
        snapshot_type: str,
        on_or_before: date | None = None,
        *,
        namespace: DataNamespace | str | None = None,
        minimum_trust: DataTrustLevel | str | None = None,
    ) -> dict[str, Any] | None:
        clause = "snapshot_type=?"
        params: list[Any] = [snapshot_type]
        if on_or_before:
            clause += " AND snapshot_date<=?"
            params.append(on_or_before.isoformat())
        if namespace is not None:
            clause += " AND namespace=?"
            params.append(DataNamespace(namespace).value)
        if minimum_trust is not None:
            clause += " AND trust_rank>=?"
            params.append(trust_rank(minimum_trust))
        with self.connect() as db:
            row = db.execute(
                f"""
                SELECT snapshot_id FROM pit_pool_snapshots WHERE {clause}
                ORDER BY snapshot_date DESC,trust_rank DESC,created_at DESC LIMIT 1
                """,
                params,
            ).fetchone()
        return self.pool_snapshot(row["snapshot_id"]) if row else None

    def latest_pool_metadata(
        self,
        snapshot_type: str,
        on_or_before: date | None = None,
        *,
        namespace: DataNamespace | str | None = None,
        minimum_trust: DataTrustLevel | str | None = None,
    ) -> dict[str, Any] | None:
        """Return readiness metadata without loading or JSON-decoding pool members.

        A production A-share pool can contain thousands of large member payloads.
        Readiness only needs the selected snapshot identity and aggregate counts,
        so loading the full immutable evidence bundle here wastes CPU and can
        stall an otherwise lightweight product page.
        """

        clause = "s.snapshot_type=?"
        params: list[Any] = [snapshot_type]
        if on_or_before:
            clause += " AND s.snapshot_date<=?"
            params.append(on_or_before.isoformat())
        if namespace is not None:
            clause += " AND s.namespace=?"
            params.append(DataNamespace(namespace).value)
        if minimum_trust is not None:
            clause += " AND s.trust_rank>=?"
            params.append(trust_rank(minimum_trust))
        with self.connect() as db:
            row = db.execute(
                f"""
                SELECT s.snapshot_id,s.snapshot_type,s.snapshot_date,s.cutoff_at,
                       s.protocol_version,s.source,s.source_version,s.evidence_stage,
                       s.fingerprint,s.created_at,s.namespace,s.trust_level,s.trust_rank,
                       s.manifest_id,s.refresh_id,
                       (SELECT COUNT(*) FROM pit_pool_members m
                        WHERE m.snapshot_id=s.snapshot_id) AS total_members,
                       (SELECT COUNT(*) FROM pit_pool_members m
                        WHERE m.snapshot_id=s.snapshot_id AND m.eligible=1) AS eligible_members
                FROM pit_pool_snapshots s WHERE {clause}
                ORDER BY s.snapshot_date DESC,s.trust_rank DESC,s.created_at DESC LIMIT 1
                """,
                params,
            ).fetchone()
        return dict(row) if row is not None else None

    def record_research_run(
        self,
        *,
        protocol_version: str,
        strategy_type: str,
        requested_range: dict[str, Any],
        status: str,
        passed: bool,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        sanitized = sanitize_for_export(payload)
        result_hash = _fingerprint(sanitized)
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO strategy_research_runs(
                    run_id,protocol_version,strategy_type,evidence_stage,requested_range,
                    status,passed,payload,result_hash,created_at
                ) VALUES(?,?,?,'research_replay',?,?,?,?,?,?)
                """,
                (
                    run_id,
                    protocol_version,
                    strategy_type,
                    json.dumps(requested_range, ensure_ascii=False),
                    status,
                    int(passed),
                    json.dumps(sanitized, ensure_ascii=False),
                    result_hash,
                    _now(),
                ),
            )
        return {
            "run_id": run_id,
            "protocol_version": protocol_version,
            "strategy_type": strategy_type,
            "evidence_stage": EvidenceStage.RESEARCH_REPLAY.value,
            "status": status,
            "passed": passed,
            "result_hash": result_hash,
        }

    def create_forward_cohort(
        self,
        *,
        protocol_version: str,
        protocol_hash: str,
        frozen_at: datetime,
        minimum_matured_samples: int = 30,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if minimum_matured_samples < 30:
            raise ValueError("forward evidence requires at least 30 matured samples")
        with self.connect() as db:
            existing = db.execute(
                """
                SELECT * FROM forward_ablation_cohorts
                WHERE protocol_version=? AND protocol_hash=?
                """,
                (protocol_version, protocol_hash),
            ).fetchone()
            if existing is not None:
                return _cohort_row(existing)
            cohort_id = str(uuid.uuid4())
            db.execute(
                """
                INSERT INTO forward_ablation_cohorts(
                    cohort_id,protocol_version,protocol_hash,frozen_at,
                    minimum_matured_samples,payload,created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    cohort_id,
                    protocol_version,
                    protocol_hash,
                    frozen_at.astimezone(UTC).isoformat(),
                    minimum_matured_samples,
                    json.dumps(sanitize_for_export(payload or {}), ensure_ascii=False),
                    _now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM forward_ablation_cohorts WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
        return _cohort_row(row)

    def register_forward_sample(
        self,
        *,
        cohort_id: str,
        sample_key: str,
        symbol: str,
        as_of: date,
        due_at: datetime,
        horizon_days: int,
        predictions: list[VariantPrediction],
        context_fingerprint: str,
        start_price: float,
        quote_source: str,
        quote_provider: str,
        quote_version: str,
        quote_fingerprint: str,
        strategy_version: str,
        prompt_version: str,
        governance_version: str,
        account_id: str | None = None,
        registration_origin: str = "internal",
    ) -> list[dict[str, Any]]:
        registered = datetime.now(UTC)
        if start_price <= 0:
            raise ValueError("forward sample requires a positive authoritative start price")
        if horizon_days not in {5, 20}:
            raise ValueError("forward ablation horizon must be 5 or 20 days")
        by_variant = {item.variant: item for item in predictions}
        if set(by_variant) != set(ABLATION_VARIANTS):
            raise ValueError("a forward sample must freeze all seven ablation variants")
        with self.connect() as db:
            cohort = db.execute(
                "SELECT * FROM forward_ablation_cohorts WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if cohort is None:
                raise ValueError("forward ablation cohort not found")
            frozen_at = datetime.fromisoformat(cohort["frozen_at"])
            if registered < frozen_at or as_of < frozen_at.date():
                raise ValueError("historical data cannot be backfilled into a forward cohort")
            if due_at.astimezone(UTC) <= registered:
                raise ValueError("forward sample due_at must be after registration")
            existing = db.execute(
                """
                SELECT * FROM forward_ablation_predictions
                WHERE cohort_id=? AND sample_key=? AND horizon_days=? ORDER BY variant
                """,
                (cohort_id, sample_key, horizon_days),
            ).fetchall()
            if existing:
                return [_prediction_row(row) for row in existing]
            now = _now()
            for variant in ABLATION_VARIANTS:
                item = by_variant[variant]
                prediction_payload = {
                    "variant": variant.value,
                    "probabilities": item.probabilities,
                    "action": item.action,
                    "target_weight": item.target_weight,
                    "actually_triggered": item.actually_triggered,
                    "data_completeness": item.data_completeness,
                    "role_completeness": item.role_completeness,
                    "payload": item.payload,
                    "context_fingerprint": context_fingerprint,
                    "strategy_version": strategy_version,
                    "prompt_version": prompt_version,
                    "governance_version": governance_version,
                }
                prediction_fingerprint = _fingerprint(prediction_payload)
                db.execute(
                    """
                    INSERT OR IGNORE INTO forward_ablation_predictions(
                        prediction_id,cohort_id,sample_key,symbol,account_id,as_of,
                        registered_at,due_at,horizon_days,variant,probabilities,action,
                        target_weight,actually_triggered,data_completeness,role_completeness,
                        context_fingerprint,start_price,quote_source,quote_provider,
                        quote_version,quote_fingerprint,strategy_version,prompt_version,
                        governance_version,prediction_fingerprint,payload,evidence_stage,created_at
                        ,registration_origin
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                             'forward_shadow',?,?)
                    """,
                    (
                        str(uuid.uuid4()),
                        cohort_id,
                        sample_key,
                        symbol,
                        account_id,
                        as_of.isoformat(),
                        registered.isoformat(),
                        due_at.astimezone(UTC).isoformat(),
                        horizon_days,
                        variant.value,
                        json.dumps(item.probabilities, sort_keys=True),
                        item.action,
                        item.target_weight,
                        int(item.actually_triggered),
                        item.data_completeness,
                        item.role_completeness,
                        context_fingerprint,
                        start_price,
                        quote_source,
                        quote_provider,
                        quote_version,
                        quote_fingerprint,
                        strategy_version,
                        prompt_version,
                        governance_version,
                        prediction_fingerprint,
                        json.dumps(sanitize_for_export(item.payload), ensure_ascii=False),
                        now,
                        registration_origin,
                    ),
                )
            rows = db.execute(
                """
                SELECT * FROM forward_ablation_predictions
                WHERE cohort_id=? AND sample_key=? AND horizon_days=? ORDER BY variant
                """,
                (cohort_id, sample_key, horizon_days),
            ).fetchall()
        return [_prediction_row(row) for row in rows]

    def record_forward_settlement_attempt(
        self,
        *,
        cohort_id: str,
        sample_key: str,
        horizon_days: int,
        status: str,
        reason: str | None,
        quote_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        attempted_at = _now()
        attempt_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO forward_settlement_attempts(
                    attempt_id,cohort_id,sample_key,horizon_days,status,reason,
                    quote_fingerprint,attempted_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    attempt_id,
                    cohort_id,
                    sample_key,
                    horizon_days,
                    status,
                    reason,
                    quote_fingerprint,
                    attempted_at,
                ),
            )
        return {
            "attempt_id": attempt_id,
            "status": status,
            "reason": reason,
            "attempted_at": attempted_at,
        }

    def settle_forward_sample(
        self,
        *,
        cohort_id: str,
        sample_key: str,
        horizon_days: int,
        realized_return_pct: float,
        outcome_source: str,
        observed_at: datetime | None = None,
        flat_threshold_pct: float = 1.0,
        turnover_by_variant: dict[str, float] | None = None,
        drawdown_by_variant: dict[str, float] | None = None,
        transaction_cost_pct_by_variant: dict[str, float] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
        if observed > datetime.now(UTC) + timedelta(seconds=1):
            raise ValueError("forward outcome observed_at cannot be in the future")
        if not outcome_source.strip():
            raise ValueError("forward outcome requires a source")
        realized_direction = (
            "up"
            if realized_return_pct > flat_threshold_pct
            else "down"
            if realized_return_pct < -flat_threshold_pct
            else "flat"
        )
        turnover_by_variant = turnover_by_variant or {}
        drawdown_by_variant = drawdown_by_variant or {}
        transaction_cost_pct_by_variant = transaction_cost_pct_by_variant or {}
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM forward_ablation_predictions
                WHERE cohort_id=? AND sample_key=? AND horizon_days=?
                  AND evidence_stage='forward_shadow' ORDER BY variant
                """,
                (cohort_id, sample_key, horizon_days),
            ).fetchall()
            if len(rows) != len(ABLATION_VARIANTS):
                raise ValueError("complete forward sample not found")
            if any(datetime.fromisoformat(row["due_at"]) > observed for row in rows):
                raise ValueError("forward sample has not reached its real due time")
            for row in rows:
                variant = row["variant"]
                cost = max(0.0, float(transaction_cost_pct_by_variant.get(variant, 0.0)))
                triggered = bool(row["actually_triggered"])
                portfolio_return = (
                    realized_return_pct * float(row["target_weight"]) - cost if triggered else 0.0
                )
                existing = db.execute(
                    "SELECT * FROM forward_ablation_outcomes WHERE prediction_id=?",
                    (row["prediction_id"],),
                ).fetchone()
                if existing is not None:
                    if (
                        abs(float(existing["realized_return_pct"]) - realized_return_pct) > 1e-12
                        or existing["outcome_source"] != outcome_source
                    ):
                        raise ValueError("forward outcomes are immutable after settlement")
                    continue
                db.execute(
                    """
                    INSERT INTO forward_ablation_outcomes(
                        prediction_id,realized_direction,realized_return_pct,
                        portfolio_return_pct,turnover,drawdown,transaction_cost_pct,
                        outcome_source,observed_at,payload,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        row["prediction_id"],
                        realized_direction,
                        realized_return_pct,
                        portfolio_return,
                        max(0.0, float(turnover_by_variant.get(variant, 0.0))),
                        float(drawdown_by_variant.get(variant, min(0.0, portfolio_return))),
                        cost,
                        outcome_source,
                        observed.isoformat(),
                        json.dumps(sanitize_for_export(payload or {}), ensure_ascii=False),
                        _now(),
                    ),
                )
            output = db.execute(
                """
                SELECT p.*,o.realized_direction,o.realized_return_pct,o.portfolio_return_pct,
                       o.turnover,o.drawdown,o.transaction_cost_pct,o.outcome_source,o.observed_at
                FROM forward_ablation_predictions p
                JOIN forward_ablation_outcomes o ON o.prediction_id=p.prediction_id
                WHERE p.cohort_id=? AND p.sample_key=? AND p.horizon_days=?
                ORDER BY p.variant
                """,
                (cohort_id, sample_key, horizon_days),
            ).fetchall()
        return [_settled_row(row) for row in output]

    def forward_scorecard(
        self,
        *,
        cohort_id: str,
        horizon_days: int,
        account_id: str | None = None,
        registration_origin: str | None = None,
    ) -> dict[str, Any]:
        """Return a read-only, trade-day-aware scorecard for a frozen forward cohort.

        A cohort can contain several symbols from one signal day.  Those symbols are useful
        cross-sectional observations, but must not be treated as independent time observations
        when making an Alpha claim.  The scorecard therefore retains per-signal diagnostics while
        aggregating economic and inferential metrics by trade day.
        """

        observed_at = datetime.now(UTC)
        with self.connect() as db:
            cohort = db.execute(
                "SELECT * FROM forward_ablation_cohorts WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if cohort is None:
                raise ValueError("forward ablation cohort not found")
            account_clause = " AND p.account_id=?" if account_id is not None else ""
            params: list[Any] = [cohort_id, horizon_days]
            if account_id is not None:
                params.append(account_id)
            origin_clause = " AND p.registration_origin=?" if registration_origin else ""
            if registration_origin:
                params.append(registration_origin)
            rows = db.execute(
                f"""
                SELECT p.*,o.prediction_id AS outcome_prediction_id,o.realized_direction,
                       o.realized_return_pct,o.portfolio_return_pct,o.turnover,o.drawdown,
                       o.transaction_cost_pct,o.observed_at
                FROM forward_ablation_predictions p
                LEFT JOIN forward_ablation_outcomes o ON o.prediction_id=p.prediction_id
                WHERE p.cohort_id=? AND p.horizon_days=?{account_clause}{origin_clause}
                ORDER BY p.as_of,p.sample_key,p.variant
                """,
                params,
            ).fetchall()
            registration = _forward_registration_summary(
                db,
                cohort_id=cohort_id,
                horizon_days=horizon_days,
            )

        sample_coverage = _forward_sample_coverage(rows, observed_at=observed_at)
        grouped: dict[str, list[sqlite3.Row]] = {item.value: [] for item in ABLATION_VARIANTS}
        settled_grouped: dict[str, list[sqlite3.Row]] = {
            item.value: [] for item in ABLATION_VARIANTS
        }
        sample_returns: dict[tuple[str, str], float] = {}
        for row in rows:
            grouped[row["variant"]].append(row)
            if row["outcome_prediction_id"] is None:
                continue
            settled_grouped[row["variant"]].append(row)
            sample_returns[(row["sample_key"], row["variant"])] = float(
                row["portfolio_return_pct"]
            )

        minimum_samples = int(cohort["minimum_matured_samples"])
        # This is deliberately stricter than the frozen sample-count milestone.  A daily
        # representative set may have several symbols, but it still provides one independent
        # market-time observation for a prospective profitability claim.
        minimum_trade_days = max(30, minimum_samples)
        variants: dict[str, Any] = {}
        daily_returns_by_variant: dict[str, dict[str, dict[str, float]]] = {}
        for variant, all_items in grouped.items():
            items = settled_grouped[variant]
            count = len(items)
            briers: list[float] = []
            losses: list[float] = []
            correct = 0
            triggered_returns: list[float] = []
            for row in items:
                probabilities = json.loads(row["probabilities"])
                outcome = row["realized_direction"]
                briers.append(
                    sum(
                        (float(probabilities[key]) - float(key == outcome)) ** 2
                        for key in ("up", "flat", "down")
                    )
                )
                losses.append(-math.log(max(1e-12, float(probabilities[outcome]))))
                predicted = max(("up", "flat", "down"), key=probabilities.get)
                correct += int(predicted == outcome)
                if bool(row["actually_triggered"]):
                    triggered_returns.append(float(row["portfolio_return_pct"]))

            trade_days = _aggregate_forward_trade_days(items)
            daily_returns_by_variant[variant] = trade_days
            daily_values = [trade_days[day] for day in sorted(trade_days)]
            gross_returns = [item["gross_return_pct"] for item in daily_values]
            net_returns = [item["net_return_pct"] for item in daily_values]
            quant_increment = [
                float(row["portfolio_return_pct"])
                - sample_returns.get((row["sample_key"], AblationVariant.QUANT_ONLY.value), 0.0)
                for row in items
            ]
            baseline_increment = [
                float(row["portfolio_return_pct"])
                - sample_returns.get(
                    (row["sample_key"], AblationVariant.SIMPLE_BASELINE.value), 0.0
                )
                for row in items
            ]
            completeness = (
                sum(
                    min(float(row["data_completeness"]), float(row["role_completeness"]))
                    for row in items
                )
                / count
                if count
                else 0.0
            )
            independent_trade_days = len(trade_days)
            stage = (
                EvidenceStage.MEASURED.value
                if (
                    count >= minimum_samples
                    and independent_trade_days >= minimum_trade_days
                    and completeness >= 0.8
                )
                else EvidenceStage.FORWARD_SHADOW.value
            )
            actual_trigger_count = sum(bool(row["actually_triggered"]) for row in items)
            variants[variant] = {
                "stage": stage,
                "claim_status": (
                    "measured_not_strategy_admission"
                    if stage == EvidenceStage.MEASURED.value
                    else "collecting_evidence"
                ),
                "matured_samples": count,
                "minimum_matured_samples": minimum_samples,
                "independent_trade_days": independent_trade_days,
                "minimum_independent_trade_days": minimum_trade_days,
                "brier": _mean(briers),
                "log_loss": _mean(losses),
                "accuracy": correct / count if count else None,
                # Kept for API compatibility.  It is now explicitly a trade-day aggregated,
                # estimated-cost signal return rather than a proxy for executable account NAV.
                "portfolio_return_pct": _compound_return(net_returns),
                "gross_portfolio_return_pct": _compound_return(gross_returns),
                "estimated_net_return_pct": _compound_return(net_returns),
                "estimated_transaction_cost_pct_sum": sum(
                    item["transaction_cost_pct"] for item in daily_values
                ),
                "maximum_drawdown_pct": _maximum_drawdown(net_returns),
                "sample_sequence_drawdown_pct": _maximum_drawdown(net_returns),
                "average_daily_turnover": _mean(
                    [item["turnover"] for item in daily_values]
                ),
                "average_daily_exposure": _mean(
                    [item["exposure"] for item in daily_values]
                ),
                "unconditional_estimated_net_return_pct": _mean(
                    [float(row["portfolio_return_pct"]) for row in items]
                ),
                "conditional_estimated_net_return_pct": _mean(triggered_returns),
                "incremental_vs_quant_pct": _mean(quant_increment),
                "incremental_vs_simple_pct": _mean(baseline_increment),
                "actual_trigger_count": actual_trigger_count,
                "signal_trigger_count": actual_trigger_count,
                "trigger_rate": actual_trigger_count / count if count else None,
                "abstain_rate": 1.0 - actual_trigger_count / count if count else None,
                "data_role_completeness": completeness,
                "metric_scope": "trade_day_aggregated_estimated_signal_returns",
                "execution_boundary": (
                    "estimated signal returns use frozen target weights and configured costs; "
                    "only the shadow-account scorecard is executable simulated P&L"
                ),
            }

        for variant, result in variants.items():
            comparisons = {}
            for comparator in (
                AblationVariant.QUANT_ONLY.value,
                AblationVariant.SIMPLE_BASELINE.value,
            ):
                if comparator == variant:
                    continue
                comparisons[comparator] = _paired_trade_day_comparison(
                    daily_returns_by_variant[variant],
                    daily_returns_by_variant[comparator],
                    minimum_trade_days=minimum_trade_days,
                )
            result["paired_trade_day_comparisons"] = comparisons

        return {
            "cohort": _cohort_row(cohort),
            "horizon_days": horizon_days,
            "account_id": account_id,
            "coverage": sample_coverage,
            "registration": registration,
            "variants": variants,
            "claim_boundary": (
                "Only samples registered after cohort freeze and settled after their real due "
                "time are included. Same-day symbols are aggregated into one trade-day observation "
                "for profitability inference. Estimated signal returns are not the seven shadow "
                "account NAV ledgers; use /api/shadow-accounts/scorecard for executable simulated "
                "P&L. Research replays are structurally excluded."
            ),
        }

    def due_forward_samples(
        self,
        *,
        as_of: datetime | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        cutoff = (as_of or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connect() as db:
            has_wide_links = db.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='wide_forward_prediction_links'"""
            ).fetchone()
            linked_wide_clause = (
                """AND (
                       p.registration_origin NOT LIKE 'wide_forward_%'
                       OR EXISTS(
                           SELECT 1 FROM wide_forward_prediction_links l
                           WHERE l.prediction_id=p.prediction_id
                       )
                   )"""
                if has_wide_links
                else ""
            )
            rows = db.execute(
                f"""
                SELECT p.cohort_id,p.sample_key,p.symbol,p.account_id,p.as_of,p.due_at,
                       p.horizon_days,p.start_price,p.quote_source,p.quote_provider,
                       p.quote_version,p.quote_fingerprint,p.context_fingerprint,
                       p.registration_origin,
                       COUNT(*) variant_count,
                       SUM(CASE WHEN o.prediction_id IS NOT NULL THEN 1 ELSE 0 END) settled_count
                FROM forward_ablation_predictions p
                LEFT JOIN forward_ablation_outcomes o ON o.prediction_id=p.prediction_id
                WHERE p.due_at<=? AND p.evidence_stage='forward_shadow'
                  {linked_wide_clause}
                GROUP BY p.cohort_id,p.sample_key,p.horizon_days
                HAVING settled_count<variant_count
                ORDER BY p.due_at LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        return [dict(row) for row in rows]


def _protocol_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    return item


def _cohort_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    return item


def _prediction_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["probabilities"] = json.loads(item["probabilities"])
    item["payload"] = json.loads(item["payload"])
    item["actually_triggered"] = bool(item["actually_triggered"])
    return item


def _settled_row(row: sqlite3.Row) -> dict[str, Any]:
    item = _prediction_row(row)
    return item


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            sanitize_for_export(payload), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _forward_registration_summary(
    db: sqlite3.Connection,
    *,
    cohort_id: str,
    horizon_days: int,
) -> dict[str, Any]:
    """Return registration denominators when this database has the Round 5 tables.

    Strategy-evidence fixtures predate the formal registration tables, so this remains optional
    for old isolated tests while production always receives the complete accounting view.
    """

    tables = {
        str(row["name"])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('forward_registration_runs','forward_registration_samples')"
        ).fetchall()
    }
    if "forward_registration_samples" not in tables:
        return {
            "available": False,
            "reason": "formal_registration_tables_unavailable",
        }
    status_rows = db.execute(
        """SELECT status,COUNT(*) AS count
           FROM forward_registration_samples
           WHERE cohort_id=? AND horizon_days=?
           GROUP BY status ORDER BY status""",
        (cohort_id, horizon_days),
    ).fetchall()
    status_counts = {str(row["status"]): int(row["count"]) for row in status_rows}
    run_rows: list[sqlite3.Row] = []
    if "forward_registration_runs" in tables:
        run_rows = db.execute(
            """SELECT status,COUNT(*) AS runs,SUM(expected_samples) AS expected_samples,
                      SUM(registered_samples) AS registered_samples,
                      SUM(failed_samples) AS failed_samples,
                      SUM(skipped_samples) AS skipped_samples
               FROM forward_registration_runs WHERE cohort_id=? GROUP BY status ORDER BY status""",
            (cohort_id,),
        ).fetchall()
    return {
        "available": True,
        "recorded_samples": sum(status_counts.values()),
        "status_counts": status_counts,
        "registration_runs": [
            {
                "status": str(row["status"]),
                "runs": int(row["runs"]),
                "expected_samples_all_horizons": int(row["expected_samples"] or 0),
                "registered_samples_all_horizons": int(row["registered_samples"] or 0),
                "failed_samples_all_horizons": int(row["failed_samples"] or 0),
                "skipped_samples_all_horizons": int(row["skipped_samples"] or 0),
            }
            for row in run_rows
        ],
    }


def _as_utc(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def _forward_sample_coverage(
    rows: list[sqlite3.Row], *, observed_at: datetime
) -> dict[str, Any]:
    samples: dict[str, dict[str, Any]] = {}
    for row in rows:
        state = samples.setdefault(
            str(row["sample_key"]),
            {
                "variants": set(),
                "settled_variants": set(),
                "due": False,
            },
        )
        state["variants"].add(str(row["variant"]))
        state["due"] = state["due"] or _as_utc(row["due_at"]) <= observed_at
        if row["outcome_prediction_id"] is not None:
            state["settled_variants"].add(str(row["variant"]))
    required_variants = len(ABLATION_VARIANTS)
    due_samples = [item for item in samples.values() if item["due"]]
    settled_samples = [
        item
        for item in due_samples
        if len(item["variants"]) == required_variants
        and len(item["settled_variants"]) == required_variants
    ]
    partial_due_samples = [item for item in due_samples if item not in settled_samples]
    return {
        "registered_samples": len(samples),
        "registered_prediction_count": len(rows),
        "due_samples": len(due_samples),
        "settled_samples": len(settled_samples),
        "pending_due_samples": len(partial_due_samples),
        "future_samples": sum(1 for item in samples.values() if not item["due"]),
        "settled_prediction_count": sum(
            len(item["settled_variants"]) for item in samples.values()
        ),
        "settlement_coverage": (
            len(settled_samples) / len(due_samples) if due_samples else None
        ),
        "independence_unit": "signal_trade_day",
        "same_day_symbol_warning": (
            "Multiple symbols from one signal day are cross-sectional observations, not "
            "independent market-time observations."
        ),
    }


def _aggregate_forward_trade_days(
    rows: list[sqlite3.Row],
) -> dict[str, dict[str, float]]:
    output: dict[str, dict[str, float]] = {}
    for row in rows:
        trade_day = str(row["as_of"])[:10]
        state = output.setdefault(
            trade_day,
            {
                "gross_return_pct": 0.0,
                "net_return_pct": 0.0,
                "transaction_cost_pct": 0.0,
                "turnover": 0.0,
                "exposure": 0.0,
                "sample_count": 0.0,
                "trigger_count": 0.0,
            },
        )
        triggered = bool(row["actually_triggered"])
        target_weight = float(row["target_weight"])
        gross = (
            float(row["realized_return_pct"]) * target_weight if triggered else 0.0
        )
        net = float(row["portfolio_return_pct"])
        state["gross_return_pct"] += gross
        state["net_return_pct"] += net
        state["transaction_cost_pct"] += (
            float(row["transaction_cost_pct"]) if triggered else 0.0
        )
        state["turnover"] += float(row["turnover"])
        state["exposure"] += target_weight if triggered else 0.0
        state["sample_count"] += 1.0
        state["trigger_count"] += float(triggered)
    return output


def _paired_trade_day_comparison(
    strategy_days: dict[str, dict[str, float]],
    comparator_days: dict[str, dict[str, float]],
    *,
    minimum_trade_days: int,
) -> dict[str, Any]:
    shared_days = sorted(set(strategy_days) & set(comparator_days))
    strategy = [strategy_days[day]["net_return_pct"] / 100.0 for day in shared_days]
    comparator = [comparator_days[day]["net_return_pct"] / 100.0 for day in shared_days]
    excess_pct = [
        (strategy_value - comparator_value) * 100.0
        for strategy_value, comparator_value in zip(strategy, comparator, strict=True)
    ]
    strategy_total = _compound_return([value * 100.0 for value in strategy])
    comparator_total = _compound_return([value * 100.0 for value in comparator])
    if strategy_total is None or comparator_total is None:
        compound_excess = None
    else:
        compound_excess = (
            ((1.0 + strategy_total / 100.0) / (1.0 + comparator_total / 100.0) - 1.0)
            * 100.0
        )
    output: dict[str, Any] = {
        "observation_unit": "independent_trade_day",
        "independent_trade_days": len(shared_days),
        "minimum_independent_trade_days": minimum_trade_days,
        "mean_daily_excess_return_pct": _mean(excess_pct),
        "compound_excess_return_pct": compound_excess,
        "strategy_estimated_net_return_pct": strategy_total,
        "comparator_estimated_net_return_pct": comparator_total,
    }
    if len(shared_days) < minimum_trade_days:
        output.update(
            {
                "status": "insufficient",
                "reason": "fewer_than_minimum_independent_trade_days",
                "inference": None,
            }
        )
        return output
    output.update(
        {
            "status": "measured",
            "inference": paired_block_bootstrap(strategy, comparator),
        }
    )
    return output


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _compound_return(returns_pct: list[float]) -> float | None:
    if not returns_pct:
        return None
    wealth = 1.0
    for value in returns_pct:
        wealth *= 1.0 + value / 100.0
    return (wealth - 1.0) * 100.0


def _maximum_drawdown(returns_pct: list[float]) -> float | None:
    if not returns_pct:
        return None
    wealth = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns_pct:
        wealth *= 1.0 + value / 100.0
        peak = max(peak, wealth)
        worst = min(worst, wealth / peak - 1.0)
    return worst * 100.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["StrategyEvidenceRepository"]
