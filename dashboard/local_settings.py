from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


_ENV_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


LLM_PROVIDER_OPTIONS = ("deepseek", "openai", "openai_compatible")


def llm_provider_key_configured(provider: str) -> bool:
    """Return whether the selected provider has a local secret without exposing it."""

    normalized = _normalize_llm_provider(provider)
    names = {
        "deepseek": ("DEEPSEEK_API_KEYS", "DEEPSEEK_API_KEY"),
        "openai": ("OPENAI_API_KEYS", "OPENAI_API_KEY"),
        "openai_compatible": ("QUANTLAB_LLM_API_KEY",),
    }[normalized]
    return any(str(os.getenv(name) or "").strip() for name in names)


def deepseek_key_configured() -> bool:
    """Compatibility wrapper for the original DeepSeek-only settings surface."""

    return llm_provider_key_configured("deepseek")


def masked_secret_status(configured: bool) -> str:
    return "已配置（内容已隐藏）" if configured else "尚未配置"


def save_deepseek_product_preferences(
    root: Path,
    *,
    api_key: str | None,
    model: str,
) -> None:
    """Compatibility wrapper for callers that still save only DeepSeek."""

    save_llm_product_preferences(
        root,
        provider="deepseek",
        api_key=api_key,
        model=model,
        base_url=None,
    )


def remove_local_deepseek_key(root: Path) -> None:
    remove_local_llm_key(root, provider="deepseek")


def save_llm_product_preferences(
    root: Path,
    *,
    provider: str,
    api_key: str | None,
    model: str,
    base_url: str | None,
) -> None:
    """Save one local LLM provider without deleting other configured providers.

    The selected provider controls new work.  Existing keys for the other
    providers intentionally remain untouched so a user can switch back without
    re-entering credentials.  Secrets stay in the local dotenv file only.
    """

    normalized = _normalize_llm_provider(provider)
    clean_key = _clean_secret(api_key)
    clean_model = _clean_model(model, normalized)
    clean_url = _clean_base_url(
        base_url,
        required=normalized == "openai_compatible",
    )
    # ``llm.model`` and ``llm.base_url`` are the first values consumed by the
    # provider factory.  They must follow the active provider instead of
    # retaining a value from a previous compatible endpoint.
    active_base_url = {
        "deepseek": clean_url or "https://api.deepseek.com",
        "openai": clean_url,
        "openai_compatible": clean_url,
    }[normalized]
    updates: dict[str, str | None] = {
        "QUANTLAB_LLM_PROVIDER": normalized,
        "QUANTLAB_LLM_MODEL": clean_model,
        "QUANTLAB_LLM_BASE_URL": active_base_url or None,
        # Keep both providers available to the existing router/advanced path.
        # Selecting a direct provider still makes that provider authoritative
        # for a new Chat or research task.
        "QUANTLAB_OPENAI_ENABLED": "true",
        "QUANTLAB_DEEPSEEK_ENABLED": "true",
    }
    if normalized == "deepseek":
        updates.update(
            {
                "QUANTLAB_DEEPSEEK_MODEL": clean_model,
                "QUANTLAB_DEEPSEEK_BASE_URL": active_base_url,
            }
        )
        if clean_key:
            # The plural form has precedence in the provider factory.  Keeping
            # one value avoids a stale pooled key unexpectedly winning.
            updates["DEEPSEEK_API_KEYS"] = clean_key
            updates["DEEPSEEK_API_KEY"] = None
    elif normalized == "openai":
        updates.update(
            {
                "QUANTLAB_OPENAI_MODEL": clean_model,
                "QUANTLAB_OPENAI_BASE_URL": active_base_url or None,
            }
        )
        if clean_key:
            updates["OPENAI_API_KEYS"] = clean_key
            updates["OPENAI_API_KEY"] = None
    else:
        updates.update(
            {
                "QUANTLAB_LLM_MODEL": clean_model,
                "QUANTLAB_LLM_BASE_URL": clean_url,
            }
        )
        if clean_key:
            updates["QUANTLAB_LLM_API_KEY"] = clean_key
            # This variable has higher precedence for the compatible driver.
            # Remove a stale local-only secret only when the user explicitly
            # saves a replacement compatible endpoint.
            updates["QUANTLAB_LOCAL_API_KEY"] = None
    _apply_env_updates(root, updates)


def remove_local_llm_key(root: Path, *, provider: str) -> None:
    normalized = _normalize_llm_provider(provider)
    updates = {
        "deepseek": {"DEEPSEEK_API_KEYS": None, "DEEPSEEK_API_KEY": None},
        "openai": {"OPENAI_API_KEYS": None, "OPENAI_API_KEY": None},
        "openai_compatible": {"QUANTLAB_LLM_API_KEY": None},
    }[normalized]
    _apply_env_updates(root, updates)


def _normalize_llm_provider(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in LLM_PROVIDER_OPTIONS:
        raise ValueError("请选择受支持的 AI 服务商")
    return normalized


def _clean_secret(value: str | None) -> str:
    clean = str(value or "").strip()
    if clean and ("\n" in clean or "\r" in clean):
        raise ValueError("API Key 不能包含换行符")
    return clean


def _clean_model(value: str, provider: str) -> str:
    clean = str(value or "").strip()
    if "\n" in clean or "\r" in clean:
        raise ValueError("模型名不能包含换行符")
    if len(clean) > 160:
        raise ValueError("模型名过长")
    if clean:
        return clean
    default = {
        "deepseek": "deepseek-chat",
        "openai": "gpt-5.6-terra",
        "openai_compatible": "",
    }[provider]
    if not default:
        raise ValueError("兼容接口需要填写模型名称")
    return default


def _clean_base_url(value: str | None, *, required: bool) -> str:
    clean = str(value or "").strip()
    if not clean:
        if required:
            raise ValueError("兼容接口需要填写请求地址")
        return ""
    if "\n" in clean or "\r" in clean:
        raise ValueError("请求地址不能包含换行符")
    parsed = urlsplit(clean)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("请求地址必须是完整的 HTTP(S) 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("请求地址不能包含账号、查询参数或片段")
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and parsed.hostname not in local_hosts:
        raise ValueError("非本机请求地址必须使用 HTTPS")
    return clean.rstrip("/")


def _apply_env_updates(root: Path, updates: dict[str, str | None]) -> None:
    _update_env_file(root / ".env", updates)
    for name, value in updates.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _update_env_file(path: Path, updates: dict[str, str | None]) -> None:
    """Atomically update selected dotenv entries while retaining unrelated settings."""

    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    pending = dict(updates)
    output: list[str] = []
    for line in original.splitlines():
        match = _ENV_ASSIGNMENT.match(line)
        key = match.group(1) if match else None
        if key not in pending:
            output.append(line)
            continue
        value = pending.pop(key)
        if value is not None:
            output.append(f"{key}={value}")
    for key, value in pending.items():
        if value is not None:
            output.append(f"{key}={value}")
    content = "\n".join(output).rstrip() + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False, newline="\n"
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


__all__ = [
    "LLM_PROVIDER_OPTIONS",
    "deepseek_key_configured",
    "llm_provider_key_configured",
    "masked_secret_status",
    "remove_local_deepseek_key",
    "remove_local_llm_key",
    "save_deepseek_product_preferences",
    "save_llm_product_preferences",
]
