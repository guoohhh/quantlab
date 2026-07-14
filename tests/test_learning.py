from datetime import date, timedelta

import pandas as pd

from quantlab.learning import LearningRepository, predict_active_model, train_registered_model
from quantlab.learning.attribution import attribute_sample
from quantlab.learning.cross_section import cross_sectional_features
from quantlab.learning.drift import monitor_active_model
from quantlab.learning.features import empty_features
from quantlab.learning.historical import generate_historical_samples
from quantlab.learning.model import OnlineSoftmaxModel


def test_chronological_model_training_activates_only_when_it_beats_baseline(tmp_path):
    repository = LearningRepository(tmp_path / "learning.db")
    start = date(2025, 1, 1)
    labels = ((1.0, "up"), (0.0, "flat"), (-1.0, "down"))
    for index in range(240):
        signal, outcome = labels[index % 3]
        features = empty_features()
        features["factor_momentum_20"] = signal
        features["factor_composite"] = signal
        repository.upsert_sample(
            sample_key=f"sample-{index}",
            run_id=None,
            source="test",
            asset_scope="etf",
            symbol="sh510300",
            as_of=start + timedelta(days=index),
            horizon_days=5,
            features=features,
            outcome=outcome,
            realized_return_pct=signal * 2,
            evaluated_at=start + timedelta(days=index + 5),
        )

    result = train_registered_model(repository, 5, "etf", minimum_samples=100)
    positive = empty_features()
    positive["factor_momentum_20"] = 1.0
    positive["factor_composite"] = 1.0
    prediction = predict_active_model(repository, 5, "etf", positive)

    assert result["status"] == "activated"
    assert result["evaluation"]["brier_score"] < result["evaluation"]["baseline_brier"]
    assert prediction is not None
    assert prediction["up_probability"] > prediction["down_probability"]
    assert predict_active_model(repository, 5, "stock", positive) is None
    active = repository.active_model(5, "etf")
    assert active is not None
    assert active["metrics"]["feature_importance"]["up"]
    assert active["metrics"]["validation_folds"]
    assert active["metrics"]["fold_pass_rate"] >= 0.67
    assert (
        train_registered_model(repository, 5, "etf", minimum_samples=100)["status"] == "up_to_date"
    )
    assert repository.challenge_history()[0]["decision"] == "activated"
    assert repository.dataset_manifest(5, "etf")["eligible_samples"] == 240


def test_model_serialization_preserves_probability_calibration():
    model = OnlineSoftmaxModel(list(empty_features()))
    model.prior_blend = 0.3

    restored = OnlineSoftmaxModel.loads(model.dumps())

    assert restored.prior_blend == 0.3


def test_legacy_active_model_is_auditable_but_has_zero_production_weight(tmp_path):
    repository = LearningRepository(tmp_path / "learning.db")
    model = OnlineSoftmaxModel(list(empty_features()))
    repository.save_model(
        horizon_days=5,
        asset_scope="etf",
        trained_until="2025-12-31",
        parameters_json=model.dumps(),
        metrics={"brier_score": 0.18, "baseline_brier": 0.22},
        training_samples=200,
        validation_samples=60,
        activate=True,
    )

    prediction = predict_active_model(repository, 5, "etf", empty_features())

    assert prediction is not None
    assert prediction["governance_status"] == "legacy_awaiting_challenge"
    assert prediction["ensemble_weight"] == 0.0


def test_historical_generation_uses_only_prefix_data(tmp_path):
    start = date(2025, 1, 1)
    frame = pd.DataFrame(
        {
            "symbol": ["sh510300"] * 150,
            "date": [start + timedelta(days=index) for index in range(150)],
            "close": [100 + index * 0.2 for index in range(150)],
            "volume": [1_000_000 + index for index in range(150)],
        }
    )
    repository = LearningRepository(tmp_path / "learning.db")

    output = generate_historical_samples(
        frame,
        repository,
        horizons=(5,),
        step=10,
        minimum_history=120,
    )

    assert output["generated"][5] == 3
    assert repository.sample_counts()["etf:5d:historical_factor"]["completed"] == 3


def test_cross_sectional_features_rank_relative_momentum_without_future_data():
    start = date(2025, 1, 1)
    rows = []
    for index in range(140):
        for symbol, slope in (("leader", 0.5), ("middle", 0.2), ("laggard", -0.1)):
            rows.append(
                {
                    "symbol": symbol,
                    "date": start + timedelta(days=index),
                    "close": 100 + slope * index,
                }
            )
    frame = pd.DataFrame(rows)

    leader = cross_sectional_features(frame, start + timedelta(days=120), "leader")
    laggard = cross_sectional_features(frame, start + timedelta(days=120), "laggard")

    assert leader["cross_section_momentum_20_rank"] == 1.0
    assert laggard["cross_section_momentum_20_rank"] < leader["cross_section_momentum_20_rank"]
    assert leader["cross_section_relative_return_20"] > 0
    assert leader["cross_section_leadership_gap_20"] == 0.0
    assert 0 <= leader["cross_section_breadth_20"] <= 1


def test_selection_biased_stock_samples_are_research_only(tmp_path):
    start = date(2025, 1, 1)
    frame = pd.DataFrame(
        {
            "symbol": ["sh600519"] * 150,
            "date": [start + timedelta(days=index) for index in range(150)],
            "close": [100 + index * 0.2 for index in range(150)],
            "volume": [1_000_000 + index for index in range(150)],
        }
    )
    repository = LearningRepository(tmp_path / "learning.db")
    generate_historical_samples(
        frame,
        repository,
        horizons=(5,),
        step=10,
        minimum_history=120,
        asset_scope="stock",
        training_eligible=False,
        universe_provenance="current_symbols",
    )

    result = train_registered_model(repository, 5, "stock", minimum_samples=1)

    assert result["status"] == "insufficient_samples"
    assert result["excluded_ineligible_samples"] == 3


def test_event_attribution_is_association_not_causal_claim():
    sample = {
        "sample_key": "x",
        "symbol": "sh600519",
        "as_of": "2026-01-01",
        "evaluated_at": "2026-01-10",
        "horizon_days": 5,
        "expected_return_pct": -1.0,
        "realized_return_pct": 3.0,
        "outcome": "up",
        "features": {
            "llm_up_probability": 0.2,
            "llm_flat_probability": 0.2,
            "llm_down_probability": 0.6,
        },
    }
    events = [
        {
            "id": 1,
            "event_date": "2026-01-05",
            "event_type": "earnings",
            "title": "earnings beat",
            "source": "exchange filing",
            "sentiment": 0.8,
            "impact_score": 0.9,
        }
    ]

    result = attribute_sample(sample, events)

    assert result["direction_correct"] is False
    assert result["candidate_event_explanations"][0]["event_id"] == 1
    assert result["causal_claim"] is False
    assert result["root_cause_candidates"]
    assert result["brier_score"] > 0


def test_duplicate_market_event_is_idempotent(tmp_path):
    repository = LearningRepository(tmp_path / "learning.db")
    first = repository.add_event(
        "sh600519", date(2026, 1, 2), "news", "same title", "source", 0.0, 0.5
    )
    second = repository.add_event(
        "sh600519", date(2026, 1, 2), "news", "same title", "source", 0.0, 0.5
    )

    assert first == second


def test_attribution_summary_aggregates_diagnostic_root_causes(tmp_path):
    repository = LearningRepository(tmp_path / "learning.db")
    repository.save_attribution(
        "sample-1",
        {
            "surprise_pct": 4.0,
            "direction_correct": False,
            "root_cause_candidates": [{"code": "model_overconfidence", "score": 0.9}],
        },
    )

    summary = repository.attribution_summary()

    assert summary["samples"] == 1
    assert summary["direction_accuracy"] == 0
    assert summary["root_cause_distribution"]["model_overconfidence"] == 1


def test_online_drift_monitor_deactivates_degraded_model(tmp_path):
    repository = LearningRepository(tmp_path / "learning.db")
    model = OnlineSoftmaxModel(list(empty_features()))
    record = repository.save_model(
        horizon_days=5,
        asset_scope="etf",
        trained_until="2025-12-31",
        parameters_json=model.dumps(),
        metrics={"brier_score": 0.15, "baseline_brier": 0.22},
        training_samples=100,
        validation_samples=30,
        activate=True,
    )
    start = date(2026, 1, 1)
    for index in range(30):
        repository.upsert_sample(
            sample_key=f"live-{index}",
            run_id=f"run-{index}",
            source="live_decision",
            asset_scope="etf",
            symbol="sh510300",
            as_of=start + timedelta(days=index),
            horizon_days=5,
            features=empty_features(),
            outcome="up",
            realized_return_pct=2.0,
            evaluated_at=start + timedelta(days=index + 5),
            context={
                "forecast_components": {
                    "statistical_model_id": record["model_id"],
                    "statistical": [0.0, 0.0, 1.0],
                    "final": [0.1, 0.1, 0.8],
                    "raw_llm": [0.7, 0.2, 0.1],
                }
            },
        )

    result = monitor_active_model(repository, 5, "etf", minimum_online_samples=30)

    assert result["status"] == "deactivated"
    assert repository.active_model(5, "etf") is None
    assert repository.monitoring_history()[0]["action"] == "deactivated"


def test_rolling_governance_deactivates_legacy_single_split_model_on_rejection(tmp_path):
    repository = LearningRepository(tmp_path / "learning.db")
    legacy = repository.save_model(
        horizon_days=5,
        asset_scope="etf",
        trained_until="2025-01-01",
        parameters_json=OnlineSoftmaxModel(list(empty_features())).dumps(),
        metrics={"brier_score": 0.2, "baseline_brier": 0.22, "total_samples": 240},
        training_samples=100,
        validation_samples=30,
        activate=True,
    )
    start = date(2025, 1, 1)
    for index in range(240):
        repository.upsert_sample(
            sample_key=f"flat-{index}",
            run_id=None,
            source="test",
            asset_scope="etf",
            symbol="sh510300",
            as_of=start + timedelta(days=index),
            horizon_days=5,
            features=empty_features(),
            outcome=("up", "flat", "down")[index % 3],
            realized_return_pct=0.0,
            evaluated_at=start + timedelta(days=index + 5),
        )

    result = train_registered_model(
        repository,
        5,
        "etf",
        minimum_samples=100,
        minimum_fold_pass_rate=1.1,
        force=True,
    )

    assert result["status"] == "rejected"
    assert result["deactivated_legacy_model_id"] == legacy["model_id"]
    assert repository.active_model(5, "etf") is None


def test_challenger_must_beat_incumbent_on_prospective_holdout(tmp_path):
    repository = LearningRepository(tmp_path / "learning.db")
    incumbent = repository.save_model(
        horizon_days=5,
        asset_scope="etf",
        trained_until="2024-12-31",
        parameters_json=OnlineSoftmaxModel(list(empty_features())).dumps(),
        metrics={
            "brier_score": 0.23,
            "baseline_brier": 0.22,
            "total_samples": 120,
            "validation_folds": [],
        },
        training_samples=100,
        validation_samples=20,
        activate=True,
    )
    start = date(2025, 1, 1)
    labels = ((1.0, "up"), (0.0, "flat"), (-1.0, "down"))
    for index in range(240):
        signal, outcome = labels[index % 3]
        features = empty_features()
        features["factor_momentum_20"] = signal
        features["factor_composite"] = signal
        repository.upsert_sample(
            sample_key=f"challenge-{index}",
            run_id=None,
            source="test",
            asset_scope="etf",
            symbol="sh510300",
            as_of=start + timedelta(days=index),
            horizon_days=5,
            features=features,
            outcome=outcome,
            realized_return_pct=signal * 2,
            evaluated_at=start + timedelta(days=index + 5),
        )

    result = train_registered_model(repository, 5, "etf", minimum_samples=100, force=True)

    assert result["status"] == "promoted"
    assert result["champion_challenger"]["champion_model_id"] == incumbent["model_id"]
    assert result["champion_challenger"]["probability_candidate_brier_better"] >= 0.90
    assert repository.active_model(5, "etf")["model_id"] == result["model_id"]
