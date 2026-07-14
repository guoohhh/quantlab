from datetime import date

from quantlab.config import Settings
from quantlab.persistence import TerminalRepository
from quantlab.workflows import daily as daily_workflow

FAKE_KEY = "sk-" + "abcdefghijklmnop"


def test_daily_cycle_records_partial_failure_without_losing_other_steps(tmp_path, monkeypatch):
    settings = Settings(values={"system": {"database_path": "quantlab.db"}}, root=tmp_path)
    monkeypatch.setattr(
        daily_workflow,
        "run_paper_cycle",
        lambda *args, **kwargs: {"fills": [], "queued_orders": [{"id": 1}]},
    )

    def fail_learning(*args, **kwargs):
        raise RuntimeError(f"data source unavailable {FAKE_KEY}")

    monkeypatch.setattr(daily_workflow, "run_learning_cycle", fail_learning)
    monkeypatch.setattr(
        daily_workflow,
        "build_today_brief",
        lambda *args, **kwargs: {
            "headline": {"market_regime": "range", "risk_appetite": "neutral"}
        },
    )

    output = daily_workflow.run_daily_cycle(settings, date(2026, 1, 2))

    assert output["status"] == "degraded"
    assert output["steps"]["paper_trading"]["status"] == "ok"
    assert output["steps"]["learning"]["status"] == "error"
    assert FAKE_KEY not in str(output)
    assert "[REDACTED_API_KEY]" in output["steps"]["learning"]["error"]
    assert output["summary"]["paper_orders"] == 1
    assert (
        TerminalRepository(tmp_path / "quantlab.db").scheduler_status(1)[0]["status"] == "degraded"
    )
