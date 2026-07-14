from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from collections import Counter
from datetime import date
from pathlib import Path


class LearningRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self):
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS learning_samples (
                    sample_key TEXT PRIMARY KEY,
                    run_id TEXT,
                    source TEXT NOT NULL,
                    asset_scope TEXT NOT NULL DEFAULT 'unknown',
                    symbol TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    features_json TEXT NOT NULL,
                    expected_return_pct REAL,
                    outcome TEXT,
                    realized_return_pct REAL,
                    evaluated_at TEXT,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_learning_complete
                  ON learning_samples(horizon_days,outcome,as_of);
                CREATE TABLE IF NOT EXISTS model_registry (
                    model_id TEXT PRIMARY KEY,
                    horizon_days INTEGER NOT NULL,
                    asset_scope TEXT NOT NULL DEFAULT 'unknown',
                    version INTEGER NOT NULL,
                    trained_until TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    training_samples INTEGER NOT NULL,
                    validation_samples INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    deactivation_reason TEXT,
                    deactivated_at TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS market_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT,
                    symbol TEXT NOT NULL,
                    event_date TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    sentiment REAL NOT NULL DEFAULT 0,
                    impact_score REAL NOT NULL DEFAULT 0.5,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS forecast_attributions (
                    sample_key TEXT PRIMARY KEY,
                    surprise_pct REAL NOT NULL,
                    direction_correct INTEGER NOT NULL,
                    attribution_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS model_monitoring (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_id TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    asset_scope TEXT NOT NULL,
                    samples INTEGER NOT NULL,
                    metrics_json TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS model_challenges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    horizon_days INTEGER NOT NULL,
                    asset_scope TEXT NOT NULL,
                    champion_model_id TEXT,
                    candidate_model_id TEXT,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)
            self._ensure_column(
                db, "learning_samples", "asset_scope", "TEXT NOT NULL DEFAULT 'unknown'"
            )
            self._ensure_column(
                db, "model_registry", "asset_scope", "TEXT NOT NULL DEFAULT 'unknown'"
            )
            self._ensure_column(db, "market_events", "event_key", "TEXT")
            self._ensure_column(db, "model_registry", "deactivation_reason", "TEXT")
            self._ensure_column(db, "model_registry", "deactivated_at", "TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_market_event_key ON market_events(event_key)"
            )
            db.execute(
                "UPDATE learning_samples SET asset_scope='etf' "
                "WHERE asset_scope='unknown' AND source='historical_factor'"
            )
            db.execute("UPDATE model_registry SET active=0 WHERE asset_scope='unknown'")

    @staticmethod
    def _ensure_column(db: sqlite3.Connection, table: str, column: str, declaration: str):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def upsert_sample(
        self,
        *,
        sample_key: str,
        run_id: str | None,
        source: str,
        asset_scope: str,
        symbol: str,
        as_of: date | str,
        horizon_days: int,
        features: dict,
        expected_return_pct: float | None = None,
        outcome: str | None = None,
        realized_return_pct: float | None = None,
        evaluated_at: date | str | None = None,
        context: dict | None = None,
    ) -> None:
        as_of_text = as_of.isoformat() if isinstance(as_of, date) else as_of
        evaluated_text = (
            evaluated_at.isoformat() if isinstance(evaluated_at, date) else evaluated_at
        )
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO learning_samples(
                    sample_key,run_id,source,asset_scope,symbol,as_of,horizon_days,features_json,
                    expected_return_pct,outcome,realized_return_pct,evaluated_at,context_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(sample_key) DO UPDATE SET
                    features_json=excluded.features_json,
                    expected_return_pct=COALESCE(excluded.expected_return_pct,expected_return_pct),
                    outcome=COALESCE(excluded.outcome,outcome),
                    realized_return_pct=COALESCE(excluded.realized_return_pct,realized_return_pct),
                    evaluated_at=COALESCE(excluded.evaluated_at,evaluated_at),
                    context_json=excluded.context_json
                """,
                (
                    sample_key,
                    run_id,
                    source,
                    asset_scope,
                    symbol,
                    as_of_text,
                    horizon_days,
                    json.dumps(features),
                    expected_return_pct,
                    outcome,
                    realized_return_pct,
                    evaluated_text,
                    json.dumps(context or {}, ensure_ascii=False),
                ),
            )

    def complete_live_sample(
        self,
        run_id: str,
        horizon_days: int,
        outcome: str,
        realized_return_pct: float,
        evaluated_at: date | str,
    ) -> str | None:
        evaluated = evaluated_at.isoformat() if isinstance(evaluated_at, date) else evaluated_at
        with self.connect() as db:
            row = db.execute(
                "SELECT sample_key FROM learning_samples WHERE run_id=? AND horizon_days=?",
                (run_id, horizon_days),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                """
                UPDATE learning_samples SET outcome=?,realized_return_pct=?,evaluated_at=?
                WHERE sample_key=?
                """,
                (outcome, realized_return_pct, evaluated, row["sample_key"]),
            )
        return row["sample_key"]

    def completed_samples(self, horizon_days: int, asset_scope: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT sample_key,symbol,as_of,evaluated_at,features_json,context_json,outcome,
                       realized_return_pct,source
                FROM learning_samples
                WHERE horizon_days=? AND asset_scope=? AND outcome IS NOT NULL
                ORDER BY as_of,sample_key
                """,
                (horizon_days, asset_scope),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["features"] = json.loads(item.pop("features_json"))
            item["context"] = json.loads(item.pop("context_json"))
            output.append(item)
        return output

    def completed_live_samples(
        self, horizon_days: int, asset_scope: str, model_id: str | None = None
    ) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM learning_samples
                WHERE horizon_days=? AND asset_scope=? AND source='live_decision'
                  AND outcome IS NOT NULL ORDER BY evaluated_at,sample_key
                """,
                (horizon_days, asset_scope),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["features"] = json.loads(item.pop("features_json"))
            item["context"] = json.loads(item.pop("context_json"))
            if model_id and (
                item["context"].get("forecast_components", {}).get("statistical_model_id")
                != model_id
            ):
                continue
            output.append(item)
        return output

    def sample_counts(self) -> dict:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT horizon_days,asset_scope,source,COUNT(*) total,
                       SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) completed
                FROM learning_samples GROUP BY horizon_days,asset_scope,source
                """
            ).fetchall()
        return {
            f"{row['asset_scope']}:{row['horizon_days']}d:{row['source']}": dict(row)
            for row in rows
        }

    def next_model_version(self, horizon_days: int, asset_scope: str) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM model_registry "
                "WHERE horizon_days=? AND asset_scope=?",
                (horizon_days, asset_scope),
            ).fetchone()
        return int(row[0])

    def save_model(
        self,
        horizon_days: int,
        asset_scope: str,
        trained_until: str,
        parameters_json: str,
        metrics: dict,
        training_samples: int,
        validation_samples: int,
        activate: bool,
    ) -> dict:
        model_id = str(uuid.uuid4())
        version = self.next_model_version(horizon_days, asset_scope)
        with self.connect() as db:
            if activate:
                db.execute(
                    "UPDATE model_registry SET active=0 WHERE horizon_days=? AND asset_scope=?",
                    (horizon_days, asset_scope),
                )
            db.execute(
                """
                INSERT INTO model_registry(
                    model_id,horizon_days,asset_scope,version,trained_until,parameters_json,metrics_json,
                    training_samples,validation_samples,active
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    model_id,
                    horizon_days,
                    asset_scope,
                    version,
                    trained_until,
                    parameters_json,
                    json.dumps(metrics),
                    training_samples,
                    validation_samples,
                    int(activate),
                ),
            )
        return {
            "model_id": model_id,
            "version": version,
            "asset_scope": asset_scope,
            "active": activate,
        }

    def active_model(self, horizon_days: int, asset_scope: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT * FROM model_registry
                WHERE horizon_days=? AND asset_scope=? AND active=1
                ORDER BY version DESC LIMIT 1
                """,
                (horizon_days, asset_scope),
            ).fetchone()
        if row is None:
            return None
        output = dict(row)
        output["metrics"] = json.loads(output.pop("metrics_json"))
        return output

    def deactivate_model(self, model_id: str, reason: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE model_registry SET active=0,deactivation_reason=?,
                    deactivated_at=CURRENT_TIMESTAMP WHERE model_id=?
                """,
                (reason, model_id),
            )

    def record_monitoring(
        self,
        model_id: str,
        horizon_days: int,
        asset_scope: str,
        samples: int,
        metrics: dict,
        action: str,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO model_monitoring(
                    model_id,horizon_days,asset_scope,samples,metrics_json,action
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    model_id,
                    horizon_days,
                    asset_scope,
                    samples,
                    json.dumps(metrics),
                    action,
                ),
            )

    def monitoring_history(self, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT id,model_id,horizon_days,asset_scope,samples,metrics_json,action,created_at
                FROM model_monitoring ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["metrics"] = json.loads(item.pop("metrics_json"))
            output.append(item)
        return output

    def record_model_challenge(
        self,
        *,
        horizon_days: int,
        asset_scope: str,
        champion_model_id: str | None,
        candidate_model_id: str | None,
        decision: str,
        reason: str,
        metrics: dict,
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO model_challenges(
                    horizon_days,asset_scope,champion_model_id,candidate_model_id,
                    decision,reason,metrics_json
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    horizon_days,
                    asset_scope,
                    champion_model_id,
                    candidate_model_id,
                    decision,
                    reason,
                    json.dumps(metrics, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def challenge_history(self, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT id,horizon_days,asset_scope,champion_model_id,candidate_model_id,
                       decision,reason,metrics_json,created_at
                FROM model_challenges ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["metrics"] = json.loads(item.pop("metrics_json"))
            output.append(item)
        return output

    def models(self, horizon_days: int | None = None, asset_scope: str | None = None) -> list[dict]:
        query = "SELECT * FROM model_registry"
        clauses = []
        params: list[object] = []
        if horizon_days is not None:
            clauses.append("horizon_days=?")
            params.append(horizon_days)
        if asset_scope is not None:
            clauses.append("asset_scope=?")
            params.append(asset_scope)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY horizon_days,version DESC"
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["metrics"] = json.loads(item.pop("metrics_json"))
            item.pop("parameters_json")
            item["active"] = bool(item["active"])
            output.append(item)
        return output

    def add_event(
        self,
        symbol: str,
        event_date: date,
        event_type: str,
        title: str,
        source: str,
        sentiment: float,
        impact_score: float,
        payload: dict | None = None,
    ) -> int:
        event_key = hashlib.sha256(
            f"{symbol}|{event_date.isoformat()}|{source}|{title}".encode("utf-8")
        ).hexdigest()
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO market_events(
                    event_key,symbol,event_date,event_type,title,source,sentiment,impact_score,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_key) DO NOTHING
                """,
                (
                    event_key,
                    symbol,
                    event_date.isoformat(),
                    event_type,
                    title,
                    source,
                    sentiment,
                    impact_score,
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
            if cursor.rowcount == 0:
                row = db.execute(
                    "SELECT id FROM market_events WHERE event_key=?", (event_key,)
                ).fetchone()
                return int(row[0])
        return int(cursor.lastrowid)

    def events_between(self, symbol: str, start: str, end: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT id,symbol,event_date,event_type,title,source,sentiment,impact_score,payload_json
                FROM market_events WHERE symbol=? AND event_date>? AND event_date<=?
                ORDER BY event_date
                """,
                (symbol, start, end),
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def event_count(self, symbol: str, start: str, end: str) -> int:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT COUNT(1) FROM market_events
                WHERE symbol=? AND event_date>? AND event_date<=?
                """,
                (symbol, start, end),
            ).fetchone()
        return int(row[0])

    def recent_events(self, symbol: str | None = None, limit: int = 50) -> list[dict]:
        query = """
            SELECT id,symbol,event_date,event_type,title,source,sentiment,impact_score,payload_json
            FROM market_events
        """
        params: list[object] = []
        if symbol:
            query += " WHERE symbol=?"
            params.append(symbol)
        query += " ORDER BY event_date DESC,id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._event_row(row) for row in rows]

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    def get_sample(self, sample_key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM learning_samples WHERE sample_key=?", (sample_key,)
            ).fetchone()
        if row is None:
            return None
        output = dict(row)
        output["features"] = json.loads(output.pop("features_json"))
        output["context"] = json.loads(output.pop("context_json"))
        return output

    def save_attribution(self, sample_key: str, attribution: dict):
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO forecast_attributions(
                    sample_key,surprise_pct,direction_correct,attribution_json
                ) VALUES(?,?,?,?)
                """,
                (
                    sample_key,
                    attribution["surprise_pct"],
                    int(attribution["direction_correct"]),
                    json.dumps(attribution, ensure_ascii=False),
                ),
            )

    def attributions(self, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT sample_key,attribution_json,created_at FROM forecast_attributions
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "sample_key": row["sample_key"],
                "attribution": json.loads(row["attribution_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def attribution_summary(self) -> dict:
        with self.connect() as db:
            rows = db.execute(
                "SELECT surprise_pct,direction_correct,attribution_json FROM forecast_attributions"
            ).fetchall()
        if not rows:
            return {
                "samples": 0,
                "direction_accuracy": None,
                "mean_absolute_surprise_pct": None,
                "root_cause_distribution": {},
            }
        causes: Counter[str] = Counter()
        for row in rows:
            attribution = json.loads(row["attribution_json"])
            for cause in attribution.get("root_cause_candidates", []):
                code = cause.get("code")
                if code:
                    causes[str(code)] += 1
        samples = len(rows)
        return {
            "samples": samples,
            "direction_accuracy": sum(int(row["direction_correct"]) for row in rows) / samples,
            "mean_absolute_surprise_pct": sum(abs(float(row["surprise_pct"])) for row in rows)
            / samples,
            "root_cause_distribution": dict(causes),
            "boundary": "root causes are diagnostic associations, not causal proof",
        }

    def dataset_manifest(self, horizon_days: int, asset_scope: str) -> dict:
        samples = self.completed_samples(horizon_days, asset_scope)
        eligible = [
            item for item in samples if item.get("context", {}).get("training_eligible", True)
        ]
        labels = Counter(item["outcome"] for item in eligible)
        sources = Counter(item["source"] for item in eligible)
        feature_names = sorted(
            {name for item in eligible for name in item.get("features", {}).keys()}
        )
        feature_schema_hash = hashlib.sha256(
            json.dumps(feature_names, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "asset_scope": asset_scope,
            "horizon_days": horizon_days,
            "completed_samples": len(samples),
            "eligible_samples": len(eligible),
            "excluded_samples": len(samples) - len(eligible),
            "as_of_start": min((item["as_of"] for item in eligible), default=None),
            "as_of_end": max((item["as_of"] for item in eligible), default=None),
            "evaluated_until": max(
                (item["evaluated_at"] for item in eligible if item.get("evaluated_at")),
                default=None,
            ),
            "label_distribution": dict(labels),
            "source_distribution": dict(sources),
            "feature_count": len(feature_names),
            "feature_schema_sha256": feature_schema_hash,
            "leakage_contract": "training rows require evaluated_at before validation start",
        }
