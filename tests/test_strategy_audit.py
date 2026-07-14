from datetime import date, timedelta

from quantlab.config import Settings
from quantlab.workflows.strategy_audit import (
    _candidate_comparison,
    _diagnose,
    _probability_range,
    _return_attribution,
)


def test_strategy_audit_candidate_comparison_counts_window_wins():
    def period(candidate_return, incumbent_return, candidate_sharpe, incumbent_sharpe):
        metrics = {
            "candidate": {
                "total_return": candidate_return,
                "sharpe": candidate_sharpe,
                "max_drawdown": -0.05,
            },
            "incumbent": {
                "total_return": incumbent_return,
                "sharpe": incumbent_sharpe,
                "max_drawdown": -0.10,
            },
        }
        return {"metrics": metrics}

    calendar = [period(0.2, 0.1, 1.2, 1.0), period(0.0, 0.1, 0.8, 1.0)]
    rolling = [period(0.2, 0.1, 1.2, 1.0), period(0.3, 0.1, 1.3, 1.0)]

    result = _candidate_comparison(calendar, rolling, "candidate", "incumbent")

    assert result["calendar_return_win_rate"] == 0.5
    assert result["rolling_return_win_rate"] == 1.0
    assert result["rolling_sharpe_win_rate"] == 1.0
    assert result["rolling_drawdown_win_rate"] == 1.0


def test_strategy_audit_return_attribution_uses_past_regime_windows():
    start = date(2025, 1, 1)
    paired = [
        (
            start + timedelta(days=index),
            0.001 if index % 3 else -0.001,
            0.0015 if index % 4 else -0.002,
        )
        for index in range(180)
    ]

    result = _return_attribution(paired)

    assert 0 <= result["monthly_excess_positive_rate"] <= 1
    assert result["up_capture"] is not None
    assert result["down_capture"] is not None
    assert sum(item["observations"] for item in result["regimes"].values()) > 0


def test_strategy_audit_diagnosis_flags_low_upside_capture_and_ranges():
    stability = {
        "adaptive_v2_full": {
            "rolling_return_win_rate": 0.4,
            "rolling_sharpe_win_rate": 0.8,
            "rolling_drawdown_win_rate": 1.0,
        }
    }
    ablation = {
        "no_feature": {
            "feature_value_positive_means_full_is_better": {
                "mean_rolling_return_delta": -0.01,
                "mean_rolling_sharpe_delta": -0.02,
            }
        }
    }
    attribution = {"adaptive_v2_full": {"up_capture": 0.6}}

    result = _diagnose(stability, ablation, attribution)

    assert "risk_budget_or_bull_market_participation" in result["primary_issues"]
    assert "low_upside_capture" in result["primary_issues"]
    assert result["harmful_or_unproven_features"] == ["no_feature"]
    assert _probability_range(
        [
            {"probability_alpha_positive": 0.4},
            {"probability_alpha_positive": 0.6},
        ]
    ) == [0.4, 0.6]


def test_strategy_audit_settings_fixture_can_resolve_report_directory(tmp_path):
    settings = Settings(values={"system": {"data_dir": "data"}}, root=tmp_path)

    assert settings.resolve(settings.get("system.data_dir")) == tmp_path / "data"
