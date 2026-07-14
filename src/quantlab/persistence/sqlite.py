from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from quantlab.agents.orchestrator import DecisionRun
from quantlab.domain.models import CalibrationReport, ForecastOutcome
from quantlab.learning import LearningRepository, with_forecast_features
from quantlab.learning.attribution import attribute_sample


class DecisionRepository:
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
                CREATE TABLE IF NOT EXISTS decision_runs (
                    run_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    action TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS forecast_outcomes (
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    as_of TEXT,
                    horizon_days INTEGER NOT NULL,
                    realized_return_pct REAL,
                    outcome TEXT,
                    evaluated_at TEXT,
                    PRIMARY KEY (run_id, horizon_days)
                );
                CREATE TABLE IF NOT EXISTS forecast_predictions (
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    up_probability REAL NOT NULL,
                    flat_probability REAL NOT NULL,
                    down_probability REAL NOT NULL,
                    confidence REAL NOT NULL,
                    PRIMARY KEY (run_id, horizon_days)
                );
            """)
            self._ensure_column(db, "forecast_outcomes", "as_of", "TEXT")
            self._ensure_column(db, "forecast_outcomes", "realized_return_pct", "REAL")
            self._ensure_column(db, "forecast_outcomes", "outcome", "TEXT")

    @staticmethod
    def _ensure_column(db: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def save(self, run: DecisionRun, research_context: dict | None = None) -> None:
        payload = {
            "reports": {
                name: report.model_dump(mode="json") for name, report in run.reports.items()
            },
            "forecasts": [forecast.model_dump(mode="json") for forecast in run.forecasts],
            "decision": run.decision.model_dump(mode="json"),
            "decision_trace": run.decision_trace,
            "audit_log": [event.model_dump(mode="json") for event in run.audit_log],
            "llm_audit": run.llm_audit,
        }
        if research_context:
            payload["research_context"] = research_context
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO decision_runs(run_id,symbol,as_of,action,confidence,payload) VALUES(?,?,?,?,?,?)",
                (
                    run.run_id,
                    run.decision.symbol,
                    run.decision.as_of.isoformat(),
                    run.decision.action,
                    run.decision.confidence,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            db.executemany(
                """
                INSERT OR REPLACE INTO forecast_predictions(
                    run_id,symbol,as_of,horizon_days,model,up_probability,
                    flat_probability,down_probability,confidence
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        run.run_id,
                        forecast.symbol,
                        forecast.as_of.isoformat(),
                        forecast.horizon_days,
                        forecast.model,
                        forecast.up_probability,
                        forecast.flat_probability,
                        forecast.down_probability,
                        forecast.confidence,
                    )
                    for forecast in run.forecasts
                ],
            )
        learning = LearningRepository(self.path)
        for forecast in run.forecasts:
            learning.upsert_sample(
                sample_key=f"live:{run.run_id}:{forecast.horizon_days}",
                run_id=run.run_id,
                source="live_decision",
                asset_scope=str(run.learning_context.get("asset_type", "unknown")),
                symbol=forecast.symbol,
                as_of=forecast.as_of,
                horizon_days=forecast.horizon_days,
                features=with_forecast_features(run.learning_features, forecast),
                expected_return_pct=forecast.expected_return_pct,
                context={
                    **run.learning_context,
                    "forecast_components": {
                        "final": [
                            forecast.up_probability,
                            forecast.flat_probability,
                            forecast.down_probability,
                        ],
                        "raw_llm": [
                            forecast.raw_llm_up_probability,
                            forecast.raw_llm_flat_probability,
                            forecast.raw_llm_down_probability,
                        ],
                        "statistical": [
                            forecast.statistical_up_probability,
                            forecast.statistical_flat_probability,
                            forecast.statistical_down_probability,
                        ],
                        "statistical_model_id": forecast.statistical_model_id,
                        "statistical_model_version": forecast.statistical_model_version,
                        "statistical_weight": forecast.statistical_weight,
                    },
                },
            )

    def record_forecast_outcome(
        self,
        run_id: str,
        horizon_days: int,
        realized_return_pct: float,
        evaluated_at: str,
        flat_threshold_pct: float = 1.0,
    ) -> ForecastOutcome:
        with self.connect() as db:
            prediction = db.execute(
                """
                SELECT symbol,as_of,horizon_days FROM forecast_predictions
                WHERE run_id=? AND horizon_days=?
                """,
                (run_id, horizon_days),
            ).fetchone()
            if prediction is None:
                raise ValueError("forecast prediction not found")
            if realized_return_pct > flat_threshold_pct:
                outcome = "up"
            elif realized_return_pct < -flat_threshold_pct:
                outcome = "down"
            else:
                outcome = "flat"
            result = ForecastOutcome(
                run_id=run_id,
                symbol=prediction["symbol"],
                as_of=prediction["as_of"],
                horizon_days=prediction["horizon_days"],
                realized_return_pct=realized_return_pct,
                outcome=outcome,
                evaluated_at=evaluated_at,
            )
            db.execute(
                """
                INSERT OR REPLACE INTO forecast_outcomes(
                    run_id,symbol,as_of,horizon_days,realized_return_pct,outcome,evaluated_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    result.run_id,
                    result.symbol,
                    result.as_of.isoformat(),
                    result.horizon_days,
                    result.realized_return_pct,
                    result.outcome,
                    result.evaluated_at.isoformat(),
                ),
            )
        learning = LearningRepository(self.path)
        sample_key = learning.complete_live_sample(
            run_id,
            horizon_days,
            result.outcome,
            result.realized_return_pct,
            result.evaluated_at,
        )
        if sample_key:
            sample = learning.get_sample(sample_key)
            if sample:
                events = learning.events_between(
                    sample["symbol"], sample["as_of"], sample["evaluated_at"]
                )
                learning.save_attribution(sample_key, attribute_sample(sample, events))
        return result

    def calibration_report(
        self,
        model: str | None = None,
        horizon_days: int | None = None,
        minimum_samples: int = 30,
    ) -> CalibrationReport:
        clauses = ["o.outcome IS NOT NULL"]
        params: list[object] = []
        if model:
            clauses.append("p.model=?")
            params.append(model)
        if horizon_days:
            clauses.append("p.horizon_days=?")
            params.append(horizon_days)
        query = f"""
            SELECT p.up_probability,p.flat_probability,p.down_probability,
                   p.confidence,o.outcome
            FROM forecast_predictions p
            JOIN forecast_outcomes o
              ON p.run_id=o.run_id AND p.horizon_days=o.horizon_days
            WHERE {" AND ".join(clauses)}
        """
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        if not rows:
            return CalibrationReport(
                model=model,
                horizon_days=horizon_days,
                samples=0,
                minimum_samples=minimum_samples,
            )
        labels = ("up", "flat", "down")
        brier = 0.0
        correct = 0
        confidence = 0.0
        for row in rows:
            probabilities = (
                row["up_probability"],
                row["flat_probability"],
                row["down_probability"],
            )
            actual = tuple(1.0 if row["outcome"] == label else 0.0 for label in labels)
            brier += (
                sum(
                    (probability - target) ** 2
                    for probability, target in zip(probabilities, actual)
                )
                / 3
            )
            predicted = labels[max(range(3), key=lambda index: probabilities[index])]
            correct += int(predicted == row["outcome"])
            confidence += row["confidence"]
        samples = len(rows)
        return CalibrationReport(
            model=model,
            horizon_days=horizon_days,
            samples=samples,
            brier_score=brier / samples,
            accuracy=correct / samples,
            mean_confidence=confidence / samples,
            calibrated=samples >= minimum_samples,
            minimum_samples=minimum_samples,
        )

    def pending_forecasts(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT p.run_id,p.symbol,p.as_of,p.horizon_days,p.model
                FROM forecast_predictions p
                LEFT JOIN forecast_outcomes o
                  ON p.run_id=o.run_id AND p.horizon_days=o.horizon_days
                WHERE o.run_id IS NULL
                ORDER BY p.as_of,p.symbol,p.horizon_days
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def recent(self, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT run_id,symbol,as_of,action,confidence,created_at FROM decision_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, run_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT run_id,symbol,as_of,action,confidence,payload,created_at
                FROM decision_runs WHERE run_id=?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def latest_for_symbol(self, symbol: str, as_of: str | None = None) -> dict | None:
        query = """
            SELECT run_id,symbol,as_of,action,confidence,payload,created_at
            FROM decision_runs WHERE symbol=?
        """
        params: list[object] = [symbol]
        if as_of:
            query += " AND as_of<=?"
            params.append(as_of)
        query += " ORDER BY as_of DESC,created_at DESC LIMIT 1"
        with self.connect() as db:
            row = db.execute(query, params).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item
