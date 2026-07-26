from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import streamlit as st

from dashboard import product_ui
from dashboard.ui_foundation import cache_research_report, set_product_page
from quantlab.config import Settings


probe = Path(os.environ["QUANTLAB_SIMULATOR_PROBE_FILE"])
quote_mode = os.environ.get("QUANTLAB_SIMULATOR_PROBE_QUOTE_MODE", "intraday")


def record(value: str) -> None:
    values = probe.read_text(encoding="utf-8").splitlines() if probe.exists() else []
    values.append(value)
    probe.write_text("\n".join(values), encoding="utf-8")


class FakeRepository:
    def accounts(self, *, include_closed: bool = False):
        return [{"account_id": "account-1", "name": "测试账户", "status": "active"}]

    def overview(self, account_id: str):
        assert account_id == "account-1"
        return {
            "equity": 100_000.0,
            "available_cash": 100_000.0,
            "frozen_cash": 0.0,
            "today_pnl": 0.0,
            "realized_pnl": 0.0,
            "cumulative_fees": 0.0,
            "positions": [],
        }

    def orders(self, account_id: str, *, limit: int = 100):
        assert account_id == "account-1"
        return []

    def fills(self, account_id: str):
        assert account_id == "account-1"
        return []

    def snapshots(self, account_id: str):
        assert account_id == "account-1"
        return []

    def reviews(self, account_id: str):
        assert account_id == "account-1"
        return []


def fake_pretrade(
    _settings,
    *,
    account_id: str,
    symbol: str,
    side: str,
    quantity: int,
    research_run_id: str | None,
):
    record(
        f"PRETRADE|{account_id}|{symbol}|{side}|{quantity}|"
        f"{research_run_id or 'unlinked'}"
    )
    next_open = quote_mode == "next_open"
    return {
        "check_id": "check-1",
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "requested_quantity": quantity,
        "suggested_action": "buy",
        "suggested_quantity": quantity,
        "post_trade_cash": 98_990.0,
        "post_trade_single_weight": 0.01,
        "loss_if_symbol_down_10pct": 100.0,
        "reference_price": 10.0,
        "reference_time": "2026-07-19T07:00:00+00:00",
        "estimated_transaction_fees": 10.0,
        "quote": {
            "symbol": symbol,
            "name": "测试 ETF",
            "asset_type": "etf",
            "raw_price": 10.0,
            "as_of": "2026-07-19",
            "available_at": "2026-07-19T07:00:00+00:00",
            "source": "simulator_probe",
            "provider": "simulator_probe",
            "source_version": "v1",
            "data_quality": "available",
            "session_status": "closed" if next_open else "open",
            "quote_kind": "current_close" if next_open else "realtime",
            "authoritative": True,
            "actionable": not next_open,
        },
        "supporting_evidence": [],
        "opposing_evidence": [],
        "invalidation_conditions": [],
        "hard_failures": [],
        "warnings": [],
        "allowed_to_submit": True,
        "research_run_id": research_run_id,
        "research_link_status": "linked" if research_run_id else "unlinked",
    }


def fake_submit(
    _settings,
    *,
    check_id: str,
    quantity: int,
    idempotency_key: str,
    user_confirmation: dict,
):
    record(
        f"SUBMIT|{check_id}|{quantity}|{user_confirmation['symbol']}|"
        f"{user_confirmation['simulation_mode']}"
    )
    return {"order_id": "order-1", "status": "pending"}


if "simulator_probe_initialized" not in st.session_state:
    report = {
        "run_id": "run-simulator",
        "symbol": "sh510300",
        "as_of": "2026-07-17",
        "data": {
            "source": "probe",
            "bars": 120,
            "effective_as_of": "2026-07-17",
        },
    }
    cache_research_report(
        st.session_state,
        report,
        symbol="sh510300",
        requested_as_of="2026-07-19",
    )
    set_product_page(
        st.session_state,
        "组合与交易",
        symbol="sh510300",
        research_run_id="run-simulator",
        research_requested_as_of="2026-07-19",
        research_effective_as_of="2026-07-17",
        account_id="account-1",
    )
    st.session_state["simulator_probe_initialized"] = True

originals = {
    "user_simulator_repository": product_ui.user_simulator_repository,
    "run_pretrade_check": product_ui.run_pretrade_check,
    "submit_user_paper_order": product_ui.submit_user_paper_order,
    "record_product_usage": product_ui.record_product_usage,
}
try:
    product_ui.user_simulator_repository = lambda _settings: FakeRepository()
    product_ui.run_pretrade_check = fake_pretrade
    product_ui.submit_user_paper_order = fake_submit
    product_ui.record_product_usage = Mock()
    product_ui.render_simulator(
        Settings(
            values={"system": {"database_path": "simulator-probe.db"}},
            root=probe.parent,
        )
    )
finally:
    for name, value in originals.items():
        setattr(product_ui, name, value)
