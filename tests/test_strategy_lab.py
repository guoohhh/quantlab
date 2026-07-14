from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd

from quantlab.domain.models import Bar
from quantlab.workflows import strategy_lab
from quantlab.workflows.strategy_lab import (
    ADAPTIVE_ETF_CANDIDATES,
    _candidate_admission,
    _segments,
)


def test_adaptive_candidate_registry_matches_preregistered_protocol():
    assert [item["name"] for item in ADAPTIVE_ETF_CANDIDATES] == [
        "adaptive_balanced",
        "adaptive_core",
        "adaptive_defensive",
        "adaptive_concentrated",
    ]
    assert len(ADAPTIVE_ETF_CANDIDATES) == 4


def test_strategy_lab_segments_are_contiguous_and_non_overlapping():
    start = date(2020, 1, 1)
    dates = [start + timedelta(days=index) for index in range(24)]

    segments = _segments(dates, 6)

    assert len(segments) == 6
    assert segments[0] == (dates[0], dates[3])
    assert segments[-1] == (dates[20], dates[23])
    assert all(left[1] < right[0] for left, right in zip(segments, segments[1:]))


def test_strategy_candidate_cannot_trade_risk_efficiency_for_failed_alpha_gate():
    admission = _candidate_admission(
        metrics={"sharpe": 1.5, "max_drawdown": -0.05},
        relative={
            "total_return_delta": -0.10,
            "sharpe_delta": 0.20,
            "max_drawdown_delta": 0.04,
        },
        stress={"sharpe": 1.4, "max_drawdown": -0.06},
        inference={"probability_alpha_positive": 0.12},
        sharpe={"multiple_testing_adjusted_psr": 0.98},
        observations=843,
    )

    assert admission["gates"]["absolute"] is True
    assert admission["gates"]["cost_stress"] is True
    assert admission["gates"]["benchmark"] is False
    assert admission["gates"]["statistical"] is False
    assert admission["passed"] is False


def test_strategy_candidate_passes_only_when_all_preregistered_gates_pass():
    admission = _candidate_admission(
        metrics={"sharpe": 1.2, "max_drawdown": -0.12},
        relative={
            "total_return_delta": 0.03,
            "sharpe_delta": 0.10,
            "max_drawdown_delta": 0.01,
        },
        stress={"sharpe": 1.0, "max_drawdown": -0.14},
        inference={"probability_alpha_positive": 0.95},
        sharpe={"multiple_testing_adjusted_psr": 0.90},
        observations=700,
    )

    assert admission["passed"] is True


def test_strategy_lab_runs_preregistered_selection_holdout_and_export(
    settings, monkeypatch
):
    settings.values["system"]["initial_capital"] = 100_000.0
    settings.values["risk"] = {"max_total_exposure": 0.8}
    settings.values["costs"] = {
        "etf": {
            "commission_rate": 0.0001,
            "minimum_commission": 5.0,
            "stamp_duty_rate": 0.0,
            "transfer_fee_rate": 0.0,
            "slippage_bps": 5.0,
            "stop_slippage_bps": 15.0,
        }
    }
    settings.values["backtest"] = {
        "bootstrap_block_days": 20,
        "bootstrap_simulations": 500,
    }
    settings.values["strategies"] = {
        "etf_rotation": {
            "lookbacks": [20, 60, 120],
            "top_k": 2,
            "universe": [
                "sh510300",
                "sz159915",
                "sh510880",
                "sh518880",
                "sh513100",
                "sh511010",
            ],
            "defensive_symbol": "sh511010",
        }
    }
    symbols = list(settings.get("strategies.etf_rotation.universe"))
    days = [day.date() for day in pd.bdate_range("2013-12-01", "2026-06-30")]
    bars = [
        Bar(
            symbol=symbol,
            date=day,
            open=100,
            high=101,
            low=99,
            close=100,
            adjusted_close=100 + index * 0.01,
            volume=1_000_000,
            source="synthetic-test",
        )
        for symbol in symbols
        for index, day in enumerate(days)
    ]

    class FakeProvider:
        name = "fake"
        last_degraded_from = []

        def bars(self, requested_symbols, start, end):
            assert requested_symbols == symbols
            return [bar for bar in bars if start <= bar.date <= end]

    fake_provider = FakeProvider()
    monkeypatch.setattr(strategy_lab, "WestockProvider", lambda root: object())
    monkeypatch.setattr(strategy_lab, "AkShareProvider", lambda: object())
    monkeypatch.setattr(strategy_lab, "FallbackProvider", lambda providers: fake_provider)
    monkeypatch.setattr(
        strategy_lab, "CachedProvider", lambda wrapped, cache_dir: fake_provider
    )

    rates = {
        "adaptive_defensive": 0.00100,
        "adaptive_balanced": 0.00040,
        "adaptive_core": 0.00035,
        "adaptive_concentrated": 0.00030,
        "legacy": 0.00020,
    }

    def result_for(rate, trade_start, trade_end):
        value = 100_000.0
        curve = []
        for index, day in enumerate(days):
            if trade_start <= day <= trade_end:
                alternating_noise = 0.0004 if index % 2 else -0.0004
                value *= 1 + rate + alternating_noise
            curve.append((day, value))
        return SimpleNamespace(equity_curve=curve, fills=[])

    def fake_backtest(
        settings_arg,
        bars_arg,
        frame_arg,
        symbols_arg,
        config,
        trade_start,
        trade_end,
        cost_multiplier=1.0,
    ):
        name = config.get("name", "legacy")
        rate = rates[name] - 0.00001 * (cost_multiplier - 1.0)
        return result_for(rate, trade_start, trade_end)

    def fake_static(
        settings_arg,
        bars_arg,
        symbols_arg,
        targets,
        trade_start,
        trade_end,
        cost_multiplier=1.0,
    ):
        rate = 0.00025 if len(targets) == 1 else 0.00032
        return result_for(rate, trade_start, trade_end)

    monkeypatch.setattr(strategy_lab, "run_etf_backtest", fake_backtest)
    monkeypatch.setattr(strategy_lab, "run_etf_static_backtest", fake_static)

    output = strategy_lab.run_adaptive_etf_candidate_lab(settings, save=True)

    assert output["selected_candidate"]["name"] == "adaptive_defensive", [
        (item["name"], item["selection_score"])
        for item in output["development_candidates"]
    ]
    assert output["locked_holdout"]["admission"]["passed"] is True, output[
        "locked_holdout"
    ]["admission"]
    assert output["status"] == "eligible_for_prospective_observation"
    assert output["formal_strategy_changed"] is False
    assert output["validation_id"] > 0
    assert settings.resolve("data/reports/strategy-lab-latest.json").exists()
    assert settings.resolve("data/reports/strategy-lab-latest.md").exists()
