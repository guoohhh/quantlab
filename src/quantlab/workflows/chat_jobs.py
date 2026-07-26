from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from quantlab.config import Settings
from quantlab.persistence.chat import ChatRepository
from quantlab.persistence.jobs import JobRepository
from quantlab.runtime.worker import JobContext
from quantlab.workflows.chat import _resolve_chat_request_identity, handle_chat_message


BACKGROUND_CHAT_TERMS = (
    "深度报告",
    "压力分析",
    "压力测试",
    "历史证据",
    "历史决策",
    "继续追问",
    "多个agent",
    "多agent",
    "multi-agent",
    "deep report",
    "stress test",
    "historical evidence",
)


def requires_background_chat(
    *,
    content: str,
    allow_research: bool,
    explicit_background: bool | None = None,
) -> bool:
    if explicit_background is not None:
        return explicit_background
    symbols = set(re.findall(r"(?:sh|sz)\d{6}", content.lower()))
    return allow_research or len(symbols) >= 2 or any(
        term.lower() in content.lower() for term in BACKGROUND_CHAT_TERMS
    )


def submit_chat_job(
    settings: Settings,
    *,
    conversation_id: str,
    content: str,
    idempotency_key: str | None,
    account_id: str | None = None,
    symbol: str | None = None,
    quantity: int | None = None,
    research_run_id: str | None = None,
    allow_research: bool = False,
) -> dict[str, Any]:
    database_path = settings.resolve(settings.get("system.database_path"))
    chat = ChatRepository(database_path)
    conversation = chat.conversation(conversation_id)
    if conversation is None or conversation.get("status") != "active":
        raise ValueError("chat conversation not found")
    if account_id and conversation.get("account_id") and account_id != conversation["account_id"]:
        raise PermissionError("conversation account cannot be switched implicitly")
    resolved_symbol, resolved_run_id = _resolve_chat_request_identity(
        settings,
        conversation,
        requested_symbol=symbol,
        requested_run_id=research_run_id,
        content=content,
    )
    key = idempotency_key or str(uuid.uuid4())
    message_key = f"chat-request:{conversation_id}:{key}:user"
    expected_message_payload = {
        "account_id": account_id,
        "symbol": resolved_symbol,
        "quantity": quantity,
        "research_run_id": resolved_run_id,
        "queued": True,
    }
    existing_message = chat.message_by_idempotency(message_key)
    if existing_message is not None and (
        existing_message["content"] != content
        or existing_message["payload"] != expected_message_payload
    ):
        raise ValueError("chat request idempotency key is bound to a different identity")
    user_message = chat.add_message(
        conversation_id=conversation_id,
        role="user",
        content=content,
        payload=expected_message_payload,
        input_tokens=max(1, len(content) // 4),
        status="queued",
        idempotency_key=message_key,
    )
    payload = {
        "conversation_id": conversation_id,
        "user_message_id": user_message["message_id"],
        "content": content,
        "account_id": account_id,
        "symbol": resolved_symbol,
        "quantity": quantity,
        "research_run_id": resolved_run_id,
        "allow_research": allow_research,
        "message_idempotency_key": key,
    }
    job = JobRepository(database_path).submit(
        job_type="chat_request",
        payload=payload,
        idempotency_key=f"chat:{conversation_id}:{key}",
        concurrency_key=f"chat-conversation:{conversation_id}",
        timeout_seconds=int(settings.get("runtime.chat_job_timeout_seconds", 900)),
        max_attempts=int(settings.get("runtime.job_max_attempts", 3)),
        cost_budget_usd=float(settings.get("runtime.per_user_chat_cost_budget_usd", 2.0)),
    )
    if job.get("payload") != payload:
        raise ValueError("chat job idempotency key is bound to a different request identity")
    user_message = chat.attach_message_job(user_message["message_id"], job["job_id"])
    return {
        "message_id": user_message["message_id"],
        "job_id": job["job_id"],
        "job_status": job["status"],
        "message": user_message,
        "idempotent": True,
    }


def execute_chat_job(
    settings: Settings,
    context: JobContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    chat = ChatRepository(settings.resolve(settings.get("system.database_path")))
    assistant_key = f"chat-job:{context.job['job_id']}:assistant"
    existing = chat.message_by_idempotency(assistant_key)
    if existing is not None:
        return {
            "message_id": existing["message_id"],
            "conversation_id": existing["conversation_id"],
            "deduplicated": True,
        }
    context.progress(0.10, "loading frozen Chat and ContextPack inputs")
    result = handle_chat_message(
        settings,
        conversation_id=payload["conversation_id"],
        content=payload["content"],
        account_id=payload.get("account_id"),
        symbol=payload.get("symbol"),
        quantity=payload.get("quantity"),
        research_run_id=payload.get("research_run_id"),
        allow_research=bool(payload.get("allow_research", False)),
        existing_user_message_id=payload["user_message_id"],
        job_id=context.job["job_id"],
    )
    chat.update_message_status(payload["user_message_id"], status="processed")
    context.progress(0.90, "persisting Chat answer and evidence citations")
    cost = _extract_cost(result)
    if cost > 0:
        context.consume_cost(cost)
    return {
        "message_id": result["message"]["message_id"],
        "conversation_id": payload["conversation_id"],
        "citation_count": len(result.get("citations", [])),
        "action_count": len(result.get("actions", [])),
        "estimated_cost_usd": cost,
        "response_fingerprint": hashlib.sha256(
            json.dumps(result["message"], sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
    }


def _extract_cost(result: dict[str, Any]) -> float:
    payload = result.get("message", {}).get("payload", {})
    governance = payload.get("llm_governance", {})
    usage = governance.get("usage", {})
    for key in ("cost_usd", "estimated_cost_usd"):
        if key in usage:
            return max(0.0, float(usage[key]))
    return 0.0


__all__ = ["execute_chat_job", "requires_background_chat", "submit_chat_job"]
