from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from quantlab.llm.providers import LLMProvider
from quantlab.persistence.evidence import EvidenceRepository


T = TypeVar("T", bound=BaseModel)


class LLMBudgetExceeded(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


@dataclass(frozen=True)
class LLMTaskBudget:
    maximum_calls: int = 12
    maximum_total_tokens: int = 80_000
    maximum_cost_usd: float = 5.0
    estimated_input_cost_per_million: float = 1.0
    estimated_output_cost_per_million: float = 4.0


@dataclass(frozen=True)
class LLMPhasePlan:
    name: str
    schema_name: str
    roles: tuple[str, ...]
    reserved_total_tokens_per_call: int
    reserved_output_tokens_per_call: int = 4_000

    @property
    def expected_calls(self) -> int:
        return len(self.roles)

    @property
    def reserved_input_tokens_per_call(self) -> int:
        return max(
            1,
            self.reserved_total_tokens_per_call - self.reserved_output_tokens_per_call,
        )


@dataclass(frozen=True)
class LLMWorkflowPlan:
    workflow: str
    phases: tuple[LLMPhasePlan, ...]
    degradation_policy: str = "fail_before_paid_call"

    @property
    def expected_calls(self) -> int:
        return sum(phase.expected_calls for phase in self.phases)

    @property
    def expected_total_tokens(self) -> int:
        return sum(
            phase.expected_calls * phase.reserved_total_tokens_per_call
            for phase in self.phases
        )

    def reservations(
        self,
        budget: LLMTaskBudget,
        completed_without_charge: dict[str, int] | None = None,
    ) -> dict[str, dict[str, Any]]:
        completed = completed_without_charge or {}
        output: dict[str, dict[str, Any]] = {}
        for phase in self.phases:
            expected = max(0, phase.expected_calls - int(completed.get(phase.schema_name, 0)))
            output[phase.schema_name] = {
                "phase": phase.name,
                "expected_calls": expected,
                "input_tokens_per_call": phase.reserved_input_tokens_per_call,
                "output_tokens_per_call": phase.reserved_output_tokens_per_call,
                "cost_per_call": _estimated_cost(
                    budget,
                    phase.reserved_input_tokens_per_call,
                    phase.reserved_output_tokens_per_call,
                ),
            }
        return output


_DEFAULT_PHASE_TOKEN_RESERVATIONS = {
    "analysts": 22_000,
    "council": 24_000,
    "debate": 18_000,
    "forecasts": 30_000,
    "reviewer": 30_000,
    "context_roles": 18_000,
    "context_synthesis": 32_000,
}

_DEFAULT_WORKFLOW_BUDGETS = {
    "stock_full_research": {
        "maximum_calls": 32,
        "maximum_total_tokens": 650_000,
        "maximum_cost_usd": 12.0,
    },
    "etf_full_research": {
        "maximum_calls": 24,
        "maximum_total_tokens": 450_000,
        "maximum_cost_usd": 10.0,
    },
    "convertible_bond_full_research": {
        "maximum_calls": 24,
        "maximum_total_tokens": 450_000,
        "maximum_cost_usd": 10.0,
    },
    "context_committee": {
        "maximum_calls": 10,
        "maximum_total_tokens": 220_000,
        "maximum_cost_usd": 6.0,
    },
}


class GovernedLLMProvider(LLMProvider):
    """Adds cache, idempotency, task budgets and prompt-free audit persistence."""

    _locks: dict[tuple[str, int], asyncio.Lock] = {}

    def __init__(
        self,
        base: LLMProvider,
        repository: EvidenceRepository,
        *,
        context_id: str | None,
        context_fingerprint: str | None,
        task_id: str | None = None,
        budget: LLMTaskBudget | None = None,
        workflow_plan: LLMWorkflowPlan | None = None,
        prompt_version: str = "unversioned",
        schema_version: str = "unversioned",
        governance_version: str = "default-role-policy-v1",
    ):
        self.base = base
        self.repository = repository
        self.context_id = context_id
        self.context_fingerprint = context_fingerprint
        self.task_id = task_id or str(uuid.uuid4())
        self.budget = budget or LLMTaskBudget()
        self.workflow_plan = workflow_plan
        self.prompt_version = prompt_version
        self.schema_version = schema_version
        self.governance_version = governance_version
        self.provider_name = getattr(base, "provider_name", "unknown")
        self.model = getattr(base, "model", "unknown")
        self.claim_owner_id = str(uuid.uuid4())
        self.audit_log: list[dict[str, Any]] = []
        self._cached_phase_counts: dict[str, int] = {}

    def prepare_workflow(self) -> dict[str, Any]:
        if self.workflow_plan is None:
            return self.budget_snapshot()
        reservations = self.workflow_plan.reservations(
            self.budget,
            self._cached_phase_counts,
        )
        try:
            self.repository.assert_llm_budget_capacity(
                task_id=self.task_id,
                maximum_calls=self.budget.maximum_calls,
                maximum_total_tokens=self.budget.maximum_total_tokens,
                maximum_cost_usd=self.budget.maximum_cost_usd,
                phase_reservations=reservations,
            )
        except RuntimeError as exc:
            details = self.budget_snapshot()
            details["degradation_reason"] = str(exc)
            raise LLMBudgetExceeded(
                f"LLM workflow budget is insufficient before paid calls: {exc}",
                details=details,
            ) from exc
        return self.budget_snapshot()

    async def structured(self, system: str, prompt: str, schema: type[T]) -> T:
        role = _role(system, schema)
        schema_name = schema.__name__
        phase = _phase_for_schema(schema_name)
        cache_key = _cache_key(
            context_fingerprint=self.context_fingerprint,
            role=role,
            provider=self.provider_name,
            model=self.model,
            role_model=_role_model_identity(self.base, role),
            prompt_version=self.prompt_version,
            schema_version=self.schema_version,
            governance_version=self.governance_version,
            system=system,
            prompt=prompt,
            schema=schema.__name__,
        )
        cached = self.repository.cached_llm_entry(cache_key)
        if cached is not None:
            result = _restore_cached_result(schema, cached)
            self.audit_log.append(
                {
                    "role": role,
                    "phase": phase,
                    "schema": schema_name,
                    "status": "cached",
                    "cache_key": cache_key,
                }
            )
            self._record_cached_phase(schema_name)
            return result
        loop_key = (cache_key, id(asyncio.get_running_loop()))
        lock = self._locks.setdefault(loop_key, asyncio.Lock())
        async with lock:
            cached = self.repository.cached_llm_entry(cache_key)
            if cached is not None:
                result = _restore_cached_result(schema, cached)
                self.audit_log.append(
                    {
                        "role": role,
                        "phase": phase,
                        "schema": schema_name,
                        "status": "cached",
                        "cache_key": cache_key,
                    }
                )
                self._record_cached_phase(schema_name)
                return result
            existing = self.repository.llm_call(self.task_id, cache_key)
            if existing is not None and existing["status"] == "error":
                raise RuntimeError("idempotent LLM call previously failed; explicit retry task required")
            if existing is not None and existing["status"] == "reserved":
                raise RuntimeError("idempotent LLM call has an unfinished reservation")
            estimated_input_tokens = _estimate_input_tokens(system, prompt, schema)
            shared_cached = await self._claim_cache_or_wait(cache_key, schema, role)
            if shared_cached is not None:
                self._record_cached_phase(schema_name)
                return shared_cached
            reserved_total_tokens, reserved_output_tokens = self._reservation_for_call(
                schema_name,
                estimated_input_tokens,
            )
            reserved_input_tokens = max(estimated_input_tokens, reserved_total_tokens - reserved_output_tokens)
            reserved_total_tokens = reserved_input_tokens + reserved_output_tokens
            reserved_cost = self._estimated_cost(reserved_input_tokens, reserved_output_tokens)
            try:
                reservation = self.repository.reserve_llm_call(
                    task_id=self.task_id,
                    idempotency_key=cache_key,
                    cache_key=cache_key,
                    context_id=self.context_id,
                    context_fingerprint=self.context_fingerprint,
                    role=role,
                    schema_name=schema_name,
                    provider=self.provider_name,
                    model=self.model,
                    reserved_input_tokens=reserved_input_tokens,
                    reserved_output_tokens=reserved_output_tokens,
                    reserved_cost_usd=reserved_cost,
                    maximum_calls=self.budget.maximum_calls,
                    maximum_total_tokens=self.budget.maximum_total_tokens,
                    maximum_cost_usd=self.budget.maximum_cost_usd,
                    phase_reservations=(
                        self.workflow_plan.reservations(
                            self.budget,
                            self._cached_phase_counts,
                        )
                        if self.workflow_plan
                        else None
                    ),
                )
            except RuntimeError as exc:
                self.repository.finish_llm_cache_claim(
                    cache_key=cache_key,
                    owner_id=self.claim_owner_id,
                    success=False,
                    error=exc,
                )
                details = self.budget_snapshot()
                details.update(
                    {
                        "blocked_phase": phase,
                        "blocked_role": role,
                        "degradation_reason": str(exc),
                    }
                )
                raise LLMBudgetExceeded(str(exc), details=details) from exc
            if reservation["status"] != "reserved":
                raise RuntimeError("LLM reservation was not acquired")
            started = time.perf_counter()
            idempotency_key = cache_key
            try:
                result = await self.base.structured(system, prompt, schema)
            except Exception as exc:
                latency = (time.perf_counter() - started) * 1000
                self.repository.finalize_llm_call(
                    task_id=self.task_id,
                    idempotency_key=idempotency_key,
                    provider=self.provider_name,
                    model=self.model,
                    status="error",
                    input_tokens=estimated_input_tokens,
                    output_tokens=0,
                    estimated_cost_usd=self._estimated_cost(estimated_input_tokens, 0),
                    latency_ms=latency,
                    result=None,
                    error=exc,
                )
                self.repository.finish_llm_cache_claim(
                    cache_key=cache_key,
                    owner_id=self.claim_owner_id,
                    success=False,
                    error=exc,
                )
                self.audit_log.append(
                    {
                        "role": role,
                        "phase": phase,
                        "schema": schema_name,
                        "status": "error",
                        "error": type(exc).__name__,
                        "reserved_tokens": reserved_total_tokens,
                    }
                )
                raise
            latency = (time.perf_counter() - started) * 1000
            usage = getattr(result, "_llm_usage", {}) or {}
            input_tokens = int(usage.get("input_tokens") or estimated_input_tokens)
            output_tokens = int(
                usage.get("output_tokens")
                or max(1, len(result.model_dump_json()) // 4)
            )
            cost = self._estimated_cost(input_tokens, output_tokens)
            self.repository.finalize_llm_call(
                task_id=self.task_id,
                idempotency_key=idempotency_key,
                provider=str(getattr(result, "_llm_provider", self.provider_name)),
                model=str(getattr(result, "_llm_model", self.model)),
                status="ok",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=cost,
                latency_ms=latency,
                result=result.model_dump(mode="json"),
            )
            self.repository.finish_llm_cache_claim(
                cache_key=cache_key,
                owner_id=self.claim_owner_id,
                success=True,
            )
            self.audit_log.append(
                {
                    "role": role,
                    "phase": phase,
                    "schema": schema_name,
                    "status": "ok",
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "estimated_cost_usd": cost,
                    "reserved_tokens": reserved_total_tokens,
                    "latency_ms": round(latency, 2),
                }
            )
            object.__setattr__(result, "_governance_cached", False)
            return result

    async def _claim_cache_or_wait(
        self,
        cache_key: str,
        schema: type[T],
        role: str,
    ) -> T | None:
        deadline = time.monotonic() + 930.0
        while True:
            claim = self.repository.claim_llm_cache(
                cache_key=cache_key,
                owner_id=self.claim_owner_id,
                task_id=self.task_id,
                lease_seconds=900,
            )
            if claim["status"] == "acquired":
                return None
            if claim["status"] == "cached":
                result = _restore_cached_result(schema, claim)
                self.audit_log.append(
                    {
                        "role": role,
                        "phase": _phase_for_schema(schema.__name__),
                        "schema": schema.__name__,
                        "status": "shared_cached",
                        "cache_key": cache_key,
                    }
                )
                return result
            if time.monotonic() >= deadline:
                raise RuntimeError("timed out waiting for an identical governed LLM request")
            await asyncio.sleep(0.05)

    async def aclose(self) -> None:
        await self.base.aclose()

    def health_snapshot(self) -> dict:
        return {
            **self.base.health_snapshot(),
            "governance": {
                "task_id": self.task_id,
                "context_id": self.context_id,
                "budget": self.budget.__dict__,
                "usage": self.repository.task_usage(self.task_id),
                "workflow_plan": self.budget_snapshot(),
                "calls": list(self.audit_log),
            },
        }

    def budget_snapshot(self) -> dict[str, Any]:
        usage = self.repository.task_usage(self.task_id)
        counts = self.repository.task_calls_by_schema(self.task_id)
        for schema_name, count in self._cached_phase_counts.items():
            counts[schema_name] = counts.get(schema_name, 0) + count
        plan = self.workflow_plan
        missing_roles: list[str] = []
        phases: list[dict[str, Any]] = []
        if plan:
            for phase in plan.phases:
                completed = min(phase.expected_calls, counts.get(phase.schema_name, 0))
                missing_roles.extend(phase.roles[completed:])
                phases.append(
                    {
                        "phase": phase.name,
                        "schema": phase.schema_name,
                        "roles": list(phase.roles),
                        "expected_calls": phase.expected_calls,
                        "completed_or_cached_calls": completed,
                        "reserved_tokens_per_call": phase.reserved_total_tokens_per_call,
                    }
                )
        return {
            "workflow": plan.workflow if plan else "unplanned_task",
            "estimated_required": {
                "calls": plan.expected_calls if plan else None,
                "total_tokens": plan.expected_total_tokens if plan else None,
                "cost_usd": (
                    round(
                        sum(
                            item["expected_calls"] * item["cost_per_call"]
                            for item in plan.reservations(self.budget).values()
                        ),
                        8,
                    )
                    if plan
                    else None
                ),
            },
            "configured_budget": self.budget.__dict__,
            "actual_usage": {
                "calls": int(usage.get("actual_calls") or 0),
                "input_tokens": int(usage.get("actual_input_tokens") or 0),
                "output_tokens": int(usage.get("actual_output_tokens") or 0),
                "cost_usd": float(usage.get("actual_cost_usd") or 0.0),
            },
            "reserved_usage": {
                "calls": int(usage.get("reserved_calls") or 0),
                "input_tokens": int(usage.get("reserved_input_tokens") or 0),
                "output_tokens": int(usage.get("reserved_output_tokens") or 0),
                "cost_usd": float(usage.get("reserved_cost_usd") or 0.0),
            },
            "missing_roles": missing_roles,
            "degraded_roles": [],
            "degradation_policy": plan.degradation_policy if plan else None,
            "phases": phases,
        }

    def _record_cached_phase(self, schema_name: str) -> None:
        self._cached_phase_counts[schema_name] = self._cached_phase_counts.get(schema_name, 0) + 1

    def _reservation_for_call(self, schema_name: str, estimated_input_tokens: int) -> tuple[int, int]:
        if self.workflow_plan:
            phase = next(
                (item for item in self.workflow_plan.phases if item.schema_name == schema_name),
                None,
            )
            if phase:
                output = phase.reserved_output_tokens_per_call
                return max(phase.reserved_total_tokens_per_call, estimated_input_tokens + output), output
        output = 4_000
        return estimated_input_tokens + output, output

    def _estimated_cost(self, input_tokens: int, output_tokens: int) -> float:
        return round(
            input_tokens / 1_000_000 * self.budget.estimated_input_cost_per_million
            + output_tokens / 1_000_000 * self.budget.estimated_output_cost_per_million,
            8,
        )


def budget_from_settings(settings: dict[str, Any]) -> LLMTaskBudget:
    return budget_for_workflow(settings)


def budget_for_workflow(
    settings: dict[str, Any],
    workflow: str | None = None,
) -> LLMTaskBudget:
    workflow_values = dict(_DEFAULT_WORKFLOW_BUDGETS.get(workflow or "", {}))
    configured = settings.get("workflow_budgets") or {}
    if workflow and isinstance(configured, dict):
        workflow_values.update(configured.get(workflow) or {})
    return LLMTaskBudget(
        maximum_calls=max(
            1,
            int(workflow_values.get("maximum_calls", settings.get("task_maximum_calls", 12))),
        ),
        maximum_total_tokens=max(
            1_000,
            int(
                workflow_values.get(
                    "maximum_total_tokens",
                    settings.get("task_token_budget", 80_000),
                )
            ),
        ),
        maximum_cost_usd=max(
            0.0,
            float(
                workflow_values.get(
                    "maximum_cost_usd",
                    settings.get("task_cost_budget_usd", 5.0),
                )
            ),
        ),
        estimated_input_cost_per_million=max(
            0.0,
            float(settings.get("estimated_input_cost_per_million", 1.0)),
        ),
        estimated_output_cost_per_million=max(
            0.0,
            float(settings.get("estimated_output_cost_per_million", 4.0)),
        ),
    )


def workflow_plan_from_settings(
    settings: dict[str, Any],
    *,
    workflow: str,
    phase_roles: dict[str, list[str] | tuple[str, ...]],
) -> LLMWorkflowPlan:
    configured = dict(_DEFAULT_PHASE_TOKEN_RESERVATIONS)
    configured.update(settings.get("phase_token_reservations") or {})
    schemas = {
        "analysts": "AnalystReport",
        "council": "ExpertOpinion",
        "debate": "DebateReport",
        "forecasts": "Forecast",
        "reviewer": "ReviewReport",
        "context_roles": "CommitteeRoleOpinion",
        "context_synthesis": "CommitteeDecision",
    }
    phases = []
    for name, roles in phase_roles.items():
        normalized = tuple(str(role) for role in roles)
        if not normalized:
            continue
        phases.append(
            LLMPhasePlan(
                name=name,
                schema_name=schemas[name],
                roles=normalized,
                reserved_total_tokens_per_call=max(1_000, int(configured[name])),
            )
        )
    return LLMWorkflowPlan(workflow=workflow, phases=tuple(phases))


def _cache_key(
    *,
    context_fingerprint: str | None,
    role: str,
    provider: str,
    model: str,
    role_model: str,
    prompt_version: str,
    schema_version: str,
    governance_version: str,
    system: str,
    prompt: str,
    schema: str,
) -> str:
    value = json.dumps(
        {
            "context": context_fingerprint,
            "role": role,
            "provider": provider,
            "model": model,
            "role_model": role_model,
            "prompt_version": prompt_version,
            "schema_version": schema_version,
            "governance_version": governance_version,
            "schema": schema,
            "system_sha256": hashlib.sha256(system.encode("utf-8")).hexdigest(),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _role_model_identity(provider: LLMProvider, role: str) -> str:
    endpoints = getattr(provider, "endpoints", None)
    if endpoints:
        identities = []
        for endpoint in endpoints:
            role_models = getattr(endpoint, "role_models", {}) or {}
            identities.append(
                {
                    "provider": getattr(endpoint, "provider_name", "unknown"),
                    "model": getattr(endpoint, "model", "unknown"),
                    "role_model": role_models.get(role),
                }
            )
        return hashlib.sha256(
            json.dumps(identities, sort_keys=True).encode("utf-8")
        ).hexdigest()
    role_models = getattr(provider, "role_models", {}) or {}
    return str(role_models.get(role) or getattr(provider, "model", "unknown"))


def _role(system: str, schema: type[BaseModel]) -> str:
    schema_roles = {
        "Forecast": "forecast",
        "ReviewReport": "review",
        "CommitteeDecision": "synthesis",
        "ChatEvidenceAnswer": "chat",
    }
    if schema.__name__ in schema_roles:
        return schema_roles[schema.__name__]
    lowered = system.lower()
    for candidate in (
        "financial_quality_gate",
        "portfolio_risk",
        "value_veto",
        "momentum",
        "technical",
        "capital_flow",
        "fundamental",
        "event",
        "macro",
        "portfolio_risk",
        "review",
        "synthesis",
        "chat",
    ):
        if candidate in lowered:
            return candidate
    return schema.__name__.lower()


def _phase_for_schema(schema_name: str) -> str:
    return {
        "AnalystReport": "analysts",
        "ExpertOpinion": "council",
        "DebateReport": "debate",
        "Forecast": "forecasts",
        "ReviewReport": "reviewer",
        "CommitteeRoleOpinion": "context_roles",
        "CommitteeDecision": "context_synthesis",
    }.get(schema_name, "other")


def _estimate_input_tokens(system: str, prompt: str, schema: type[BaseModel]) -> int:
    """Conservative UTF-8 estimate including the structured-output schema contract."""
    contract = json.dumps(
        schema.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    payload = f"{system}\n{prompt}\n{contract}"
    byte_estimate = math.ceil(len(payload.encode("utf-8")) / 2)
    character_estimate = len(payload)
    return max(1, byte_estimate, character_estimate)


def _estimated_cost(
    budget: LLMTaskBudget,
    input_tokens: int,
    output_tokens: int,
) -> float:
    return round(
        input_tokens / 1_000_000 * budget.estimated_input_cost_per_million
        + output_tokens / 1_000_000 * budget.estimated_output_cost_per_million,
        8,
    )


def _restore_cached_result(schema: type[T], cached: dict[str, Any]) -> T:
    """Restore runtime identity that is intentionally outside the Pydantic schema."""
    result = schema.model_validate(cached["result"])
    object.__setattr__(result, "_llm_provider", str(cached.get("provider") or "unknown"))
    object.__setattr__(result, "_llm_model", str(cached.get("model") or "unknown"))
    object.__setattr__(result, "_governance_cached", True)
    return result


__all__ = [
    "GovernedLLMProvider",
    "LLMBudgetExceeded",
    "LLMPhasePlan",
    "LLMTaskBudget",
    "LLMWorkflowPlan",
    "budget_for_workflow",
    "budget_from_settings",
    "workflow_plan_from_settings",
]
