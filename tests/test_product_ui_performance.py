from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import Mock

from dashboard import product_ui


class _Settings:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def get(self, key: str, default=None):
        values = {
            "system.database_path": self.database_path.name,
            "strategies.forward_primary.minimum_trust_level": "server_observed",
            "strategies.forward_primary.candidate_count": 3,
            "llm.provider": "deepseek",
        }
        return values.get(key, default)

    def resolve(self, _value):
        return self.database_path


def test_ui_readiness_snapshot_is_short_lived_session_cache(monkeypatch, tmp_path):
    database_path = tmp_path / "quantlab.db"
    database_path.touch()
    state: dict[str, object] = {}
    readiness = {"blockers": [], "data": {"source_states": {}}}
    primary = Mock(return_value=readiness)
    monkeypatch.setattr(product_ui.st, "session_state", state)
    monkeypatch.setattr(product_ui, "primary_start_readiness", primary)
    settings = _Settings(database_path)

    assert product_ui._ui_readiness_snapshot(settings) is readiness
    assert product_ui._ui_readiness_snapshot(settings) is readiness
    primary.assert_called_once_with(settings, require_runtime=False)

    assert product_ui._ui_readiness_snapshot(settings, force=True) is readiness
    assert primary.call_count == 2


def test_product_chat_never_calls_the_llm_inline():
    source = inspect.getsource(product_ui._render_chat)

    assert "submit_chat_job(" in source
    assert "handle_chat_message(" not in source
