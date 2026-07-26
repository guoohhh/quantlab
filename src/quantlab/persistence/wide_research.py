from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from quantlab.backtest.statistics import paired_block_bootstrap
from quantlab.domain.strategy_evidence import ABLATION_VARIANTS
from quantlab.persistence.migrations import record_component_migration
from quantlab.security import sanitize_for_export


EVIDENCE_BOUNDARY = "wide_forward_research"


class WideResearchRepository:
    """Persistence boundary for broad forward validation and fractional research NAVs."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        record_component_migration(
            self.path,
            component="wide_research",
            version=1,
            migration_identity="wide-forward-fractional-portfolios-user-adoption-v1",
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
                CREATE TABLE IF NOT EXISTS wide_forward_experiments (
                    experiment_id TEXT PRIMARY KEY,
                    protocol_version TEXT NOT NULL UNIQUE,
                    protocol_fingerprint TEXT NOT NULL UNIQUE,
                    cohort_id TEXT NOT NULL UNIQUE,
                    evidence_boundary TEXT NOT NULL CHECK(evidence_boundary='wide_forward_research'),
                    status TEXT NOT NULL,
                    target_sample_size INTEGER NOT NULL CHECK(target_sample_size BETWEEN 20 AND 30),
                    minimum_sample_size INTEGER NOT NULL CHECK(minimum_sample_size BETWEEN 20 AND 30),
                    signal_start_date TEXT NOT NULL,
                    frozen_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wide_forward_batches (
                    batch_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    snapshot_fingerprint TEXT NOT NULL,
                    manifest_id TEXT,
                    schedule_run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    registration_started_at TEXT NOT NULL,
                    registration_completed_at TEXT,
                    member_count INTEGER NOT NULL DEFAULT 0,
                    prediction_count INTEGER NOT NULL DEFAULT 0,
                    independent_trade_days INTEGER NOT NULL DEFAULT 1,
                    llm_calls INTEGER NOT NULL DEFAULT 0,
                    llm_input_tokens INTEGER NOT NULL DEFAULT 0,
                    llm_output_tokens INTEGER NOT NULL DEFAULT 0,
                    llm_cost_usd REAL NOT NULL DEFAULT 0,
                    llm_latency_ms REAL NOT NULL DEFAULT 0,
                    role_completeness REAL NOT NULL DEFAULT 0,
                    failure_reason TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(experiment_id,trade_date),
                    FOREIGN KEY(experiment_id) REFERENCES wide_forward_experiments(experiment_id)
                );
                CREATE TABLE IF NOT EXISTS wide_forward_members (
                    batch_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    selection_rank INTEGER NOT NULL,
                    industry TEXT NOT NULL,
                    market_cap_bucket TEXT NOT NULL,
                    trend_bucket TEXT NOT NULL,
                    price_change_state TEXT NOT NULL,
                    style_bucket TEXT NOT NULL,
                    quant_score REAL NOT NULL,
                    quant_direction TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    missing_reasons TEXT NOT NULL DEFAULT '[]',
                    payload TEXT NOT NULL,
                    PRIMARY KEY(batch_id,symbol),
                    UNIQUE(batch_id,selection_rank),
                    FOREIGN KEY(batch_id) REFERENCES wide_forward_batches(batch_id)
                );
                CREATE TABLE IF NOT EXISTS wide_forward_prediction_links (
                    batch_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL CHECK(horizon_days IN (5,20)),
                    variant TEXT NOT NULL,
                    prediction_id TEXT NOT NULL UNIQUE,
                    sample_key TEXT NOT NULL,
                    context_fingerprint TEXT NOT NULL,
                    quote_fingerprint TEXT NOT NULL,
                    prompt_fingerprint TEXT NOT NULL,
                    model_fingerprint TEXT NOT NULL,
                    prediction_fingerprint TEXT NOT NULL,
                    expected_return_low_pct REAL NOT NULL,
                    expected_return_high_pct REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(batch_id,symbol,horizon_days,variant),
                    FOREIGN KEY(batch_id,symbol) REFERENCES wide_forward_members(batch_id,symbol)
                );
                CREATE TABLE IF NOT EXISTS research_portfolios (
                    portfolio_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    evidence_boundary TEXT NOT NULL CHECK(evidence_boundary='wide_forward_research'),
                    initial_nav REAL NOT NULL CHECK(initial_nav>0),
                    fractional_units INTEGER NOT NULL CHECK(fractional_units=1),
                    weighting_method TEXT NOT NULL,
                    execution_convention TEXT NOT NULL,
                    commission_rate REAL NOT NULL,
                    transfer_fee_rate REAL NOT NULL,
                    stamp_duty_rate REAL NOT NULL,
                    slippage_bps REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(experiment_id,variant),
                    FOREIGN KEY(experiment_id) REFERENCES wide_forward_experiments(experiment_id)
                );
                CREATE TABLE IF NOT EXISTS research_portfolio_positions (
                    portfolio_id TEXT NOT NULL,
                    prediction_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    signal_date TEXT NOT NULL,
                    execution_date TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    weight REAL NOT NULL,
                    triggered INTEGER NOT NULL,
                    realized_direction TEXT NOT NULL,
                    realized_return_pct REAL NOT NULL,
                    benchmark_return_pct REAL,
                    gross_return_pct REAL NOT NULL,
                    transaction_cost_pct REAL NOT NULL,
                    net_return_pct REAL NOT NULL,
                    outcome_observed_at TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(portfolio_id,prediction_id),
                    FOREIGN KEY(portfolio_id) REFERENCES research_portfolios(portfolio_id),
                    FOREIGN KEY(batch_id) REFERENCES wide_forward_batches(batch_id)
                );
                CREATE TABLE IF NOT EXISTS research_portfolio_nav (
                    portfolio_id TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL CHECK(horizon_days IN (5,20)),
                    nav_date TEXT NOT NULL,
                    nav_gross REAL NOT NULL,
                    nav_net REAL NOT NULL,
                    benchmark_nav REAL,
                    drawdown_pct REAL NOT NULL,
                    positions_count INTEGER NOT NULL,
                    independent_trade_days INTEGER NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(portfolio_id,horizon_days,nav_date),
                    FOREIGN KEY(portfolio_id) REFERENCES research_portfolios(portfolio_id)
                );
                CREATE TABLE IF NOT EXISTS user_adoption_records (
                    adoption_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL UNIQUE,
                    check_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    research_run_id TEXT,
                    research_link_status TEXT NOT NULL,
                    ai_action TEXT,
                    ai_suggested_quantity INTEGER NOT NULL,
                    user_side TEXT NOT NULL,
                    user_quantity INTEGER NOT NULL,
                    adoption_status TEXT NOT NULL,
                    pretrade_price REAL NOT NULL,
                    pretrade_observed_at TEXT NOT NULL,
                    quote_fingerprint TEXT NOT NULL,
                    context_id TEXT,
                    context_fingerprint TEXT,
                    evidence_payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_wide_batch_date
                  ON wide_forward_batches(experiment_id,trade_date,status);
                CREATE INDEX IF NOT EXISTS idx_wide_member_strata
                  ON wide_forward_members(batch_id,industry,market_cap_bucket,trend_bucket);
                CREATE INDEX IF NOT EXISTS idx_research_nav
                  ON research_portfolio_nav(portfolio_id,horizon_days,nav_date);
                CREATE INDEX IF NOT EXISTS idx_user_adoption_account
                  ON user_adoption_records(account_id,created_at);
                """
            )

    def create_experiment(
        self,
        *,
        protocol_version: str,
        cohort_id: str,
        target_sample_size: int,
        minimum_sample_size: int,
        signal_start_date: date,
        frozen_at: datetime,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not 20 <= minimum_sample_size <= target_sample_size <= 30:
            raise ValueError("wide forward sample size must remain between 20 and 30")
        normalized = sanitize_for_export(payload)
        fingerprint = _fingerprint(normalized)
        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM wide_forward_experiments WHERE protocol_version=?",
                (protocol_version,),
            ).fetchone()
            if existing is not None:
                if existing["protocol_fingerprint"] != fingerprint:
                    raise ValueError("a frozen wide-forward protocol cannot be modified")
                return _json_row(existing, "payload")
            now = _now()
            experiment_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO wide_forward_experiments(
                       experiment_id,protocol_version,protocol_fingerprint,cohort_id,
                       evidence_boundary,status,target_sample_size,minimum_sample_size,
                       signal_start_date,frozen_at,payload,created_at
                   ) VALUES(?,?,?,?,?,'preregistered',?,?,?,?,?,?)""",
                (
                    experiment_id,
                    protocol_version,
                    fingerprint,
                    cohort_id,
                    EVIDENCE_BOUNDARY,
                    target_sample_size,
                    minimum_sample_size,
                    signal_start_date.isoformat(),
                    frozen_at.astimezone(UTC).isoformat(),
                    json.dumps(normalized, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM wide_forward_experiments WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
        return _json_row(row, "payload")

    def experiment(self, experiment_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM wide_forward_experiments WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
        return _json_row(row, "payload") if row else None

    def experiment_by_protocol_version(
        self, protocol_version: str
    ) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM wide_forward_experiments WHERE protocol_version=?",
                (protocol_version,),
            ).fetchone()
        return _json_row(row, "payload") if row else None

    def latest_experiment(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM wide_forward_experiments ORDER BY frozen_at DESC LIMIT 1"
            ).fetchone()
        return _json_row(row, "payload") if row else None

    def experiments(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM wide_forward_experiments ORDER BY frozen_at DESC"
            ).fetchall()
        return [_json_row(row, "payload") for row in rows]

    def begin_batch(
        self,
        *,
        experiment: dict[str, Any],
        trade_date: date,
        snapshot: dict[str, Any],
        schedule_run_id: str,
        started_at: datetime,
    ) -> dict[str, Any]:
        if trade_date < date.fromisoformat(experiment["signal_start_date"]):
            raise ValueError("wide forward batches cannot predate protocol registration")
        if snapshot["snapshot_date"] != trade_date.isoformat():
            raise ValueError("wide forward registration requires the exact trade-date snapshot")
        if datetime.fromisoformat(snapshot["cutoff_at"]) > started_at.astimezone(UTC):
            raise ValueError("wide forward snapshot was not knowable when registration started")
        with self.connect() as db:
            existing = db.execute(
                """SELECT * FROM wide_forward_batches
                   WHERE experiment_id=? AND trade_date=?""",
                (experiment["experiment_id"], trade_date.isoformat()),
            ).fetchone()
            if existing is not None:
                return {**_json_row(existing, "payload"), "_newly_created": False}
            batch_id = str(uuid.uuid4())
            now = _now()
            db.execute(
                """INSERT INTO wide_forward_batches(
                       batch_id,experiment_id,trade_date,snapshot_id,snapshot_fingerprint,
                       manifest_id,schedule_run_id,status,registration_started_at,created_at
                   ) VALUES(?,?,?,?,?,?,?,'running',?,?)""",
                (
                    batch_id,
                    experiment["experiment_id"],
                    trade_date.isoformat(),
                    snapshot["snapshot_id"],
                    snapshot["fingerprint"],
                    snapshot.get("manifest_id"),
                    schedule_run_id,
                    started_at.astimezone(UTC).isoformat(),
                    now,
                ),
            )
            db.execute(
                "UPDATE wide_forward_experiments SET status='active' WHERE experiment_id=?",
                (experiment["experiment_id"],),
            )
            row = db.execute(
                "SELECT * FROM wide_forward_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
        return {**_json_row(row, "payload"), "_newly_created": True}

    def save_members(self, batch_id: str, members: list[dict[str, Any]]) -> None:
        with self.connect() as db:
            for item in members:
                db.execute(
                    """INSERT OR IGNORE INTO wide_forward_members(
                           batch_id,symbol,selection_rank,industry,market_cap_bucket,
                           trend_bucket,price_change_state,style_bucket,quant_score,
                           quant_direction,observed_at,source,source_fingerprint,
                           missing_reasons,payload
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        batch_id,
                        item["symbol"],
                        int(item["selection_rank"]),
                        item["industry"],
                        item["market_cap_bucket"],
                        item["trend_bucket"],
                        item["price_change_state"],
                        item["style_bucket"],
                        float(item["quant_score"]),
                        item["quant_direction"],
                        item["observed_at"],
                        item["source"],
                        item["source_fingerprint"],
                        json.dumps(item.get("missing_reasons", []), ensure_ascii=False),
                        json.dumps(
                            sanitize_for_export(item.get("payload", {})),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )

    def link_predictions(
        self,
        *,
        batch_id: str,
        symbol: str,
        horizon_days: int,
        predictions: list[dict[str, Any]],
    ) -> int:
        now = _now()
        with self.connect() as db:
            for prediction in predictions:
                probabilities = prediction["probabilities"]
                score = float(probabilities["up"]) - float(probabilities["down"])
                center = score * (4.0 if horizon_days == 5 else 8.0)
                radius = 3.0 if horizon_days == 5 else 6.0
                model_identity = {
                    "statistical_model_id": prediction.get("payload", {}).get(
                        "statistical_model_id"
                    ),
                    "raw_llm_provider": prediction.get("payload", {}).get(
                        "raw_llm_provider"
                    ),
                    "raw_llm_model": prediction.get("payload", {}).get("raw_llm_model"),
                    "governance_version": prediction.get("governance_version"),
                }
                db.execute(
                    """INSERT OR IGNORE INTO wide_forward_prediction_links(
                           batch_id,symbol,horizon_days,variant,prediction_id,sample_key,
                           context_fingerprint,quote_fingerprint,prompt_fingerprint,
                           model_fingerprint,prediction_fingerprint,
                           expected_return_low_pct,expected_return_high_pct,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        batch_id,
                        symbol,
                        horizon_days,
                        prediction["variant"],
                        prediction["prediction_id"],
                        prediction["sample_key"],
                        prediction["context_fingerprint"],
                        prediction["quote_fingerprint"],
                        _fingerprint({"prompt_version": prediction["prompt_version"]}),
                        _fingerprint(model_identity),
                        prediction["prediction_fingerprint"],
                        center - radius,
                        center + radius,
                        now,
                    ),
                )
        return len(predictions)

    def finish_batch(
        self,
        batch_id: str,
        *,
        status: str,
        member_count: int,
        prediction_count: int,
        llm_usage: dict[str, Any],
        role_completeness: float,
        payload: dict[str, Any],
        failure_reason: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "blocked"}:
            raise ValueError("invalid wide forward batch terminal status")
        with self.connect() as db:
            db.execute(
                """UPDATE wide_forward_batches SET status=?,registration_completed_at=?,
                       member_count=?,prediction_count=?,llm_calls=?,llm_input_tokens=?,
                       llm_output_tokens=?,llm_cost_usd=?,llm_latency_ms=?,
                       role_completeness=?,failure_reason=?,payload=? WHERE batch_id=?""",
                (
                    status,
                    _now(),
                    member_count,
                    prediction_count,
                    int(llm_usage.get("calls", 0)),
                    int(llm_usage.get("input_tokens", 0)),
                    int(llm_usage.get("output_tokens", 0)),
                    float(llm_usage.get("cost_usd", 0.0)),
                    float(llm_usage.get("latency_ms", 0.0)),
                    max(0.0, min(1.0, role_completeness)),
                    failure_reason,
                    json.dumps(sanitize_for_export(payload), ensure_ascii=False, sort_keys=True),
                    batch_id,
                ),
            )
            row = db.execute(
                "SELECT * FROM wide_forward_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
        return _json_row(row, "payload")

    def reconcile_batch_usage(
        self, batch_id: str, *, llm_usage: dict[str, Any]
    ) -> dict[str, Any]:
        """Correct terminal-batch telemetry without changing its scientific payload."""
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM wide_forward_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if row is None:
                raise ValueError("wide forward batch not found")
            if row["status"] not in {"completed", "failed", "blocked"}:
                raise ValueError("wide forward batch must be terminal before usage reconciliation")
            payload = json.loads(row["payload"] or "{}")
            payload["llm_usage_reconciliation"] = {
                "reconciled_at": _now(),
                "reason": "include_all_wide_committee_governed_calls",
                "previous": {
                    "calls": int(row["llm_calls"]),
                    "input_tokens": int(row["llm_input_tokens"]),
                    "output_tokens": int(row["llm_output_tokens"]),
                    "cost_usd": float(row["llm_cost_usd"]),
                    "latency_ms": float(row["llm_latency_ms"]),
                },
            }
            db.execute(
                """UPDATE wide_forward_batches
                   SET llm_calls=?,llm_input_tokens=?,llm_output_tokens=?,
                       llm_cost_usd=?,llm_latency_ms=?,payload=?
                   WHERE batch_id=?""",
                (
                    int(llm_usage.get("calls", 0)),
                    int(llm_usage.get("input_tokens", 0)),
                    int(llm_usage.get("output_tokens", 0)),
                    float(llm_usage.get("cost_usd", 0.0)),
                    float(llm_usage.get("latency_ms", 0.0)),
                    json.dumps(sanitize_for_export(payload), ensure_ascii=False, sort_keys=True),
                    batch_id,
                ),
            )
            updated = db.execute(
                "SELECT * FROM wide_forward_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
        return _json_row(updated, "payload")

    def batches(self, experiment_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM wide_forward_batches"
        params: tuple[Any, ...] = ()
        if experiment_id:
            query += " WHERE experiment_id=?"
            params = (experiment_id,)
        query += " ORDER BY trade_date DESC,created_at DESC"
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [_json_row(row, "payload") for row in rows]

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM wide_forward_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if row is None:
                return None
            members = db.execute(
                """SELECT * FROM wide_forward_members WHERE batch_id=?
                   ORDER BY selection_rank""",
                (batch_id,),
            ).fetchall()
            predictions = db.execute(
                """SELECT * FROM wide_forward_prediction_links WHERE batch_id=?
                   ORDER BY symbol,horizon_days,variant""",
                (batch_id,),
            ).fetchall()
        item = _json_row(row, "payload")
        item["members"] = [
            _json_row(member, "missing_reasons", "payload") for member in members
        ]
        item["prediction_links"] = [dict(prediction) for prediction in predictions]
        return item

    def ensure_portfolios(
        self,
        *,
        experiment: dict[str, Any],
        cost_rules: dict[str, float],
        initial_nav: float = 100.0,
    ) -> list[dict[str, Any]]:
        now = _now()
        with self.connect() as db:
            for variant in ABLATION_VARIANTS:
                db.execute(
                    """INSERT OR IGNORE INTO research_portfolios(
                           portfolio_id,experiment_id,variant,evidence_boundary,initial_nav,
                           fractional_units,weighting_method,execution_convention,
                           commission_rate,transfer_fee_rate,stamp_duty_rate,slippage_bps,
                           status,created_at
                       ) VALUES(?,?,?,?,?,1,'equal_notional','T_close_signal_T_plus_1_open',
                                ?,?,?,?,'active',?)""",
                    (
                        str(uuid.uuid4()),
                        experiment["experiment_id"],
                        variant.value,
                        EVIDENCE_BOUNDARY,
                        initial_nav,
                        float(cost_rules.get("commission_rate", 0.00025)),
                        float(cost_rules.get("transfer_fee_rate", 0.00001)),
                        float(cost_rules.get("stamp_duty_rate", 0.0005)),
                        float(cost_rules.get("slippage_bps", 10.0)),
                        now,
                    ),
                )
            rows = db.execute(
                """SELECT * FROM research_portfolios WHERE experiment_id=?
                   ORDER BY variant""",
                (experiment["experiment_id"],),
            ).fetchall()
            for row in rows:
                for horizon in (5, 20):
                    db.execute(
                        """INSERT OR IGNORE INTO research_portfolio_nav(
                               portfolio_id,horizon_days,nav_date,nav_gross,nav_net,
                               benchmark_nav,drawdown_pct,positions_count,
                               independent_trade_days,payload,created_at
                           ) VALUES(?,?,?,?,?,?,0,0,0,?,?)""",
                        (
                            row["portfolio_id"],
                            horizon,
                            experiment["signal_start_date"],
                            initial_nav,
                            initial_nav,
                            initial_nav,
                            json.dumps(
                                {
                                    "state": "preregistered_initial_nav",
                                    "not_real_executable_account": True,
                                },
                                sort_keys=True,
                            ),
                            now,
                        ),
                    )
        return [dict(row) for row in rows]

    def portfolios(self, experiment_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM research_portfolios"
        params: tuple[Any, ...] = ()
        if experiment_id:
            query += " WHERE experiment_id=?"
            params = (experiment_id,)
        query += " ORDER BY experiment_id,variant"
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def portfolio(self, portfolio_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM research_portfolios WHERE portfolio_id=?", (portfolio_id,)
            ).fetchone()
            if row is None:
                return None
            nav = db.execute(
                """SELECT * FROM research_portfolio_nav WHERE portfolio_id=?
                   ORDER BY horizon_days,nav_date""",
                (portfolio_id,),
            ).fetchall()
            positions = db.execute(
                """SELECT * FROM research_portfolio_positions WHERE portfolio_id=?
                   ORDER BY horizon_days,signal_date,symbol""",
                (portfolio_id,),
            ).fetchall()
        return {
            **dict(row),
            "nav": [_json_row(item, "payload") for item in nav],
            "positions": [_json_row(item, "payload") for item in positions],
        }

    def mark_settled_positions(
        self,
        *,
        experiment_id: str,
        benchmark_returns: dict[tuple[str, int], float] | None = None,
        standardized_returns: dict[tuple[str, str, int], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        benchmark_returns = benchmark_returns or {}
        standardized_returns = standardized_returns or {}
        with self.connect() as db:
            portfolios = {
                row["variant"]: row
                for row in db.execute(
                    "SELECT * FROM research_portfolios WHERE experiment_id=?",
                    (experiment_id,),
                ).fetchall()
            }
            rows = db.execute(
                """SELECT l.*,b.trade_date,b.member_count,p.registered_at,p.due_at,
                          p.target_weight,p.actually_triggered,p.probabilities,p.action,
                          o.realized_direction,o.realized_return_pct,o.observed_at,
                          o.outcome_source,o.payload outcome_payload
                   FROM wide_forward_prediction_links l
                   JOIN wide_forward_batches b ON b.batch_id=l.batch_id
                   JOIN forward_ablation_predictions p ON p.prediction_id=l.prediction_id
                   JOIN forward_ablation_outcomes o ON o.prediction_id=p.prediction_id
                   WHERE b.experiment_id=? AND b.status='completed'
                   ORDER BY b.trade_date,l.horizon_days,l.symbol,l.variant""",
                (experiment_id,),
            ).fetchall()
            inserted = 0
            for row in rows:
                portfolio = portfolios.get(row["variant"])
                if portfolio is None:
                    continue
                standardized = standardized_returns.get(
                    (str(row["symbol"]), str(row["trade_date"]), int(row["horizon_days"]))
                )
                if standardized_returns and standardized is None:
                    continue
                member_count = max(1, int(row["member_count"]))
                weight = 1.0 / member_count
                triggered = bool(row["actually_triggered"])
                realized_return = float(
                    standardized["realized_return_pct"]
                    if standardized is not None
                    else row["realized_return_pct"]
                )
                realized_direction = (
                    "up"
                    if realized_return > 1.0
                    else "down"
                    if realized_return < -1.0
                    else "flat"
                )
                gross = realized_return * weight if triggered else 0.0
                round_trip_rate = (
                    float(portfolio["commission_rate"]) * 2
                    + float(portfolio["transfer_fee_rate"]) * 2
                    + float(portfolio["stamp_duty_rate"])
                    + float(portfolio["slippage_bps"]) * 2 / 10_000
                )
                cost = round_trip_rate * weight * 100.0 if triggered else 0.0
                benchmark_return = benchmark_returns.get(
                    (str(row["trade_date"]), int(row["horizon_days"]))
                )
                before = db.total_changes
                db.execute(
                    """INSERT OR IGNORE INTO research_portfolio_positions(
                           portfolio_id,prediction_id,batch_id,symbol,horizon_days,
                           signal_date,execution_date,due_at,weight,triggered,
                           realized_direction,realized_return_pct,benchmark_return_pct,
                           gross_return_pct,transaction_cost_pct,net_return_pct,
                           outcome_observed_at,payload,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        portfolio["portfolio_id"],
                        row["prediction_id"],
                        row["batch_id"],
                        row["symbol"],
                        int(row["horizon_days"]),
                        row["trade_date"],
                        (
                            standardized["execution_date"]
                            if standardized is not None
                            else row["registered_at"]
                        ),
                        row["due_at"],
                        weight,
                        int(triggered),
                        realized_direction,
                        realized_return,
                        benchmark_return,
                        gross,
                        cost,
                        gross - cost,
                        row["observed_at"],
                        json.dumps(
                            sanitize_for_export(
                                {
                                    "downside_predictions_are_avoidance_only": True,
                                    "outcome_source": row["outcome_source"],
                                    "probabilities": json.loads(row["probabilities"]),
                                    "action": row["action"],
                                    "target_weight": row["target_weight"],
                                    "standardized_execution": standardized,
                                }
                            ),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        _now(),
                    ),
                )
                inserted += int(db.total_changes > before)
            self._rebuild_nav_in_tx(db, portfolios.values())
        return {"inserted_positions": inserted, "portfolios": len(portfolios)}

    def _rebuild_nav_in_tx(
        self, db: sqlite3.Connection, portfolios: Any
    ) -> None:
        for portfolio in portfolios:
            for horizon in (5, 20):
                rows = db.execute(
                    """SELECT signal_date,SUM(gross_return_pct) gross,
                              SUM(net_return_pct) net,AVG(benchmark_return_pct) benchmark,
                              COUNT(*) positions
                       FROM research_portfolio_positions
                       WHERE portfolio_id=? AND horizon_days=?
                       GROUP BY signal_date ORDER BY signal_date""",
                    (portfolio["portfolio_id"], horizon),
                ).fetchall()
                gross_nav = float(portfolio["initial_nav"])
                net_nav = float(portfolio["initial_nav"])
                benchmark_nav = float(portfolio["initial_nav"])
                peak = net_nav
                db.execute(
                    """DELETE FROM research_portfolio_nav WHERE portfolio_id=?
                       AND horizon_days=? AND positions_count>0""",
                    (portfolio["portfolio_id"], horizon),
                )
                for index, row in enumerate(rows, start=1):
                    gross_nav *= 1.0 + float(row["gross"] or 0.0) / 100.0
                    net_nav *= 1.0 + float(row["net"] or 0.0) / 100.0
                    if row["benchmark"] is not None:
                        benchmark_nav *= 1.0 + float(row["benchmark"]) / 100.0
                    peak = max(peak, net_nav)
                    drawdown = (net_nav / peak - 1.0) * 100.0
                    db.execute(
                        """INSERT OR REPLACE INTO research_portfolio_nav(
                               portfolio_id,horizon_days,nav_date,nav_gross,nav_net,
                               benchmark_nav,drawdown_pct,positions_count,
                               independent_trade_days,payload,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            portfolio["portfolio_id"],
                            horizon,
                            row["signal_date"],
                            gross_nav,
                            net_nav,
                            benchmark_nav,
                            drawdown,
                            int(row["positions"]),
                            index,
                            json.dumps(
                                {"fractional_units": True, "real_account": False},
                                sort_keys=True,
                            ),
                            _now(),
                        ),
                    )

    def scorecard(self, experiment_id: str, horizon_days: int) -> dict[str, Any]:
        if horizon_days not in {5, 20}:
            raise ValueError("wide research scorecard horizon must be 5 or 20")
        observed_at = datetime.now(UTC)
        with self.connect() as db:
            experiment = db.execute(
                "SELECT * FROM wide_forward_experiments WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
            if experiment is None:
                raise ValueError("wide forward experiment not found")
            portfolios = {
                str(row["variant"]): row
                for row in db.execute(
                    "SELECT * FROM research_portfolios WHERE experiment_id=?", (experiment_id,)
                ).fetchall()
            }
            batches = db.execute(
                """SELECT batch_id,trade_date,status,member_count,prediction_count,
                          independent_trade_days,llm_calls,llm_cost_usd,role_completeness,
                          failure_reason
                   FROM wide_forward_batches WHERE experiment_id=? ORDER BY trade_date""",
                (experiment_id,),
            ).fetchall()
            rows = db.execute(
                """SELECT l.variant,l.symbol,l.sample_key,b.batch_id,b.trade_date,
                          b.member_count,p.prediction_id AS persisted_prediction_id,
                          p.probabilities,p.actually_triggered,p.due_at,
                          o.prediction_id AS outcome_prediction_id,
                          COALESCE(rp.realized_direction,o.realized_direction) realized_direction,
                          COALESCE(rp.realized_return_pct,o.realized_return_pct) realized_return_pct,
                          rp.weight,rp.triggered AS portfolio_triggered,
                          rp.gross_return_pct,rp.transaction_cost_pct,rp.net_return_pct,
                          rp.benchmark_return_pct
                   FROM wide_forward_prediction_links l
                   JOIN wide_forward_batches b ON b.batch_id=l.batch_id
                   LEFT JOIN forward_ablation_predictions p ON p.prediction_id=l.prediction_id
                   LEFT JOIN forward_ablation_outcomes o ON o.prediction_id=p.prediction_id
                   LEFT JOIN research_portfolios rpf
                     ON rpf.experiment_id=b.experiment_id AND rpf.variant=l.variant
                   LEFT JOIN research_portfolio_positions rp
                     ON rp.portfolio_id=rpf.portfolio_id AND rp.prediction_id=l.prediction_id
                   WHERE b.experiment_id=? AND l.horizon_days=?
                   ORDER BY b.trade_date,l.symbol,l.variant""",
                (experiment_id, horizon_days),
            ).fetchall()
        coverage = _wide_sample_coverage(rows, observed_at=observed_at)
        variants: dict[str, Any] = {}
        quant_triggered = {
            (row["trade_date"], row["symbol"]): bool(row["actually_triggered"])
            for row in rows
            if row["variant"] == "quant_only"
        }
        daily_returns_by_variant: dict[str, dict[str, dict[str, float]]] = {}
        minimum_trade_days = 30
        for variant in (item.value for item in ABLATION_VARIANTS):
            all_items = [row for row in rows if row["variant"] == variant]
            items = [row for row in all_items if row["outcome_prediction_id"] is not None]
            economic_items = [row for row in items if row["net_return_pct"] is not None]
            briers: list[float] = []
            log_losses: list[float] = []
            correct = 0
            avoided_losses = 0.0
            missed_upside = 0.0
            for row in items:
                probabilities = json.loads(row["probabilities"])
                outcome = row["realized_direction"]
                briers.append(
                    sum(
                        (float(probabilities[key]) - float(key == outcome)) ** 2
                        for key in ("up", "flat", "down")
                    )
                )
                log_losses.append(-math.log(max(1e-12, float(probabilities[outcome]))))
                correct += int(max(probabilities, key=probabilities.get) == outcome)
                key = (row["trade_date"], row["symbol"])
                vetoed = quant_triggered.get(key, False) and not bool(row["actually_triggered"])
                realized = float(row["realized_return_pct"])
                if vetoed and realized < 0:
                    avoided_losses += abs(realized)
                elif vetoed and realized > 0:
                    missed_upside += realized
            trade_days = _aggregate_wide_trade_days(economic_items)
            daily_returns_by_variant[variant] = trade_days
            daily_values = [trade_days[day] for day in sorted(trade_days)]
            nav = self._latest_nav(experiment_id, variant, horizon_days)
            initial_nav = float(portfolios[variant]["initial_nav"]) if variant in portfolios else None
            actual_trigger_count = sum(bool(row["actually_triggered"]) for row in items)
            variants[variant] = {
                "stocks": len({row["symbol"] for row in items}),
                "samples": len(items),
                "registered_samples": len(all_items),
                "settled_samples": len(items),
                "portfolio_marked_samples": len(economic_items),
                "portfolio_marking_coverage": (
                    len(economic_items) / len(items) if items else None
                ),
                "independent_trade_days": len(trade_days),
                "minimum_independent_trade_days": minimum_trade_days,
                "claim_status": "research_only_collecting_evidence",
                "direction_accuracy": correct / len(items) if items else None,
                "brier_score": _mean(briers),
                "log_loss": _mean(log_losses),
                "average_realized_return_pct": _mean(
                    [float(row["realized_return_pct"]) for row in items]
                ),
                "average_excess_vs_hs300_pct": _mean(
                    [
                        float(row["realized_return_pct"])
                        - float(row["benchmark_return_pct"])
                        for row in items
                        if row["benchmark_return_pct"] is not None
                    ]
                ),
                "estimated_gross_portfolio_return_pct": _compound_return(
                    [item["gross_return_pct"] for item in daily_values]
                ),
                "estimated_net_portfolio_return_pct": _compound_return(
                    [item["net_return_pct"] for item in daily_values]
                ),
                "estimated_transaction_cost_pct_sum": sum(
                    item["transaction_cost_pct"] for item in daily_values
                ),
                "maximum_drawdown_pct": _maximum_drawdown(
                    [item["net_return_pct"] for item in daily_values]
                ),
                "average_daily_exposure": _mean(
                    [item["exposure"] for item in daily_values]
                ),
                "actual_trigger_count": actual_trigger_count,
                "trigger_rate": actual_trigger_count / len(items) if items else None,
                "abstain_rate": 1.0 - actual_trigger_count / len(items) if items else None,
                "ai_veto_avoided_loss_pct_sum": avoided_losses,
                "ai_missed_upside_opportunity_pct_sum": missed_upside,
                "nav": nav,
                "nav_boundary": (
                    "fractional research NAV only; it is not a shadow account or broker ledger"
                ),
                "initial_nav": initial_nav,
            }
        for variant, result in variants.items():
            comparisons = {}
            for comparator in ("quant_only", "simple_baseline"):
                if comparator == variant:
                    continue
                comparisons[comparator] = _wide_trade_day_comparison(
                    daily_returns_by_variant[variant],
                    daily_returns_by_variant[comparator],
                    minimum_trade_days=minimum_trade_days,
                )
            benchmark_days = {
                day: {"net_return_pct": values["same_exposure_benchmark_return_pct"]}
                for day, values in daily_returns_by_variant[variant].items()
                if values["benchmark_complete"] > 0
            }
            result["paired_trade_day_comparisons"] = comparisons
            result["same_exposure_hs300_comparison"] = _wide_trade_day_comparison(
                daily_returns_by_variant[variant],
                benchmark_days,
                minimum_trade_days=minimum_trade_days,
            )
        return {
            "experiment": _json_row(experiment, "payload"),
            "horizon_days": horizon_days,
            "coverage": coverage,
            "batches": {
                "total": len(batches),
                "completed": sum(str(row["status"]) == "completed" for row in batches),
                "failed": sum(str(row["status"]) == "failed" for row in batches),
                "independent_trade_days": len({str(row["trade_date"]) for row in batches}),
                "llm_calls": sum(int(row["llm_calls"] or 0) for row in batches),
                "llm_cost_usd": sum(float(row["llm_cost_usd"] or 0.0) for row in batches),
            },
            "variants": variants,
            "correlation_warning": (
                "Stocks from the same signal day are correlated; both stock count and "
                "independent trade-day count are reported."
            ),
            "shorting_boundary": (
                "Down predictions are scored for direction and avoided-buy value only; "
                "research NAV never assumes naked short execution in A-shares."
            ),
            "claim_boundary": (
                "This is an isolated wide-forward research boundary. It reports fractional "
                "cost-aware research NAV and trade-day comparisons, but never enters Primary "
                "or shadow-account scorecards and cannot establish formal profitability."
            ),
        }

    def _latest_nav(
        self, experiment_id: str, variant: str, horizon_days: int
    ) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT n.* FROM research_portfolio_nav n
                   JOIN research_portfolios p ON p.portfolio_id=n.portfolio_id
                   WHERE p.experiment_id=? AND p.variant=? AND n.horizon_days=?
                   ORDER BY n.nav_date DESC LIMIT 1""",
                (experiment_id, variant, horizon_days),
            ).fetchone()
        return _json_row(row, "payload") if row else None

    def record_user_adoption(
        self,
        *,
        order: dict[str, Any],
        check: dict[str, Any],
    ) -> dict[str, Any]:
        ai_action = check.get("llm_suggested_action") or check.get("suggested_action")
        user_side = str(order["side"])
        supportive = (
            user_side == "buy" and ai_action in {"buy", "add"}
        ) or (user_side == "sell" and ai_action in {"sell", "reduce", "avoid"})
        status = "adopted" if supportive else "overrode" if ai_action else "unavailable"
        quote = check.get("quote") or {}
        identity = {
            "order_id": order["order_id"],
            "check_id": order["check_id"],
            "research_run_id": order.get("research_run_id"),
        }
        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM user_adoption_records WHERE order_id=?",
                (order["order_id"],),
            ).fetchone()
            if existing is not None:
                return _json_row(existing, "evidence_payload")
            adoption_id = str(uuid.uuid4())
            db.execute(
                """INSERT OR IGNORE INTO user_adoption_records(
                       adoption_id,order_id,check_id,account_id,symbol,research_run_id,
                       research_link_status,ai_action,ai_suggested_quantity,user_side,
                       user_quantity,adoption_status,pretrade_price,pretrade_observed_at,
                       quote_fingerprint,context_id,context_fingerprint,evidence_payload,
                       created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    adoption_id,
                    order["order_id"],
                    order["check_id"],
                    order["account_id"],
                    order["symbol"],
                    order.get("research_run_id"),
                    check.get("research_link_status") or "unlinked",
                    ai_action,
                    int(check.get("suggested_quantity") or 0),
                    user_side,
                    int(order["requested_quantity"]),
                    status,
                    float(check["reference_price"]),
                    str(check["reference_time"]),
                    str(quote.get("quote_fingerprint") or "unavailable"),
                    order.get("context_id"),
                    order.get("context_fingerprint"),
                    json.dumps(
                        sanitize_for_export(
                            {
                                "identity": identity,
                                "pretrade_check": check,
                                "formal_forward_scorecard_eligible": False,
                                "evaluation_scope": "user_adoption_outcome_only",
                            }
                        ),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    _now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM user_adoption_records WHERE order_id=?",
                (order["order_id"],),
            ).fetchone()
        return _json_row(row, "evidence_payload")

    def user_adoption_outcomes(
        self, *, account_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if account_id:
            clauses.append("a.account_id=?")
            params.append(account_id)
        params.append(limit)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT a.*,o.status order_status,o.filled_quantity,
                           COALESCE(SUM(f.quantity),0) fill_quantity,
                           COALESCE(SUM(f.gross_value),0) fill_gross_value,
                           COALESCE(SUM(f.transaction_fees),0) transaction_fees,
                           CASE WHEN COALESCE(SUM(f.quantity),0)>0
                                THEN SUM(f.fill_price*f.quantity)/SUM(f.quantity)
                                ELSE NULL END average_fill_price,
                           MIN(f.trade_date) first_fill_date,MAX(f.trade_date) last_fill_date,
                           acc.realized_pnl account_realized_pnl,
                           acc.cumulative_fees account_cumulative_fees,
                           pos.quantity current_position_quantity,
                           pos.average_cost current_position_average_cost,
                           pos.latest_price current_position_latest_price,
                           pos.realized_pnl symbol_realized_pnl
                    FROM user_adoption_records a
                    JOIN user_paper_orders o ON o.order_id=a.order_id
                    JOIN user_paper_accounts acc ON acc.account_id=a.account_id
                    LEFT JOIN user_paper_fills f ON f.order_id=a.order_id
                    LEFT JOIN user_paper_positions pos
                      ON pos.account_id=a.account_id AND pos.symbol=a.symbol
                    {where}
                    GROUP BY a.adoption_id
                    ORDER BY a.created_at DESC LIMIT ?""",
                params,
            ).fetchall()
        output = []
        for row in rows:
            item = _json_row(row, "evidence_payload")
            item["outcome_status"] = (
                "filled_pending_horizon_evaluation"
                if int(item["fill_quantity"] or 0) > 0
                else "not_filled"
            )
            fill_price = item.get("average_fill_price")
            latest_price = item.get("current_position_latest_price")
            if (
                item["user_side"] == "buy"
                and fill_price is not None
                and float(fill_price) > 0
                and latest_price is not None
                and float(latest_price) > 0
            ):
                item["marked_return_pct"] = (
                    float(latest_price) / float(fill_price) - 1.0
                ) * 100.0
                item["marked_pnl_after_order_fees"] = (
                    float(latest_price) - float(fill_price)
                ) * int(item["fill_quantity"]) - float(item["transaction_fees"])
                item["return_scope"] = "filled_buy_marked_to_latest_server_account_price"
            else:
                item["marked_return_pct"] = None
                item["marked_pnl_after_order_fees"] = None
                item["return_scope"] = (
                    "sell_outcome_uses_symbol_and_account_realized_pnl"
                    if item["user_side"] == "sell"
                    else "return_unavailable_until_fill_and_mark"
                )
            item["formal_forward_scorecard_eligible"] = False
            output.append(item)
        return output


def _json_row(row: sqlite3.Row, *fields: str) -> dict[str, Any]:
    item = dict(row)
    for field in fields:
        if item.get(field) is not None and isinstance(item[field], str):
            item[field] = json.loads(item[field])
    return item


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            sanitize_for_export(payload), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _as_utc(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def _wide_sample_coverage(
    rows: list[sqlite3.Row], *, observed_at: datetime
) -> dict[str, Any]:
    samples: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{row['batch_id']}:{row['sample_key']}"
        state = samples.setdefault(
            key,
            {"variants": set(), "settled_variants": set(), "due": False},
        )
        state["variants"].add(str(row["variant"]))
        if row["due_at"] is None:
            state["unresolved_prediction_links"] = state.get(
                "unresolved_prediction_links", 0
            ) + 1
        else:
            state["due"] = state["due"] or _as_utc(row["due_at"]) <= observed_at
        if row["outcome_prediction_id"] is not None:
            state["settled_variants"].add(str(row["variant"]))
    required_variants = len(ABLATION_VARIANTS)
    due = [item for item in samples.values() if item["due"]]
    settled = [
        item
        for item in due
        if len(item["variants"]) == required_variants
        and len(item["settled_variants"]) == required_variants
    ]
    return {
        "registered_samples": len(samples),
        "registered_prediction_count": len(rows),
        "due_samples": len(due),
        "settled_samples": len(settled),
        "pending_due_samples": len(due) - len(settled),
        "future_samples": sum(1 for item in samples.values() if not item["due"]),
        "settlement_coverage": len(settled) / len(due) if due else None,
        "unresolved_prediction_links": sum(
            int(item.get("unresolved_prediction_links", 0)) for item in samples.values()
        ),
        "independence_unit": "signal_trade_day",
    }


def _aggregate_wide_trade_days(
    rows: list[sqlite3.Row],
) -> dict[str, dict[str, float]]:
    output: dict[str, dict[str, float]] = {}
    for row in rows:
        trade_day = str(row["trade_date"])
        state = output.setdefault(
            trade_day,
            {
                "gross_return_pct": 0.0,
                "net_return_pct": 0.0,
                "transaction_cost_pct": 0.0,
                "same_exposure_benchmark_return_pct": 0.0,
                "benchmark_complete": 1.0,
                "exposure": 0.0,
                "trigger_count": 0.0,
            },
        )
        triggered = bool(row["portfolio_triggered"])
        weight = float(row["weight"])
        state["gross_return_pct"] += float(row["gross_return_pct"])
        state["net_return_pct"] += float(row["net_return_pct"])
        state["transaction_cost_pct"] += float(row["transaction_cost_pct"])
        if triggered:
            state["exposure"] += weight
            state["trigger_count"] += 1.0
            if row["benchmark_return_pct"] is None:
                state["benchmark_complete"] = 0.0
            else:
                state["same_exposure_benchmark_return_pct"] += (
                    float(row["benchmark_return_pct"]) * weight
                )
    return output


def _wide_trade_day_comparison(
    strategy_days: dict[str, dict[str, float]],
    comparator_days: dict[str, dict[str, float]],
    *,
    minimum_trade_days: int,
) -> dict[str, Any]:
    shared_days = sorted(set(strategy_days) & set(comparator_days))
    strategy = [strategy_days[day]["net_return_pct"] / 100.0 for day in shared_days]
    comparator = [comparator_days[day]["net_return_pct"] / 100.0 for day in shared_days]
    excess_pct = [
        (left - right) * 100.0 for left, right in zip(strategy, comparator, strict=True)
    ]
    strategy_total = _compound_return([value * 100.0 for value in strategy])
    comparator_total = _compound_return([value * 100.0 for value in comparator])
    compound_excess = (
        ((1.0 + strategy_total / 100.0) / (1.0 + comparator_total / 100.0) - 1.0)
        * 100.0
        if strategy_total is not None and comparator_total is not None
        else None
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


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["EVIDENCE_BOUNDARY", "WideResearchRepository"]
