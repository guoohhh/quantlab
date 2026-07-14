from __future__ import annotations

from datetime import date
from typing import Any, Callable

from quantlab.config import Settings
from quantlab.persistence import TerminalRepository
from quantlab.security import safe_error_detail
from quantlab.workflows.learning import run_learning_cycle
from quantlab.workflows.paper import run_paper_cycle, run_stock_paper_cycle
from quantlab.workflows.today import build_today_brief


def run_daily_cycle(
    settings: Settings,
    as_of: date | None = None,
    run_research: bool = False,
) -> dict[str, Any]:
    cycle_date = as_of or date.today()
    steps: dict[str, Any] = {}
    errors = []

    def execute(name: str, operation: Callable[[], Any]) -> Any:
        try:
            value = operation()
            steps[name] = {"status": "ok", "result": value}
            return value
        except Exception as exc:
            detail = safe_error_detail(exc)
            error = f"{name} failed: {detail}"
            errors.append(error)
            steps[name] = {"status": "error", "error": detail}
            return None

    paper = execute(
        "paper_trading",
        lambda: run_paper_cycle(settings, cycle_date, run_research, 1),
    )
    stock_paper = None
    if bool(settings.get("strategies.stock_evidence.daily_paper_enabled", False)):
        stock_paper = execute(
            "stock_paper_trading",
            lambda: run_stock_paper_cycle(
                settings,
                list(settings.get("strategies.stock_evidence.default_universe")),
                cycle_date,
                top_n=int(settings.get("strategies.stock_evidence.top_k", 3)),
                max_correlation=float(
                    settings.get("strategies.stock_evidence.max_correlation", 0.85)
                ),
                run_research=run_research,
                research_limit=1,
            ),
        )
    execute("learning", lambda: run_learning_cycle(settings, cycle_date))
    today = execute("today_brief", lambda: build_today_brief(settings, cycle_date))
    status = "ok" if not errors else "degraded"
    output = {
        "as_of": cycle_date.isoformat(),
        "status": status,
        "steps": steps,
        "errors": errors,
        "summary": {
            "paper_fills": len(paper.get("fills", [])) if paper else 0,
            "paper_orders": len(paper.get("queued_orders", [])) if paper else 0,
            "stock_paper_fills": len(stock_paper.get("fills", [])) if stock_paper else 0,
            "stock_paper_orders": (len(stock_paper.get("queued_orders", [])) if stock_paper else 0),
            "market_regime": (today.get("headline", {}).get("market_regime") if today else None),
            "risk_appetite": (today.get("headline", {}).get("risk_appetite") if today else None),
        },
    }
    terminal = TerminalRepository(settings.resolve(settings.get("system.database_path")))
    terminal.record_scheduler_run(
        "daily_cycle",
        status,
        {
            "as_of": output["as_of"],
            "summary": output["summary"],
            "errors": errors,
        },
    )
    return output
