from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, date, datetime
from pathlib import Path

from quantlab.agents.orchestrator import DecisionRun
from quantlab.domain.models import CalibrationReport, ForecastOutcome
from quantlab.domain.research import ResearchProvenance
from quantlab.learning import LearningRepository, with_forecast_features
from quantlab.learning.attribution import attribute_sample
from quantlab.persistence.decision_learning import (
    apply_decision_learning_schema,
    decision_learning_schema_ready,
    ensure_decision_research_indexes,
)


_RESEARCH_INDEX_LOCK = threading.RLock()
_RESEARCH_INDEX_READY: dict[str, tuple[int, int] | None] = {}


class DecisionRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists() or self.path.stat().st_size == 0
        if fresh:
            apply_decision_learning_schema(self.path)
        elif not decision_learning_schema_ready(self.path):
            with sqlite3.connect(self.path) as db:
                tables = {
                    str(row[0])
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            if tables.intersection(
                {"decision_runs", "forecast_predictions", "learning_samples"}
            ):
                raise RuntimeError(
                    "database requires the unified decision-learning migration "
                    "before repository use"
                )
            apply_decision_learning_schema(self.path)
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
            # Existing databases may already have the provenance columns, so
            # ensure the read-path indexes separately from a full migration.
            self._ensure_research_indexes_once(db)

    def _ensure_research_indexes_once(self, db: sqlite3.Connection) -> None:
        """Avoid schema DDL on every short-lived repository instance."""

        try:
            stat = self.path.stat()
            identity: tuple[int, int] | None = (stat.st_dev, stat.st_ino)
        except OSError:
            identity = None
        cache_key = str(self.path.resolve()).casefold()
        with _RESEARCH_INDEX_LOCK:
            if (
                cache_key in _RESEARCH_INDEX_READY
                and _RESEARCH_INDEX_READY[cache_key] == identity
            ):
                return
            ensure_decision_research_indexes(db)
            _RESEARCH_INDEX_READY[cache_key] = identity

    def save(
        self,
        run: DecisionRun,
        research_context: dict | None = None,
        *,
        provenance: ResearchProvenance | None = None,
    ) -> None:
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
        admission = self._admission(run, research_context, provenance)
        payload["research_identity"] = admission.copy()
        with self.connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO decision_runs(
                   run_id,symbol,as_of,action,confidence,payload,requested_as_of,
                   effective_as_of,origin,evidence_stage,settlement_eligible,
                   training_eligible,registration_id,context_id,context_fingerprint,
                   quarantine_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run.run_id,
                    run.decision.symbol,
                    run.decision.as_of.isoformat(),
                    run.decision.action,
                    run.decision.confidence,
                    json.dumps(payload, ensure_ascii=False),
                    admission["requested_as_of"],
                    admission["effective_as_of"],
                    admission["origin"],
                    admission["evidence_stage"],
                    int(admission["settlement_eligible"]),
                    int(admission["training_eligible"]),
                    admission["registration_id"],
                    admission["context_id"],
                    admission["context_fingerprint"],
                    admission["quarantine_reason"],
                ),
            )
            db.executemany(
                """
                INSERT OR REPLACE INTO forecast_predictions(
                    run_id,symbol,as_of,horizon_days,model,up_probability,
                    flat_probability,down_probability,confidence,origin,evidence_stage,
                    settlement_eligible,training_eligible,registration_id,quarantine_reason
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                        admission["origin"],
                        admission["evidence_stage"],
                        int(admission["settlement_eligible"]),
                        int(admission["training_eligible"]),
                        admission["registration_id"],
                        admission["quarantine_reason"],
                    )
                    for forecast in run.forecasts
                ],
            )
        learning = LearningRepository(self.path)
        for forecast in run.forecasts:
            learning.upsert_sample(
                sample_key=f"live:{run.run_id}:{forecast.horizon_days}",
                run_id=run.run_id,
                source=admission["origin"],
                asset_scope=str(run.learning_context.get("asset_type", "unknown")),
                symbol=forecast.symbol,
                as_of=forecast.as_of,
                horizon_days=forecast.horizon_days,
                features=with_forecast_features(run.learning_features, forecast),
                expected_return_pct=forecast.expected_return_pct,
                context={
                    **run.learning_context,
                    "research_identity": admission,
                    "training_eligible": bool(admission["training_eligible"]),
                    "settlement_eligible": bool(admission["settlement_eligible"]),
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
                origin=admission["origin"],
                evidence_stage=admission["evidence_stage"],
                settlement_eligible=bool(admission["settlement_eligible"]),
                training_eligible=bool(admission["training_eligible"]),
                registration_id=admission["registration_id"],
                quarantine_reason=admission["quarantine_reason"],
            )

    def _admission(
        self,
        run: DecisionRun,
        research_context: dict | None,
        provenance: ResearchProvenance | None,
    ) -> dict:
        effective = run.decision.as_of.isoformat()
        requested = _requested_as_of(research_context, provenance) or effective
        origin = (provenance or ResearchProvenance()).origin
        if origin == "user_interactive_research" and date.fromisoformat(requested) < date.today():
            origin = "historical_research"
        evidence_stage = (provenance or ResearchProvenance()).evidence_stage
        registration_id = (provenance or ResearchProvenance()).registration_id
        context_id, fingerprint = _context_identity(run, research_context)
        eligible = False
        quarantine_reason = None
        if origin in {"registered_forward_research", "system_production_research"}:
            eligible = self._valid_registration(
                registration_id=registration_id,
                symbol=run.decision.symbol,
                requested_as_of=requested,
                effective_as_of=effective,
                generated_at=_generated_at(research_context),
                horizons={int(item.horizon_days) for item in run.forecasts},
            )
            if not eligible:
                raise ValueError("formal research requires a matching prior registration")
            evidence_stage = "registered_forward"
        elif origin == "legacy_unclassified":
            evidence_stage = "legacy_quarantined"
            quarantine_reason = "source_unproven"
        return {
            "symbol": run.decision.symbol,
            "requested_as_of": requested,
            "effective_as_of": effective,
            "run_id": run.run_id,
            "origin": origin,
            "evidence_stage": evidence_stage,
            "settlement_eligible": eligible,
            "training_eligible": eligible,
            "registration_id": registration_id,
            "context_id": context_id,
            "context_fingerprint": fingerprint,
            "quarantine_reason": quarantine_reason,
        }

    def _valid_registration(
        self,
        *,
        registration_id: str | None,
        symbol: str,
        requested_as_of: str,
        effective_as_of: str,
        generated_at: datetime,
        horizons: set[int],
    ) -> bool:
        if not registration_id or requested_as_of != effective_as_of:
            return False
        with self.connect() as db:
            tables = {
                str(row[0])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not {"forward_registration_runs", "forward_registration_samples"}.issubset(tables):
                return False
            rows = db.execute(
                """SELECT s.horizon_days,r.status,r.trade_date,r.started_at,r.completed_at
                   FROM forward_registration_samples s
                   JOIN forward_registration_runs r ON r.registration_id=s.registration_id
                   WHERE s.registration_id=? AND s.symbol=? AND s.status='registered'
                   ORDER BY s.horizon_days""",
                (registration_id, symbol),
            ).fetchall()
        return bool(
            rows
            and {int(row[0]) for row in rows} == horizons
            and all(
                str(row[1]) == "completed"
                and str(row[2]) == requested_as_of
                and _registered_before(str(row[3]), generated_at)
                and _registered_before(str(row[4]), generated_at)
                for row in rows
            )
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
            eligibility = db.execute(
                "SELECT settlement_eligible FROM forecast_predictions "
                "WHERE run_id=? AND horizon_days=?",
                (run_id, horizon_days),
            ).fetchone()
            if eligibility is None or not bool(eligibility[0]):
                raise ValueError("forecast is not eligible for formal settlement")
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
        clauses = ["o.outcome IS NOT NULL", "p.settlement_eligible=1"]
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
                SELECT p.run_id,p.symbol,p.as_of,p.horizon_days,p.model,p.origin,
                       p.evidence_stage,p.registration_id
                FROM forecast_predictions p
                LEFT JOIN forecast_outcomes o
                  ON p.run_id=o.run_id AND p.horizon_days=o.horizon_days
                WHERE o.run_id IS NULL AND p.settlement_eligible=1
                ORDER BY p.as_of,p.symbol,p.horizon_days
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def recent(self, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT run_id,symbol,as_of,requested_as_of,effective_as_of,origin,
                   evidence_stage,settlement_eligible,training_eligible,registration_id,
                   action,confidence,created_at FROM decision_runs
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def research_page(
        self,
        *,
        page: int = 1,
        page_size: int = 8,
        query: str | None = None,
        action: str | None = None,
        evidence_stage: str | None = None,
    ) -> dict[str, object]:
        """Return a small, filterable research index without loading report payloads.

        The report payload can be large because it carries frozen evidence.  The
        research workbench only needs its lightweight index until a user opens
        one report, so this query deliberately never selects ``payload``.
        """

        safe_page = max(1, int(page))
        safe_page_size = max(1, min(int(page_size), 50))
        clauses: list[str] = []
        params: list[object] = []
        normalized_query = str(query or "").strip()
        if normalized_query:
            clauses.append("(symbol LIKE ? OR run_id LIKE ?)")
            token = f"%{normalized_query}%"
            params.extend((token, token))
        normalized_action = str(action or "").strip().lower()
        if normalized_action:
            clauses.append("action=?")
            params.append(normalized_action)
        normalized_stage = str(evidence_stage or "").strip().lower()
        if normalized_stage:
            clauses.append("evidence_stage=?")
            params.append(normalized_stage)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        select = (
            "SELECT run_id,symbol,as_of,requested_as_of,effective_as_of,origin,"
            "evidence_stage,settlement_eligible,training_eligible,registration_id,"
            "action,confidence,created_at FROM decision_runs"
        )
        with self.connect() as db:
            total = int(
                db.execute(f"SELECT COUNT(*) FROM decision_runs{where}", params).fetchone()[0]
            )
            rows = db.execute(
                f"{select}{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, safe_page_size, (safe_page - 1) * safe_page_size],
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "page": safe_page,
            "page_size": safe_page_size,
            "total": total,
        }

    def get(self, run_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT run_id,symbol,as_of,action,confidence,payload,created_at,
                       requested_as_of,effective_as_of,origin,evidence_stage,
                       settlement_eligible,training_eligible,registration_id,
                       context_id,context_fingerprint,quarantine_reason
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
            SELECT run_id,symbol,as_of,action,confidence,payload,created_at,
                   requested_as_of,effective_as_of,origin,evidence_stage,
                   settlement_eligible,training_eligible,registration_id,
                   context_id,context_fingerprint,quarantine_reason
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


def _requested_as_of(
    research_context: dict | None, provenance: ResearchProvenance | None
) -> str | None:
    explicit = provenance.requested_as_of if provenance else None
    if explicit is None and research_context:
        history = research_context.get("price_history")
        if isinstance(history, dict):
            explicit = history.get("requested_cutoff_date")
    if explicit is None or not str(explicit).strip():
        return None
    return date.fromisoformat(str(explicit)[:10]).isoformat()


def _context_identity(
    run: DecisionRun, research_context: dict | None
) -> tuple[str | None, str | None]:
    decision_context = getattr(run.decision, "context_id", None)
    decision_fingerprint = getattr(run.decision, "context_fingerprint", None)
    pack = research_context.get("analysis_context_pack") if research_context else None
    pack = pack if isinstance(pack, dict) else {}
    pack_context = pack.get("context_id")
    pack_fingerprint = pack.get("fingerprint")
    if decision_context and pack_context and str(decision_context) != str(pack_context):
        raise ValueError("research context_id identity mismatch")
    if decision_fingerprint and pack_fingerprint and str(decision_fingerprint) != str(
        pack_fingerprint
    ):
        raise ValueError("research context fingerprint identity mismatch")
    return (
        str(decision_context or pack_context) if decision_context or pack_context else None,
        str(decision_fingerprint or pack_fingerprint)
        if decision_fingerprint or pack_fingerprint
        else None,
    )


def _generated_at(research_context: dict | None) -> datetime:
    value = research_context.get("generated_at") if research_context else None
    if value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _registered_before(value: str, generated_at: datetime) -> bool:
    if not value or value == "None":
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed <= generated_at
