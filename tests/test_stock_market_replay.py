from datetime import date

import pandas as pd

from quantlab.config import Settings
from quantlab.data import PointInTimeSecurity
from quantlab.domain.models import Bar
from quantlab.learning import LearningRepository
from quantlab.workflows.stock_market_replay import run_market_wide_stock_replay
from quantlab.workflows.universe import refresh_a_share_security_master


def _settings(tmp_path):
    return Settings(
        values={
            "system": {
                "database_path": "quantlab.db",
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
            "strategies": {
                "stock_market_replay": {
                    "benchmark_symbol": "sh510300",
                    "seed": "fixture-v1",
                    "max_correlation": 0.85,
                    "minimum_average_turnover": 10_000_000.0,
                }
            },
        },
        root=tmp_path,
    )


def _universe_records():
    output = []
    for prefix, exchange, board in (
        ("600", "SH", "main"),
        ("688", "SH", "star"),
        ("000", "SZ", "main"),
        ("300", "SZ", "chinext"),
    ):
        for index in range(4):
            code = f"{prefix}{100 + index:03d}"
            output.append(
                PointInTimeSecurity(
                    symbol=("sh" if exchange == "SH" else "sz") + code,
                    name=f"样本{code}",
                    exchange=exchange,
                    board=board,
                    trade_status=True,
                )
            )
    return output


class FakePointInTimeProvider:
    name = "baostock"

    def point_in_time_universe(self, day):
        return _universe_records()

    def bars(self, symbols, start, end):
        dates = pd.bdate_range(start, end)
        output = []
        for symbol in symbols:
            price = 10.0 + (int(symbol[-2:]) % 5)
            drift = 0.0002 + (int(symbol[-2:]) % 4) * 0.00015
            for index, timestamp in enumerate(dates):
                price *= 1 + drift + (0.0003 if index % 11 < 5 else -0.0001)
                is_st = symbol == "sz000100" and timestamp.year == 2024
                output.append(
                    Bar(
                        symbol=symbol,
                        date=timestamp.date(),
                        open=price * 0.999,
                        high=price * 1.01,
                        low=price * 0.99,
                        close=price,
                        adjusted_open=price * 0.999,
                        adjusted_high=price * 1.01,
                        adjusted_low=price * 0.99,
                        adjusted_close=price,
                        volume=10_000_000,
                        amount=150_000_000,
                        is_st=is_st,
                        source=self.name,
                    )
                )
        return output


def _master_frames():
    records = _universe_records()
    return {
        "sse_main_active": pd.DataFrame(
            [
                {
                    "证券代码": item.symbol[2:],
                    "证券简称": item.name,
                    "上市日期": "2010-01-01",
                }
                for item in records
                if item.exchange == "SH" and item.board == "main"
            ]
        ),
        "sse_star_active": pd.DataFrame(
            [
                {
                    "证券代码": item.symbol[2:],
                    "证券简称": item.name,
                    "上市日期": "2020-01-01",
                }
                for item in records
                if item.board == "star"
            ]
        ),
        "szse_active": pd.DataFrame(
            [
                {
                    "A股代码": item.symbol[2:],
                    "A股简称": item.name,
                    "A股上市日期": "2010-01-01",
                }
                for item in records
                if item.exchange == "SZ"
            ]
        ),
    }


def test_market_wide_replay_uses_historical_universe_and_eligible_learning_samples(tmp_path):
    settings = _settings(tmp_path)
    refresh_a_share_security_master(settings, frames=_master_frames())
    progress = []

    output = run_market_wide_stock_replay(
        settings,
        date(2024, 1, 2),
        date(2024, 8, 30),
        horizon_days=5,
        episodes=3,
        sample_size=12,
        top_k=2,
        save=False,
        provider=FakePointInTimeProvider(),
        progress_callback=progress.append,
    )

    assert output["completed_episodes"] == 3
    assert output["evidence_qualification"]["point_in_time_market_universe_available"] is True
    assert output["evidence_qualification"]["historical_st_status_available"] is True
    assert output["evidence_qualification"]["snapshot_integrity"] is True
    assert output["evidence_qualification"]["minimum_exchange_master_jaccard"] == 1.0
    assert output["evidence_qualification"]["failed_snapshot_integrity_count"] == 0
    assert output["strategy_admission"]["passed"] is False
    assert output["strategy_admission"]["status"] == "research_only"
    assert output["evidence_qualification"]["qualified_for_market_wide_selection_claim"] is False
    assert output["learning_samples"]["training_eligible"] is True
    assert output["benchmark_type"] == "non_tradeable_index_comparator"
    assert output["episodes"][0]["trades"]["benchmark_hs300"]["traded_legs"] == 1
    assert output["metrics"]["benchmark_hs300"]["total_return"] != 0
    assert output["episodes"][0]["trades"]["benchmark_hs300_multi_name"][
        "target_weight"
    ] == output["episodes"][0]["trades"]["system_diversified_top_k"]["target_weight"]
    assert output["paired_comparisons"]["benchmark_hs300_multi_name"]["samples"] == 3
    assert output["snapshot_audits"][0]["cross_validation"]["jaccard"] == 1.0
    assert [item["completed"] for item in progress] == [1, 2, 3]
    assert all(item["full_market_securities"] == 16 for item in progress)
    samples = LearningRepository(tmp_path / "quantlab.db").completed_samples(5, "stock")
    assert samples
    assert all(sample["context"]["survivorship_bias_control"] is True for sample in samples)


def test_market_wide_replay_validates_sample_and_horizon(tmp_path):
    settings = _settings(tmp_path)
    with_error = [
        ({"horizon_days": 10, "sample_size": 12}, "5 or 20"),
        ({"horizon_days": 5, "sample_size": 8}, "between 12 and 200"),
    ]
    for kwargs, expected in with_error:
        try:
            run_market_wide_stock_replay(
                settings,
                date(2024, 1, 2),
                date(2024, 8, 30),
                save=False,
                provider=FakePointInTimeProvider(),
                **kwargs,
            )
            assert False, "invalid replay configuration must fail"
        except ValueError as exc:
            assert expected in str(exc)


def test_market_wide_replay_applies_frozen_policy_portfolio_contract(tmp_path):
    settings = _settings(tmp_path)
    refresh_a_share_security_master(settings, frames=_master_frames())
    policy = {
        "kind": "static_cross_section_v2",
        "name": "broad_contrarian_four",
        "portfolio": {"top_k": 4, "total_exposure": 0.40, "selection": "rank_top_k"},
        "governance": {"signal_schedule_horizon_days": 5, "holding_horizon_days": 20},
    }

    output = run_market_wide_stock_replay(
        settings,
        date(2024, 1, 2),
        date(2024, 8, 30),
        horizon_days=20,
        episodes=3,
        sample_size=12,
        top_k=2,
        save=False,
        record_learning_samples=False,
        provider=FakePointInTimeProvider(),
        ranking_policy=policy,
    )

    assert output["signal_schedule_horizon_days"] == 5
    assert output["budget_contract"]["multi_name_budget"] == 0.40
    assert output["budget_contract"]["ranking_policy_total_exposure"] == 0.40
    assert all(len(item["diversified_top_k"]) == 4 for item in output["episodes"])
    assert all(
        item["trades"]["system_diversified_top_k"]["target_weight"] == 0.40
        for item in output["episodes"]
    )
