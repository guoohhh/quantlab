import asyncio
from datetime import date

import pytest

from quantlab.agents import MultiAgentDecisionSystem, ResearchContext
from quantlab.llm import MockLLMProvider
from quantlab.learning import LearningRepository
from quantlab.persistence import DecisionRepository, HistoricalReplayRepository


def test_forecast_outcome_and_calibration(tmp_path):
    run = asyncio.run(
        MultiAgentDecisionSystem(MockLLMProvider()).run(
            ResearchContext(symbol="sh510300", as_of=date(2026, 1, 2), price=4.0)
        )
    )
    repository = DecisionRepository(tmp_path / "quantlab.db")
    repository.save(run)

    outcome = repository.record_forecast_outcome(
        run.run_id, 5, 2.5, "2026-01-09", flat_threshold_pct=1.0
    )
    report = repository.calibration_report(horizon_days=5, minimum_samples=2)
    learning = LearningRepository(tmp_path / "quantlab.db")

    assert outcome.outcome == "up"
    assert report.samples == 1
    assert report.brier_score == pytest.approx((0.6**2 + 0.3**2 + 0.3**2) / 3)
    assert report.accuracy == 1.0
    assert report.calibrated is False
    assert learning.sample_counts()["stock:5d:live_decision"]["completed"] == 1
    assert learning.attributions()[0]["attribution"]["unresolved"] is True


def test_missing_forecast_cannot_receive_outcome(tmp_path):
    repository = DecisionRepository(tmp_path / "quantlab.db")
    with pytest.raises(ValueError, match="not found"):
        repository.record_forecast_outcome("missing", 5, 1.0, "2026-01-09")


def test_historical_replay_repository_round_trip_and_missing_lookup(tmp_path):
    repository = HistoricalReplayRepository(tmp_path / "quantlab.db")
    payload = {
        "method": "blind replay",
        "metrics": {"full_system": {"total_return": 0.01}},
    }

    replay_id = repository.save(
        date(2025, 1, 1),
        date(2025, 6, 30),
        20,
        2,
        "ok",
        payload,
    )

    records = repository.list(limit=1)
    stored = repository.get(replay_id)

    assert records[0]["id"] == replay_id
    assert records[0]["horizon_days"] == 20
    assert stored["payload"] == payload
    assert repository.get(replay_id + 1) is None
