import asyncio

import pytest

from quantlab.llm import LLMProvider, await_with_provider_close
from quantlab.security import redact_sensitive_text, safe_error_detail, sanitize_for_export

FAKE_KEY = "sk-" + "abcdefghijklmnop"


class CloseFailingProvider(LLMProvider):
    provider_name = "close-failing"
    model = "test"

    async def structured(self, system, prompt, schema):
        raise NotImplementedError

    async def aclose(self):
        raise RuntimeError("close failed")


def test_sensitive_text_redaction_covers_keys_bearer_tokens_and_named_secrets():
    text = f"key={FAKE_KEY} Bearer abcdefghijklmnop access_token=token-value secret:secret-value"

    redacted = redact_sensitive_text(text)

    assert FAKE_KEY not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "token-value" not in redacted
    assert "secret-value" not in redacted
    assert redacted.count("[REDACTED") >= 4


def test_export_sanitizer_removes_sensitive_fields_and_redacts_nested_values():
    payload = {
        "api_key": "must-disappear",
        "client_secret": "must-disappear",
        "total_tokens": 123,
        "nested": [
            {"authorization": "Bearer must-disappear"},
            f"model echoed {FAKE_KEY}",
        ],
    }

    sanitized = sanitize_for_export(payload)

    assert "api_key" not in sanitized
    assert "client_secret" not in sanitized
    assert sanitized["total_tokens"] == 123
    assert "authorization" not in sanitized["nested"][0]
    assert FAKE_KEY not in sanitized["nested"][1]


def test_safe_error_detail_redacts_secret_and_local_path():
    error = RuntimeError(f"failed with {FAKE_KEY} at E:\\private\\config.env")

    detail = safe_error_detail(error)

    assert detail.startswith("RuntimeError:")
    assert FAKE_KEY not in detail
    assert "E:\\private" not in detail
    assert "[REDACTED_PATH]" in detail


def test_original_operation_error_is_not_masked_when_close_also_fails():
    async def failing_operation():
        raise ValueError("primary failure")

    with pytest.raises(ValueError, match="primary failure") as captured:
        asyncio.run(await_with_provider_close(CloseFailingProvider(), failing_operation()))

    assert any("close also failed" in note for note in captured.value.__notes__)


def test_close_error_is_visible_when_operation_succeeds():
    async def successful_operation():
        return "ok"

    with pytest.raises(RuntimeError, match="close failed"):
        asyncio.run(await_with_provider_close(CloseFailingProvider(), successful_operation()))
