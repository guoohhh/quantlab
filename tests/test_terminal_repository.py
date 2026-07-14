from datetime import date

import pytest

from quantlab.persistence import TerminalRepository


def test_watchlist_signal_and_alert_persistence(tmp_path):
    repository = TerminalRepository(tmp_path / "terminal.db")
    repository.upsert_watchlist("sh600519", "贵州茅台", "core", "quality watch")
    signal_id = repository.record_signal(
        "sh600519", "factor_momentum", -0.2, "watch", date(2026, 7, 10), {"source": "test"}
    )
    alert_id = repository.add_alert("sh600519", "price_below", 1100)

    assert repository.list_watchlist()[0]["group_name"] == "core"
    assert repository.latest_signals()[0]["id"] == signal_id
    assert repository.latest_signals()[0]["payload"] == {"source": "test"}
    assert repository.list_alerts()[0]["id"] == alert_id


def test_manual_trade_ledger_never_claims_broker_execution(tmp_path):
    repository = TerminalRepository(tmp_path / "terminal.db")
    repository.set_capital(100_000)
    repository.record_trade("sh600519", "buy", 100, 10, 5, date(2026, 1, 2))
    overview = repository.portfolio_overview()

    assert overview["cash"] == 98_995
    assert overview["positions"][0]["quantity"] == 100
    assert overview["positions"][0]["mark_source"] == "last_recorded_trade"
    with pytest.raises(ValueError, match="cannot sell"):
        repository.record_trade("sh600519", "sell", 200, 11, 5, date(2026, 1, 3))


def test_portfolio_plan_round_trip(tmp_path):
    repository = TerminalRepository(tmp_path / "terminal.db")
    plan_id = repository.save_portfolio_plan(
        date(2026, 7, 13),
        {"plan": {"manual_execution_only": True}, "managed_targets": {"sh510300": {}}},
    )

    latest = repository.latest_portfolio_plan()
    assert latest["plan_id"] == plan_id
    assert latest["plan"]["manual_execution_only"] is True
    assert repository.portfolio_plans(1)[0]["plan_id"] == plan_id


def test_strategy_validation_round_trip(tmp_path):
    repository = TerminalRepository(tmp_path / "terminal.db")
    validation_id = repository.save_strategy_validation(
        "etf_rotation",
        date(2023, 1, 1),
        date(2026, 1, 1),
        252,
        63,
        {"selected_oos": {"folds": 4, "mean_sharpe": 1.1}},
    )

    latest = repository.latest_strategy_validation("etf_rotation")
    assert latest["validation_id"] == validation_id
    assert latest["selected_oos"]["mean_sharpe"] == 1.1


def test_validation_consumption_is_atomic_and_one_shot(tmp_path):
    repository = TerminalRepository(tmp_path / "terminal.db")

    assert repository.claim_validation_once(
        "frozen-key",
        "protocol-hash",
        "validation",
        {"period": ["2023-01-01", "2025-12-31"]},
    )
    assert not repository.claim_validation_once(
        "frozen-key",
        "protocol-hash",
        "validation",
    )
    record = repository.validation_consumption("frozen-key")
    assert record is not None
    assert record["payload"]["period"] == ["2023-01-01", "2025-12-31"]


def test_llm_evaluation_round_trip(tmp_path):
    repository = TerminalRepository(tmp_path / "terminal.db")
    evaluation_id = repository.save_llm_evaluation("smoke", {"summary": {"success_rate": 1.0}})

    latest = repository.llm_evaluations(1)[0]
    assert latest["evaluation_id"] == evaluation_id
    assert latest["summary"]["success_rate"] == 1.0
