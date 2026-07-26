from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from quantlab.config import Settings
from quantlab.domain import AnalysisContextPack
from quantlab.domain.context import EvidenceDomain, reproducible_fingerprint
from quantlab.persistence.round8 import Round8Repository
from quantlab.persistence.round9 import Round9Repository
from quantlab.workflows.context import build_analysis_context_pack
from quantlab.workflows.experiment_recorder import ExperimentRecorder
from quantlab.workflows.investment_thesis import check_investment_thesis
from quantlab.workflows.reflection import record_outcome_reflection


def thesis_due_scan(settings: Settings, *, as_of: date | None = None) -> dict[str, Any]:
    resolved = as_of or _configured_market_date(settings)
    path = settings.resolve(settings.get("system.database_path"))
    lifecycle = Round8Repository(path)
    tasks = Round9Repository(path)
    due = [
        item
        for item in lifecycle.theses(
            statuses=("active", "strengthened", "unchanged", "weakened", "damaged")
        )
        if item.get("next_check_at") and str(item["next_check_at"])[:10] <= resolved.isoformat()
    ]
    created = []
    for thesis in due:
        frozen = thesis.get("current_frozen_revision")
        if not frozen:
            continue
        due_at = str(thesis["next_check_at"])
        condition_fingerprint = reproducible_fingerprint(
            {
                "thesis_id": thesis["thesis_id"],
                "frozen_revision_id": frozen["revision_id"],
                "due_at": due_at,
            }
        )
        created.append(
            tasks.upsert_decision_task(
                {
                    "category": "needs_review",
                    "task_type": "investment_thesis_due_review",
                    "severity": "warning",
                    "title": "投资论文到期复评",
                    "user_summary": f"{thesis['symbol']} 的投资论文已到复评日期，需要查看最新证据。",
                    "diagnostic_detail": "thesis next_check_at reached",
                    "account_id": thesis["portfolio_id"],
                    "symbol": thesis["symbol"],
                    "decision_run_id": thesis.get("run_id"),
                    "source_type": "investment_thesis",
                    "source_id": thesis["thesis_id"],
                    "dedup_key": (
                        f"thesis-due:{thesis['thesis_id']}:{frozen['revision_id']}:{due_at}"
                    ),
                    "condition_fingerprint": condition_fingerprint,
                    "management_source": "system_managed",
                    "payload": {
                        "thesis_id": thesis["thesis_id"],
                        "due_at": due_at,
                        "frozen_revision_id": frozen["revision_id"],
                        "frozen_revision_fingerprint": frozen["fingerprint"],
                    },
                }
            )
        )
    return {"as_of": resolved.isoformat(), "due": len(due), "tasks": created}


def thesis_event_check(settings: Settings, *, as_of: date | None = None) -> dict[str, Any]:
    return _automatic_thesis_check(settings, as_of=as_of, trigger="event")


def thesis_price_invalidation_check(
    settings: Settings, *, as_of: date | None = None
) -> dict[str, Any]:
    return _automatic_thesis_check(settings, as_of=as_of, trigger="price")


def _automatic_thesis_check(
    settings: Settings, *, as_of: date | None, trigger: str
) -> dict[str, Any]:
    resolved = as_of or _configured_market_date(settings)
    path = settings.resolve(settings.get("system.database_path"))
    lifecycle = Round8Repository(path)
    results: list[dict[str, Any]] = []
    for thesis in lifecycle.theses(
        statuses=("active", "strengthened", "unchanged", "weakened", "damaged")
    ):
        try:
            payload = build_analysis_context_pack(
                settings,
                symbol=thesis["symbol"],
                as_of=resolved,
                account_id=None,
                include_events=trigger == "event",
                save=True,
            )
            pack = AnalysisContextPack.model_validate(payload)
            domains = (
                {EvidenceDomain.EVENT}
                if trigger == "event"
                else {EvidenceDomain.MARKET, EvidenceDomain.TECHNICAL, EvidenceDomain.VALUATION}
            )
            block_ids = [block.block_id for block in pack.blocks if block.domain in domains]
            refs = [
                {"assumption_id": assumption["assumption_id"], "block_id": block_id}
                for assumption in thesis["assumptions"]
                for block_id in block_ids
            ]
            results.append(
                check_investment_thesis(
                    settings,
                    thesis_id=thesis["thesis_id"],
                    context_id=pack.context_id,
                    context_fingerprint=pack.fingerprint,
                    trigger_type=f"automatic_{trigger}_check",
                    evidence_refs=refs,
                    user_resolution="system_verified",
                )
            )
        except Exception as exc:
            results.append(
                {
                    "thesis_id": thesis["thesis_id"],
                    "status": "unavailable",
                    "reason": f"{type(exc).__name__}",
                }
            )
    return {
        "as_of": resolved.isoformat(),
        "trigger": trigger,
        "checked": len(results),
        "results": results,
    }


def authoritative_reflection_settlement(
    settings: Settings, *, limit: int = 500
) -> dict[str, Any]:
    """Create reflections only from immutable, server-settled full-system outcomes."""
    path = settings.resolve(settings.get("system.database_path"))
    recorder = ExperimentRecorder(settings)
    with sqlite3.connect(path, timeout=30) as db:
        db.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {
            "forward_ablation_predictions",
            "forward_ablation_outcomes",
            "outcome_reflections",
        }
        missing = sorted(required - tables)
        if missing:
            return {
                "candidates": 0,
                "settled": 0,
                "unavailable": [
                    {
                        "source_id": None,
                        "reason": "required settlement tables missing: " + ",".join(missing),
                    }
                ],
                "reflections": [],
            }
        rows = db.execute(
            """SELECT p.prediction_id,p.symbol,p.horizon_days,p.context_fingerprint,
                      p.sample_key,p.cohort_id,o.observed_at
               FROM forward_ablation_predictions p
               JOIN forward_ablation_outcomes o ON o.prediction_id=p.prediction_id
               WHERE p.variant='full_system'
                 AND p.registration_origin='automatic_primary'
                 AND NOT EXISTS(
                   SELECT 1 FROM outcome_reflections r
                   WHERE r.source_type='forward_sample'
                     AND r.source_id=p.prediction_id
                     AND r.horizon_days=p.horizon_days
                 )
               ORDER BY o.observed_at LIMIT ?""",
            (limit,),
        ).fetchall()
    settled: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for row in rows:
        run = recorder.start(
            experiment_name="authoritative-reflection-lifecycle",
            experiment_type="forward_outcome_reflection",
            run_type="authoritative_settlement",
            evidence_boundary="forward_shadow",
            idempotency_key=f"reflection:{row['prediction_id']}:{row['horizon_days']}",
            context_fingerprint=row["context_fingerprint"],
            parameters={
                "symbol": row["symbol"],
                "cohort_id": row["cohort_id"],
                "sample_key": row["sample_key"],
                "horizon_days": row["horizon_days"],
            },
            workflow_version="authoritative-reflection-v1",
        )
        recorder.link(
            run["run_id"],
            entity_type="forward_prediction",
            entity_id=row["prediction_id"],
            relation="authoritative_source",
        )
        if run["status"] == "running":
            recorder.complete(
                run["run_id"],
                result_summary={"source_id": row["prediction_id"], "settled_at": row["observed_at"]},
            )
        try:
            settled.append(
                record_outcome_reflection(
                    settings,
                    run_id=run["run_id"],
                    source_type="forward_sample",
                    source_id=row["prediction_id"],
                    horizon_days=int(row["horizon_days"]),
                    evidence_refs=[
                        {
                            "context_fingerprint": row["context_fingerprint"],
                            "source": "authoritative_forward_outcome",
                        }
                    ],
                )
            )
        except ValueError as exc:
            unavailable.append(
                {"source_id": row["prediction_id"], "reason": str(exc)}
            )
    return {
        "candidates": len(rows),
        "settled": len(settled),
        "unavailable": unavailable,
        "reflections": settled,
    }


def controlled_memory_refresh(settings: Settings) -> dict[str, Any]:
    path = settings.resolve(settings.get("system.database_path"))
    minimum = int(settings.get("learning.reflection_minimum_mature_samples", 30))
    with sqlite3.connect(path, timeout=30) as db:
        matured = int(
            db.execute(
                """SELECT COUNT(*) FROM outcome_reflections
                   WHERE evidence_boundary IN ('production','forward_shadow')"""
            ).fetchone()[0]
        )
        updated = 0
        if matured >= minimum:
            updated = db.execute(
                """UPDATE controlled_research_memories
                   SET challenge_eligible=1
                   WHERE mature_evidence=1 AND status='candidate' AND challenge_eligible=0"""
            ).rowcount
    return {
        "matured_authoritative_reflections": matured,
        "minimum_for_challenge": minimum,
        "challenge_eligible_updated": updated,
        "automatic_rule_change": False,
        "automatic_weight_change": False,
        "automatic_threshold_change": False,
    }


def decision_run_audit_bundle(settings: Settings, *, run_id: str) -> dict[str, Any]:
    return Round9Repository(
        settings.resolve(settings.get("system.database_path"))
    ).export_decision_run(run_id)


def _configured_market_date(settings: Settings) -> date:
    timezone = ZoneInfo(str(settings.get("system.timezone", "Asia/Shanghai")))
    return datetime.now(UTC).astimezone(timezone).date()


__all__ = [
    "authoritative_reflection_settlement",
    "controlled_memory_refresh",
    "decision_run_audit_bundle",
    "thesis_due_scan",
    "thesis_event_check",
    "thesis_price_invalidation_check",
]
