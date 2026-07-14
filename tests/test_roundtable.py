from __future__ import annotations

import asyncio
import importlib
import json

import httpx
import pytest

from quantlab.agents.roundtable import roundtable_participant_catalog
from quantlab.api.app import app
from quantlab.persistence import DecisionRepository, RoundtableRepository
from quantlab.workflows import run_expert_roundtable


api_module = importlib.import_module("quantlab.api.app")


def _seed_research_run(settings, run_id: str = "a" * 32) -> str:
    path = settings.resolve(settings.get("system.database_path"))
    repository = DecisionRepository(path)
    payload = {
        "research_context": {
            "asset_type": "stock",
            "price": 1500.0,
            "market_regime": "range",
            "quant_factors": {"composite_score": 0.25, "rsi_14": 52.0},
            "price_history": {"evidence_id": "price_history", "bars": 252},
            "fundamentals": {"roe": 0.28, "free_cash_flow": 100.0},
            "news": [{"title": "sample disclosed event", "source": "fixture"}],
            "api_key": "sk-this-must-never-reach-the-roundtable",
            "data_quality": 1.0,
            "degraded_sources": [],
        },
        "reports": {
            "bull": {"stance": "bullish", "thesis": ["pricing power"]},
            "bear": {"stance": "bearish", "thesis": ["valuation risk"]},
            "reviewer": {"status": "approved", "approved": True},
        },
        "forecasts": [{"horizon_days": 20, "up_probability": 0.45}],
        "decision": {"action": "hold", "target_weight": 0.0},
        "decision_trace": {"deterministic_action": "hold"},
    }
    with repository.connect() as db:
        db.execute(
            """
            INSERT INTO decision_runs(run_id,symbol,as_of,action,confidence,payload)
            VALUES(?,?,?,?,?,?)
            """,
            (
                run_id,
                "sh600519",
                "2026-07-13",
                "hold",
                0.62,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
    return run_id


def _post(path: str, payload: dict):
    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json=payload)

    return asyncio.run(request())


def test_roundtable_catalog_contains_investment_masters_and_adversarial_roles():
    catalog = {item["key"]: item for item in roundtable_participant_catalog()}

    assert {"buffett", "munger", "graham", "fisher", "lynch"} <= set(catalog)
    assert {"bull", "bear", "risk", "taleb", "damodaran"} <= set(catalog)
    assert catalog["buffett"]["label"] == "沃伦·巴菲特"


def test_roundtable_runs_multiple_rounds_on_frozen_research_and_persists(settings):
    source_run_id = _seed_research_run(settings)

    result = run_expert_roundtable(
        settings,
        source_run_id,
        ["buffett", "munger", "bear"],
        "What is most likely wrong with the source thesis?",
        rounds=2,
        save=True,
    )

    assert result["status"] == "completed"
    assert len(result["turns"]) == 6
    assert {turn["round_number"] for turn in result["turns"]} == {1, 2}
    assert result["source_snapshot"]["source_identity"]["formal_action"] == "hold"
    assert "api_key" not in result["source_snapshot"]["research_context"]
    assert result["synthesis"]["research_only"] is True
    assert result["synthesis"]["formal_decision_changed"] is False
    assert "does_not_modify" in result["execution_boundary"]

    repository = RoundtableRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    stored = repository.get(result["session_id"])
    assert stored is not None
    assert stored["source_run_id"] == source_run_id
    assert repository.recent(1)[0]["session_id"] == result["session_id"]


def test_roundtable_rejects_unknown_or_insufficient_participants(settings):
    source_run_id = _seed_research_run(settings)

    with pytest.raises(ValueError, match="at least two"):
        run_expert_roundtable(
            settings,
            source_run_id,
            ["buffett"],
            "Discuss the thesis",
            save=False,
        )
    with pytest.raises(ValueError, match="unknown roundtable participant"):
        run_expert_roundtable(
            settings,
            source_run_id,
            ["buffett", "unknown_master"],
            "Discuss the thesis",
            save=False,
        )


def test_roundtable_requires_an_existing_research_run(settings):
    with pytest.raises(ValueError, match="research run not found"):
        run_expert_roundtable(
            settings,
            "f" * 32,
            ["buffett", "munger"],
            "Discuss the thesis",
            save=False,
        )


def test_roundtable_api_exposes_catalog_and_execution(monkeypatch, settings):
    source_run_id = _seed_research_run(settings)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)

    response = _post(
        "/api/roundtables",
        {
            "source_run_id": source_run_id,
            "participants": ["buffett", "munger"],
            "topic": "Which evidence would change the conclusion?",
            "rounds": 1,
            "save": True,
        },
    )

    assert response.status_code == 200
    assert len(response.json()["turns"]) == 2
    assert response.json()["synthesis"]["formal_decision_changed"] is False
