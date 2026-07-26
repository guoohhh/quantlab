from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, date, datetime

import httpx

from quantlab.api.app import app
from quantlab.config import Settings
from quantlab.market import TradingCalendarService
from quantlab.runtime.notification_delivery import EmailChannelAdapter


api_module = importlib.import_module("quantlab.api.app")


def _settings(tmp_path, *, rate_limit: int = 300) -> Settings:
    return Settings(
        values={
            "system": {"database_path": "quantlab.db", "data_dir": "data"},
            "runtime": {
                "api_requests_per_minute": rate_limit,
                "backup_directory": "data/backups",
            },
            "llm": {"task_cost_budget_usd": 1.0},
            "risk": {"max_single_position": 0.15},
        },
        root=tmp_path,
    )


def _request(method: str, path: str, payload=None):
    async def request():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, json=payload)

    return asyncio.run(request())


def test_job_runtime_and_smoothed_rebalance_api(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "_settings", lambda: _settings(tmp_path))
    created = _request(
        "POST",
        "/api/jobs",
        {
            "job_type": "notification_dispatch",
            "payload": {"limit": 10},
            "idempotency_key": "api-job-once",
        },
    )
    assert created.status_code == 200
    job_id = created.json()["job_id"]
    assert _request("GET", f"/api/jobs/{job_id}").json()["status"] == "queued"
    cancelled = _request(
        "POST", f"/api/jobs/{job_id}/cancel", {"reason": "test cancellation"}
    )
    assert cancelled.json()["status"] == "cancelled"

    rebalance = _request(
        "POST",
        "/api/portfolio/smoothed-rebalance",
        {
            "nav": 100000,
            "available_cash": 100000,
            "current_quantities": {},
            "desired_weights": {"sh510300": 0.5},
            "prices": {"sh510300": 10.0},
        },
    )
    assert rebalance.status_code == 200
    assert rebalance.json()["cash_reserve_satisfied"] is True


def test_point_in_time_etf_api_and_channel_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "_settings", lambda: _settings(tmp_path))
    cutoff = datetime(2026, 7, 17, 7, tzinfo=UTC).isoformat()
    response = _request(
        "POST",
        "/api/point-in-time/etf-pools",
        {
            "snapshot_date": "2026-07-17",
            "cutoff_at": cutoff,
            "source_version": "v1",
            "master_records": [
                {
                    "symbol": "sh510300",
                    "security_type": "etf",
                    "exchange": "sh",
                    "listing_date": "2012-05-28",
                    "asset_class": "equity",
                    "category": "large_equity",
                    "source": "licensed",
                    "source_version": "v1",
                    "available_at": "2012-05-28T00:00:00+00:00",
                }
            ],
            "trade_statuses": [
                {
                    "symbol": "sh510300",
                    "trade_date": "2026-07-17",
                    "trade_status": True,
                    "amount": 100000000,
                    "fund_size": 1000000000,
                    "source": "licensed",
                    "methodology": "official",
                    "available_at": cutoff,
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["members"][0]["representative"] is True
    assert response.json()["namespace"] == "research"
    assert response.json()["trust_level"] == "user_imported"

    missing = _request(
        "PUT",
        "/api/notifications/channels",
        {"channel": "email", "enabled": True, "config": {}},
    )
    assert missing.status_code == 422
    disabled = _request(
        "PUT",
        "/api/notifications/channels",
        {"channel": "email", "enabled": False, "config": {}},
    )
    assert disabled.status_code == 200


def test_email_channel_status_and_test_endpoint_are_redacted_and_async(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(api_module, "_settings", lambda: _settings(tmp_path))
    monkeypatch.setenv("QUANTLAB_TEST_SMTP_PASSWORD", "smtp-secret-must-not-leak")

    def fail_if_smtp_is_called(*_args, **_kwargs):
        raise AssertionError("test endpoint must not connect to SMTP inline")

    monkeypatch.setattr(EmailChannelAdapter, "send", fail_if_smtp_is_called)

    not_ready = _request("POST", "/api/notifications/channels/email/test", {})
    assert not_ready.status_code == 422
    assert "smtp-secret-must-not-leak" not in not_ready.text

    configured = _request(
        "PUT",
        "/api/notifications/channels",
        {
            "channel": "email",
            "enabled": True,
            "config": {
                "smtp_host": "smtp.example.com",
                "from_address": "from@example.com",
                "to_address": "to@example.com",
                "username": "quantlab",
                "password_env": "QUANTLAB_TEST_SMTP_PASSWORD",
            },
        },
    )
    assert configured.status_code == 200

    channels = _request("GET", "/api/notifications/channels")
    assert channels.status_code == 200
    assert channels.json()["email_status"]["state"] == "ready"
    assert "smtp-secret-must-not-leak" not in channels.text

    queued = _request("POST", "/api/notifications/channels/email/test", {})
    assert queued.status_code == 200
    assert queued.json()["status"] == "queued"
    assert "smtp-secret-must-not-leak" not in queued.text

    status = _request("GET", "/api/notifications/channels").json()["email_status"]
    assert status["state"] == "queued"
    assert status["latest_delivery"]["attempts"] == 0


def test_round5_public_writes_cannot_create_formal_calendar_or_select_registration_day(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)
    uploaded = _request(
        "POST",
        "/api/runtime/trading-calendar",
        {
            "items": [
                {
                    "trade_date": "2020-01-02",
                    "is_open": True,
                    "source": "user-upload",
                    "available_at": "2020-01-01T00:00:00+00:00",
                }
            ]
        },
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["manifest"]["namespace"] == "research"
    assert uploaded.json()["manifest"]["trust_level"] == "user_imported"
    try:
        TradingCalendarService.from_settings(settings).day(
            date(2020, 1, 2), formal=True
        )
    except ValueError as exc:
        assert "trusted production" in str(exc)
    else:
        raise AssertionError("public calendar upload entered the formal namespace")

    registration = _request(
        "POST",
        "/api/forward-experiments/registration-jobs?as_of=2020-01-02",
    )
    assert registration.status_code == 403
    assert "scheduler-only" in registration.json()["detail"]


def test_api_rate_limit_and_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "_settings", lambda: _settings(tmp_path, rate_limit=2))
    assert _request("GET", "/api/health").status_code == 200
    assert _request("GET", "/api/health").status_code == 200
    assert _request("GET", "/api/health").status_code == 429
