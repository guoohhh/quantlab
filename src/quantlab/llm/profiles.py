from __future__ import annotations

from copy import deepcopy
from typing import Any

from quantlab.config import Settings


OPENAI_MODEL_OPTIONS = (
    "gpt-5.4",
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
)
REASONING_EFFORT_OPTIONS = ("none", "low", "medium", "high", "xhigh", "max")
OPENAI_ROLE_KEYS = (
    "forecast",
    "review",
    "value_veto",
    "risk",
    "fundamental",
    "buffett",
    "munger",
    "graham",
    "fisher",
    "lynch",
    "duan_yongping",
    "li_lu",
    "damodaran",
    "taleb",
    "bull",
    "bear",
    "technical",
    "momentum",
    "news",
    "quant",
    "macro",
)
ROLE_LABELS = {
    "forecast": "概率预测",
    "review": "最终审核",
    "value_veto": "估值否决",
    "risk": "风险否决",
    "fundamental": "基本面",
    "buffett": "巴菲特",
    "munger": "芒格",
    "graham": "格雷厄姆",
    "fisher": "费雪",
    "lynch": "彼得·林奇",
    "duan_yongping": "段永平",
    "li_lu": "李录",
    "damodaran": "达摩达兰",
    "taleb": "塔勒布",
    "bull": "看多辩手",
    "bear": "看空辩手",
    "technical": "技术分析",
    "momentum": "动量分析",
    "news": "新闻分析",
    "quant": "量化分析",
    "macro": "宏观分析",
}


def _role_map(primary: str, fallback: str) -> dict[str, str]:
    primary_roles = {
        "forecast",
        "review",
        "value_veto",
        "risk",
        "fundamental",
        "buffett",
        "munger",
        "graham",
        "fisher",
        "lynch",
        "duan_yongping",
        "li_lu",
        "damodaran",
        "taleb",
        "bull",
        "bear",
    }
    return {role: primary if role in primary_roles else fallback for role in OPENAI_ROLE_KEYS}


def _effort_map(critical: str, normal: str, fallback: str) -> dict[str, str]:
    critical_roles = {"forecast", "review", "value_veto", "risk", "munger", "taleb"}
    normal_roles = {
        "fundamental",
        "buffett",
        "graham",
        "fisher",
        "lynch",
        "duan_yongping",
        "li_lu",
        "damodaran",
        "bull",
        "bear",
    }
    return {
        role: critical if role in critical_roles else normal if role in normal_roles else fallback
        for role in OPENAI_ROLE_KEYS
    }


LLM_PROFILES: dict[str, dict[str, Any]] = {
    "速度优先": {
        "default_model": "gpt-5.6-luna",
        "default_effort": "low",
        "role_models": _role_map("gpt-5.6-terra", "gpt-5.6-luna"),
        "role_efforts": _effort_map("medium", "low", "low"),
    },
    "平衡": {
        "default_model": "gpt-5.6-terra",
        "default_effort": "medium",
        "role_models": _role_map("gpt-5.6-terra", "gpt-5.6-luna")
        | {
            "forecast": "gpt-5.6-sol",
            "review": "gpt-5.6-sol",
            "value_veto": "gpt-5.6-sol",
            "risk": "gpt-5.6-sol",
        },
        "role_efforts": _effort_map("high", "medium", "low"),
    },
    "质量优先": {
        "default_model": "gpt-5.6-terra",
        "default_effort": "high",
        "role_models": _role_map("gpt-5.6-sol", "gpt-5.6-terra"),
        "role_efforts": _effort_map("xhigh", "high", "medium"),
    },
}


def llm_profile(name: str) -> dict[str, Any]:
    if name not in LLM_PROFILES:
        raise ValueError(f"unknown LLM profile: {name}")
    return deepcopy(LLM_PROFILES[name])


def apply_openai_runtime_config(
    settings: Settings,
    *,
    default_model: str,
    default_effort: str,
    role_models: dict[str, str],
    role_efforts: dict[str, str],
) -> Settings:
    return settings.with_overrides(
        {
            "llm": {
                "openai_model": default_model,
                "openai_reasoning_effort": default_effort,
                "openai_role_models": dict(role_models),
                "openai_role_reasoning_effort": dict(role_efforts),
            }
        }
    )
