from datetime import date

import pytest

from quantlab.config import Settings
from quantlab.data import DemoDataProvider
from quantlab.learning import LearningRepository, build_point_in_time_predictor
from quantlab.llm import MockLLMProvider
from quantlab.llm.providers import _routing_key
from quantlab.reporting import prepare_historical_replay_export, render_historical_replay_markdown
from quantlab.workflows.replay import (
    _episode_llm_validation,
    _forecast_metrics,
    _gate_effect,
    _outcome,
    _trade_metrics,
    run_historical_blind_replay,
)


class AuditedLiveProvider(MockLLMProvider):
    provider_name = "test-live"
    model = "deterministic-live-test"

    def __init__(self):
        self.call_log = []
        self.prompts = []
        self.closed = False

    async def structured(self, system, prompt, schema):
        self.prompts.append(prompt)
        routing_key = _routing_key(system, schema)
        if not self.call_log:
            self.call_log.append(
                {
                    "endpoint_id": "primary-test",
                    "provider": self.provider_name,
                    "model": self.model,
                    "schema": schema.__name__,
                    "routing_key": routing_key,
                    "status": "error",
                    "error_type": "TimeoutError",
                    "usage": {},
                }
            )
        result = await super().structured(system, prompt, schema)
        self.call_log.append(
            {
                "endpoint_id": "fallback-test",
                "provider": self.provider_name,
                "model": self.model,
                "schema": schema.__name__,
                "routing_key": routing_key,
                "status": "ok",
                "error_type": None,
                "usage": {},
            }
        )
        return result

    async def aclose(self):
        self.closed = True

    def health_snapshot(self):
        return {
            "provider": self.provider_name,
            "model": self.model,
            "status": "ready",
        }


def _settings(tmp_path):
    return Settings(
        values={
            "system": {
                "database_path": "quantlab.db",
                "data_dir": "data",
                "initial_capital": 100_000.0,
            },
            "risk": {"max_total_exposure": 0.8, "max_single_position": 0.15},
            "costs": {
                "etf": {
                    "commission_rate": 0.0001,
                    "minimum_commission": 5.0,
                    "stamp_duty_rate": 0.0,
                    "transfer_fee_rate": 0.0,
                    "slippage_bps": 5.0,
                    "stop_slippage_bps": 15.0,
                }
            },
            "calibration": {"flat_threshold_pct": 1.0},
            "learning": {
                "minimum_samples": 100,
                "minimum_validation_samples": 20,
                "validation_fraction": 0.2,
                "validation_folds": 3,
                "minimum_fold_pass_rate": 0.67,
                "maximum_statistical_weight": 0.5,
            },
            "strategies": {
                "etf_rotation": {
                    "lookbacks": [20, 60, 120],
                    "top_k": 2,
                    "universe": DemoDataProvider.ETF_SYMBOLS,
                    "defensive_symbol": "sh511010",
                }
            },
        },
        root=tmp_path,
    )


def test_point_in_time_predictor_excludes_labels_not_known_at_cutoff(tmp_path):
    repository = LearningRepository(tmp_path / "quantlab.db")
    for key, as_of, evaluated in (
        ("known", date(2025, 1, 1), date(2025, 1, 10)),
        ("future", date(2025, 1, 2), date(2025, 3, 1)),
    ):
        repository.upsert_sample(
            sample_key=key,
            run_id=None,
            source="historical_factor",
            asset_scope="etf",
            symbol="sh510300",
            as_of=as_of,
            horizon_days=5,
            features={},
            outcome="up",
            realized_return_pct=2.0,
            evaluated_at=evaluated,
            context={"training_eligible": True},
            origin="historical_research",
            evidence_stage="historical_training",
            training_eligible=True,
        )

    predictor, audit = build_point_in_time_predictor(
        repository,
        "etf",
        date(2025, 2, 1),
        minimum_samples=2,
        minimum_validation_samples=1,
    )

    assert predictor(5, {}) is None
    assert audit["horizons"][5]["samples"] == 1
    assert audit["horizons"][5]["status"] == "insufficient_samples"


def test_historical_replay_blinds_identity_and_uses_next_open(tmp_path):
    settings = _settings(tmp_path)
    provider = DemoDataProvider()
    bars = provider.bars(provider.ETF_SYMBOLS, date(2023, 1, 1), date(2025, 12, 31))
    progress = []

    output = run_historical_blind_replay(
        settings,
        date(2024, 1, 1),
        date(2024, 6, 30),
        horizon_days=5,
        episodes=2,
        save=False,
        bars=bars,
        provider_factory=MockLLMProvider,
        progress_callback=progress.append,
    )

    assert output["completed_episodes"] == 2
    assert [item["completed"] for item in progress] == [1, 2]
    assert all(item["requested"] == 2 for item in progress)
    assert output["blinding"]["actual_symbol_supplied_to_llm"] is False
    assert output["blinding"]["actual_date_supplied_to_llm"] is False
    assert output["evidence_status"] == "illustrative"
    assert output["evidence_qualification"]["qualified"] is False
    assert "required live LLM roles" in output["evidence_qualification"]["limitations"][0]
    assert output["metrics"]["strategy_only"]["trades"] == 2
    assert output["llm_validation"]["live_llm_complete"] is False
    for episode in output["episodes"]:
        assert episode["blind_symbol"].startswith("ETF_CANDIDATE_")
        assert episode["entry_date"] > episode["actual_as_of"]
        assert episode["decision"]["entry_price"] is None
        assert episode["council"]["veto_triggered"] is False
        assert episode["point_in_time_model"]["cutoff"] == episode["actual_as_of"]
        assert episode["gate_counterfactuals"]["current_strict"]["trade"] == episode[
            "full_system_trade"
        ]
        assert (
            episode["decision_trace"]["council_diagnostics"]["momentum_technical_sync"]
            == episode["council"]["momentum_tech_sync"]
        )
    for current, following in zip(output["episodes"], output["episodes"][1:]):
        for field in ("strategy_trade", "full_system_trade", "benchmark_trade"):
            assert following[field]["capital_before"] == current[field]["capital_after"]
    assert (
        output["metrics"]["strategy_only"]["final_equity"]
        == output["episodes"][-1]["strategy_trade"]["capital_after"]
    )
    markdown = render_historical_replay_markdown(output)
    assert "历史盲测回放报告" in markdown
    assert "角色输出：0/22" in markdown
    legacy = {key: value for key, value in output.items() if key != "evidence_qualification"}
    assert prepare_historical_replay_export(legacy)["evidence_qualification"]["qualified"] is False


def test_historical_replay_counts_role_success_separately_from_fallback_attempts(tmp_path):
    settings = _settings(tmp_path)
    data_provider = DemoDataProvider()
    bars = data_provider.bars(data_provider.ETF_SYMBOLS, date(2023, 1, 1), date(2025, 12, 31))
    providers = []

    def provider_factory():
        provider = AuditedLiveProvider()
        providers.append(provider)
        return provider

    output = run_historical_blind_replay(
        settings,
        date(2024, 1, 1),
        date(2024, 6, 30),
        horizon_days=5,
        episodes=2,
        save=False,
        bars=bars,
        provider_factory=provider_factory,
    )

    validation = output["llm_validation"]
    assert validation["live_llm_complete"] is True
    assert validation["successful_non_mock_role_outputs"] == 22
    assert validation["recorded_endpoint_attempts"] == 24
    assert len(validation["fallback_errors"]) == 2
    assert validation["episodes"][0]["missing_roles"] == {}
    assert all(provider.closed for provider in providers)
    assert all("normalized_price_path_120" in "\n".join(provider.prompts) for provider in providers)
    for provider, episode in zip(providers, output["episodes"], strict=True):
        prompt_text = "\n".join(provider.prompts)
        assert "blinded_normalized_price_history" in prompt_text
        assert "normalized_adjusted_close_path_120" in prompt_text
        assert "latest_normalized_close_vs_moving_averages" in prompt_text
        assert "latest_observation=100" in prompt_text
        assert '"absolute_prices_included": false' in prompt_text
        assert '"price_is_executable": false' in prompt_text
        assert "normalized_index_latest_observation_100" in prompt_text
        assert '"decision_mode": "historical_blind_replay"' in prompt_text
        assert '"execution_evidence_required": false' in prompt_text
        assert '"maximum_final_weight": 0.15' in prompt_text
        assert episode["actual_symbol"] not in prompt_text
        assert episode["actual_as_of"] not in prompt_text


def test_llm_validation_rejects_duplicate_successes_when_a_role_is_missing():
    expected = ["quant", "technical", "forecast", "forecast", "review"]
    calls = [
        {"status": "ok", "provider": "live", "routing_key": role}
        for role in ("quant", "technical", "forecast", "forecast", "forecast")
    ]

    validation = _episode_llm_validation({"calls": calls}, expected)

    assert validation["successful_non_mock_role_outputs"] == len(expected)
    assert validation["missing_roles"] == {"review": 1}
    assert validation["unexpected_success_roles"] == {"forecast": 1}
    assert validation["complete"] is False


def test_replay_outcomes_and_capital_path_guard_are_explicit():
    assert _outcome(1.1, 1.0) == "up"
    assert _outcome(-1.1, 1.0) == "down"
    assert _outcome(0.2, 1.0) == "flat"

    rows = [
        {
            "trade": {
                "traded": True,
                "net_return": 0.01,
                "capital_before": 99_000.0,
                "capital_after": 99_990.0,
            }
        }
    ]
    try:
        _trade_metrics(rows, "trade", 100_000.0)
    except ValueError as exc:
        assert "capital path is discontinuous" in str(exc)
    else:
        raise AssertionError("discontinuous replay capital must be rejected")

    invalid = _forecast_metrics(
        [
            {
                "forecast": {
                    "up_probability": 0.8,
                    "flat_probability": 0.6,
                    "down_probability": 0.1,
                },
                "outcome": "up",
            }
        ],
        "final",
    )
    assert invalid["samples"] == 0
    assert invalid["invalid_samples"] == 1


def test_gate_effect_distinguishes_capture_reduction_and_avoidance():
    positive = {"traded": True, "net_return": 0.02}
    negative = {"traded": True, "net_return": -0.02}
    no_trade = {"traded": False, "net_return": 0.0}

    assert _gate_effect(positive, no_trade) == "missed_gain"
    assert _gate_effect(positive, {"traded": True, "net_return": 0.01}) == "reduced_gain"
    assert _gate_effect(positive, positive) == "participated_gain"
    assert _gate_effect(negative, no_trade) == "avoided_loss"
    assert _gate_effect(negative, {"traded": True, "net_return": -0.01}) == "reduced_loss"
    assert _gate_effect(negative, negative) == "participated_loss"
    assert _gate_effect({"traded": True, "net_return": 0.0}, no_trade) == "flat_outcome"


def test_historical_replay_episode_limit_supports_measured_scale_but_caps_abuse(tmp_path):
    with pytest.raises(ValueError, match="between 1 and 60"):
        run_historical_blind_replay(
            _settings(tmp_path),
            date(2020, 1, 1),
            date(2025, 1, 1),
            episodes=61,
            save=False,
            bars=[],
        )
    with pytest.raises(ValueError, match="allow_large_run=true"):
        run_historical_blind_replay(
            _settings(tmp_path),
            date(2020, 1, 1),
            date(2025, 1, 1),
            episodes=13,
            save=False,
            bars=[],
        )
