from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, date, datetime, timedelta

import httpx

from quantlab.api.app import app
from quantlab.config import Settings
from quantlab.domain import (
    AnalysisContextPack,
    AssetType,
    EvidenceBlock,
    EvidenceDomain,
    EvidenceQuality,
)
from quantlab.persistence import EvidenceRepository
from quantlab.workflows.capital_flow import SIGNED_TURNOVER_METHODOLOGY


api_module = importlib.import_module("quantlab.api.app")


def _settings(tmp_path) -> Settings:
    return Settings(
        values={
            "system": {"database_path": "quantlab.db", "data_dir": "data"},
            "llm": {"role_minimum_matured_samples": 2},
            "risk": {
                "max_total_exposure": 0.8,
                "max_single_position": 0.15,
                "max_industry_exposure": 0.3,
            },
            "strategies": {"etf_rotation": {"universe": []}},
        },
        root=tmp_path,
    )


def _request(method: str, path: str, payload=None):
    async def request():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as session:
            return await session.request(method, path, json=payload)

    return asyncio.run(request())


def _pack() -> AnalysisContextPack:
    at = datetime(2026, 7, 17, 7, 0, tzinfo=UTC)
    blocks = [
        EvidenceBlock(
            block_id="api-market",
            domain=EvidenceDomain.MARKET,
            title="market",
            source="fixture",
            methodology="fixture",
            as_of=at,
            available_at=at,
            fetched_at=at,
            freshness="fresh",
            quality=EvidenceQuality.AVAILABLE,
            payload={"current_raw_price": 10},
        ),
        EvidenceBlock(
            block_id="api-technical",
            domain=EvidenceDomain.TECHNICAL,
            title="technical",
            source="fixture",
            methodology="fixture",
            as_of=at,
            available_at=at,
            fetched_at=at,
            freshness="fresh",
            quality=EvidenceQuality.AVAILABLE,
            payload={"trend": "up"},
        ),
    ]
    return AnalysisContextPack(
        context_id="api-context-1",
        symbol="sh600001",
        asset_type=AssetType.STOCK,
        as_of=date(2026, 7, 17),
        cutoff_at=datetime(2026, 7, 17, 15, 0, tzinfo=UTC),
        blocks=blocks,
    )


def _records(days: int):
    start = date(2026, 7, 1)
    return [
        {
            "symbol": "sh600001",
            "industry": "制造",
            "date": (start + timedelta(days=index)).isoformat(),
            "close": 10 + index * 0.1,
            "amount": 100_000 + index * 1_000,
            "source": "fixture",
            "methodology": SIGNED_TURNOVER_METHODOLOGY,
        }
        for index in range(days)
    ]


def test_context_flow_committee_and_notification_rule_api(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)
    pack = _pack()
    EvidenceRepository(tmp_path / "quantlab.db").save_context(pack)

    loaded = _request("GET", f"/api/context-packs/{pack.context_id}")
    assert loaded.status_code == 200
    assert loaded.json()["schema_version"] == "2.0"
    latest = _request("GET", "/api/context-packs/latest/sh600001")
    assert latest.json()["context_id"] == pack.context_id

    rule = _request(
        "POST",
        "/api/notification-rules",
        {
            "rule_type": "flow_positive_streak",
            "idempotency_key": "api-flow-rule-0001",
            "account_id": "account-1",
            "symbol": "sh600001",
            "threshold": 1,
            "consecutive_periods": 2,
            "cooldown_seconds": 3600,
        },
    )
    assert rule.status_code == 200
    rules = _request("GET", "/api/notification-rules?account_id=account-1")
    assert rules.json()["rules"][0]["rule_id"] == rule.json()["rule_id"]

    flow = _request(
        "POST",
        "/api/capital-flow/calculate",
        {
            "scope": "stock",
            "as_of": "2026-07-05",
            "source": "fixture",
            "methodology": SIGNED_TURNOVER_METHODOLOGY,
            "symbol": "sh600001",
            "records": _records(5),
            "save": True,
            "account_id": "account-1",
        },
    )
    assert flow.status_code == 200
    snapshot = flow.json()["flows"][0]["snapshot"]
    assert snapshot["payload"]["scope"] == "stock"
    stock = _request("GET", "/api/capital-flow/stocks/sh600001?as_of=2026-07-05")
    assert stock.status_code == 200
    assert stock.json()["fingerprint"] == snapshot["fingerprint"]

    monkeypatch.setattr(
        api_module,
        "run_context_committee",
        lambda *_args, **_kwargs: {
            "action": "review_required",
            "context_id": pack.context_id,
            "context_version": "2.0",
            "suggested_weight_max": 0,
        },
    )
    committee = _request(
        "POST",
        "/api/llm/context-committee",
        {
            "context_id": pack.context_id,
            "deterministic_max_weight": 0.15,
            "idempotency_key": "api-committee-0001",
        },
    )
    assert committee.status_code == 200
    assert committee.json()["context_version"] == "2.0"

    invalid = _request(
        "POST",
        "/api/capital-flow/calculate",
        {
            "scope": "stock",
            "as_of": "2026-07-05",
            "source": "fixture",
            "methodology": SIGNED_TURNOVER_METHODOLOGY,
            "symbol": "sh600001",
            "records": _records(5),
            "unexpected": True,
        },
    )
    assert invalid.status_code == 422


def test_role_governance_api_never_auto_promotes(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)
    for index in range(2):
        response = _request(
            "POST",
            "/api/llm/roles/observations",
            {
                "role": "capital_flow",
                "run_id": f"api-role-{index}",
                "symbol": "sh600001",
                "as_of": f"2026-07-{10 + index:02d}",
                "horizon_days": 5,
                "probabilities": {"up": 0.6, "flat": 0.2, "down": 0.2},
                "realized_direction": "up",
                "realized_return_pct": 1.5,
                "market_regime": "bull",
                "fact_errors": 0,
                "cost_usd": 0.01,
                "latency_ms": 100,
            },
        )
        assert response.status_code == 200
    scorecard = _request("GET", "/api/llm/roles/capital_flow/scorecard")
    assert scorecard.json()["stage"] == "frozen_challenge_required"
    assert scorecard.json()["automatic_weight_change_allowed"] is False
    challenge = _request("POST", "/api/llm/roles/capital_flow/challenges")
    assert challenge.status_code == 200
    decision = _request(
        "POST",
        f"/api/llm/challenges/{challenge.json()['challenge_id']}/decision",
        {
            "passed": False,
            "decision": "reject",
            "reason": "frozen challenge did not pass",
        },
    )
    assert decision.status_code == 200
    assert decision.json()["decision"] == "reject"
