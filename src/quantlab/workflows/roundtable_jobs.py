from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from quantlab.agents.roundtable import (
    normalize_roundtable_participants,
    roundtable_participant_catalog,
)
from quantlab.config import Settings
from quantlab.persistence import DecisionRepository, RoundtableRepository
from quantlab.persistence.jobs import JobRepository
from quantlab.workflows.roundtable import run_expert_roundtable

if TYPE_CHECKING:
    from quantlab.runtime.worker import JobContext


def submit_roundtable_job(
    settings: Settings,
    *,
    source_run_id: str,
    symbol: str,
    as_of: str,
    participants: list[str],
    topic: str,
    rounds: int,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Queue a review without holding the Streamlit request open for LLM calls."""

    database_path = settings.resolve(settings.get("system.database_path"))
    source = DecisionRepository(database_path).get(source_run_id)
    if source is None or str(source.get("symbol")) != symbol:
        raise ValueError("roundtable source research is unavailable or does not match the report")
    selected = normalize_roundtable_participants(participants)
    clean_topic = str(topic or "").strip()
    if not clean_topic:
        raise ValueError("roundtable topic is required")
    if not 1 <= int(rounds) <= 6:
        raise ValueError("rounds must be between 1 and 6")

    labels = {item["key"]: item["label"] for item in roundtable_participant_catalog()}
    sessions = RoundtableRepository(database_path)
    session = sessions.create_pending(
        source_run_id=source_run_id,
        symbol=symbol,
        as_of=as_of,
        topic=clean_topic,
        participants=selected,
        participant_labels={key: labels[key] for key in selected},
        rounds=int(rounds),
    )
    payload = {
        "roundtable_session_id": session["session_id"],
        "source_run_id": source_run_id,
        "symbol": symbol,
        "participants": selected,
        "topic": clean_topic,
        "rounds": int(rounds),
    }
    key = idempotency_key or uuid.uuid4().hex
    job = JobRepository(database_path).submit(
        job_type="roundtable_request",
        payload=payload,
        idempotency_key=f"roundtable:{source_run_id}:{key}",
        concurrency_key=f"roundtable-source:{source_run_id}",
        timeout_seconds=int(settings.get("runtime.roundtable_job_timeout_seconds", 900)),
        max_attempts=int(settings.get("runtime.job_max_attempts", 3)),
        cost_budget_usd=float(settings.get("runtime.roundtable_cost_budget_usd", 2.0)),
    )
    if job.get("payload") != payload:
        raise ValueError("roundtable job idempotency key is bound to a different request")
    session = sessions.attach_job(session["session_id"], job["job_id"])
    return {"session": session, "job": job, "idempotent": True}


def execute_roundtable_job(
    settings: Settings,
    context: JobContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Persist each completed seat so a page revisit can resume the transcript."""

    database_path = settings.resolve(settings.get("system.database_path"))
    sessions = RoundtableRepository(database_path)
    session_id = str(payload["roundtable_session_id"])

    def update(event: dict[str, Any]) -> None:
        event_progress = float(event.get("progress") or 0.0)
        message = str(event.get("message") or "专家正在讨论")
        status = "running" if event.get("kind") != "completed" else "completed"
        sessions.record_progress(
            session_id,
            status=status,
            progress=event_progress,
            message=message,
            turn=event.get("turn") if isinstance(event.get("turn"), dict) else None,
            audit_event=(
                event.get("audit_event") if isinstance(event.get("audit_event"), dict) else None
            ),
        )
        # The event log intentionally contains only phase metadata.  Speaker
        # text lives in the roundtable session, not in every job heartbeat.
        context.progress(
            event_progress,
            message,
            {
                "roundtable_session_id": session_id,
                "event_kind": event.get("kind"),
                "round": event.get("round"),
                "participant": event.get("participant"),
            },
        )

    try:
        context.progress(0.02, "正在核验冻结研究身份")
        result = run_expert_roundtable(
            settings,
            str(payload["source_run_id"]),
            list(payload["participants"]),
            str(payload["topic"]),
            rounds=int(payload["rounds"]),
            save=True,
            session_id=session_id,
            progress_callback=update,
        )
        context.progress(0.98, "正在保存完整讨论记录")
        return {
            "roundtable_session_id": session_id,
            "source_run_id": result["source_run_id"],
            "symbol": result["symbol"],
            "status": result["status"],
            "turn_count": len(result.get("turns") or []),
        }
    except Exception as exc:
        sessions.mark_failed(session_id, "圆桌任务未完成，请查看任务状态后再重试。")
        raise exc


__all__ = ["execute_roundtable_job", "submit_roundtable_job"]
