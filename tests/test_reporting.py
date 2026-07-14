import asyncio
from datetime import date

from quantlab.agents import MultiAgentDecisionSystem, ResearchContext
from quantlab.agents.schemas import AnalystReport
from quantlab.llm import MockLLMProvider
from quantlab.persistence import DecisionRepository
from quantlab.reporting import (
    audit_package_json,
    build_research_audit_package,
    build_stored_audit_package,
    render_research_markdown,
    research_persistence_context,
)

FAKE_KEY = "sk-" + "abcdefghijklmnop"


def _research_output():
    run = asyncio.run(
        MultiAgentDecisionSystem(MockLLMProvider()).run(
            ResearchContext(
                symbol="sh510300",
                as_of=date(2026, 1, 2),
                price=4.0,
                asset_type="etf",
            )
        )
    )
    run.llm_audit["api_key"] = "must-not-leak"
    return {
        "report": AnalystReport(
            stance="neutral",
            confidence=0.5,
            summary="factor report fixture",
        ),
        "financial_report": None,
        "financial_degraded_sources": [],
        "event_degraded_sources": [],
        "source": "test-cache",
        "bars": 300,
        "price": 4.0,
        "price_history": {
            "evidence_type": "market_price_history",
            "cutoff_date": "2026-01-02",
            "observations": 120,
        },
        "as_of": date(2026, 1, 2),
        "degraded_sources": [],
        "decision_run": run,
    }


def test_research_report_is_downloadable_and_sanitizes_secret_fields(tmp_path):
    output = _research_output()
    package = build_research_audit_package(output)
    package["agent_reports"]["quant"]["summary"] = f"echoed {FAKE_KEY}"
    package["agent_reports"]["reviewer"]["summary"] = f"echoed {FAKE_KEY}"
    markdown = render_research_markdown(package)
    json_output = audit_package_json(package)

    assert package["execution_boundary"] == "manual_orders_only"
    assert package["price_history"]["observations"] == 120
    assert "api_key" not in package["llm_audit"]
    assert "must-not-leak" not in markdown
    assert FAKE_KEY not in markdown
    assert FAKE_KEY not in json_output
    assert "[REDACTED_API_KEY]" in markdown
    assert "概率预测" in markdown
    assert output["decision_run"].run_id in markdown

    repository = DecisionRepository(tmp_path / "quantlab.db")
    repository.save(output["decision_run"], research_persistence_context(output))
    stored = build_stored_audit_package(repository.get(output["decision_run"].run_id))

    assert stored["factor_report"]["summary"] == "factor report fixture"
    assert stored["price_history"]["cutoff_date"] == "2026-01-02"
    assert stored["data"]["source"] == "test-cache"
    assert stored["run_id"] == output["decision_run"].run_id
