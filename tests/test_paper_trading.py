from datetime import date, datetime

import pandas as pd

from quantlab.config import Settings
from quantlab.domain.models import Bar, MarketRegime, StrategySignal
from quantlab.persistence import PaperTradingRepository
from quantlab.workflows import paper as paper_workflow
from quantlab.workflows.candidates import ScanResult


def test_paper_repository_tracks_cash_positions_and_snapshots(tmp_path):
    repository = PaperTradingRepository(tmp_path / "paper.db")
    repository.ensure_account("test", "Test", "fixture", 100_000)
    order_id = repository.queue_order(
        account_id="test",
        symbol="sh510300",
        name="沪深300ETF",
        strategy="fixture",
        side="buy",
        quantity=1_000,
        signal_date=date(2026, 1, 2),
        reference_price=4.0,
        target_weight=0.04,
    )
    repeated_id = repository.queue_order(
        account_id="test",
        symbol="sh510300",
        name="沪深300ETF",
        strategy="fixture",
        side="buy",
        quantity=2_000,
        signal_date=date(2026, 1, 2),
        reference_price=4.2,
        target_weight=0.08,
    )
    assert repeated_id == order_id
    assert repository.pending_orders("test")[0]["quantity"] == 1_000
    repository.fill_order(
        order_id,
        trade_date=date(2026, 1, 5),
        price=4.01,
        fees=5.0,
        gross_value=4_010.0,
    )
    overview = repository.overview(
        "test", {"sh510300": {"price": 4.2, "as_of": "2026-01-05", "source": "test"}}
    )
    repository.save_snapshot("test", date(2026, 1, 5), overview)

    assert overview["cash"] == 95_985.0
    assert overview["positions"][0]["quantity"] == 1_000
    assert overview["equity"] == 100_185.0
    assert repository.scorecard()["accounts"][0]["snapshots"] == 1


def test_paper_cycle_freezes_signals_then_fills_at_later_open(tmp_path, monkeypatch):
    settings = Settings(
        values={
            "system": {
                "database_path": "quantlab.db",
                "initial_capital": 100_000.0,
            },
            "risk": {"max_total_exposure": 0.8, "minimum_order_value": 1_000.0},
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
            "strategies": {
                "etf_rotation": {
                    "universe": ["sh510300"],
                    "defensive_symbol": "sh510300",
                }
            },
        },
        root=tmp_path,
    )

    def fake_scan(_settings, as_of):
        days = [date(2026, 1, 2)]
        if as_of >= date(2026, 1, 5):
            days.append(date(2026, 1, 5))
        bars = [
            Bar(
                symbol="sh510300",
                date=day,
                open=10.0 if day.day == 2 else 11.0,
                high=11.5,
                low=9.5,
                close=10.0 if day.day == 2 else 11.0,
                available_at=datetime.combine(day, datetime.min.time()),
                source="test",
            )
            for day in days
        ]
        latest = bars[-1]
        return ScanResult(
            signals=[
                StrategySignal(
                    strategy="etf_rotation",
                    symbol="sh510300",
                    as_of=latest.date,
                    score=0.8,
                    target_weight=1.0,
                    confidence=0.8,
                )
            ],
            market_data={
                "sh510300": {
                    "name": "沪深300ETF",
                    "price": latest.close,
                    "open": latest.open,
                    "as_of": latest.date.isoformat(),
                    "source": "test",
                    "trade_lot": 100,
                }
            },
            market_regime=MarketRegime.RANGE,
            bars=bars,
        )

    monkeypatch.setattr(paper_workflow, "scan_etf_rotation", fake_scan)

    first = paper_workflow.run_paper_cycle(settings, date(2026, 1, 2))
    second = paper_workflow.run_paper_cycle(settings, date(2026, 1, 5))

    assert first["fills"] == []
    assert len(first["queued_orders"]) == 4
    assert len(second["fills"]) == 4
    assert all(item["trade_date"] == "2026-01-05" for item in second["fills"])
    accounts = {item["account_id"]: item for item in second["scorecard"]["accounts"]}
    assert accounts["etf_strategy"]["turnover_count"] == 1.0
    assert accounts["adaptive_v2_shadow"]["turnover_count"] == 1.0
    assert accounts["full_system"]["latest_equity"] == 100_000.0
    assert first["adaptive_v2_diagnostics"]["paper_target_exposure"] == 0.8


def test_stock_paper_cycle_queues_next_open_orders_and_uses_stock_accounts(tmp_path):
    settings = Settings(
        values={
            "system": {
                "database_path": "paper.db",
                "data_dir": "data",
                "initial_capital": 100_000.0,
            },
            "risk": {
                "max_total_exposure": 0.8,
                "max_single_position": 0.15,
                "minimum_order_value": 1_000.0,
            },
            "costs": {
                "stock": {
                    "commission_rate": 0.00025,
                    "minimum_commission": 5.0,
                    "stamp_duty_rate": 0.0005,
                    "transfer_fee_rate": 0.00001,
                    "slippage_bps": 10.0,
                    "stop_slippage_bps": 25.0,
                    "trade_lot": 100,
                },
                "etf": {
                    "commission_rate": 0.0001,
                    "minimum_commission": 5.0,
                    "stamp_duty_rate": 0.0,
                    "transfer_fee_rate": 0.0,
                    "slippage_bps": 5.0,
                    "stop_slippage_bps": 15.0,
                    "trade_lot": 100,
                },
            },
        },
        root=tmp_path,
    )
    dates = pd.bdate_range("2023-10-02", periods=360)
    bars = []
    for symbol, drift in (("sh600001", 0.001), ("sz000002", 0.0004)):
        price = 15.0
        for timestamp in dates:
            price *= 1 + drift
            bars.append(
                {
                    "symbol": symbol,
                    "date": timestamp.date(),
                    "open": price * 0.999,
                    "high": price * 1.01,
                    "low": price * 0.99,
                    "close": price,
                    "adjusted_close": price,
                    "volume": 5_000_000,
                    "amount": 100_000_000,
                    "source": "fixture",
                }
            )
    signal_date = dates[300].date()
    fill_date = dates[301].date()

    first = paper_workflow.run_stock_paper_cycle(
        settings,
        ["600001", "000002"],
        signal_date,
        top_n=1,
        bars=bars,
    )
    assert first["run_type"] == "stock"
    assert first["fills"] == []
    assert any(order["account_id"] == "stock_top_rank_shadow" for order in first["queued_orders"])
    assert not any(
        order["account_id"] == "stock_full_system_shadow" for order in first["queued_orders"]
    )

    second = paper_workflow.run_stock_paper_cycle(
        settings,
        ["600001", "000002"],
        fill_date,
        top_n=1,
        bars=bars,
    )
    assert second["fills"]
    assert all(fill["trade_date"] == fill_date.isoformat() for fill in second["fills"])
    account_ids = {item["account_id"] for item in second["scorecard"]["accounts"]}
    assert {
        "stock_radar_equal_weight",
        "stock_top_rank_shadow",
        "stock_full_system_shadow",
    }.issubset(account_ids)
