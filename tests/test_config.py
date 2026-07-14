import os

from quantlab.config import Settings, _load_env_file


def test_dotenv_loader_does_not_override_process_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "EXISTING_KEY=file-value\nNEW_KEY='new-value'\nINVALID-NAME=x\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EXISTING_KEY", "process-value")
    monkeypatch.delenv("NEW_KEY", raising=False)

    _load_env_file(env_file)

    assert os.environ["EXISTING_KEY"] == "process-value"
    assert os.environ["NEW_KEY"] == "new-value"


def test_runtime_overrides_do_not_mutate_base_settings(tmp_path):
    settings = Settings(
        values={"llm": {"openai_model": "gpt-5.4", "openai_reasoning_effort": "medium"}},
        root=tmp_path,
    )

    overridden = settings.with_overrides(
        {"llm": {"openai_model": "gpt-5.6-sol", "openai_reasoning_effort": "high"}}
    )

    assert overridden.get("llm.openai_model") == "gpt-5.6-sol"
    assert overridden.get("llm.openai_reasoning_effort") == "high"
    assert settings.get("llm.openai_model") == "gpt-5.4"


def test_default_config_has_evidence_first_etf_core_budget():
    settings = Settings.load()

    assert settings.get("strategies.etf_core.min_weight") == 0.45
    assert settings.get("strategies.etf_core.max_weight") == 0.45
    assert settings.get("strategies.etf_core.target_exposure") == 0.45
    assert settings.get("strategies.etf_core.rebalance_frequency") == "semiannual"
    assert settings.get("strategies.etf_core.rebalance_tolerance_weight") == 0.02
