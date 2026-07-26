from __future__ import annotations

import asyncio
import importlib
import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from quantlab.api.app import app
from quantlab.config import Settings


api_module = importlib.import_module("quantlab.api.app")
quote_module = importlib.import_module("quantlab.market.quotes")
simulator_module = importlib.import_module("quantlab.workflows.simulator")


def _settings(tmp_path) -> Settings:
    return Settings(
        values={
            "system": {
                "database_path": "quantlab.db",
                "data_dir": "data",
                "initial_capital": 100_000.0,
                "test_mode": True,
            },
            "risk": {
                "max_total_exposure": 0.80,
                "max_single_position": 0.15,
                "max_industry_exposure": 0.30,
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
            "strategies": {"etf_rotation": {"universe": []}},
        },
        root=tmp_path,
    )


def _request(
    method: str,
    path: str,
    *,
    payload=None,
    headers=None,
    client=("127.0.0.1", 12345),
):
    async def request():
        transport = httpx.ASGITransport(app=app, client=client)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as session:
            return await session.request(
                method,
                path,
                json=payload,
                headers=headers,
            )

    return asyncio.run(request())


def _quote(price=10.0, *, observed: datetime | None = None):
    observed = observed or datetime.now(UTC)
    return {
        "symbol": "sh600001",
        "name": "测试股票",
        "asset_type": "stock",
        "raw_price": price,
        "as_of": observed.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(),
        "available_at": observed.isoformat(),
        "source": "fixture",
        "industry": "制造",
        "trade_lot": 100,
        "t_plus_one": True,
        "session_status": "open",
        "risk_metadata": {
            "risk_check_complete": True,
            "financial_check_complete": True,
            "financial_quality_score": 0.8,
            "listing_days": 1000,
        },
    }


def _install_test_quote(monkeypatch, price=10.0, *, observed: datetime | None = None):
    monkeypatch.setenv("QUANTLAB_ENABLE_TEST_QUOTES", "1")
    response = _request(
        "POST",
        "/internal/test/quotes",
        payload=_quote(price, observed=observed),
    )
    assert response.status_code == 200
    assert response.json()["authoritative"] is False


def _confirmation(check: dict, *, source: str = "simulator_api_test") -> dict:
    return {
        "confirmed": True,
        "check_id": check["check_id"],
        "account_id": check["account_id"],
        "symbol": check["symbol"],
        "side": check["side"],
        "quantity": check["requested_quantity"],
        "source": source,
        "simulation_mode": "intraday_simulation",
        "close_reference_acknowledged": False,
    }


def test_simulator_account_pretrade_order_settlement_and_performance_api(
    tmp_path,
    monkeypatch,
):
    fixed_now = datetime(2026, 7, 24, 2, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    monkeypatch.setattr(quote_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(simulator_module, "datetime", FrozenDateTime)
    settings = _settings(tmp_path)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)
    _install_test_quote(monkeypatch, observed=fixed_now)
    created = _request(
        "POST",
        "/api/simulator/accounts",
        payload={
            "name": "API模拟账户",
            "initial_capital": 100000,
            "benchmark_symbol": "sh000300",
            "idempotency_key": "api-account-0001",
        },
    )
    assert created.status_code == 200
    account_id = created.json()["account_id"]
    check = _request(
        "POST",
        "/api/simulator/pretrade-check",
        payload={
            "account_id": account_id,
            "symbol": "sh600001",
            "side": "buy",
            "quantity": 100,
        },
    )
    assert check.status_code == 200
    assert check.json()["hard_risk_passed"] is True
    order = _request(
        "POST",
        "/api/simulator/orders",
        payload={
            "check_id": check.json()["check_id"],
            "quantity": 100,
            "idempotency_key": "api-order-0001",
            "user_confirmation": _confirmation(check.json()),
        },
    )
    assert order.status_code == 200
    settled = _request(
        "POST",
        f"/api/simulator/orders/{order.json()['order_id']}/settle",
        payload={"fill_key": "api-fill-0001"},
    )
    assert settled.status_code == 200
    assert settled.json()["order"]["status"] == "filled"
    positions = _request(
        "GET",
        f"/api/simulator/accounts/{account_id}/positions",
    )
    assert positions.json()["positions"][0]["quantity"] == 100
    performance = _request(
        "GET",
        f"/api/simulator/accounts/{account_id}/performance",
    )
    assert performance.status_code == 200
    assert performance.json()["account_type"] == "user_paper"
    assert performance.json()["evidence_eligible"] is False


def test_order_confirmation_requires_explicit_matching_user_decision(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)
    _install_test_quote(monkeypatch)
    account = _request(
        "POST",
        "/api/simulator/accounts",
        payload={
            "name": "确认边界账户",
            "initial_capital": 100000,
            "benchmark_symbol": "sh000300",
            "idempotency_key": "confirmation-boundary-account",
        },
    ).json()
    check = _request(
        "POST",
        "/api/simulator/pretrade-check",
        payload={
            "account_id": account["account_id"],
            "symbol": "sh600001",
            "side": "buy",
            "quantity": 100,
        },
    ).json()
    base = {
        "check_id": check["check_id"],
        "quantity": 100,
        "idempotency_key": "confirmation-boundary-order",
    }
    assert _request("POST", "/api/simulator/orders", payload=base).status_code == 422
    missing_mode_confirmation = _confirmation(check)
    missing_mode_confirmation.pop("simulation_mode")
    assert _request(
        "POST",
        "/api/simulator/orders",
        payload={**base, "user_confirmation": missing_mode_confirmation},
    ).status_code == 422
    false_confirmation = _confirmation(check)
    false_confirmation["confirmed"] = False
    assert _request(
        "POST",
        "/api/simulator/orders",
        payload={**base, "user_confirmation": false_confirmation},
    ).status_code == 422
    for field, value in (
        ("check_id", "different-check"),
        ("account_id", "different-account"),
        ("symbol", "sz000001"),
        ("side", "sell"),
        ("quantity", 200),
    ):
        forged = _confirmation(check)
        forged[field] = value
        response = _request(
            "POST",
            "/api/simulator/orders",
            payload={**base, "user_confirmation": forged},
        )
        assert response.status_code in {409, 422}
    assert _request(
        "POST",
        "/api/simulator/orders",
        payload={**base, "user_confirmation": _confirmation(check)},
    ).status_code == 200


def test_notification_get_endpoints_do_not_consume_outbox(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)
    path = tmp_path / "quantlab.db"
    api_module._notifications().emit(
        event_type="paper_order_submitted",
        aggregate_type="user_paper_order",
        aggregate_id="read-only-get-order",
        payload={
            "account_id": "read-only-account",
            "order_id": "read-only-get-order",
            "symbol": "sh600001",
            "content": "pending outbox must not be consumed by GET",
        },
        dedup_key="read-only-get-notification",
        defer=True,
    )

    def counts() -> tuple[int, int, int]:
        with sqlite3.connect(path) as db:
            return (
                db.execute(
                    "SELECT COUNT(*) FROM notification_outbox WHERE status='pending'"
                ).fetchone()[0],
                db.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0],
            )

    before = counts()
    listed = _request("GET", "/api/notifications")
    unread = _request("GET", "/api/notifications/unread-count")
    assert listed.status_code == 200
    assert unread.status_code == 200
    assert listed.json()["notifications"] == []
    assert unread.json()["unread_count"] == 0
    assert counts() == before
    processed = api_module._notifications().process_outbox()
    assert processed["processed"] == 1
    assert counts() == (0, 1, 1)


def test_chat_and_notification_api_requires_independent_confirmation(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)
    _install_test_quote(monkeypatch)
    account = _request(
        "POST",
        "/api/simulator/accounts",
        payload={
            "name": "Chat API账户",
            "idempotency_key": "api-account-0002",
        },
    ).json()
    conversation = _request(
        "POST",
        "/api/chat/conversations",
        payload={
            "title": "Chat",
            "account_id": account["account_id"],
            "symbol": "sh600001",
            "idempotency_key": "api-chat-0001",
        },
    )
    assert conversation.status_code == 200
    response = _request(
        "POST",
        f"/api/chat/conversations/{conversation.json()['conversation_id']}/messages",
        payload={
            "content": "帮我模拟买入100股",
        },
    )
    assert response.status_code == 200
    action = response.json()["actions"][0]
    orders_before = _request(
        "GET",
        f"/api/simulator/accounts/{account['account_id']}/orders",
    )
    assert orders_before.json()["orders"] == []
    missing_mode = _request(
        "POST",
        f"/api/chat/actions/{action['action_id']}/confirm",
        payload={"quantity": 100},
    )
    assert missing_mode.status_code == 409
    confirmation = _request(
        "POST",
        f"/api/chat/actions/{action['action_id']}/confirm",
        payload={"quantity": 100, "simulation_mode": "intraday_simulation"},
    )
    assert confirmation.status_code == 200
    assert confirmation.json()["status"] == "confirmed"
    notifications = _request(
        "GET",
        f"/api/notifications?account_id={account['account_id']}",
    )
    assert notifications.status_code == 200
    assert notifications.json()["notifications"]


def test_remote_write_is_blocked_without_token_and_allowed_with_token(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)
    remote = ("203.0.113.10", 4321)
    rejected = _request(
        "POST",
        "/api/simulator/accounts",
        payload={
            "name": "Remote",
            "idempotency_key": "remote-account-0001",
        },
        client=remote,
    )
    assert rejected.status_code == 403
    assert "loopback client or API token" in rejected.json()["detail"]

    monkeypatch.setenv("QUANTLAB_API_TOKEN", "remote-test-token")
    accepted = _request(
        "POST",
        "/api/simulator/accounts",
        payload={
            "name": "Remote",
            "idempotency_key": "remote-account-0002",
        },
        headers={"X-QuantLab-Token": "remote-test-token"},
        client=remote,
    )
    assert accepted.status_code == 200
    assert "remote-test-token" not in accepted.text


def test_simulator_api_validation_and_error_detail_are_safe(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)
    invalid = _request(
        "POST",
        "/api/simulator/pretrade-check",
        payload={
            "account_id": "missing",
            "symbol": "sh600001",
            "side": "buy",
            "quantity": 100,
            "amount": 1000,
            "quote": _quote(),
        },
    )
    assert invalid.status_code == 422
    missing = _request("GET", "/api/simulator/accounts/not-found")
    assert missing.status_code == 404
    assert "E:\\" not in missing.text
