from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from quantlab.domain.context import AnalysisContextPack, EvidenceBlock
from quantlab.persistence.migrations import record_component_migration
from quantlab.domain.data_governance import DataNamespace, DataTrustLevel, trust_rank
from quantlab.security import safe_error_detail, sanitize_for_export


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            sanitize_for_export(value),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class EvidenceRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        record_component_migration(
            self.path,
            component="evidence",
            version=6,
            migration_identity="round5-industry-namespace-trust-v1",
        )
        record_component_migration(
            self.path,
            component="evidence",
            version=7,
            migration_identity="adversarial-llm-cache-cross-process-lease-v1",
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
                CREATE TABLE IF NOT EXISTS analysis_context_packs (
                    context_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    cutoff_at TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    quality_score REAL NOT NULL,
                    review_required INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_context_symbol_asof
                  ON analysis_context_packs(symbol,as_of,created_at);

                CREATE TABLE IF NOT EXISTS capital_flow_snapshots (
                    flow_id TEXT PRIMARY KEY,
                    scope_type TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    methodology TEXT NOT NULL,
                    estimated INTEGER NOT NULL,
                    quality TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_flow_scope_asof
                  ON capital_flow_snapshots(scope_type,scope_key,as_of,created_at);

                CREATE TABLE IF NOT EXISTS industry_membership_history (
                    symbol TEXT NOT NULL,
                    industry TEXT NOT NULL,
                    effective_date TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    record_fingerprint TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    namespace TEXT NOT NULL DEFAULT 'research',
                    trust_level TEXT NOT NULL DEFAULT 'research_external',
                    trust_rank INTEGER NOT NULL DEFAULT 2,
                    manifest_id TEXT,
                    PRIMARY KEY(symbol,industry,effective_date,source)
                );
                CREATE INDEX IF NOT EXISTS idx_industry_membership_pit
                  ON industry_membership_history(symbol,effective_date,available_at);

                CREATE TABLE IF NOT EXISTS llm_governed_calls (
                    call_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    context_id TEXT,
                    context_fingerprint TEXT,
                    role TEXT NOT NULL,
                    schema_name TEXT NOT NULL,
                    provider TEXT,
                    model TEXT,
                    status TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0,
                    latency_ms REAL NOT NULL DEFAULT 0,
                    result_payload TEXT,
                    error_detail TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_llm_cache
                  ON llm_governed_calls(cache_key,status,created_at);
                CREATE INDEX IF NOT EXISTS idx_llm_task
                  ON llm_governed_calls(task_id,created_at);

                CREATE TABLE IF NOT EXISTS llm_cache_claims (
                    cache_key TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
                    lease_until TEXT NOT NULL,
                    error_detail TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_llm_cache_claim_lease
                  ON llm_cache_claims(status,lease_until);

                CREATE TABLE IF NOT EXISTS llm_role_observations (
                    observation_id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    market_regime TEXT,
                    up_probability REAL,
                    flat_probability REAL,
                    down_probability REAL,
                    predicted_direction TEXT,
                    realized_direction TEXT,
                    realized_return_pct REAL,
                    direction_correct INTEGER,
                    brier_score REAL,
                    log_loss REAL,
                    drawdown_reduction REAL,
                    fact_errors INTEGER NOT NULL DEFAULT 0,
                    quant_incremental_return_pct REAL,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    latency_ms REAL NOT NULL DEFAULT 0,
                    matured INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(role,run_id,horizon_days)
                );
                CREATE INDEX IF NOT EXISTS idx_role_observation
                  ON llm_role_observations(role,matured,market_regime,as_of);

                CREATE TABLE IF NOT EXISTS llm_role_challenges (
                    challenge_id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    frozen_at TEXT NOT NULL,
                    sample_count INTEGER NOT NULL,
                    minimum_samples INTEGER NOT NULL DEFAULT 30,
                    status TEXT NOT NULL,
                    metrics TEXT NOT NULL,
                    decision TEXT,
                    reason TEXT,
                    decided_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS llm_role_policies (
                    policy_id TEXT PRIMARY KEY,
                    governance_version TEXT NOT NULL,
                    role TEXT NOT NULL,
                    weight REAL NOT NULL CHECK(weight>=0 AND weight<=2),
                    minimum_samples INTEGER NOT NULL,
                    applicable_regimes TEXT NOT NULL DEFAULT '["all"]',
                    challenge_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(governance_version,role),
                    FOREIGN KEY(challenge_id) REFERENCES llm_role_challenges(challenge_id)
                );
                CREATE INDEX IF NOT EXISTS idx_role_policy_active
                  ON llm_role_policies(role,active,created_at);
                """
            )
            challenge_columns = {
                row[1]
                for row in db.execute(
                    "PRAGMA table_info(llm_role_challenges)"
                ).fetchall()
            }
            if "minimum_samples" not in challenge_columns:
                db.execute(
                    "ALTER TABLE llm_role_challenges "
                    "ADD COLUMN minimum_samples INTEGER NOT NULL DEFAULT 30"
                )
            industry_columns = {
                row[1]
                for row in db.execute(
                    "PRAGMA table_info(industry_membership_history)"
                ).fetchall()
            }
            for column, declaration in (
                ("namespace", "TEXT NOT NULL DEFAULT 'research'"),
                ("trust_level", "TEXT NOT NULL DEFAULT 'research_external'"),
                ("trust_rank", "INTEGER NOT NULL DEFAULT 2"),
                ("manifest_id", "TEXT"),
            ):
                if column not in industry_columns:
                    db.execute(
                        f"ALTER TABLE industry_membership_history ADD COLUMN {column} {declaration}"
                    )

    def save_context(self, pack: AnalysisContextPack) -> dict[str, Any]:
        payload = json.dumps(pack.model_dump(mode="json"), ensure_ascii=False)
        with self.transaction() as db:
            existing = db.execute(
                "SELECT context_id FROM analysis_context_packs WHERE fingerprint=?",
                (pack.fingerprint,),
            ).fetchone()
            if existing is not None:
                return self.context(str(existing["context_id"])) or {}
            db.execute(
                """
                INSERT INTO analysis_context_packs(
                    context_id,schema_version,symbol,asset_type,as_of,cutoff_at,
                    fingerprint,quality_score,review_required,payload,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    pack.context_id,
                    pack.schema_version,
                    pack.symbol,
                    pack.asset_type.value,
                    pack.as_of.isoformat(),
                    pack.cutoff_at.isoformat(),
                    pack.fingerprint,
                    pack.quality_score,
                    int(pack.review_required),
                    payload,
                    _now(),
                ),
            )
        return pack.model_dump(mode="json")

    def context(self, context_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM analysis_context_packs WHERE context_id=?",
                (context_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def latest_context(
        self,
        symbol: str,
        *,
        as_of: str | None = None,
    ) -> dict[str, Any] | None:
        query = "SELECT payload FROM analysis_context_packs WHERE symbol=?"
        params: list[Any] = [symbol]
        if as_of is not None:
            query += " AND as_of<=?"
            params.append(as_of)
        query += " ORDER BY as_of DESC,created_at DESC LIMIT 1"
        with self.connect() as db:
            row = db.execute(query, params).fetchone()
        return json.loads(row["payload"]) if row else None

    def contexts(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT payload FROM analysis_context_packs
                WHERE symbol=? ORDER BY as_of DESC,created_at DESC LIMIT ?
                """,
                (symbol, limit),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_flow(self, block: EvidenceBlock) -> dict[str, Any]:
        scope = str(block.payload.get("scope") or "unknown")
        key = str(block.payload.get("scope_key") or "unknown")
        payload = block.model_dump(mode="json")
        with self.transaction() as db:
            existing = db.execute(
                "SELECT flow_id,payload FROM capital_flow_snapshots WHERE fingerprint=?",
                (block.fingerprint,),
            ).fetchone()
            if existing is not None:
                return json.loads(existing["payload"])
            flow_id = str(uuid.uuid4())
            db.execute(
                """
                INSERT INTO capital_flow_snapshots(
                    flow_id,scope_type,scope_key,as_of,available_at,source,
                    methodology,estimated,quality,fingerprint,payload,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    flow_id,
                    scope,
                    key,
                    block.as_of.isoformat(),
                    block.available_at.isoformat(),
                    block.source,
                    block.methodology,
                    int(block.estimated),
                    block.quality.value,
                    block.fingerprint,
                    json.dumps(payload, ensure_ascii=False),
                    _now(),
                ),
            )
        return payload

    def save_industry_memberships(
        self,
        records: list[dict[str, Any]],
        *,
        source: str,
        source_version: str,
        namespace: DataNamespace | str = DataNamespace.RESEARCH,
        trust_level: DataTrustLevel | str = DataTrustLevel.RESEARCH_EXTERNAL,
        manifest_id: str | None = None,
    ) -> int:
        resolved_namespace = DataNamespace(namespace)
        resolved_trust = DataTrustLevel(trust_level)
        if resolved_namespace == DataNamespace.PRODUCTION and trust_rank(
            resolved_trust
        ) < trust_rank(DataTrustLevel.SERVER_OBSERVED):
            raise ValueError("production industry membership requires server-observed data")
        saved = 0
        with self.transaction() as db:
            for record in records:
                symbol = str(record.get("symbol") or "").strip()
                industry = str(record.get("industry") or "").strip()
                effective = str(record.get("date") or record.get("effective_date") or "")[:10]
                available = str(record.get("available_at") or "")
                if not symbol or not industry or not effective or not available:
                    continue
                fingerprint = _fingerprint(
                    {
                        "symbol": symbol,
                        "industry": industry,
                        "effective_date": effective,
                        "available_at": available,
                        "source": source,
                        "source_version": source_version,
                        "namespace": resolved_namespace.value,
                        "trust_level": resolved_trust.value,
                        "manifest_id": manifest_id,
                    }
                )
                result = db.execute(
                    """
                    INSERT OR IGNORE INTO industry_membership_history(
                        symbol,industry,effective_date,available_at,source,
                        source_version,record_fingerprint,created_at,namespace,
                        trust_level,trust_rank,manifest_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        symbol,
                        industry,
                        effective,
                        available,
                        source,
                        source_version,
                        fingerprint,
                        _now(),
                        resolved_namespace.value,
                        resolved_trust.value,
                        trust_rank(resolved_trust),
                        manifest_id,
                    ),
                )
                saved += int(result.rowcount > 0)
        return saved

    def industry_as_of(
        self,
        symbol: str,
        *,
        as_of: str,
        cutoff_at: str | None = None,
        namespace: DataNamespace | str | None = None,
        minimum_trust: DataTrustLevel | str | None = None,
    ) -> dict[str, Any] | None:
        cutoff = cutoff_at or datetime.now(UTC).isoformat()
        clauses = ["symbol=?", "effective_date<=?", "available_at<=?"]
        params: list[Any] = [symbol, as_of[:10], cutoff]
        if namespace is not None:
            clauses.append("namespace=?")
            params.append(DataNamespace(namespace).value)
        if minimum_trust is not None:
            clauses.append("trust_rank>=?")
            params.append(trust_rank(minimum_trust))
        with self.connect() as db:
            row = db.execute(
                f"""
                SELECT * FROM industry_membership_history
                WHERE {' AND '.join(clauses)}
                ORDER BY effective_date DESC,trust_rank DESC,available_at DESC LIMIT 1
                """,
                params,
            ).fetchone()
        return dict(row) if row else None

    def flows(
        self,
        scope_type: str,
        *,
        scope_key: str | None = None,
        as_of: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = "SELECT payload FROM capital_flow_snapshots WHERE scope_type=?"
        params: list[Any] = [scope_type]
        if scope_key:
            query += " AND scope_key=?"
            params.append(scope_key)
        if as_of:
            query += " AND substr(as_of,1,10)<=?"
            params.append(as_of)
        query += " ORDER BY as_of DESC,created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def cached_llm_result(self, cache_key: str) -> dict[str, Any] | None:
        entry = self.cached_llm_entry(cache_key)
        return entry["result"] if entry is not None else None

    def cached_llm_entry(self, cache_key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT result_payload,provider,model FROM llm_governed_calls
                WHERE cache_key=? AND status='ok' AND result_payload IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "result": json.loads(row["result_payload"]),
            "provider": row["provider"] or "unknown",
            "model": row["model"] or "unknown",
        }

    def claim_llm_cache(
        self,
        *,
        cache_key: str,
        owner_id: str,
        task_id: str,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        observed = (now or datetime.now(UTC)).astimezone(UTC)
        lease_until = observed + timedelta(seconds=max(1, int(lease_seconds)))
        with self.transaction() as db:
            cached = db.execute(
                """SELECT result_payload,provider,model FROM llm_governed_calls
                   WHERE cache_key=? AND status='ok' AND result_payload IS NOT NULL
                   ORDER BY created_at DESC LIMIT 1""",
                (cache_key,),
            ).fetchone()
            if cached is not None:
                return {
                    "status": "cached",
                    "result": json.loads(cached["result_payload"]),
                    "provider": cached["provider"] or "unknown",
                    "model": cached["model"] or "unknown",
                }
            row = db.execute(
                "SELECT * FROM llm_cache_claims WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
            if row is None:
                db.execute(
                    """INSERT INTO llm_cache_claims(
                        cache_key,owner_id,task_id,status,lease_until,created_at,updated_at
                    ) VALUES(?,?,?,'running',?,?,?)""",
                    (
                        cache_key,
                        owner_id,
                        task_id,
                        lease_until.isoformat(),
                        observed.isoformat(),
                        observed.isoformat(),
                    ),
                )
                return {"status": "acquired", "lease_until": lease_until.isoformat()}
            current_lease = datetime.fromisoformat(row["lease_until"]).astimezone(UTC)
            if row["status"] == "running" and current_lease > observed:
                return {
                    "status": "waiting",
                    "lease_until": current_lease.isoformat(),
                }
            db.execute(
                """UPDATE llm_cache_claims SET owner_id=?,task_id=?,status='running',
                   lease_until=?,error_detail=NULL,updated_at=? WHERE cache_key=?""",
                (
                    owner_id,
                    task_id,
                    lease_until.isoformat(),
                    observed.isoformat(),
                    cache_key,
                ),
            )
            return {"status": "acquired", "lease_until": lease_until.isoformat()}

    def finish_llm_cache_claim(
        self,
        *,
        cache_key: str,
        owner_id: str,
        success: bool,
        error: BaseException | None = None,
    ) -> bool:
        with self.connect() as db:
            result = db.execute(
                """UPDATE llm_cache_claims SET status=?,error_detail=?,updated_at=?
                   WHERE cache_key=? AND owner_id=? AND status='running'""",
                (
                    "completed" if success else "failed",
                    safe_error_detail(error) if error else None,
                    _now(),
                    cache_key,
                    owner_id,
                ),
            )
        return result.rowcount == 1

    def llm_call(self, task_id: str, idempotency_key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT * FROM llm_governed_calls
                WHERE task_id=? AND idempotency_key=?
                """,
                (task_id, idempotency_key),
            ).fetchone()
        return self._llm_row(row) if row else None

    def record_llm_call(
        self,
        *,
        task_id: str,
        idempotency_key: str,
        cache_key: str,
        context_id: str | None,
        context_fingerprint: str | None,
        role: str,
        schema_name: str,
        provider: str | None,
        model: str | None,
        status: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float,
        latency_ms: float,
        result: dict[str, Any] | None,
        error: BaseException | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            existing = db.execute(
                """
                SELECT * FROM llm_governed_calls
                WHERE task_id=? AND idempotency_key=?
                """,
                (task_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return self._llm_row(existing)
            call_id = str(uuid.uuid4())
            db.execute(
                """
                INSERT INTO llm_governed_calls(
                    call_id,task_id,idempotency_key,cache_key,context_id,
                    context_fingerprint,role,schema_name,provider,model,status,
                    input_tokens,output_tokens,estimated_cost_usd,latency_ms,
                    result_payload,error_detail,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    call_id,
                    task_id,
                    idempotency_key,
                    cache_key,
                    context_id,
                    context_fingerprint,
                    role,
                    schema_name,
                    provider,
                    model,
                    status,
                    input_tokens,
                    output_tokens,
                    estimated_cost_usd,
                    latency_ms,
                    json.dumps(sanitize_for_export(result), ensure_ascii=False) if result else None,
                    safe_error_detail(error) if error else None,
                    _now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM llm_governed_calls WHERE call_id=?",
                (call_id,),
            ).fetchone()
        return self._llm_row(row)

    def assert_llm_budget_capacity(
        self,
        *,
        task_id: str,
        maximum_calls: int,
        maximum_total_tokens: int,
        maximum_cost_usd: float,
        phase_reservations: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomically prove that the remaining declared workflow fits the task budget."""
        with self.transaction() as db:
            projection = self._llm_budget_projection(
                db,
                task_id=task_id,
                phase_reservations=phase_reservations,
            )
            self._enforce_llm_budget(
                projection,
                maximum_calls=maximum_calls,
                maximum_total_tokens=maximum_total_tokens,
                maximum_cost_usd=maximum_cost_usd,
            )
        return projection

    def reserve_llm_call(
        self,
        *,
        task_id: str,
        idempotency_key: str,
        cache_key: str,
        context_id: str | None,
        context_fingerprint: str | None,
        role: str,
        schema_name: str,
        provider: str | None,
        model: str | None,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        reserved_cost_usd: float,
        maximum_calls: int,
        maximum_total_tokens: int,
        maximum_cost_usd: float,
        phase_reservations: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Reserve call, token and cost capacity in the same SQLite write transaction."""
        with self.transaction() as db:
            existing = db.execute(
                """SELECT * FROM llm_governed_calls
                   WHERE task_id=? AND idempotency_key=?""",
                (task_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return self._llm_row(existing)
            planned = dict(phase_reservations or {})
            current_phase = dict(planned.get(schema_name) or {})
            current_phase["expected_calls"] = max(
                1,
                int(current_phase.get("expected_calls") or 0),
            )
            current_phase["input_tokens_per_call"] = max(
                int(current_phase.get("input_tokens_per_call") or 0),
                int(reserved_input_tokens),
            )
            current_phase["output_tokens_per_call"] = max(
                int(current_phase.get("output_tokens_per_call") or 0),
                int(reserved_output_tokens),
            )
            current_phase["cost_per_call"] = max(
                float(current_phase.get("cost_per_call") or 0.0),
                float(reserved_cost_usd),
            )
            planned[schema_name] = current_phase
            projection = self._llm_budget_projection(
                db,
                task_id=task_id,
                phase_reservations=planned,
                pending_schema_name=schema_name,
                pending_input_tokens=reserved_input_tokens,
                pending_output_tokens=reserved_output_tokens,
                pending_cost_usd=reserved_cost_usd,
            )
            self._enforce_llm_budget(
                projection,
                maximum_calls=maximum_calls,
                maximum_total_tokens=maximum_total_tokens,
                maximum_cost_usd=maximum_cost_usd,
            )
            call_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO llm_governed_calls(
                    call_id,task_id,idempotency_key,cache_key,context_id,
                    context_fingerprint,role,schema_name,provider,model,status,
                    input_tokens,output_tokens,estimated_cost_usd,latency_ms,
                    result_payload,error_detail,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,'reserved',?,?,?,?,NULL,NULL,?)""",
                (
                    call_id,
                    task_id,
                    idempotency_key,
                    cache_key,
                    context_id,
                    context_fingerprint,
                    role,
                    schema_name,
                    provider,
                    model,
                    int(reserved_input_tokens),
                    int(reserved_output_tokens),
                    float(reserved_cost_usd),
                    0.0,
                    _now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM llm_governed_calls WHERE call_id=?",
                (call_id,),
            ).fetchone()
        return self._llm_row(row)

    def finalize_llm_call(
        self,
        *,
        task_id: str,
        idempotency_key: str,
        provider: str | None,
        model: str | None,
        status: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float,
        latency_ms: float,
        result: dict[str, Any] | None,
        error: BaseException | None = None,
    ) -> dict[str, Any]:
        if status not in {"ok", "error"}:
            raise ValueError("final LLM call status must be ok or error")
        with self.transaction() as db:
            row = db.execute(
                """SELECT * FROM llm_governed_calls
                   WHERE task_id=? AND idempotency_key=?""",
                (task_id, idempotency_key),
            ).fetchone()
            if row is None:
                raise KeyError("LLM reservation does not exist")
            if row["status"] == "reserved":
                db.execute(
                    """UPDATE llm_governed_calls
                       SET provider=?,model=?,status=?,input_tokens=?,output_tokens=?,
                           estimated_cost_usd=?,latency_ms=?,result_payload=?,error_detail=?
                       WHERE call_id=? AND status='reserved'""",
                    (
                        provider,
                        model,
                        status,
                        int(input_tokens),
                        int(output_tokens),
                        float(estimated_cost_usd),
                        float(latency_ms),
                        json.dumps(sanitize_for_export(result), ensure_ascii=False)
                        if result
                        else None,
                        safe_error_detail(error) if error else None,
                        row["call_id"],
                    ),
                )
            row = db.execute(
                "SELECT * FROM llm_governed_calls WHERE call_id=?",
                (row["call_id"],),
            ).fetchone()
        return self._llm_row(row)

    def task_usage(self, task_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens),0) AS input_tokens,
                       COALESCE(SUM(output_tokens),0) AS output_tokens,
                       COALESCE(SUM(estimated_cost_usd),0) AS cost_usd,
                       COALESCE(SUM(latency_ms),0) AS latency_ms,
                       SUM(CASE WHEN status='reserved' THEN 1 ELSE 0 END) AS reserved_calls,
                       SUM(CASE WHEN status!='reserved' THEN 1 ELSE 0 END) AS actual_calls,
                       COALESCE(SUM(CASE WHEN status='reserved' THEN input_tokens ELSE 0 END),0)
                         AS reserved_input_tokens,
                       COALESCE(SUM(CASE WHEN status='reserved' THEN output_tokens ELSE 0 END),0)
                         AS reserved_output_tokens,
                       COALESCE(SUM(CASE WHEN status='reserved' THEN estimated_cost_usd ELSE 0 END),0)
                         AS reserved_cost_usd,
                       COALESCE(SUM(CASE WHEN status!='reserved' THEN input_tokens ELSE 0 END),0)
                         AS actual_input_tokens,
                       COALESCE(SUM(CASE WHEN status!='reserved' THEN output_tokens ELSE 0 END),0)
                         AS actual_output_tokens,
                       COALESCE(SUM(CASE WHEN status!='reserved' THEN estimated_cost_usd ELSE 0 END),0)
                         AS actual_cost_usd
                FROM llm_governed_calls WHERE task_id=?
                """,
                (task_id,),
            ).fetchone()
        return dict(row)

    def task_calls_by_schema(self, task_id: str) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT schema_name,COUNT(*) AS calls
                   FROM llm_governed_calls WHERE task_id=? GROUP BY schema_name""",
                (task_id,),
            ).fetchall()
        return {str(row["schema_name"]): int(row["calls"]) for row in rows}

    @staticmethod
    def _enforce_llm_budget(
        projection: dict[str, Any],
        *,
        maximum_calls: int,
        maximum_total_tokens: int,
        maximum_cost_usd: float,
    ) -> None:
        if int(projection["projected_calls"]) > int(maximum_calls):
            raise RuntimeError("LLM task call budget insufficient for declared workflow")
        if int(projection["projected_total_tokens"]) > int(maximum_total_tokens):
            raise RuntimeError("LLM task token budget insufficient for declared workflow")
        if float(projection["projected_cost_usd"]) > float(maximum_cost_usd) + 1e-12:
            raise RuntimeError("LLM task cost budget insufficient for declared workflow")

    @staticmethod
    def _llm_budget_projection(
        db: sqlite3.Connection,
        *,
        task_id: str,
        phase_reservations: dict[str, dict[str, Any]],
        pending_schema_name: str | None = None,
        pending_input_tokens: int = 0,
        pending_output_tokens: int = 0,
        pending_cost_usd: float = 0.0,
    ) -> dict[str, Any]:
        rows = db.execute(
            """SELECT schema_name,COUNT(*) AS calls,
                      COALESCE(SUM(input_tokens),0) AS input_tokens,
                      COALESCE(SUM(output_tokens),0) AS output_tokens,
                      COALESCE(SUM(estimated_cost_usd),0) AS cost_usd
               FROM llm_governed_calls WHERE task_id=? GROUP BY schema_name""",
            (task_id,),
        ).fetchall()
        recorded = {str(row["schema_name"]): dict(row) for row in rows}
        committed_calls = sum(int(row["calls"]) for row in recorded.values())
        committed_input = sum(int(row["input_tokens"]) for row in recorded.values())
        committed_output = sum(int(row["output_tokens"]) for row in recorded.values())
        committed_cost = sum(float(row["cost_usd"]) for row in recorded.values())
        remaining_calls = 0
        remaining_input = 0
        remaining_output = 0
        remaining_cost = 0.0
        missing_by_schema: dict[str, int] = {}
        for schema_name, reservation in phase_reservations.items():
            observed = int(recorded.get(schema_name, {}).get("calls", 0))
            if schema_name == pending_schema_name:
                observed += 1
            missing = max(0, int(reservation.get("expected_calls") or 0) - observed)
            if missing:
                missing_by_schema[schema_name] = missing
            remaining_calls += missing
            remaining_input += missing * int(reservation.get("input_tokens_per_call") or 0)
            remaining_output += missing * int(reservation.get("output_tokens_per_call") or 0)
            remaining_cost += missing * float(reservation.get("cost_per_call") or 0.0)
        projected_calls = committed_calls + (1 if pending_schema_name else 0) + remaining_calls
        projected_input = committed_input + int(pending_input_tokens) + remaining_input
        projected_output = committed_output + int(pending_output_tokens) + remaining_output
        projected_cost = committed_cost + float(pending_cost_usd) + remaining_cost
        return {
            "committed_calls": committed_calls,
            "committed_input_tokens": committed_input,
            "committed_output_tokens": committed_output,
            "committed_cost_usd": committed_cost,
            "remaining_calls": remaining_calls,
            "remaining_input_tokens": remaining_input,
            "remaining_output_tokens": remaining_output,
            "remaining_cost_usd": round(remaining_cost, 8),
            "projected_calls": projected_calls,
            "projected_input_tokens": projected_input,
            "projected_output_tokens": projected_output,
            "projected_total_tokens": projected_input + projected_output,
            "projected_cost_usd": round(projected_cost, 8),
            "missing_by_schema": missing_by_schema,
        }

    def record_role_observation(self, observation: dict[str, Any]) -> dict[str, Any]:
        observation_id = str(uuid.uuid4())
        values = dict(observation)
        with self.transaction() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO llm_role_observations(
                    observation_id,role,run_id,symbol,as_of,horizon_days,market_regime,
                    up_probability,flat_probability,down_probability,predicted_direction,
                    realized_direction,realized_return_pct,direction_correct,brier_score,
                    log_loss,drawdown_reduction,fact_errors,quant_incremental_return_pct,
                    cost_usd,latency_ms,matured,payload,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    observation_id,
                    values["role"],
                    values["run_id"],
                    values["symbol"],
                    values["as_of"],
                    int(values["horizon_days"]),
                    values.get("market_regime"),
                    values.get("up_probability"),
                    values.get("flat_probability"),
                    values.get("down_probability"),
                    values.get("predicted_direction"),
                    values.get("realized_direction"),
                    values.get("realized_return_pct"),
                    _optional_bool(values.get("direction_correct")),
                    values.get("brier_score"),
                    values.get("log_loss"),
                    values.get("drawdown_reduction"),
                    int(values.get("fact_errors", 0)),
                    values.get("quant_incremental_return_pct"),
                    float(values.get("cost_usd", 0)),
                    float(values.get("latency_ms", 0)),
                    int(bool(values.get("matured", False))),
                    json.dumps(sanitize_for_export(values.get("payload", {})), ensure_ascii=False),
                    _now(),
                ),
            )
            row = db.execute(
                """
                SELECT * FROM llm_role_observations
                WHERE role=? AND run_id=? AND horizon_days=?
                """,
                (values["role"], values["run_id"], int(values["horizon_days"])),
            ).fetchone()
        return self._role_row(row)

    def role_scorecard(self, role: str, minimum_samples: int = 30) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM llm_role_observations WHERE role=? AND matured=1",
                (role,),
            ).fetchall()
            latest_challenge = db.execute(
                """
                SELECT * FROM llm_role_challenges WHERE role=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (role,),
            ).fetchone()
        samples = len(rows)
        metrics = _aggregate_role_metrics(rows)
        if samples < minimum_samples:
            stage = "shadow_observation"
        elif latest_challenge is None:
            stage = "frozen_challenge_required"
        elif latest_challenge["status"] == "passed" and latest_challenge["decision"] == "promote":
            stage = "promoted"
        elif latest_challenge["status"] in {"passed", "failed"}:
            stage = "rejected"
        else:
            stage = "frozen_challenge"
        return {
            "role": role,
            "stage": stage,
            "minimum_samples": minimum_samples,
            "matured_samples": samples,
            "metrics": metrics,
            "market_regime_metrics": _role_metrics_by_regime(rows),
            "latest_challenge": self._challenge_row(latest_challenge) if latest_challenge else None,
            "automatic_weight_change_allowed": False,
        }

    def freeze_role_challenge(
        self,
        role: str,
        *,
        minimum_samples: int = 30,
    ) -> dict[str, Any]:
        scorecard = self.role_scorecard(role, minimum_samples)
        if scorecard["matured_samples"] < minimum_samples:
            raise ValueError("insufficient matured samples for a frozen challenge")
        challenge_id = str(uuid.uuid4())
        now = _now()
        with self.transaction() as db:
            db.execute(
                """
                INSERT INTO llm_role_challenges(
                    challenge_id,role,frozen_at,sample_count,minimum_samples,
                    status,metrics,created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    challenge_id,
                    role,
                    now,
                    scorecard["matured_samples"],
                    minimum_samples,
                    "frozen",
                    json.dumps(scorecard["metrics"], ensure_ascii=False),
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM llm_role_challenges WHERE challenge_id=?",
                (challenge_id,),
            ).fetchone()
        return self._challenge_row(row)

    def decide_role_challenge(
        self,
        challenge_id: str,
        *,
        passed: bool,
        decision: str,
        reason: str,
        applicable_regimes: list[str] | None = None,
    ) -> dict[str, Any]:
        if decision not in {"promote", "reject"}:
            raise ValueError("challenge decision must be promote or reject")
        if decision == "promote" and not passed:
            raise ValueError("a failed challenge cannot promote a role")
        regimes = _normalize_applicable_regimes(applicable_regimes)
        with self.transaction() as db:
            challenge = db.execute(
                "SELECT * FROM llm_role_challenges WHERE challenge_id=?",
                (challenge_id,),
            ).fetchone()
            if challenge is None:
                raise ValueError("role challenge not found")
            if regimes != ["all"]:
                counts = {
                    str(item[0]): int(item[1])
                    for item in db.execute(
                        """
                        SELECT market_regime,COUNT(*) FROM llm_role_observations
                        WHERE role=? AND matured=1 AND market_regime IS NOT NULL
                          AND created_at<=?
                        GROUP BY market_regime
                        """,
                        (challenge["role"], challenge["frozen_at"]),
                    ).fetchall()
                }
                minimum = int(challenge["minimum_samples"])
                insufficient = [
                    regime for regime in regimes if counts.get(regime, 0) < minimum
                ]
                if insufficient:
                    raise ValueError(
                        "insufficient matured samples for applicable market regimes: "
                        + ",".join(insufficient)
                    )
            db.execute(
                """
                UPDATE llm_role_challenges
                SET status=?,decision=?,reason=?,decided_at=?
                WHERE challenge_id=? AND status='frozen'
                """,
                ("passed" if passed else "failed", decision, reason, _now(), challenge_id),
            )
            row = db.execute(
                "SELECT * FROM llm_role_challenges WHERE challenge_id=?",
                (challenge_id,),
            ).fetchone()
            if row is not None and row["decided_at"] is not None:
                existing_policy = db.execute(
                    "SELECT * FROM llm_role_policies WHERE challenge_id=?",
                    (challenge_id,),
                ).fetchone()
                if existing_policy is None:
                    governance_version = (
                        f"role-policy-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
                    )
                    db.execute(
                        "UPDATE llm_role_policies SET active=0 WHERE role=? AND active=1",
                        (row["role"],),
                    )
                    db.execute(
                        """
                        INSERT INTO llm_role_policies(
                            policy_id,governance_version,role,weight,minimum_samples,
                            applicable_regimes,challenge_id,decision,active,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,1,?)
                        """,
                        (
                            str(uuid.uuid4()),
                            governance_version,
                            row["role"],
                            1.25 if decision == "promote" else 0.50,
                            int(row["minimum_samples"]),
                            json.dumps(regimes, ensure_ascii=False),
                            challenge_id,
                            decision,
                            _now(),
                        ),
                    )
        if row is None:
            raise ValueError("role challenge not found")
        return self._challenge_row(row)

    def active_role_policy(
        self,
        roles: list[str],
        *,
        market_regime: str | None = None,
        default_minimum_samples: int = 30,
    ) -> dict[str, Any]:
        policies: dict[str, dict[str, Any]] = {}
        with self.connect() as db:
            for role in roles:
                rows = db.execute(
                    """
                    SELECT * FROM llm_role_policies
                    WHERE role=? AND active=1 ORDER BY created_at DESC
                    """,
                    (role,),
                ).fetchall()
                selected = None
                for row in rows:
                    regimes = json.loads(row["applicable_regimes"])
                    if "all" in regimes or market_regime in regimes:
                        selected = row
                        break
                if selected is None:
                    policies[role] = {
                        "role": role,
                        "weight": 1.0,
                        "minimum_samples": default_minimum_samples,
                        "applicable_regimes": ["all"],
                        "governance_version": "default-role-policy-v1",
                        "decision": "default",
                    }
                else:
                    item = dict(selected)
                    item["applicable_regimes"] = json.loads(item["applicable_regimes"])
                    item["active"] = bool(item["active"])
                    policies[role] = item
        canonical = {
            role: {
                "weight": item["weight"],
                "minimum_samples": item["minimum_samples"],
                "applicable_regimes": item["applicable_regimes"],
                "governance_version": item["governance_version"],
            }
            for role, item in sorted(policies.items())
        }
        governance_version = _fingerprint(canonical)
        return {
            "governance_version": governance_version,
            "market_regime": market_regime,
            "roles": policies,
        }

    @staticmethod
    def _llm_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["result_payload"] = (
            json.loads(item["result_payload"]) if item.get("result_payload") else None
        )
        return item

    @staticmethod
    def _role_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["matured"] = bool(item["matured"])
        item["direction_correct"] = (
            bool(item["direction_correct"]) if item["direction_correct"] is not None else None
        )
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _challenge_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metrics"] = json.loads(item["metrics"])
        return item


def _normalize_applicable_regimes(values: list[str] | None) -> list[str]:
    regimes = sorted(
        {
            str(value).strip().lower()
            for value in (values or ["all"])
            if str(value).strip()
        }
    )
    if not regimes:
        raise ValueError("at least one applicable market regime is required")
    if "all" in regimes and len(regimes) > 1:
        raise ValueError("market regime 'all' cannot be combined with scoped regimes")
    return regimes


def _optional_bool(value: Any) -> int | None:
    return None if value is None else int(bool(value))


def _aggregate_role_metrics(rows: list[sqlite3.Row]) -> dict[str, Any]:
    return {
        "sample_count": len(rows),
        "direction_accuracy": _mean([row["direction_correct"] for row in rows]),
        "brier_score": _mean([row["brier_score"] for row in rows]),
        "log_loss": _mean([row["log_loss"] for row in rows]),
        "mean_realized_return_pct": _mean([row["realized_return_pct"] for row in rows]),
        "mean_drawdown_reduction": _mean([row["drawdown_reduction"] for row in rows]),
        "fact_errors": sum(int(row["fact_errors"] or 0) for row in rows),
        "mean_quant_incremental_return_pct": _mean(
            [row["quant_incremental_return_pct"] for row in rows]
        ),
        "total_cost_usd": round(sum(float(row["cost_usd"] or 0) for row in rows), 6),
        "mean_latency_ms": _mean([row["latency_ms"] for row in rows]),
    }


def _role_metrics_by_regime(rows: list[sqlite3.Row]) -> dict[str, Any]:
    regimes: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        regimes.setdefault(str(row["market_regime"] or "unknown"), []).append(row)
    return {name: _aggregate_role_metrics(items) for name, items in regimes.items()}


def _mean(values: list[Any]) -> float | None:
    numeric = [float(value) for value in values if value is not None]
    return round(sum(numeric) / len(numeric), 6) if numeric else None


__all__ = ["EvidenceRepository"]
