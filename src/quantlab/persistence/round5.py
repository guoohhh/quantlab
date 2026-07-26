from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterator

from quantlab.domain.data_governance import (
    DataNamespace,
    DataTrustLevel,
    payload_fingerprint,
    trust_rank,
)
from quantlab.domain.strategy_evidence import ABLATION_VARIANTS
from quantlab.persistence.migrations import record_component_migration
from quantlab.security import sanitize_for_export


ROUND5_SCHEMA_VERSION = 3


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Round5Repository:
    """Persistence boundary for scientific forward experiments and investor ledgers."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        record_component_migration(
            self.path,
            component="round5",
            version=1,
            migration_identity="round5-forward-shadow-trust-investor-v1",
        )
        record_component_migration(
            self.path,
            component="round5",
            version=2,
            migration_identity="round5-trusted-manifest-refresh-timing-v2",
        )
        record_component_migration(
            self.path,
            component="round5",
            version=3,
            migration_identity="round5-append-only-registration-recovery-v3",
        )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _init_schema(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS trusted_data_manifests (
                    manifest_id TEXT PRIMARY KEY,
                    batch_type TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    trust_level TEXT NOT NULL,
                    trust_rank INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    source TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    refresh_started_at TEXT,
                    refresh_finalized_at TEXT,
                    snapshot_cutoff_at TEXT,
                    license_status TEXT NOT NULL,
                    payload_fingerprint TEXT NOT NULL,
                    raw_fingerprint TEXT NOT NULL,
                    record_count INTEGER NOT NULL,
                    date_start TEXT,
                    date_end TEXT,
                    status TEXT NOT NULL,
                    missing_reason TEXT,
                    failure_records TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_type,namespace,payload_fingerprint)
                );
                CREATE TABLE IF NOT EXISTS trusted_calendar_days (
                    namespace TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    is_open INTEGER NOT NULL,
                    trust_level TEXT NOT NULL,
                    trust_rank INTEGER NOT NULL,
                    manifest_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    record_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(namespace,trade_date,manifest_id),
                    FOREIGN KEY(manifest_id) REFERENCES trusted_data_manifests(manifest_id)
                );
                CREATE INDEX IF NOT EXISTS idx_round5_calendar_lookup
                  ON trusted_calendar_days(namespace,trade_date,trust_rank,available_at);
                CREATE TABLE IF NOT EXISTS trusted_industry_membership (
                    namespace TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    industry TEXT NOT NULL,
                    effective_date TEXT NOT NULL,
                    trust_level TEXT NOT NULL,
                    trust_rank INTEGER NOT NULL,
                    manifest_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    record_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(namespace,symbol,effective_date,manifest_id),
                    FOREIGN KEY(manifest_id) REFERENCES trusted_data_manifests(manifest_id)
                );
                CREATE INDEX IF NOT EXISTS idx_round5_industry_lookup
                  ON trusted_industry_membership(namespace,symbol,effective_date,trust_rank);

                CREATE TABLE IF NOT EXISTS forward_experiment_protocols (
                    experiment_id TEXT PRIMARY KEY,
                    protocol_version TEXT NOT NULL UNIQUE,
                    cohort_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    is_primary INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    asset_scope TEXT NOT NULL,
                    daily_sampling_rule TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    horizons TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    governance_version TEXT NOT NULL,
                    initial_capital REAL NOT NULL,
                    cost_rules TEXT NOT NULL,
                    matching_rules TEXT NOT NULL,
                    missing_data_rule TEXT NOT NULL,
                    minimum_trust_level TEXT NOT NULL,
                    upgrade_conditions TEXT NOT NULL,
                    stop_conditions TEXT NOT NULL,
                    frozen_payload TEXT NOT NULL,
                    protocol_fingerprint TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS primary_cohort_governance (
                    governance_id TEXT PRIMARY KEY,
                    previous_experiment_id TEXT,
                    new_experiment_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    FOREIGN KEY(new_experiment_id) REFERENCES forward_experiment_protocols(experiment_id)
                );
                CREATE TABLE IF NOT EXISTS forward_registration_runs (
                    registration_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    cohort_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL DEFAULT 1,
                    recovery_of_registration_id TEXT,
                    recovery_reason TEXT,
                    pool_snapshot_id TEXT,
                    pool_fingerprint TEXT,
                    manifest_id TEXT,
                    status TEXT NOT NULL,
                    expected_samples INTEGER NOT NULL DEFAULT 0,
                    registered_samples INTEGER NOT NULL DEFAULT 0,
                    failed_samples INTEGER NOT NULL DEFAULT 0,
                    skipped_samples INTEGER NOT NULL DEFAULT 0,
                    failure_reason TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(experiment_id,trade_date,attempt_number),
                    FOREIGN KEY(experiment_id) REFERENCES forward_experiment_protocols(experiment_id)
                );
                CREATE TABLE IF NOT EXISTS forward_registration_samples (
                    registration_sample_id TEXT PRIMARY KEY,
                    registration_id TEXT NOT NULL,
                    experiment_id TEXT NOT NULL,
                    cohort_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    sample_key TEXT,
                    reason TEXT,
                    context_fingerprint TEXT,
                    prediction_fingerprints TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(registration_id,symbol,horizon_days),
                    FOREIGN KEY(registration_id) REFERENCES forward_registration_runs(registration_id)
                );
                CREATE INDEX IF NOT EXISTS idx_forward_registration_status
                  ON forward_registration_samples(experiment_id,trade_date,status);
                CREATE TABLE IF NOT EXISTS forward_milestone_scorecards (
                    milestone_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    cohort_id TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    threshold_samples INTEGER NOT NULL,
                    matured_samples INTEGER NOT NULL,
                    scorecard TEXT NOT NULL,
                    scorecard_fingerprint TEXT NOT NULL UNIQUE,
                    frozen_at TEXT NOT NULL,
                    UNIQUE(experiment_id,horizon_days,threshold_samples)
                );
                CREATE TABLE IF NOT EXISTS manual_forward_explorations (
                    exploration_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    account_id TEXT,
                    horizon_days INTEGER NOT NULL,
                    evidence_stage TEXT NOT NULL DEFAULT 'manual_exploration',
                    context_fingerprint TEXT,
                    predictions TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shadow_accounts (
                    account_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    cohort_id TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    initial_capital REAL NOT NULL,
                    cash REAL NOT NULL,
                    frozen_cash REAL NOT NULL DEFAULT 0,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    cumulative_cost REAL NOT NULL DEFAULT 0,
                    cumulative_turnover REAL NOT NULL DEFAULT 0,
                    actual_trigger_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(cohort_id,variant),
                    FOREIGN KEY(experiment_id) REFERENCES forward_experiment_protocols(experiment_id)
                );
                CREATE TABLE IF NOT EXISTS shadow_orders (
                    order_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    sample_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    requested_quantity INTEGER NOT NULL,
                    filled_quantity INTEGER NOT NULL DEFAULT 0,
                    target_weight REAL NOT NULL,
                    signal_date TEXT NOT NULL,
                    eligible_trade_date TEXT NOT NULL,
                    expires_at TEXT,
                    status TEXT NOT NULL,
                    reference_close REAL NOT NULL,
                    reserved_cash REAL NOT NULL DEFAULT 0,
                    reason TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_id,sample_key,symbol),
                    FOREIGN KEY(account_id) REFERENCES shadow_accounts(account_id)
                );
                CREATE TABLE IF NOT EXISTS shadow_fills (
                    fill_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    raw_open REAL NOT NULL,
                    fill_price REAL NOT NULL,
                    gross_value REAL NOT NULL,
                    transaction_cost REAL NOT NULL,
                    trade_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    trust_level TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES shadow_orders(order_id)
                );
                CREATE TABLE IF NOT EXISTS shadow_positions (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    average_cost REAL NOT NULL,
                    latest_price REAL NOT NULL,
                    latest_price_at TEXT,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id,symbol),
                    FOREIGN KEY(account_id) REFERENCES shadow_accounts(account_id)
                );
                CREATE TABLE IF NOT EXISTS shadow_nav (
                    nav_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    nav_date TEXT NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    equity REAL NOT NULL,
                    daily_pnl REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    cumulative_cost REAL NOT NULL,
                    cumulative_turnover REAL NOT NULL,
                    drawdown REAL NOT NULL,
                    maximum_drawdown REAL NOT NULL,
                    position_drift REAL NOT NULL,
                    data_status TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(account_id,nav_date),
                    FOREIGN KEY(account_id) REFERENCES shadow_accounts(account_id)
                );
                CREATE TABLE IF NOT EXISTS shadow_events (
                    event_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    symbol TEXT,
                    event_date TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS investor_portfolios (
                    portfolio_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    cash REAL NOT NULL,
                    initial_equity REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    read_only INTEGER NOT NULL DEFAULT 1,
                    evidence_eligible INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS investor_imports (
                    import_id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    import_type TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    file_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    valid_count INTEGER NOT NULL,
                    duplicate_count INTEGER NOT NULL,
                    error_count INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    UNIQUE(portfolio_id,idempotency_key),
                    UNIQUE(portfolio_id,file_fingerprint)
                );
                CREATE TABLE IF NOT EXISTS investor_import_rows (
                    import_id TEXT NOT NULL,
                    row_number INTEGER NOT NULL,
                    row_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    error TEXT,
                    PRIMARY KEY(import_id,row_number),
                    UNIQUE(import_id,row_fingerprint)
                );
                CREATE TABLE IF NOT EXISTS investor_positions (
                    portfolio_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    asset_type TEXT NOT NULL,
                    industry TEXT,
                    quantity INTEGER NOT NULL,
                    average_cost REAL NOT NULL,
                    latest_price REAL NOT NULL DEFAULT 0,
                    latest_price_at TEXT,
                    price_status TEXT NOT NULL DEFAULT 'unavailable',
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(portfolio_id,symbol)
                );
                CREATE TABLE IF NOT EXISTS investor_trades (
                    trade_id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    import_id TEXT,
                    idempotency_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    transaction_cost REAL NOT NULL,
                    trade_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(portfolio_id,idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS investor_nav (
                    nav_id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    nav_date TEXT NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    equity REAL NOT NULL,
                    today_pnl REAL NOT NULL,
                    cumulative_pnl REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    cumulative_cost REAL NOT NULL,
                    concentration REAL NOT NULL,
                    industry_exposure TEXT NOT NULL,
                    drawdown REAL NOT NULL,
                    maximum_drawdown REAL NOT NULL,
                    stale_symbols TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(portfolio_id,nav_date)
                );
                CREATE TABLE IF NOT EXISTS investor_recommendations (
                    recommendation_id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    action TEXT NOT NULL,
                    quantity_min INTEGER NOT NULL,
                    quantity_max INTEGER NOT NULL,
                    actionable INTEGER NOT NULL,
                    context_id TEXT,
                    context_fingerprint TEXT,
                    research_run_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS investor_recommendation_adoptions (
                    adoption_id TEXT PRIMARY KEY,
                    recommendation_id TEXT NOT NULL UNIQUE,
                    decision TEXT NOT NULL,
                    actual_quantity INTEGER,
                    actual_price REAL,
                    note TEXT,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(recommendation_id) REFERENCES investor_recommendations(recommendation_id)
                );
                CREATE TABLE IF NOT EXISTS investor_recommendation_outcomes (
                    outcome_id TEXT PRIMARY KEY,
                    recommendation_id TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    due_date TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    start_price REAL NOT NULL,
                    end_price REAL NOT NULL,
                    realized_return_pct REAL NOT NULL,
                    source TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(recommendation_id,horizon_days),
                    FOREIGN KEY(recommendation_id) REFERENCES investor_recommendations(recommendation_id)
                );
                """
            )
            manifest_columns = {
                row[1]
                for row in db.execute("PRAGMA table_info(trusted_data_manifests)")
            }
            for column in (
                "refresh_started_at",
                "refresh_finalized_at",
                "snapshot_cutoff_at",
            ):
                if column not in manifest_columns:
                    db.execute(
                        f"ALTER TABLE trusted_data_manifests ADD COLUMN {column} TEXT"
                    )
        self._upgrade_registration_attempt_schema()

    def _upgrade_registration_attempt_schema(self) -> None:
        with sqlite3.connect(self.path, timeout=30) as probe:
            columns = {
                row[1] for row in probe.execute("PRAGMA table_info(forward_registration_runs)")
            }
        if {
            "attempt_number",
            "recovery_of_registration_id",
            "recovery_reason",
        }.issubset(columns):
            return
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA foreign_keys=OFF")
            db.executescript(
                """
                BEGIN IMMEDIATE;
                DROP TABLE IF EXISTS forward_registration_runs_v3;
                CREATE TABLE forward_registration_runs_v3 (
                    registration_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    cohort_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL DEFAULT 1,
                    recovery_of_registration_id TEXT,
                    recovery_reason TEXT,
                    pool_snapshot_id TEXT,
                    pool_fingerprint TEXT,
                    manifest_id TEXT,
                    status TEXT NOT NULL,
                    expected_samples INTEGER NOT NULL DEFAULT 0,
                    registered_samples INTEGER NOT NULL DEFAULT 0,
                    failed_samples INTEGER NOT NULL DEFAULT 0,
                    skipped_samples INTEGER NOT NULL DEFAULT 0,
                    failure_reason TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(experiment_id,trade_date,attempt_number),
                    FOREIGN KEY(experiment_id) REFERENCES forward_experiment_protocols(experiment_id)
                );
                INSERT INTO forward_registration_runs_v3(
                    registration_id,experiment_id,cohort_id,trade_date,attempt_number,
                    recovery_of_registration_id,recovery_reason,pool_snapshot_id,
                    pool_fingerprint,manifest_id,status,expected_samples,registered_samples,
                    failed_samples,skipped_samples,failure_reason,payload,started_at,completed_at
                )
                SELECT registration_id,experiment_id,cohort_id,trade_date,1,NULL,NULL,
                       pool_snapshot_id,pool_fingerprint,manifest_id,status,expected_samples,
                       registered_samples,failed_samples,skipped_samples,failure_reason,
                       payload,started_at,completed_at
                FROM forward_registration_runs;
                DROP TABLE forward_registration_runs;
                ALTER TABLE forward_registration_runs_v3 RENAME TO forward_registration_runs;
                CREATE INDEX IF NOT EXISTS idx_forward_registration_runs_date
                  ON forward_registration_runs(experiment_id,trade_date,attempt_number);
                COMMIT;
                """
            )
            db.execute("PRAGMA foreign_keys=ON")
            violations = db.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("registration recovery migration violated foreign keys")
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create_manifest(
        self,
        *,
        batch_type: str,
        namespace: DataNamespace | str,
        trust_level: DataTrustLevel | str,
        provider: str,
        source: str,
        endpoint: str,
        source_version: str,
        available_at: datetime,
        refresh_started_at: datetime | None = None,
        refresh_finalized_at: datetime | None = None,
        snapshot_cutoff_at: datetime | None = None,
        license_status: str,
        payload: Any,
        raw_fingerprint: str,
        record_count: int,
        status: str = "completed",
        date_start: date | None = None,
        date_end: date | None = None,
        missing_reason: str | None = None,
        failure_records: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        resolved_namespace = DataNamespace(namespace)
        resolved_trust = DataTrustLevel(trust_level)
        if resolved_namespace == DataNamespace.PRODUCTION and trust_rank(resolved_trust) < trust_rank(
            DataTrustLevel.SERVER_OBSERVED
        ):
            raise ValueError("production manifests require server-observed or stronger data")
        fingerprint = payload_fingerprint(payload)
        with self.connect() as db:
            existing = db.execute(
                """SELECT * FROM trusted_data_manifests
                   WHERE batch_type=? AND namespace=? AND payload_fingerprint=?""",
                (batch_type, resolved_namespace.value, fingerprint),
            ).fetchone()
            if existing:
                return _json_row(existing, "failure_records")
            manifest_id = str(uuid.uuid4())
            now = _now()
            db.execute(
                """INSERT INTO trusted_data_manifests(
                    manifest_id,batch_type,namespace,trust_level,trust_rank,provider,source,
                    endpoint,source_version,fetched_at,available_at,refresh_started_at,
                    refresh_finalized_at,snapshot_cutoff_at,license_status,
                    payload_fingerprint,raw_fingerprint,record_count,date_start,date_end,status,
                    missing_reason,failure_records,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    manifest_id,
                    batch_type,
                    resolved_namespace.value,
                    resolved_trust.value,
                    trust_rank(resolved_trust),
                    provider,
                    source,
                    endpoint,
                    source_version,
                    now,
                    available_at.astimezone(UTC).isoformat(),
                    refresh_started_at.astimezone(UTC).isoformat()
                    if refresh_started_at
                    else None,
                    refresh_finalized_at.astimezone(UTC).isoformat()
                    if refresh_finalized_at
                    else None,
                    snapshot_cutoff_at.astimezone(UTC).isoformat()
                    if snapshot_cutoff_at
                    else None,
                    license_status,
                    fingerprint,
                    raw_fingerprint,
                    max(0, int(record_count)),
                    date_start.isoformat() if date_start else None,
                    date_end.isoformat() if date_end else None,
                    status,
                    missing_reason,
                    json.dumps(sanitize_for_export(failure_records or []), ensure_ascii=False),
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM trusted_data_manifests WHERE manifest_id=?", (manifest_id,)
            ).fetchone()
        return _json_row(row, "failure_records")

    def manifests(self, batch_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM trusted_data_manifests"
        params: list[Any] = []
        if batch_type:
            query += " WHERE batch_type=?"
            params.append(batch_type)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [_json_row(row, "failure_records") for row in rows]

    def update_manifest_result(
        self,
        manifest_id: str,
        *,
        status: str,
        record_count: int | None = None,
        missing_reason: str | None = None,
        failure_records: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        assignments = ["status=?", "missing_reason=?"]
        params: list[Any] = [status, missing_reason]
        if record_count is not None:
            assignments.append("record_count=?")
            params.append(max(0, int(record_count)))
        if failure_records is not None:
            assignments.append("failure_records=?")
            params.append(
                json.dumps(sanitize_for_export(failure_records), ensure_ascii=False)
            )
        params.append(manifest_id)
        with self.connect() as db:
            result = db.execute(
                f"UPDATE trusted_data_manifests SET {','.join(assignments)} WHERE manifest_id=?",
                params,
            )
            if not result.rowcount:
                raise ValueError("trusted data manifest not found")
            row = db.execute(
                "SELECT * FROM trusted_data_manifests WHERE manifest_id=?",
                (manifest_id,),
            ).fetchone()
        return _json_row(row, "failure_records")

    def save_calendar_days(self, manifest_id: str, records: list[dict[str, Any]]) -> int:
        manifest = self._manifest(manifest_id)
        saved = 0
        with self.transaction() as db:
            for record in records:
                trade_date = date.fromisoformat(str(record["trade_date"])[:10])
                payload = {
                    "namespace": manifest["namespace"],
                    "trade_date": trade_date.isoformat(),
                    "is_open": bool(record["is_open"]),
                    "manifest_id": manifest_id,
                }
                result = db.execute(
                    """INSERT OR IGNORE INTO trusted_calendar_days(
                        namespace,trade_date,is_open,trust_level,trust_rank,manifest_id,source,
                        source_version,available_at,record_fingerprint,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        manifest["namespace"],
                        trade_date.isoformat(),
                        int(bool(record["is_open"])),
                        manifest["trust_level"],
                        manifest["trust_rank"],
                        manifest_id,
                        manifest["source"],
                        manifest["source_version"],
                        manifest["available_at"],
                        payload_fingerprint(payload),
                        _now(),
                    ),
                )
                saved += int(result.rowcount > 0)
        return saved

    def calendar_day(
        self,
        trade_date: date,
        *,
        namespace: DataNamespace | str = DataNamespace.PRODUCTION,
        minimum_trust: DataTrustLevel | str = DataTrustLevel.SERVER_OBSERVED,
        cutoff_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        cutoff = (cutoff_at or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connect() as db:
            row = db.execute(
                """SELECT * FROM trusted_calendar_days
                   WHERE namespace=? AND trade_date=? AND trust_rank>=? AND available_at<=?
                   ORDER BY trust_rank DESC,available_at DESC LIMIT 1""",
                (
                    DataNamespace(namespace).value,
                    trade_date.isoformat(),
                    trust_rank(minimum_trust),
                    cutoff,
                ),
            ).fetchone()
        return {**dict(row), "is_open": bool(row["is_open"]), "estimated": False} if row else None

    def save_industry_memberships(
        self, manifest_id: str, records: list[dict[str, Any]]
    ) -> int:
        manifest = self._manifest(manifest_id)
        saved = 0
        with self.transaction() as db:
            for record in records:
                symbol = str(record.get("symbol") or "").strip()
                industry = str(record.get("industry") or "").strip()
                effective = str(record.get("effective_date") or record.get("date") or "")[:10]
                if not symbol or not industry or not effective:
                    continue
                fingerprint = payload_fingerprint(
                    [manifest["namespace"], symbol, industry, effective, manifest_id]
                )
                result = db.execute(
                    """INSERT OR IGNORE INTO trusted_industry_membership(
                        namespace,symbol,industry,effective_date,trust_level,trust_rank,
                        manifest_id,source,source_version,available_at,record_fingerprint,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        manifest["namespace"],
                        symbol,
                        industry,
                        effective,
                        manifest["trust_level"],
                        manifest["trust_rank"],
                        manifest_id,
                        manifest["source"],
                        manifest["source_version"],
                        manifest["available_at"],
                        fingerprint,
                        _now(),
                    ),
                )
                saved += int(result.rowcount > 0)
        return saved

    def industry_as_of(
        self,
        symbol: str,
        *,
        as_of: date,
        minimum_trust: DataTrustLevel | str = DataTrustLevel.SERVER_OBSERVED,
    ) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT * FROM trusted_industry_membership
                   WHERE namespace='production' AND symbol=? AND effective_date<=?
                     AND trust_rank>=?
                   ORDER BY effective_date DESC,trust_rank DESC,available_at DESC LIMIT 1""",
                (symbol, as_of.isoformat(), trust_rank(minimum_trust)),
            ).fetchone()
        return dict(row) if row else None

    def create_experiment(
        self,
        *,
        protocol_version: str,
        cohort_id: str,
        frozen_payload: dict[str, Any],
        make_primary: bool = False,
        reason: str = "initial primary forward experiment",
    ) -> dict[str, Any]:
        fingerprint = payload_fingerprint(frozen_payload)
        now = _now()
        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM forward_experiment_protocols WHERE protocol_version=?",
                (protocol_version,),
            ).fetchone()
            if existing:
                if existing["protocol_fingerprint"] != fingerprint:
                    raise ValueError("a frozen forward experiment protocol cannot be modified")
                if make_primary and (
                    not bool(existing["is_primary"]) or existing["status"] != "active"
                ):
                    raise ValueError(
                        "a replaced forward protocol cannot be promoted again; use a new "
                        "protocol version and preserve the governance history"
                    )
                return _experiment_row(existing)
            experiment_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO forward_experiment_protocols(
                    experiment_id,protocol_version,cohort_id,status,is_primary,started_at,
                    asset_scope,daily_sampling_rule,candidate_count,horizons,model_version,
                    prompt_version,strategy_version,governance_version,initial_capital,
                    cost_rules,matching_rules,missing_data_rule,minimum_trust_level,
                    upgrade_conditions,stop_conditions,frozen_payload,protocol_fingerprint,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    experiment_id,
                    protocol_version,
                    cohort_id,
                    "active",
                    int(make_primary),
                    now,
                    json.dumps(frozen_payload["asset_scope"], ensure_ascii=False),
                    frozen_payload["daily_sampling_rule"],
                    int(frozen_payload["candidate_count"]),
                    json.dumps(frozen_payload["horizons"]),
                    frozen_payload["model_version"],
                    frozen_payload["prompt_version"],
                    frozen_payload["strategy_version"],
                    frozen_payload["governance_version"],
                    float(frozen_payload["initial_capital"]),
                    json.dumps(frozen_payload["cost_rules"], sort_keys=True),
                    json.dumps(frozen_payload["matching_rules"], sort_keys=True),
                    frozen_payload["missing_data_rule"],
                    frozen_payload["minimum_trust_level"],
                    json.dumps(frozen_payload["upgrade_conditions"], ensure_ascii=False),
                    json.dumps(frozen_payload["stop_conditions"], ensure_ascii=False),
                    json.dumps(sanitize_for_export(frozen_payload), ensure_ascii=False),
                    fingerprint,
                    now,
                ),
            )
            if make_primary:
                previous = db.execute(
                    "SELECT experiment_id FROM forward_experiment_protocols WHERE is_primary=1 AND experiment_id<>?",
                    (experiment_id,),
                ).fetchone()
                db.execute(
                    "UPDATE forward_experiment_protocols SET is_primary=0,status='replaced' WHERE experiment_id<>? AND is_primary=1",
                    (experiment_id,),
                )
                db.execute(
                    """INSERT INTO primary_cohort_governance(
                        governance_id,previous_experiment_id,new_experiment_id,decision,reason,evidence,changed_at
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()),
                        previous["experiment_id"] if previous else None,
                        experiment_id,
                        "promote_primary",
                        reason,
                        json.dumps({"protocol_fingerprint": fingerprint}),
                        now,
                    ),
                )
            row = db.execute(
                "SELECT * FROM forward_experiment_protocols WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
        return _experiment_row(row)

    def primary_experiment(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM forward_experiment_protocols WHERE is_primary=1 ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return _experiment_row(row) if row else None

    def experiment_for_cohort(self, cohort_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM forward_experiment_protocols WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
        return _experiment_row(row) if row else None

    def experiments(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM forward_experiment_protocols ORDER BY created_at"
            ).fetchall()
            governance = db.execute(
                "SELECT * FROM primary_cohort_governance ORDER BY changed_at"
            ).fetchall()
        return [
            {
                **_experiment_row(row),
                "governance": [
                    _json_row(item, "evidence")
                    for item in governance
                    if item["new_experiment_id"] == row["experiment_id"]
                    or item["previous_experiment_id"] == row["experiment_id"]
                ],
            }
            for row in rows
        ]

    def begin_registration(
        self,
        experiment: dict[str, Any],
        trade_date: date,
        *,
        pool_snapshot_id: str | None,
        pool_fingerprint: str | None,
        manifest_id: str | None,
        force_new_attempt: bool = False,
        recovery_of_registration_id: str | None = None,
        recovery_reason: str | None = None,
    ) -> dict[str, Any]:
        with self.connect() as db:
            existing = db.execute(
                """SELECT * FROM forward_registration_runs
                   WHERE experiment_id=? AND trade_date=?
                   ORDER BY attempt_number DESC LIMIT 1""",
                (experiment["experiment_id"], trade_date.isoformat()),
            ).fetchone()
            if existing and not force_new_attempt:
                return _json_row(existing, "payload")
            if force_new_attempt:
                if existing is None:
                    raise ValueError("registration recovery requires a preserved prior attempt")
                if existing["status"] == "running":
                    return _json_row(existing, "payload")
                if not str(recovery_reason or "").strip():
                    raise ValueError("registration recovery requires an audit reason")
                prior_id = recovery_of_registration_id or existing["registration_id"]
                if prior_id != existing["registration_id"]:
                    raise ValueError("registration recovery must reference the latest attempt")
                attempt_number = int(existing["attempt_number"]) + 1
            else:
                prior_id = None
                attempt_number = 1
            registration_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO forward_registration_runs(
                    registration_id,experiment_id,cohort_id,trade_date,attempt_number,
                    recovery_of_registration_id,recovery_reason,pool_snapshot_id,
                    pool_fingerprint,manifest_id,status,started_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,'running',?)""",
                (
                    registration_id,
                    experiment["experiment_id"],
                    experiment["cohort_id"],
                    trade_date.isoformat(),
                    attempt_number,
                    prior_id,
                    str(recovery_reason).strip() if recovery_reason else None,
                    pool_snapshot_id,
                    pool_fingerprint,
                    manifest_id,
                    _now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM forward_registration_runs WHERE registration_id=?",
                (registration_id,),
            ).fetchone()
        return _json_row(row, "payload")

    def record_registration_sample(
        self,
        registration: dict[str, Any],
        *,
        symbol: str,
        horizon_days: int,
        ordinal: int,
        status: str,
        sample_key: str | None = None,
        reason: str | None = None,
        context_fingerprint: str | None = None,
        prediction_fingerprints: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self.connect() as db:
            existing = db.execute(
                """SELECT * FROM forward_registration_samples
                   WHERE registration_id=? AND symbol=? AND horizon_days=?""",
                (registration["registration_id"], symbol, horizon_days),
            ).fetchone()
            if existing:
                return _json_row(existing, "prediction_fingerprints")
            row_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO forward_registration_samples(
                    registration_sample_id,registration_id,experiment_id,cohort_id,trade_date,
                    symbol,horizon_days,ordinal,status,sample_key,reason,context_fingerprint,
                    prediction_fingerprints,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row_id,
                    registration["registration_id"],
                    registration["experiment_id"],
                    registration["cohort_id"],
                    registration["trade_date"],
                    symbol,
                    horizon_days,
                    ordinal,
                    status,
                    sample_key,
                    reason,
                    context_fingerprint,
                    json.dumps(prediction_fingerprints or {}, sort_keys=True),
                    _now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM forward_registration_samples WHERE registration_sample_id=?",
                (row_id,),
            ).fetchone()
        return _json_row(row, "prediction_fingerprints")

    def finish_registration(
        self,
        registration_id: str,
        *,
        status: str,
        expected: int,
        registered: int,
        failed: int,
        skipped: int,
        failure_reason: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.connect() as db:
            db.execute(
                """UPDATE forward_registration_runs SET status=?,expected_samples=?,
                   registered_samples=?,failed_samples=?,skipped_samples=?,failure_reason=?,
                   payload=?,completed_at=? WHERE registration_id=?""",
                (
                    status,
                    expected,
                    registered,
                    failed,
                    skipped,
                    failure_reason,
                    json.dumps(sanitize_for_export(payload or {}), ensure_ascii=False),
                    _now(),
                    registration_id,
                ),
            )
            row = db.execute(
                "SELECT * FROM forward_registration_runs WHERE registration_id=?",
                (registration_id,),
            ).fetchone()
        return _json_row(row, "payload")

    def registration_runs(self, experiment_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM forward_registration_runs"
        params: tuple[Any, ...] = ()
        if experiment_id:
            query += " WHERE experiment_id=?"
            params = (experiment_id,)
        query += " ORDER BY trade_date,attempt_number,started_at"
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [_json_row(row, "payload") for row in rows]

    def consecutive_registration_failures(self, experiment_id: str) -> int:
        with self.connect() as db:
            rows = db.execute(
                """SELECT registered_samples FROM forward_registration_runs
                   WHERE experiment_id=? ORDER BY trade_date DESC,started_at DESC""",
                (experiment_id,),
            ).fetchall()
        count = 0
        for row in rows:
            if int(row["registered_samples"]) > 0:
                break
            count += 1
        return count

    def registration_samples(self, registration_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM forward_registration_samples WHERE registration_id=? ORDER BY ordinal,horizon_days",
                (registration_id,),
            ).fetchall()
        return [_json_row(row, "prediction_fingerprints") for row in rows]

    def save_milestone(
        self,
        *,
        experiment_id: str,
        cohort_id: str,
        horizon_days: int,
        threshold: int,
        matured_samples: int,
        scorecard: dict[str, Any],
    ) -> dict[str, Any]:
        fingerprint = payload_fingerprint(scorecard)
        with self.connect() as db:
            existing = db.execute(
                """SELECT * FROM forward_milestone_scorecards
                   WHERE experiment_id=? AND horizon_days=? AND threshold_samples=?""",
                (experiment_id, horizon_days, threshold),
            ).fetchone()
            if existing:
                return _json_row(existing, "scorecard")
            milestone_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO forward_milestone_scorecards(
                    milestone_id,experiment_id,cohort_id,horizon_days,threshold_samples,
                    matured_samples,scorecard,scorecard_fingerprint,frozen_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    milestone_id,
                    experiment_id,
                    cohort_id,
                    horizon_days,
                    threshold,
                    matured_samples,
                    json.dumps(sanitize_for_export(scorecard), ensure_ascii=False),
                    fingerprint,
                    _now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM forward_milestone_scorecards WHERE milestone_id=?",
                (milestone_id,),
            ).fetchone()
        return _json_row(row, "scorecard")

    def milestones(self, experiment_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM forward_milestone_scorecards WHERE experiment_id=? ORDER BY horizon_days,threshold_samples",
                (experiment_id,),
            ).fetchall()
        return [_json_row(row, "scorecard") for row in rows]

    def save_manual_exploration(
        self,
        *,
        symbol: str,
        account_id: str | None,
        horizon_days: int,
        context_fingerprint: str | None,
        predictions: list[dict[str, Any]],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        exploration_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute(
                """INSERT INTO manual_forward_explorations(
                    exploration_id,symbol,account_id,horizon_days,context_fingerprint,
                    predictions,payload,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    exploration_id,
                    symbol,
                    account_id,
                    horizon_days,
                    context_fingerprint,
                    json.dumps(sanitize_for_export(predictions), ensure_ascii=False),
                    json.dumps(sanitize_for_export(payload or {}), ensure_ascii=False),
                    _now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM manual_forward_explorations WHERE exploration_id=?",
                (exploration_id,),
            ).fetchone()
        return _json_row(row, "predictions", "payload")

    def ensure_shadow_accounts(
        self, experiment: dict[str, Any]
    ) -> list[dict[str, Any]]:
        initial = float(experiment["initial_capital"])
        now = _now()
        with self.transaction() as db:
            for variant in ABLATION_VARIANTS:
                db.execute(
                    """INSERT OR IGNORE INTO shadow_accounts(
                        account_id,experiment_id,cohort_id,variant,initial_capital,cash,
                        created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()),
                        experiment["experiment_id"],
                        experiment["cohort_id"],
                        variant.value,
                        initial,
                        initial,
                        now,
                        now,
                    ),
                )
            rows = db.execute(
                "SELECT * FROM shadow_accounts WHERE cohort_id=? ORDER BY variant",
                (experiment["cohort_id"],),
            ).fetchall()
        return [dict(row) for row in rows]

    def shadow_accounts(self, cohort_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM shadow_accounts WHERE cohort_id=? ORDER BY variant", (cohort_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def shadow_overview(self, account_id: str) -> dict[str, Any]:
        with self.connect() as db:
            account = db.execute(
                "SELECT * FROM shadow_accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            if account is None:
                raise ValueError("shadow account not found")
            positions = db.execute(
                "SELECT * FROM shadow_positions WHERE account_id=? ORDER BY symbol", (account_id,)
            ).fetchall()
            orders = db.execute(
                "SELECT * FROM shadow_orders WHERE account_id=? ORDER BY created_at", (account_id,)
            ).fetchall()
            fills = db.execute(
                "SELECT * FROM shadow_fills WHERE account_id=? ORDER BY created_at", (account_id,)
            ).fetchall()
            nav = db.execute(
                "SELECT * FROM shadow_nav WHERE account_id=? ORDER BY nav_date", (account_id,)
            ).fetchall()
        return {
            "account": dict(account),
            "positions": [dict(row) for row in positions],
            "orders": [_json_row(row, "payload") for row in orders],
            "fills": [dict(row) for row in fills],
            "nav": [_json_row(row, "payload") for row in nav],
        }

    def create_investor_portfolio(self, *, name: str, cash: float) -> dict[str, Any]:
        if cash < 0:
            raise ValueError("investor portfolio cash cannot be negative")
        portfolio_id = str(uuid.uuid4())
        now = _now()
        with self.connect() as db:
            db.execute(
                """INSERT INTO investor_portfolios(
                    portfolio_id,name,cash,initial_equity,created_at,updated_at
                ) VALUES(?,?,?,?,?,?)""",
                (portfolio_id, name, cash, cash, now, now),
            )
            row = db.execute(
                "SELECT * FROM investor_portfolios WHERE portfolio_id=?", (portfolio_id,)
            ).fetchone()
        return dict(row)

    def investor_portfolios(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM investor_portfolios ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def investor_overview(self, portfolio_id: str) -> dict[str, Any]:
        with self.connect() as db:
            portfolio = db.execute(
                "SELECT * FROM investor_portfolios WHERE portfolio_id=?", (portfolio_id,)
            ).fetchone()
            if portfolio is None:
                raise ValueError("investor portfolio not found")
            positions = db.execute(
                "SELECT * FROM investor_positions WHERE portfolio_id=? ORDER BY symbol",
                (portfolio_id,),
            ).fetchall()
            trades = db.execute(
                "SELECT * FROM investor_trades WHERE portfolio_id=? ORDER BY trade_date,created_at",
                (portfolio_id,),
            ).fetchall()
            nav = db.execute(
                "SELECT * FROM investor_nav WHERE portfolio_id=? ORDER BY nav_date",
                (portfolio_id,),
            ).fetchall()
        return {
            "portfolio": dict(portfolio),
            "positions": [dict(row) for row in positions],
            "trades": [dict(row) for row in trades],
            "nav": [_json_row(row, "industry_exposure", "stale_symbols") for row in nav],
        }

    def save_import_preview(
        self,
        *,
        portfolio_id: str,
        import_type: str,
        idempotency_key: str,
        file_fingerprint: str,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        valid = sum(item["status"] == "valid" for item in rows)
        duplicates = sum(item["status"] == "duplicate" for item in rows)
        errors = sum(item["status"] == "error" for item in rows)
        with self.transaction() as db:
            existing = db.execute(
                """SELECT * FROM investor_imports
                   WHERE portfolio_id=? AND (idempotency_key=? OR file_fingerprint=?)""",
                (portfolio_id, idempotency_key, file_fingerprint),
            ).fetchone()
            if existing:
                return _json_row(existing, "summary")
            if db.execute(
                "SELECT 1 FROM investor_portfolios WHERE portfolio_id=?", (portfolio_id,)
            ).fetchone() is None:
                raise ValueError("investor portfolio not found")
            import_id = str(uuid.uuid4())
            summary = {
                "row_count": len(rows),
                "valid_count": valid,
                "duplicate_count": duplicates,
                "error_count": errors,
            }
            db.execute(
                """INSERT INTO investor_imports(
                    import_id,portfolio_id,import_type,idempotency_key,file_fingerprint,status,
                    row_count,valid_count,duplicate_count,error_count,summary,created_at
                ) VALUES(?,?,?,?,?,'previewed',?,?,?,?,?,?)""",
                (
                    import_id,
                    portfolio_id,
                    import_type,
                    idempotency_key,
                    file_fingerprint,
                    len(rows),
                    valid,
                    duplicates,
                    errors,
                    json.dumps(summary),
                    _now(),
                ),
            )
            for index, item in enumerate(rows, 1):
                db.execute(
                    """INSERT INTO investor_import_rows(
                        import_id,row_number,row_fingerprint,status,payload,error
                    ) VALUES(?,?,?,?,?,?)""",
                    (
                        import_id,
                        index,
                        item["row_fingerprint"],
                        item["status"],
                        json.dumps(sanitize_for_export(item["payload"]), ensure_ascii=False),
                        item.get("error"),
                    ),
                )
            row = db.execute(
                "SELECT * FROM investor_imports WHERE import_id=?", (import_id,)
            ).fetchone()
        return _json_row(row, "summary")

    def import_rows(self, import_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM investor_import_rows WHERE import_id=? ORDER BY row_number",
                (import_id,),
            ).fetchall()
        return [_json_row(row, "payload") for row in rows]

    def recommendation(self, recommendation_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM investor_recommendations WHERE recommendation_id=?",
                (recommendation_id,),
            ).fetchone()
        return _json_row(row, "payload") if row else None

    def recommendations(self, portfolio_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM investor_recommendations WHERE portfolio_id=? ORDER BY created_at",
                (portfolio_id,),
            ).fetchall()
        return [_json_row(row, "payload") for row in rows]

    def _manifest(self, manifest_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM trusted_data_manifests WHERE manifest_id=?", (manifest_id,)
            ).fetchone()
        if row is None:
            raise ValueError("trusted data manifest not found")
        return _json_row(row, "failure_records")


def _json_row(row: sqlite3.Row, *fields: str) -> dict[str, Any]:
    item = dict(row)
    for field in fields:
        item[field] = json.loads(item[field])
    return item


def _experiment_row(row: sqlite3.Row) -> dict[str, Any]:
    item = _json_row(
        row,
        "asset_scope",
        "horizons",
        "cost_rules",
        "matching_rules",
        "upgrade_conditions",
        "stop_conditions",
        "frozen_payload",
    )
    item["is_primary"] = bool(item["is_primary"])
    return item


__all__ = ["ROUND5_SCHEMA_VERSION", "Round5Repository"]
