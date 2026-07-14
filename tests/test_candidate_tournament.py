from datetime import date, timedelta
from math import sin
from types import SimpleNamespace

import numpy as np
import pytest

from quantlab.config import Settings
from quantlab.domain.models import Forecast
from quantlab.persistence import TerminalRepository
from quantlab.workflows import tournament as tournament_module
from quantlab.workflows.tournament import (
    _candidate_record,
    candidate_tournament_scorecard,
    rank_tournament_candidates,
    run_candidate_tournament,
    settle_candidate_tournaments,
    stress_test_portfolio,
)


def _bars(return_series: dict[str, list[float]]) -> list[dict]:
    output = []
    start = date(2025, 1, 1)
    for symbol, returns in return_series.items():
        price = 100.0
        for index, daily_return in enumerate(returns):
            price *= 1 + daily_return
            output.append(
                {
                    "symbol": symbol,
                    "date": start + timedelta(days=index),
                    "close": price,
                    "adjusted_close": price,
                }
            )
    return output


def _candidate(symbol: str, score: float, eligible: bool = True) -> dict:
    return {
        "symbol": symbol,
        "name": symbol,
        "status": "ok",
        "tournament_score": score,
        "review_eligible": eligible,
        "action": "buy" if eligible else "review_required",
    }


def _forecast(symbol: str, horizon: int, up: float, down: float) -> Forecast:
    return Forecast(
        symbol=symbol,
        as_of=date(2026, 7, 13),
        horizon_days=horizon,
        up_probability=up,
        flat_probability=1 - up - down,
        down_probability=down,
        expected_return_pct=2.0,
        lower_return_pct=-3.0,
        upper_return_pct=6.0,
        confidence=0.8,
    )


def test_candidate_record_combines_forecast_factor_radar_and_review_gates():
    symbol = "sh510300"
    decision = SimpleNamespace(
        symbol=symbol,
        action="buy",
        requires_human_review=False,
        degraded_sources=[],
        confidence=0.78,
        target_weight=0.15,
    )
    run = SimpleNamespace(
        decision=decision,
        reports={
            "reviewer": SimpleNamespace(approved=True),
            "council": SimpleNamespace(veto_triggered=False),
        },
        forecasts=[_forecast(symbol, 5, 0.62, 0.18), _forecast(symbol, 20, 0.68, 0.17)],
        decision_trace={"composite_score": 0.55},
        run_id="run-1",
    )

    record = _candidate_record(
        {"decision_run": run, "report": SimpleNamespace(composite_score=0.4)},
        {
            "symbol": symbol,
            "name": "沪深300ETF",
            "category": "A股宽基",
            "risk_bucket": "risk_on",
            "strength_score": 72,
        },
    )

    assert record["review_eligible"] is True
    assert record["actionable"] is True
    assert record["tournament_score"] > 50
    assert record["score_trace"]["forecast_20d"] > record["score_trace"]["forecast_5d"]


def test_ranking_blocks_failed_review_and_deduplicates_only_high_positive_correlation():
    common = [0.001 + 0.008 * sin(index / 5) for index in range(300)]
    inverse = [-value * 0.9 for value in common]
    bars = _bars({"A": common, "B": common, "C": inverse, "D": common})
    candidates = [
        _candidate("D", 99, eligible=False),
        _candidate("A", 90),
        _candidate("B", 85),
        _candidate("C", 80),
    ]

    output = rank_tournament_candidates(
        candidates,
        bars,
        shortlist_size=2,
        max_correlation=0.8,
    )

    assert [item["symbol"] for item in output["ranked_candidates"]] == ["D", "A", "B", "C"]
    assert [item["symbol"] for item in output["diversified_shortlist"]] == ["A", "C"]
    statuses = {
        item["symbol"]: item["diversification_status"] for item in output["ranked_candidates"]
    }
    assert statuses["D"] == "not_eligible"
    assert statuses["B"] == "excluded_high_correlation:A"
    assert output["correlation_matrix"]["A"]["C"] < 0


def test_stress_test_is_reproducible_and_reports_historical_tail_risk():
    first = [0.001 + 0.009 * sin(index / 4) for index in range(300)]
    second = [0.0005 + 0.006 * sin(index / 7 + 0.3) for index in range(300)]
    output = stress_test_portfolio(
        {"growth": 0.2, "gold": 0.1},
        _bars({"growth": first, "gold": second}),
        capital=100_000,
        metadata={
            "growth": {"category": "A股成长"},
            "gold": {"category": "黄金"},
        },
    )

    selloff = next(item for item in output["scenarios"] if item["scenario"] == "equity_selloff")
    assert selloff["portfolio_return"] == pytest.approx(-0.026)
    assert selloff["pnl_amount"] == pytest.approx(-2_600)
    historical = output["historical_risk"]
    assert historical["observations"] == 252
    assert historical["one_day_var_95_pct"] >= 0
    assert historical["one_day_cvar_95_pct"] >= historical["one_day_var_95_pct"]
    assert sum(historical["variance_contribution"].values()) == pytest.approx(1.0)


def test_stress_test_discloses_partial_history_and_validates_inputs():
    output = stress_test_portfolio(
        {"available": 0.2, "missing": 0.1},
        _bars({"available": [0.001 + 0.005 * sin(index / 3) for index in range(300)]}),
        capital=100_000,
    )

    assert output["historical_risk"]["available_symbols"] == ["available"]
    assert output["historical_risk"]["missing_symbols"] == ["missing"]
    assert stress_test_portfolio({}, [], capital=100_000)["invested_weight"] == 0
    with pytest.raises(ValueError, match="positive"):
        stress_test_portfolio({}, [], capital=0)
    with pytest.raises(ValueError, match="non-negative"):
        stress_test_portfolio({"A": -0.1}, [], capital=100_000)
    with pytest.raises(ValueError, match="sum to at most one"):
        stress_test_portfolio({"A": 0.8, "B": 0.3}, [], capital=100_000)
    with pytest.raises(ValueError, match="finite"):
        stress_test_portfolio({"A": np.nan}, [], capital=100_000)


def test_candidate_tournament_persistence_round_trip(tmp_path):
    repository = TerminalRepository(tmp_path / "terminal.db")
    tournament_id = repository.save_candidate_tournament(
        date(2026, 7, 13),
        {
            "candidates": [{"symbol": "A"}, {"symbol": "B"}],
            "diversified_shortlist": [{"symbol": "A"}],
            "stress_test": {"worst_scenario": {"scenario": "rates_up"}},
        },
    )

    summary = repository.candidate_tournaments(1)[0]
    detail = repository.candidate_tournament(tournament_id)
    assert summary["candidate_count"] == 2
    assert summary["shortlist_count"] == 1
    assert detail["tournament_id"] == tournament_id
    assert detail["diversified_shortlist"][0]["symbol"] == "A"
    assert repository.candidate_tournament(999_999) is None
    assert repository.update_candidate_tournament(999_999, {}) is False


def test_one_analyzer_failure_does_not_abort_tournament(tmp_path, monkeypatch):
    symbols = ["A", "B"]
    common = [0.001 + 0.005 * sin(index / 5) for index in range(300)]
    bars = _bars({"A": common, "B": [value * 0.5 for value in common]})
    radar = {
        "as_of": (date(2025, 1, 1) + timedelta(days=299)).isoformat(),
        "instruments": [
            {
                "symbol": symbol,
                "name": symbol,
                "category": "A股宽基",
                "risk_bucket": "risk_on",
                "strength_score": 70 - index,
                "observations": 300,
            }
            for index, symbol in enumerate(symbols)
        ],
        "degraded_sources": [],
    }
    settings = Settings(
        values={
            "system": {
                "database_path": "quantlab.db",
                "initial_capital": 100_000,
            },
            "strategies": {"etf_rotation": {"universe": symbols}},
        },
        root=tmp_path,
    )

    def analyzer(settings, symbol, as_of, **kwargs):
        if symbol == "A":
            raise RuntimeError("provider timeout")
        return {"symbol": symbol}

    monkeypatch.setattr(
        tournament_module,
        "_candidate_record",
        lambda output, radar_row: {
            **_candidate(radar_row["symbol"], 75),
            "category": radar_row["category"],
            "risk_bucket": radar_row["risk_bucket"],
        },
    )

    output = run_candidate_tournament(
        settings,
        candidate_limit=2,
        shortlist_size=1,
        save=False,
        bars=bars,
        radar=radar,
        analyzer=analyzer,
    )

    assert len(output["candidates"]) == 2
    assert next(item for item in output["candidates"] if item["symbol"] == "A")["status"] == "error"
    assert output["diversified_shortlist"][0]["symbol"] == "B"
    assert output["comparison_portfolio"]["hypothetical_only"] is True


def test_tournament_settlement_measures_agent_incremental_value_and_scorecard(tmp_path):
    origin = date(2025, 1, 1)
    settings = Settings(
        values={
            "system": {"database_path": "quantlab.db", "data_dir": "data"},
        },
        root=tmp_path,
    )
    repository = TerminalRepository(tmp_path / "quantlab.db")
    tournament_id = repository.save_candidate_tournament(
        origin,
        {
            "as_of": origin.isoformat(),
            "candidates": [
                {
                    **_candidate("A", 90),
                    "tournament_rank": 1,
                    "radar": {"strength_score": 60},
                },
                {
                    **_candidate("B", 80),
                    "tournament_rank": 2,
                    "radar": {"strength_score": 80},
                },
            ],
            "diversified_shortlist": [{"symbol": "A"}],
            "stress_test": {"worst_scenario": None},
        },
    )
    bars = _bars({"A": [0.01] * 30, "B": [-0.005] * 30})

    output = settle_candidate_tournaments(
        settings,
        origin + timedelta(days=29),
        bars=bars,
    )

    assert output["settled"][0]["tournament_id"] == tournament_id
    detail = repository.candidate_tournament(tournament_id)
    horizon_20 = detail["settlement"]["20"]
    assert horizon_20["status"] == "settled"
    assert horizon_20["top_ranked_symbol"] == "A"
    assert horizon_20["radar_leader_symbol"] == "B"
    assert horizon_20["agent_rank_excess_vs_radar_pct"] > 0
    assert horizon_20["top_ranked_excess_vs_equal_weight_pct"] > 0
    assert horizon_20["top_ranked_winner"] is True
    assert horizon_20["rank_information_coefficient"] == pytest.approx(1.0)
    scorecard = candidate_tournament_scorecard(settings)
    assert scorecard["horizons"]["20"]["samples"] == 1
    assert scorecard["horizons"]["20"]["top_rank_win_rate"] == 1
    assert scorecard["horizons"]["20"]["evidence_status"] == "illustrative"
    assert repository.candidate_tournaments(1)[0]["settlement_status"] == "settled"

    repeated = settle_candidate_tournaments(
        settings,
        origin + timedelta(days=29),
        bars=bars,
    )
    assert repeated["settled"] == []
    assert repeated["pending"] == []


def test_tournament_settlement_stays_pending_until_every_candidate_has_horizon(tmp_path):
    origin = date(2025, 1, 1)
    settings = Settings(
        values={"system": {"database_path": "quantlab.db", "data_dir": "data"}},
        root=tmp_path,
    )
    repository = TerminalRepository(tmp_path / "quantlab.db")
    repository.save_candidate_tournament(
        origin,
        {
            "as_of": origin.isoformat(),
            "candidates": [
                {**_candidate("A", 90), "tournament_rank": 1, "radar_rank": 1},
                {**_candidate("B", 80), "tournament_rank": 2, "radar_rank": 2},
            ],
            "diversified_shortlist": [],
            "stress_test": {"worst_scenario": None},
        },
    )
    bars = _bars({"A": [0.01] * 8, "B": [0.005] * 4})

    output = settle_candidate_tournaments(
        settings,
        origin + timedelta(days=7),
        bars=bars,
    )

    assert output["settled"] == []
    assert output["pending"][0]["settled_horizons"] == []
    detail = repository.candidate_tournament_records(1)[0]
    assert detail["settlement"]["5"]["status"] == "pending"
    assert detail["settlement"]["5"]["missing_symbols"] == ["B"]
    assert candidate_tournament_scorecard(settings)["horizons"]["5"]["samples"] == 0


def test_tournament_settlement_handles_empty_repository_and_bad_limit(tmp_path):
    settings = Settings(
        values={"system": {"database_path": "quantlab.db", "data_dir": "data"}},
        root=tmp_path,
    )

    assert settle_candidate_tournaments(settings)["settled"] == []
    with pytest.raises(ValueError, match="between 1 and 200"):
        settle_candidate_tournaments(settings, limit=0)


def test_tournament_with_only_one_successful_research_is_not_comparable(tmp_path):
    origin = date(2025, 1, 1)
    settings = Settings(
        values={"system": {"database_path": "quantlab.db", "data_dir": "data"}},
        root=tmp_path,
    )
    repository = TerminalRepository(tmp_path / "quantlab.db")
    tournament_id = repository.save_candidate_tournament(
        origin,
        {
            "as_of": origin.isoformat(),
            "candidates": [
                {**_candidate("A", 90), "tournament_rank": 1, "radar_rank": 1},
                {
                    "symbol": "B",
                    "status": "error",
                    "tournament_rank": 2,
                    "tournament_score": 0,
                },
            ],
            "diversified_shortlist": [{"symbol": "A"}],
            "stress_test": {"worst_scenario": None},
        },
    )

    output = settle_candidate_tournaments(
        settings,
        origin + timedelta(days=30),
        bars=[],
    )

    assert output["not_comparable"][0]["tournament_id"] == tournament_id
    assert output["pending"] == []
    detail = repository.candidate_tournament(tournament_id)
    assert detail["settlement"]["5"]["status"] == "not_comparable"
    assert repository.candidate_tournaments(1)[0]["settlement_status"] == "not_comparable"
    repeated = settle_candidate_tournaments(
        settings,
        origin + timedelta(days=31),
        bars=[],
    )
    assert repeated["not_comparable"] == []
