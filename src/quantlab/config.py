from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass(frozen=True)
class Settings:
    values: dict[str, Any]
    root: Path

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        root = Path(__file__).resolve().parents[2]
        env_path = Path(os.getenv("QUANTLAB_ENV_FILE", root / ".env"))
        _load_env_file(env_path)
        default_path = root / "config" / "default.toml"
        with default_path.open("rb") as fh:
            values = tomllib.load(fh)
        if path:
            with Path(path).open("rb") as fh:
                values = _merge(values, tomllib.load(fh))
        system = values.setdefault("system", {})
        system["database_path"] = os.getenv(
            "QUANTLAB_DATABASE_PATH", system.get("database_path", "data/quantlab.db")
        )
        system["data_dir"] = os.getenv(
            "QUANTLAB_DATA_DIR", system.get("data_dir", "data")
        )
        llm = values.setdefault("llm", {})
        llm["provider"] = os.getenv("QUANTLAB_LLM_PROVIDER", llm.get("provider", "mock"))
        llm["model"] = os.getenv("QUANTLAB_LLM_MODEL", llm.get("model", ""))
        llm["base_url"] = os.getenv("QUANTLAB_LLM_BASE_URL", llm.get("base_url", ""))
        llm["openai_model"] = os.getenv(
            "QUANTLAB_OPENAI_MODEL", llm.get("openai_model", "gpt-5.6-terra")
        )
        llm["openai_reasoning_effort"] = os.getenv(
            "QUANTLAB_OPENAI_REASONING_EFFORT",
            llm.get("openai_reasoning_effort", "medium"),
        )
        llm["deepseek_model"] = os.getenv(
            "QUANTLAB_DEEPSEEK_MODEL", llm.get("deepseek_model", "deepseek-chat")
        )
        llm["openai_base_url"] = os.getenv(
            "QUANTLAB_OPENAI_BASE_URL", llm.get("openai_base_url", "")
        )
        llm["deepseek_base_url"] = os.getenv(
            "QUANTLAB_DEEPSEEK_BASE_URL", llm.get("deepseek_base_url", "https://api.deepseek.com")
        )
        llm["local_model"] = os.getenv("QUANTLAB_LOCAL_MODEL", llm.get("local_model", ""))
        llm["local_base_url"] = os.getenv("QUANTLAB_LOCAL_BASE_URL", llm.get("local_base_url", ""))
        llm["openai_enabled"] = _env_bool(
            "QUANTLAB_OPENAI_ENABLED", bool(llm.get("openai_enabled", True))
        )
        llm["deepseek_enabled"] = _env_bool(
            "QUANTLAB_DEEPSEEK_ENABLED", bool(llm.get("deepseek_enabled", True))
        )
        runtime = values.setdefault("runtime", {})
        runtime["trusted_data_auto_refresh_enabled"] = _env_bool(
            "QUANTLAB_TRUSTED_DATA_AUTO_REFRESH",
            bool(runtime.get("trusted_data_auto_refresh_enabled", True)),
        )
        return cls(values=values, root=root)

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.values.get(name, {}))

    def with_overrides(self, overrides: dict[str, Any]) -> "Settings":
        return Settings(values=_merge(self.values, overrides), root=self.root)

    def get(self, dotted: str, default: Any = None) -> Any:
        current: Any = self.values
        for part in dotted.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def resolve(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root / path


def _load_env_file(path: Path) -> None:
    """Load a local dotenv file without overriding process-level secrets."""

    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}
