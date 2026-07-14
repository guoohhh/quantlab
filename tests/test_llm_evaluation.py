from quantlab.llm import run_llm_replay


def test_mock_replay_is_persisted_without_secrets(settings):
    settings.values["llm"] = {"provider": "mock"}

    output = run_llm_replay(settings, suite="smoke", runs=1, save=True)

    assert output["summary"]["calls"] == 2
    assert output["summary"]["success_rate"] == 1.0
    assert output["security"]["keys_returned"] is False
    assert output["evaluation_id"] > 0
    assert "api_key" not in str(output).lower()
