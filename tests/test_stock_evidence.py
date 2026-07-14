from datetime import date

import pandas as pd

from quantlab.config import Settings
from quantlab.execution import CostModel
from quantlab.learning import LearningRepository
from quantlab.persistence import StockRankingReplayRepository
from quantlab.workflows.stock_discovery import _bar_frame
from quantlab.workflows.stock_evidence import (
    _simulate_stock_portfolio,
    run_stock_ranking_replay,
)


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
                "stock": {
                    "commission_rate": 0.00025,
                    "minimum_commission": 5.0,
                    "stamp_duty_rate": 0.0005,
                    "transfer_fee_rate": 0.00001,
                    "slippage_bps": 10.0,
                    "stop_slippage_bps": 25.0,
                },
                "etf": {
                    "commission_rate": 0.0001,
                    "minimum_commission": 5.0,
                    "stamp_duty_rate": 0.0,
                    "transfer_fee_rate": 0.0,
                    "slippage_bps": 5.0,
                    "stop_slippage_bps": 15.0,
                },
            },
            "calibration": {"flat_threshold_pct": 1.0},
            "strategies": {"stock_evidence": {"benchmark_symbol": "sh510300"}},
        },
        root=tmp_path,
    )


def _bars():
    dates = pd.bdate_range("2023-01-02", periods=620)
    slopes = {
        "sh600001": 0.0012,
        "sz000002": 0.0005,
        "sz300003": -0.0001,
        "sh510300": 0.0003,
    }
    output = []
    for symbol, slope in slopes.items():
        price = 20.0
        for index, timestamp in enumerate(dates):
            cycle = 0.0008 if index % 17 < 8 else -0.0003
            price *= 1 + slope + cycle
            output.append(
                {
                    "symbol": symbol,
                    "date": timestamp.date(),
                    "open": price * 0.999,
                    "high": price * 1.01,
                    "low": price * 0.99,
                    "close": price,
                    "adjusted_close": price,
                    "volume": 10_000_000,
                    "amount": 200_000_000,
                    "source": "fixture",
                }
            )
    return output


def test_stock_ranking_replay_is_point_in_time_costed_and_persisted(tmp_path):
    settings = _settings(tmp_path)
    output = run_stock_ranking_replay(
        settings,
        ["600001", "000002", "300003"],
        date(2024, 1, 2),
        date(2024, 9, 30),
        horizon_days=20,
        episodes=5,
        top_k=2,
        save=True,
        bars=_bars(),
    )

    assert output["completed_episodes"] == 5
    assert output["blinding"]["ranking_inputs_cut_off_at_signal_date"] is True
    assert output["metrics"]["system_top_rank"]["episodes"] == 5
    assert output["paired_comparisons"]["benchmark_hs300"]["samples"] == 5
    assert output["learning_samples"]["training_eligible"] is False
    assert all(
        episode["trades"]["system_top_rank"]["legs"][0]["entry_date"] > episode["signal_date"]
        for episode in output["episodes"]
    )

    stored = StockRankingReplayRepository(tmp_path / "quantlab.db").get(output["replay_id"])
    assert stored["payload"]["universe_hash"] == output["universe_hash"]
    assert stored["payload"]["replay_id"] == output["replay_id"]
    samples = LearningRepository(tmp_path / "quantlab.db").completed_samples(20, "stock")
    assert len(samples) == 15
    assert all(sample["context"]["training_eligible"] is False for sample in samples)


def test_stock_ranking_replay_rejects_single_name_and_bad_horizon(tmp_path):
    settings = _settings(tmp_path)
    try:
        run_stock_ranking_replay(
            settings,
            ["600001"],
            date(2024, 1, 2),
            date(2024, 9, 30),
            bars=_bars(),
            save=False,
        )
        assert False, "single-name universe must be rejected"
    except ValueError as exc:
        assert "at least two" in str(exc)

    try:
        run_stock_ranking_replay(
            settings,
            ["600001", "000002"],
            date(2024, 1, 2),
            date(2024, 9, 30),
            horizon_days=10,
            bars=_bars(),
            save=False,
        )
        assert False, "unsupported horizon must be rejected"
    except ValueError as exc:
        assert "5 or 20" in str(exc)


def test_delisted_position_uses_last_sellable_close_instead_of_dropping_sample(tmp_path):
    settings = _settings(tmp_path)
    frame = _bar_frame(
        [
            {
                "symbol": "sz000005",
                "date": date(2024, 4, 22),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
            },
            {
                "symbol": "sz000005",
                "date": date(2024, 4, 23),
                "open": 0.9,
                "high": 0.95,
                "low": 0.85,
                "close": 0.9,
            },
            {
                "symbol": "sz000005",
                "date": date(2024, 4, 26),
                "open": 0.5,
                "high": 0.55,
                "low": 0.45,
                "close": 0.5,
            },
        ]
    )
    trade = _simulate_stock_portfolio(
        frame,
        ["sz000005"],
        date(2024, 4, 22),
        date(2024, 5, 20),
        100_000.0,
        0.15,
        CostModel.from_dict(settings.get("costs.stock")),
    )

    leg = trade["legs"][0]
    assert leg["traded"] is True
    assert leg["terminal_exit"] is True
    assert leg["exit_date"] == "2024-04-26"
    assert leg["position_net_return"] < 0
