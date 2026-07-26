import pytest

from quantlab.config import Settings


@pytest.fixture(autouse=True)
def isolate_external_llm_credentials(monkeypatch):
    """Local developer credentials must never change deterministic test routing."""
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_API_KEYS",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_API_KEYS",
        "QUANTLAB_LLM_API_KEY",
        "QUANTLAB_LOCAL_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolate_production_database(monkeypatch, tmp_path):
    """Every test-owned Settings.load() must resolve to a disposable database."""

    monkeypatch.setenv("QUANTLAB_DATABASE_PATH", str(tmp_path / "quantlab-test.db"))


@pytest.fixture
def settings(tmp_path):
    values = {
        "system": {
            "database_path": "quantlab.db",
            "data_dir": "data",
            "test_mode": True,
        },
        "calibration": {"flat_threshold_pct": 1.0},
    }
    return Settings(values=values, root=tmp_path)
