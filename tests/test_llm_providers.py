import asyncio
import json
from collections import deque
from types import SimpleNamespace

from quantlab.agents.schemas import AnalystReport
from quantlab.llm.providers import (
    CompatibleProvider,
    LLMProvider,
    OpenAIProvider,
    ResilientLLMProvider,
    build_provider,
    provider_configuration_summary,
)


class FakeCompletions:
    async def create(self, **kwargs):
        payload = {
            "stance": "neutral",
            "confidence": 0.4,
            "summary": "structured",
            "evidence": [],
            "risks": [],
            "missing_data": [],
        }
        message = SimpleNamespace(content=f"```json\n{json.dumps(payload)}\n```")
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class FakeResponses:
    def __init__(self):
        self.kwargs = None

    async def parse(self, **kwargs):
        self.kwargs = kwargs
        report = AnalystReport(
            stance="neutral",
            confidence=0.4,
            summary="role-routed",
        )
        usage = SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            input_tokens_details=SimpleNamespace(cached_tokens=10),
            output_tokens_details=SimpleNamespace(reasoning_tokens=8),
        )
        return SimpleNamespace(output_parsed=report, usage=usage)


def test_compatible_provider_parses_structured_json():
    provider = CompatibleProvider.__new__(CompatibleProvider)
    provider.provider_name = "deepseek"
    provider.model = "deepseek-chat"
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    provider.max_retries = 0
    provider.temperature = 0.1
    provider.call_log = deque(maxlen=10)
    provider._semaphore = asyncio.Semaphore(1)

    report = asyncio.run(provider.structured("system", "prompt", AnalystReport))

    assert report.summary == "structured"
    assert report.confidence == 0.4
    assert report._llm_usage["total_tokens"] == 120
    assert provider.health_snapshot()["recent_total_tokens"] == 120
    assert provider.call_log[0]["routing_key"] == "analystreport"


def test_openai_role_model_and_reasoning_are_preserved_in_router_audit():
    responses = FakeResponses()
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.provider_name = "openai"
    provider.model = "gpt-5.6-terra"
    provider.endpoint_id = "openai-1"
    provider.reasoning_effort = "medium"
    provider.role_models = {"review": "gpt-5.6-sol"}
    provider.role_reasoning_efforts = {"review": "high"}
    provider.call_log = deque(maxlen=10)
    provider._semaphore = asyncio.Semaphore(1)
    provider.client = SimpleNamespace(responses=responses)
    router = ResilientLLMProvider([provider])

    report = asyncio.run(
        router.structured(
            "You are the final audit reviewer. Reject unsupported confidence.",
            "x",
            AnalystReport,
        )
    )

    assert responses.kwargs["model"] == "gpt-5.6-sol"
    assert responses.kwargs["reasoning"] == {"effort": "high"}
    assert report._llm_model == "gpt-5.6-sol"
    assert report._llm_reasoning_effort == "high"
    assert report._llm_usage["reasoning_tokens"] == 8
    assert router.call_log[0]["model"] == "gpt-5.6-sol"
    assert router.call_log[0]["reasoning_effort"] == "high"
    assert provider.call_log[0]["routing_key"] == "review"


class StubProvider(LLMProvider):
    def __init__(self, provider_name, endpoint_id, fail=False):
        self.provider_name = provider_name
        self.endpoint_id = endpoint_id
        self.model = f"{provider_name}-model"
        self.fail = fail
        self.calls = 0

    async def structured(self, system, prompt, schema):
        self.calls += 1
        if self.fail:
            raise RuntimeError("temporary failure")
        return AnalystReport(
            stance="neutral",
            confidence=0.5,
            summary=self.endpoint_id,
        )


def test_resilient_router_fails_over_and_opens_circuit():
    failing = StubProvider("openai", "openai-1", fail=True)
    healthy = StubProvider("deepseek", "deepseek-1")
    router = ResilientLLMProvider([failing, healthy], failure_threshold=1, cooldown_seconds=60)

    first = asyncio.run(router.structured("You are the quant analyst", "x", AnalystReport))
    second = asyncio.run(router.structured("You are the quant analyst", "x", AnalystReport))

    assert first.summary == "deepseek-1"
    assert second.summary == "deepseek-1"
    assert failing.calls == 1
    assert router.health_snapshot()["endpoints"][0]["status"] == "cooldown"


def test_resilient_router_honors_role_provider_preference():
    openai = StubProvider("openai", "openai-1")
    deepseek = StubProvider("deepseek", "deepseek-1")
    router = ResilientLLMProvider(
        [openai, deepseek], role_preferences={"technical": ["deepseek", "openai"]}
    )

    result = asyncio.run(router.structured("You are the technical specialist", "x", AnalystReport))

    assert result.summary == "deepseek-1"


def test_explicit_role_identity_beats_incidental_risk_terms():
    openai = StubProvider("openai", "openai-1")
    deepseek = StubProvider("deepseek", "deepseek-1")
    router = ResilientLLMProvider(
        [openai, deepseek],
        role_preferences={
            "macro": ["deepseek", "openai"],
            "review": ["deepseek", "openai"],
            "risk": ["openai", "deepseek"],
        },
    )

    macro = asyncio.run(
        router.structured(
            "You are the macro specialist. Assess regime risk without inventing releases.",
            "x",
            AnalystReport,
        )
    )
    reviewer = asyncio.run(
        router.structured(
            "You are the final audit reviewer. Reject missing risk discussion.",
            "x",
            AnalystReport,
        )
    )

    assert macro.summary == "deepseek-1"
    assert reviewer.summary == "deepseek-1"


def test_provider_summary_counts_key_pool_without_exposing_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEYS", "key-a,key-b;key-a")
    monkeypatch.setenv("DEEPSEEK_API_KEYS", "key-c")

    summary = provider_configuration_summary({"provider": "router"})

    assert summary["endpoint_count"] == 3
    assert summary["openai_key_count"] == 2
    assert summary["deepseek_key_count"] == 1
    assert summary["secrets_exposed"] is False
    assert "key-a" not in str(summary)


def test_auto_router_can_include_local_openai_compatible_endpoint(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEYS", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEYS", raising=False)
    settings = {
        "provider": "auto",
        "local_model": "local-finance-model",
        "local_base_url": "http://127.0.0.1:8001/v1",
        "allow_mock_fallback": True,
    }

    provider = build_provider(settings)
    summary = provider_configuration_summary(settings)

    assert isinstance(provider, ResilientLLMProvider)
    assert provider.endpoints[0].provider_name == "local"
    assert summary["local_endpoint_count"] == 1
    assert summary["endpoint_count"] == 1
