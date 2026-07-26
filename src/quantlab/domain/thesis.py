from __future__ import annotations

from typing import Literal, TypeAlias


CheckFrequency: TypeAlias = Literal[
    "daily",
    "weekly",
    "monthly",
    "quarterly",
    "event_driven",
    "manual",
]

CHECK_FREQUENCIES: tuple[CheckFrequency, ...] = (
    "daily",
    "weekly",
    "monthly",
    "quarterly",
    "event_driven",
    "manual",
)

_FREQUENCY_ALIASES: dict[str, CheckFrequency] = {
    "daily": "daily",
    "每天": "daily",
    "每日": "daily",
    "weekly": "weekly",
    "每周": "weekly",
    "monthly": "monthly",
    "每月": "monthly",
    "quarterly": "quarterly",
    "每季度": "quarterly",
    "季度": "quarterly",
    "event_driven": "event_driven",
    "event-driven": "event_driven",
    "事件触发": "event_driven",
    "财报披露后": "event_driven",
    "manual": "manual",
    "手动": "manual",
    "手动复核": "manual",
    "人工复核": "manual",
    # This mixed boundary input carries a fixed monthly review rule. Event/red-line
    # triggers remain represented separately by the thesis invalidation rules.
    "事件触发并每月复核": "monthly",
}


def normalize_check_frequency(value: object) -> CheckFrequency:
    text = str(value or "").strip().lower()
    normalized = _FREQUENCY_ALIASES.get(text)
    if normalized is None:
        raise ValueError(
            "check_frequency must be one of: " + ", ".join(CHECK_FREQUENCIES)
        )
    return normalized


__all__ = ["CHECK_FREQUENCIES", "CheckFrequency", "normalize_check_frequency"]
