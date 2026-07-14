from __future__ import annotations

import re
from typing import Any


SENSITIVE_FIELD_TOKENS = (
    "api_key",
    "authorization",
    "system_prompt",
    "user_prompt",
    "raw_request",
)
SENSITIVE_EXACT_FIELDS = {
    "access_token",
    "client_secret",
    "password",
    "secret",
    "token",
}

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret)\b(\s*[=:]\s*)([^\s,;]+)"),
)
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:[\\/](?:[^\s\r\n]+)")


def redact_sensitive_text(value: str) -> str:
    text = value
    text = _SECRET_PATTERNS[0].sub("[REDACTED_API_KEY]", text)
    text = _SECRET_PATTERNS[1].sub("Bearer [REDACTED]", text)
    text = _SECRET_PATTERNS[2].sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return text


def sanitize_for_export(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: sanitize_for_export(item)
            for key, item in value.items()
            if not _sensitive_field_name(key)
        }
    if isinstance(value, list):
        return [sanitize_for_export(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_for_export(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def _sensitive_field_name(value: Any) -> bool:
    normalized = str(value).strip().lower().replace("-", "_")
    return any(token in normalized for token in SENSITIVE_FIELD_TOKENS) or (
        normalized in SENSITIVE_EXACT_FIELDS
        or normalized.endswith(("_access_token", "_client_secret", "_password"))
    )


def safe_error_detail(exc: BaseException, maximum_length: int = 300) -> str:
    message = redact_sensitive_text(str(exc)).replace("\r", " ").replace("\n", " ").strip()
    message = _WINDOWS_PATH.sub("[REDACTED_PATH]", message)
    if not message:
        return type(exc).__name__
    bounded = message[: max(1, maximum_length)]
    return f"{type(exc).__name__}: {bounded}"
