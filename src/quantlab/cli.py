from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from quantlab.config import Settings
from quantlab.domain import ResearchProvenance
from quantlab.data import WestockProvider
from quantlab.demo import run_demo
from quantlab.learning import LearningRepository
from quantlab.llm import build_provider, provider_configuration_summary, run_llm_replay
from quantlab.persistence import (
    DecisionRepository,
    HistoricalReplayRepository,
    StockRankingReplayRepository,
)
from quantlab.reporting import (
    audit_package_json,
    build_stored_audit_package,
    prepare_historical_replay_export,
    render_research_markdown,
    render_historical_replay_markdown,
    research_persistence_context,
)
from quantlab.stock_reporting import render_stock_ranking_replay_markdown
from quantlab.runtime.operations import (
    backup_database,
    restore_database,
    restore_database_dry_run,
    verify_database_backup,
)
from quantlab.runtime.autostart import RuntimeAutostartManager
from quantlab.persistence.migrations import initialize_or_upgrade_database
from quantlab.runtime.scheduler import RuntimeScheduler
from quantlab.runtime.service import RuntimeServiceController, run_runtime_component
from quantlab.runtime.soak import capture_soak_observation, soak_report
from quantlab.runtime.worker import JobWorker
from quantlab.workflows import (
    STOCK_DISCOVERY_STYLES,
    ADAPTIVE_ETF_CANDIDATES,
    analyze_symbol,
    bootstrap_learning_history,
    build_evidence_summary,
    build_decision_gate_scorecard,
    build_market_radar,
    build_today_brief,
    candidate_tournament_scorecard,
    collect_all_events,
    export_profitability_evidence,
    generate_portfolio_plan,
    learning_status,
    load_quant_report,
    paper_scorecard,
    run_etf_workflow,
    run_etf_variant_research,
    run_etf_strategy_robustness_audit,
    run_etf_v3_candidate_audit,
    run_adaptive_etf_candidate_lab,
    run_etf_core_protocol_validation,
    run_etf_walk_forward,
    run_learning_cycle,
    run_daily_cycle,
    run_candidate_tournament,
    run_historical_blind_replay,
    run_paper_cycle,
    run_stock_paper_cycle,
    run_stock_ranking_replay,
    run_market_wide_stock_replay,
    run_a_share_strategy_lab,
    render_a_share_strategy_lab_markdown,
    freeze_a_share_locked_holdout_policy,
    evaluate_a_share_locked_holdout,
    run_a_share_strategy_lab_v2,
    freeze_a_share_v2_locked_holdout_policy,
    evaluate_a_share_v2_locked_holdout,
    run_a_share_strategy_lab_v3_development,
    run_a_share_strategy_lab_v3_validation,
    render_a_share_strategy_lab_v3_markdown,
    freeze_a_share_v3_locked_holdout_policy,
    evaluate_a_share_v3_locked_holdout,
    refresh_a_share_security_master,
    capture_point_in_time_universe,
    run_stock_research_batch,
    render_decision_gate_scorecard_markdown,
    scan_convertible_bonds,
    scan_reversal,
    recommend_stocks,
    screen_selected_stocks,
    search_stocks,
    settle_candidate_tournaments,
    settle_forecasts,
    strategy_budgets,
    train_learning_models,
)


def _configure_utf8_stdio() -> None:
    if sys.platform != "win32":
        return
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


_configure_utf8_stdio()
app = typer.Typer(help="QuantLab personal multi-agent decision system")
console = Console()


@app.command()
def doctor(config: Path | None = None):
    """Check runtime, data tools and LLM configuration."""
    settings = Settings.load(config)
    checks = []
    checks.append(("Python", sys.version.split()[0], sys.version_info >= (3, 11)))
    westock = WestockProvider(settings.root.parent)
    try:
        node = westock.doctor()
        checks.append(("Node for westock", str(node["version"]), bool(node["compatible"])))
    except Exception as exc:
        checks.append(("Node for westock", str(exc), False))
    llm_summary = provider_configuration_summary(settings.section("llm"))
    checks.append(
        (
            "LLM provider",
            f"{llm_summary['provider']} ({llm_summary['endpoint_count']} endpoints)",
            llm_summary["endpoint_count"] > 0,
        )
    )
    table = Table("Component", "Value", "Status")
    for name, value, ok in checks:
        table.add_row(name, value, "OK" if ok else "FAIL")
    console.print(table)
    if not all(item[2] for item in checks):
        raise typer.Exit(1)


@app.command("llm-status")
def llm_status(config: Path | None = None):
    """Show configured provider pool and circuit health without exposing API keys."""
    settings = Settings.load(config)
    summary = provider_configuration_summary(settings.section("llm"))
    provider = build_provider(settings.section("llm"))
    console.print_json(data={"configuration": summary, "health": provider.health_snapshot()})


@app.command("llm-replay")
def llm_replay(
    suite: str = "smoke",
    runs: int = 1,
    save: bool = True,
    config: Path | None = None,
):
    """Replay fixed non-sensitive cases to measure schema reliability, latency and tokens."""
    settings = Settings.load(config)
    console.print_json(data=run_llm_replay(settings, suite, runs, save))


@app.command()
def demo(config: Path | None = None, save: bool = True):
    """Run an explicit synthetic-data end-to-end demo."""
    settings = Settings.load(config)
    output = run_demo(settings)
    backtest = output["backtest"]
    run = output["decision_run"]
    console.print("[bold]Demo backtest[/bold]")
    console.print(json.dumps(backtest.metrics, indent=2, ensure_ascii=False))
    console.print("[bold]Multi-agent decision[/bold]")
    console.print_json(data=run.decision.model_dump(mode="json"))
    if save:
        repository = DecisionRepository(settings.resolve(settings.get("system.database_path")))
        repository.save(
            run,
            provenance=ResearchProvenance(
                origin="demo_research", evidence_stage="demo"
            ),
        )
        console.print(f"Saved run {run.run_id}")


@app.command("recent-decisions")
def recent_decisions(config: Path | None = None, limit: int = 20):
    settings = Settings.load(config)
    rows = DecisionRepository(settings.resolve(settings.get("system.database_path"))).recent(limit)
    console.print_json(data=rows)


@app.command("forecast-outcome")
def forecast_outcome(
    run_id: str,
    horizon_days: int,
    realized_return_pct: float,
    evaluated_at: str | None = None,
    flat_threshold_pct: float = 1.0,
    config: Path | None = None,
):
    """Record a realized forecast outcome for calibration."""
    settings = Settings.load(config)
    repository = DecisionRepository(settings.resolve(settings.get("system.database_path")))
    outcome = repository.record_forecast_outcome(
        run_id,
        horizon_days,
        realized_return_pct,
        evaluated_at or date.today().isoformat(),
        flat_threshold_pct,
    )
    console.print_json(data=outcome.model_dump(mode="json"))


@app.command("forecast-calibration")
def forecast_calibration(
    model: str | None = None,
    horizon_days: int | None = None,
    minimum_samples: int = 30,
    config: Path | None = None,
):
    """Report multiclass Brier score and forecast hit rate."""
    settings = Settings.load(config)
    report = DecisionRepository(
        settings.resolve(settings.get("system.database_path"))
    ).calibration_report(model, horizon_days, minimum_samples)
    console.print_json(data=report.model_dump(mode="json"))


@app.command("forecast-settle")
def forecast_settle(as_of: str | None = None, config: Path | None = None):
    """Use market data to settle all matured 5/20 trading-day forecasts."""
    settings = Settings.load(config)
    output = settle_forecasts(settings, date.fromisoformat(as_of) if as_of else None)
    console.print_json(data=output)


@app.command("etf-backtest")
def etf_backtest(
    start: str = "2023-01-01",
    end: str | None = None,
    strategy_variant: str = "legacy",
    config: Path | None = None,
    save: bool = True,
):
    """Run the ETF rotation workflow with real free data and multi-agent analysis."""
    settings = Settings.load(config)
    output = run_etf_workflow(
        settings,
        date.fromisoformat(start),
        date.fromisoformat(end) if end else date.today(),
        strategy_variant,
        True,
    )
    console.print(f"Loaded {output['bars']} bars via {output['source']}")
    console.print(
        f"Requested start: {output['requested_start']} | "
        f"Effective common start: {output['effective_start']}"
    )
    console.print_json(data=output["coverage"])
    console.print_json(data=output["backtest"].metrics)
    console.print(
        f"Strategy variant: {output['strategy_variant']} | research_only={output['research_only']}"
    )
    run = output["decision_run"]
    if run:
        console.print_json(data=run.decision.model_dump(mode="json"))
        if save:
            DecisionRepository(settings.resolve(settings.get("system.database_path"))).save(
                run,
                provenance=ResearchProvenance(
                    origin="historical_research",
                    requested_as_of=end or date.today(),
                    evidence_stage="historical_replay",
                ),
            )


@app.command("etf-walk-forward")
def etf_walk_forward(
    start: str = "2018-01-01",
    end: str | None = None,
    train_days: int | None = None,
    test_days: int | None = None,
    save: bool = True,
    config: Path | None = None,
):
    """Run rolling out-of-sample ETF validation and parameter sensitivity analysis."""
    settings = Settings.load(config)
    output = run_etf_walk_forward(
        settings,
        date.fromisoformat(start),
        date.fromisoformat(end) if end else date.today(),
        train_days,
        test_days,
        save,
    )
    console.print_json(data=output)


@app.command("etf-core-validation")
def etf_core_validation_command(
    start: str = "2015-01-01",
    end: str | None = None,
    save: bool = True,
    config: Path | None = None,
):
    """Validate the frozen lot-aware ETF core production execution protocol."""

    output = run_etf_core_protocol_validation(
        Settings.load(config),
        date.fromisoformat(start),
        date.fromisoformat(end) if end else date.today(),
        save,
    )
    console.print_json(data=output)


@app.command("etf-variant-research")
def etf_variant_research_command(
    start: str = "2023-01-01",
    end: str | None = None,
    strategy_variant: str = "adaptive_v2",
    config: Path | None = None,
):
    """Compare an explicitly selected ETF strategy variant without invoking an LLM."""

    output = run_etf_variant_research(
        Settings.load(config),
        date.fromisoformat(start),
        date.fromisoformat(end) if end else date.today(),
        strategy_variant,
    )
    console.print_json(data=output)


@app.command("adaptive-etf-lab")
def adaptive_etf_lab_command(save: bool = True, config: Path | None = None):
    """Run the preregistered adaptive ETF development and locked-holdout experiment."""
    output = run_adaptive_etf_candidate_lab(Settings.load(config), save)
    console.print_json(
        data={
            "experiment_id": output["experiment_id"],
            "status": output["status"],
            "candidate_count": len(ADAPTIVE_ETF_CANDIDATES),
            "selected_candidate": output["selected_candidate"],
            "locked_holdout": output["locked_holdout"],
            "reports": output.get("reports"),
        }
    )


@app.command("strategy-robustness-audit")
def strategy_robustness_audit_command(
    save: bool = True,
    config: Path | None = None,
):
    """Run the preregistered multi-window ETF robustness and ablation audit."""

    output = run_etf_strategy_robustness_audit(Settings.load(config), save)
    console.print_json(
        data={
            "experiment_id": output["experiment_id"],
            "status": output["status"],
            "stability": output["stability"],
            "diagnosis": output["diagnosis"],
            "reports": output.get("reports"),
        }
    )


@app.command("adaptive-etf-v3-audit")
def adaptive_etf_v3_audit_command(
    save: bool = True,
    config: Path | None = None,
):
    """Run the preregistered V3-versus-V2 multi-window diagnostic."""

    output = run_etf_v3_candidate_audit(Settings.load(config), save)
    console.print_json(
        data={
            "experiment_id": output["experiment_id"],
            "status": output["status"],
            "v3_vs_v2": output["v3_vs_v2"],
            "exploratory_screen": output["exploratory_screen"],
            "reports": output.get("reports"),
        }
    )


@app.command("candidate-scan")
def candidate_scan(config: Path | None = None, reversal_limit: int = 20):
    """Scan current reversal and convertible-bond candidates."""
    settings = Settings.load(config)
    as_of = date.today()
    reversal = scan_reversal(settings, as_of, reversal_limit)
    bonds = scan_convertible_bonds(settings, as_of)
    console.print(
        "[bold]Dynamic strategy budgets (uncalibrated strategies receive low weight)[/bold]"
    )
    console.print_json(data=strategy_budgets(bool(reversal.signals), bool(bonds.signals)))
    console.print("[bold]Stock reversal candidates[/bold]")
    console.print_json(data=[item.model_dump(mode="json") for item in reversal.signals])
    console.print("[bold]Convertible-bond double-low candidates[/bold]")
    console.print_json(data=[item.model_dump(mode="json") for item in bonds.signals])
    degraded_sources = reversal.degraded_sources + bonds.degraded_sources
    if degraded_sources:
        console.print("[yellow]Degraded data sources[/yellow]")
        console.print_json(data=degraded_sources)


@app.command("stock-search")
def stock_search_command(
    keyword: str,
    limit: int = 20,
    config: Path | None = None,
):
    """Search A-share codes and names through the bundled free-data adapter."""
    console.print_json(data=search_stocks(Settings.load(config), keyword, limit))


@app.command("stock-screen")
def stock_screen_command(
    symbols: str,
    as_of: str | None = None,
    top_n: int = 10,
    max_correlation: float = 0.85,
    save: bool = True,
    config: Path | None = None,
):
    """Rank a user-selected A-share list without making LLM calls."""
    output = screen_selected_stocks(
        Settings.load(config),
        symbols,
        date.fromisoformat(as_of) if as_of else None,
        top_n=top_n,
        max_correlation=max_correlation,
        save=save,
    )
    console.print_json(data=output)


@app.command("stock-recommend")
def stock_recommend_command(
    styles: str = ",".join(STOCK_DISCOVERY_STYLES),
    candidate_limit: int = 30,
    top_n: int = 10,
    max_correlation: float = 0.85,
    save: bool = True,
    config: Path | None = None,
):
    """Discover current A-share research candidates across several transparent styles."""
    selected_styles = [item.strip() for item in styles.split(",") if item.strip()]
    output = recommend_stocks(
        Settings.load(config),
        styles=selected_styles,
        candidate_limit=candidate_limit,
        top_n=top_n,
        max_correlation=max_correlation,
        save=save,
    )
    console.print_json(data=output)


@app.command("stock-research-batch")
def stock_research_batch_command(
    symbols: str,
    as_of: str | None = None,
    include_events: bool = True,
    save: bool = True,
    config: Path | None = None,
):
    """Run full multi-agent and investment-master research for up to five stocks."""
    output = run_stock_research_batch(
        Settings.load(config),
        symbols,
        date.fromisoformat(as_of) if as_of else None,
        include_events=include_events,
        save=save,
    )
    console.print_json(data=output)


@app.command("stock-ranking-replay")
def stock_ranking_replay_command(
    symbols: str,
    start: str,
    end: str,
    horizon_days: int = 20,
    episodes: int = 12,
    top_k: int = 3,
    max_correlation: float = 0.85,
    save: bool = True,
    record_learning_samples: bool = True,
    config: Path | None = None,
):
    """Replay point-in-time A-share ranking against momentum, equal-weight and HS300."""
    output = run_stock_ranking_replay(
        Settings.load(config),
        symbols,
        date.fromisoformat(start),
        date.fromisoformat(end),
        horizon_days=horizon_days,
        episodes=episodes,
        top_k=top_k,
        max_correlation=max_correlation,
        save=save,
        record_learning_samples=record_learning_samples,
    )
    console.print_json(
        data={
            "replay_id": output.get("replay_id"),
            "completed_episodes": output["completed_episodes"],
            "evidence_status": output["evidence_status"],
            "metrics": output["metrics"],
            "paired_comparisons": output["paired_comparisons"],
            "claim_boundary": output["claim_boundary"],
        }
    )


@app.command("stock-ranking-replays")
def stock_ranking_replays_command(limit: int = 20, config: Path | None = None):
    """List saved A-share ranking replays."""
    settings = Settings.load(config)
    console.print_json(
        data=StockRankingReplayRepository(
            settings.resolve(settings.get("system.database_path"))
        ).list(limit)
    )


@app.command("stock-ranking-replay-report")
def stock_ranking_replay_report_command(
    replay_id: int,
    output: Path,
    config: Path | None = None,
):
    """Export one saved A-share ranking replay as Markdown or sanitized JSON."""
    settings = Settings.load(config)
    record = StockRankingReplayRepository(
        settings.resolve(settings.get("system.database_path"))
    ).get(replay_id)
    if record is None:
        raise typer.BadParameter("stock ranking replay not found")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".json":
        content = audit_package_json(record["payload"])
    elif output.suffix.lower() in {".md", ".markdown"}:
        content = render_stock_ranking_replay_markdown(record["payload"])
    else:
        raise typer.BadParameter("output extension must be .md or .json")
    output.write_text(content, encoding="utf-8")
    console.print(f"Wrote {output}")


@app.command("stock-master-refresh")
def stock_master_refresh_command(config: Path | None = None):
    """Refresh the versioned Shanghai/Shenzhen active and delisted security master."""
    console.print_json(data=refresh_a_share_security_master(Settings.load(config)))


@app.command("stock-universe-snapshot")
def stock_universe_snapshot_command(
    snapshot_date: str,
    force: bool = False,
    config: Path | None = None,
):
    """Capture the actual A-share universe and trading state on one historical date."""
    output = capture_point_in_time_universe(
        Settings.load(config),
        date.fromisoformat(snapshot_date),
        force=force,
    )
    console.print_json(data={key: value for key, value in output.items() if key != "records"})


@app.command("stock-market-replay")
def stock_market_replay_command(
    start: str,
    end: str,
    horizon_days: int = 5,
    episodes: int = 12,
    sample_size: int = 60,
    top_k: int = 3,
    save: bool = True,
    record_learning_samples: bool = True,
    ranking_policy: Path | None = None,
    config: Path | None = None,
):
    """Replay ranking on point-in-time market samples including future delistings."""
    policy = (
        json.loads(ranking_policy.read_text(encoding="utf-8")) if ranking_policy is not None else None
    )
    output = run_market_wide_stock_replay(
        Settings.load(config),
        date.fromisoformat(start),
        date.fromisoformat(end),
        horizon_days=horizon_days,
        episodes=episodes,
        sample_size=sample_size,
        top_k=top_k,
        save=save,
        record_learning_samples=record_learning_samples,
        ranking_policy=policy,
        progress_callback=lambda item: console.print(
            f"Market replay {item['completed']}/{item['requested']} · "
            f"{item['signal_date']} · market={item['full_market_securities']} · "
            f"eligible={item['eligible_candidates']} · top={item['top_ranked_symbol']}"
        ),
    )
    console.print_json(
        data={
            "replay_id": output.get("replay_id"),
            "completed_episodes": output["completed_episodes"],
            "evidence_status": output["evidence_status"],
            "evidence_qualification": output["evidence_qualification"],
            "strategy_admission": output["strategy_admission"],
            "survivorship_audit": output["survivorship_audit"],
            "metrics": output["metrics"],
            "paired_comparisons": output["paired_comparisons"],
            "claim_boundary": output["claim_boundary"],
        }
    )


@app.command("stock-strategy-lab")
def stock_strategy_lab_command(
    output: Path = Path("data/reports/a-share-strategy-lab-v1.json"),
    config: Path | None = None,
):
    """Run the preregistered A-share cross-sectional development and validation lab."""
    result = run_a_share_strategy_lab(Settings.load(config), output_path=output)
    markdown = output.with_suffix(".md")
    markdown.write_text(render_a_share_strategy_lab_markdown(result), encoding="utf-8")
    console.print_json(
        data={
            "status": result["status"],
            "selected_candidate": result["selected_candidate"],
            "locked_holdout_ready": result["locked_holdout_ready"],
            "validation_admission": (
                result["validation_result"]["admission"]
                if result["validation_result"]
                else None
            ),
            "json_report": str(output),
            "markdown_report": str(markdown),
        }
    )


@app.command("stock-strategy-freeze")
def stock_strategy_freeze_command(
    lab_report: Path = Path("data/reports/a-share-strategy-lab-v1.json"),
    output: Path = Path("data/reports/a-share-holdout-policy-v1.json"),
    config: Path | None = None,
):
    """Freeze the validation winner before opening the 2026 locked holdout."""
    lab_result = json.loads(lab_report.read_text(encoding="utf-8"))
    policy = freeze_a_share_locked_holdout_policy(
        Settings.load(config), lab_result, output_path=output
    )
    console.print_json(
        data={
            "policy_hash": policy["policy_hash"],
            "selected_candidate": policy["governance"]["selected_candidate"],
            "training_end": policy["governance"]["training_end"],
            "output": str(output),
        }
    )


@app.command("stock-strategy-holdout-score")
def stock_strategy_holdout_score_command(
    replay_id: int,
    lab_report: Path = Path("data/reports/a-share-strategy-lab-v1.json"),
    output: Path = Path("data/reports/a-share-strategy-holdout-v1.json"),
    config: Path | None = None,
):
    """Score the single permitted 2026 locked-holdout replay."""
    settings = Settings.load(config)
    record = StockRankingReplayRepository(
        settings.resolve(settings.get("system.database_path"))
    ).get(replay_id)
    if record is None:
        raise typer.BadParameter("stock ranking replay not found")
    lab_result = json.loads(lab_report.read_text(encoding="utf-8"))
    result = evaluate_a_share_locked_holdout(lab_result, record["payload"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print_json(data={**result, "output": str(output)})


@app.command("stock-strategy-lab-v2")
def stock_strategy_lab_v2_command(
    output: Path = Path("data/reports/a-share-strategy-lab-v2.json"),
    config: Path | None = None,
):
    """Run the frozen 20-day A-share defensive-reversal validation lab."""
    result = run_a_share_strategy_lab_v2(Settings.load(config), output_path=output)
    console.print_json(
        data={
            "status": result["status"],
            "selected_candidate": result["selected_candidate"],
            "locked_holdout_ready": result["locked_holdout_ready"],
            "development_admission": result["development_results"][
                result["selected_candidate"]
            ]["development_admission"],
            "validation_admission": (
                result["validation_result"]["admission"]
                if result["validation_result"]
                else None
            ),
            "json_report": str(output),
        }
    )


@app.command("stock-strategy-freeze-v2")
def stock_strategy_freeze_v2_command(
    lab_report: Path = Path("data/reports/a-share-strategy-lab-v2.json"),
    output: Path = Path("data/reports/a-share-holdout-policy-v2.json"),
):
    """Freeze the V2 validation policy before opening the 2026 holdout."""
    lab_result = json.loads(lab_report.read_text(encoding="utf-8"))
    policy = freeze_a_share_v2_locked_holdout_policy(lab_result, output_path=output)
    console.print_json(
        data={
            "policy_hash": policy["policy_hash"],
            "selected_candidate": policy["governance"]["selected_candidate"],
            "holding_horizon_days": policy["governance"]["holding_horizon_days"],
            "output": str(output),
        }
    )


@app.command("stock-strategy-holdout-score-v2")
def stock_strategy_holdout_score_v2_command(
    replay_id: int,
    lab_report: Path = Path("data/reports/a-share-strategy-lab-v2.json"),
    output: Path = Path("data/reports/a-share-strategy-holdout-v2.json"),
    config: Path | None = None,
):
    """Score the single permitted V2 2026 locked-holdout replay."""
    settings = Settings.load(config)
    record = StockRankingReplayRepository(
        settings.resolve(settings.get("system.database_path"))
    ).get(replay_id)
    if record is None:
        raise typer.BadParameter("stock ranking replay not found")
    lab_result = json.loads(lab_report.read_text(encoding="utf-8"))
    result = evaluate_a_share_v2_locked_holdout(lab_result, record["payload"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print_json(data={**result, "output": str(output)})


@app.command("stock-strategy-lab-v3-development")
def stock_strategy_lab_v3_development_command(
    output: Path = Path("data/reports/a-share-strategy-lab-v3-development.json"),
    markdown: Path = Path("data/reports/a-share-strategy-lab-v3-development.md"),
    config: Path | None = None,
):
    """Run only the frozen 2018-2022 V3 development stage."""
    result = run_a_share_strategy_lab_v3_development(
        Settings.load(config), output_path=output
    )
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text(render_a_share_strategy_lab_v3_markdown(result), encoding="utf-8")
    console.print_json(
        data={
            "status": result["status"],
            "protocol_hash": result["protocol_hash"],
            "policy_hash": result["policy_hash"],
            "development_admission": result["development_result"]["admission"],
            "validation_opened": result["validation_opened"],
            "json_report": str(output),
            "markdown_report": str(markdown),
        }
    )


@app.command("stock-strategy-lab-v3-validation")
def stock_strategy_lab_v3_validation_command(
    development_report: Path = Path(
        "data/reports/a-share-strategy-lab-v3-development.json"
    ),
    output: Path = Path("data/reports/a-share-strategy-lab-v3-validation.json"),
    markdown: Path = Path("data/reports/a-share-strategy-lab-v3-validation.md"),
    config: Path | None = None,
):
    """Open the single frozen 2023-2025 V3 validation after development passes."""
    development = json.loads(development_report.read_text(encoding="utf-8"))
    result = run_a_share_strategy_lab_v3_validation(
        Settings.load(config), development, output_path=output
    )
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text(render_a_share_strategy_lab_v3_markdown(result), encoding="utf-8")
    console.print_json(
        data={
            "status": result["status"],
            "protocol_hash": result["protocol_hash"],
            "policy_hash": result["policy_hash"],
            "validation_admission": result["validation_result"]["admission"],
            "locked_holdout_ready": result["locked_holdout_ready"],
            "json_report": str(output),
            "markdown_report": str(markdown),
        }
    )


@app.command("stock-strategy-freeze-v3")
def stock_strategy_freeze_v3_command(
    validation_report: Path = Path(
        "data/reports/a-share-strategy-lab-v3-validation.json"
    ),
    output: Path = Path("data/reports/a-share-holdout-policy-v3.json"),
):
    """Freeze the validated dynamic V3 policy before opening the 2026 holdout."""
    validation = json.loads(validation_report.read_text(encoding="utf-8"))
    policy = freeze_a_share_v3_locked_holdout_policy(validation, output_path=output)
    console.print_json(
        data={
            "policy_hash": policy["policy_hash"],
            "protocol_hash": policy["governance"]["protocol_hash"],
            "holding_horizon_days": policy["governance"]["holding_horizon_days"],
            "output": str(output),
        }
    )


@app.command("stock-strategy-holdout-score-v3")
def stock_strategy_holdout_score_v3_command(
    replay_id: int,
    validation_report: Path = Path(
        "data/reports/a-share-strategy-lab-v3-validation.json"
    ),
    output: Path = Path("data/reports/a-share-strategy-holdout-v3.json"),
    config: Path | None = None,
):
    """Score the single permitted V3 2026 locked-holdout replay."""
    settings = Settings.load(config)
    record = StockRankingReplayRepository(
        settings.resolve(settings.get("system.database_path"))
    ).get(replay_id)
    if record is None:
        raise typer.BadParameter("stock ranking replay not found")
    validation = json.loads(validation_report.read_text(encoding="utf-8"))
    result = evaluate_a_share_v3_locked_holdout(validation, record["payload"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print_json(data={**result, "output": str(output)})


@app.command("portfolio-plan")
def portfolio_plan(
    as_of: str | None = None,
    reversal_limit: int = 10,
    skip_stock_risk_checks: bool = False,
    etf_policy: str = "evidence_first",
    allow_unvalidated_stock: bool = False,
    save: bool = True,
    config: Path | None = None,
):
    """Build an auditable sell-first manual order list across all enabled strategies."""
    settings = Settings.load(config)
    output = generate_portfolio_plan(
        settings,
        date.fromisoformat(as_of) if as_of else None,
        reversal_limit,
        not skip_stock_risk_checks,
        save,
        etf_policy,
        allow_unvalidated_stock,
    )
    console.print_json(data=output)


@app.command("quant-report")
def quant_report(symbol: str, as_of: str | None = None, config: Path | None = None):
    """Generate the IronQ-inspired auditable factor report for one symbol."""
    settings = Settings.load(config)
    output = load_quant_report(settings, symbol, date.fromisoformat(as_of) if as_of else None)
    console.print_json(data=output["report"].model_dump(mode="json"))
    if output["degraded_sources"]:
        console.print_json(data={"degraded_sources": output["degraded_sources"]})


@app.command("analyze-symbol")
def analyze_symbol_command(
    symbol: str,
    as_of: str | None = None,
    config: Path | None = None,
    asset_type: str | None = None,
    include_events: bool = True,
    save: bool = True,
):
    """Run factor analysis plus the routed multi-agent investment committee."""
    settings = Settings.load(config)
    if asset_type not in (None, "stock", "etf"):
        raise typer.BadParameter("asset_type must be stock or etf")
    output = analyze_symbol(
        settings,
        symbol,
        date.fromisoformat(as_of) if as_of else None,
        asset_type=asset_type,
        include_events=include_events,
    )
    run = output["decision_run"]
    console.print_json(data=output["report"].model_dump(mode="json"))
    if output["financial_report"]:
        console.print_json(data=output["financial_report"].model_dump(mode="json"))
    if output["financial_degraded_sources"]:
        console.print_json(data={"degraded_sources": output["financial_degraded_sources"]})
    console.print_json(data=run.reports["council"].model_dump(mode="json"))
    console.print_json(data=run.decision.model_dump(mode="json"))
    if save:
        repository = DecisionRepository(settings.resolve(settings.get("system.database_path")))
        repository.save(
            run,
            research_persistence_context(output),
            provenance=ResearchProvenance(
                origin="user_interactive_research",
                requested_as_of=as_of or date.today(),
                evidence_stage="research_only",
            ),
        )
        console.print(f"Saved run {run.run_id}")


@app.command("market-radar")
def market_radar_command(
    as_of: str | None = None,
    include_sectors: bool = False,
    sector_limit: int = 15,
    config: Path | None = None,
):
    """Build a real-data cross-asset ETF radar and optional industry heat snapshot."""
    settings = Settings.load(config)
    output = build_market_radar(
        settings,
        date.fromisoformat(as_of) if as_of else None,
        include_sectors,
        sector_limit,
    )
    console.print_json(data=output)


@app.command("candidate-tournament")
def candidate_tournament_command(
    as_of: str | None = None,
    candidate_limit: int = 3,
    shortlist_size: int = 2,
    max_correlation: float = 0.80,
    save: bool = True,
    config: Path | None = None,
):
    """Run the same multi-agent committee across several ETFs and rank the survivors."""
    settings = Settings.load(config)
    console.print(
        f"[bold]Candidate tournament[/bold] · estimated role calls: {candidate_limit * 11}"
    )
    output = run_candidate_tournament(
        settings,
        date.fromisoformat(as_of) if as_of else None,
        candidate_limit=candidate_limit,
        shortlist_size=shortlist_size,
        max_correlation=max_correlation,
        save=save,
    )
    table = Table(title="Multi-agent candidate ranking")
    for column in ("Rank", "Symbol", "Name", "Score", "Action", "Review", "Diversification"):
        table.add_column(column)
    for item in output["candidates"]:
        table.add_row(
            str(item.get("tournament_rank", "-")),
            item["symbol"],
            item.get("name", item["symbol"]),
            f"{float(item.get('tournament_score', 0)):.2f}",
            item.get("action", "review_required"),
            "pass" if item.get("review_eligible") else "blocked",
            item.get("diversification_status", "-"),
        )
    console.print(table)
    historical = output["stress_test"]["historical_risk"]
    console.print_json(
        data={
            "tournament_id": output.get("tournament_id"),
            "diversified_shortlist": [item["symbol"] for item in output["diversified_shortlist"]],
            "comparison_portfolio": output["comparison_portfolio"],
            "worst_scenario": output["stress_test"]["worst_scenario"],
            "historical_risk": {
                "observations": historical["observations"],
                "one_day_var_95_pct": historical["one_day_var_95_pct"],
                "one_day_cvar_95_pct": historical["one_day_cvar_95_pct"],
                "annualized_volatility": historical["annualized_volatility"],
                "maximum_historical_drawdown": historical["maximum_historical_drawdown"],
                "missing_symbols": historical["missing_symbols"],
            },
        }
    )


@app.command("candidate-tournament-settle")
def candidate_tournament_settle_command(
    as_of: str | None = None,
    limit: int = 20,
    config: Path | None = None,
):
    """Settle recorded tournament rankings against later 5/20-day adjusted closes."""
    settings = Settings.load(config)
    output = settle_candidate_tournaments(
        settings,
        date.fromisoformat(as_of) if as_of else None,
        limit=limit,
    )
    console.print_json(data=output)


@app.command("candidate-tournament-scorecard")
def candidate_tournament_scorecard_command(
    limit: int = 100,
    config: Path | None = None,
):
    """Show whether recorded multi-agent rankings added value after settlement."""
    console.print_json(data=candidate_tournament_scorecard(Settings.load(config), limit))


@app.command("research-report")
def research_report_command(
    run_id: str,
    format: str = "markdown",
    output: Path | None = None,
    config: Path | None = None,
):
    """Export a persisted research run as Markdown or JSON without secrets or full prompts."""
    if format not in {"markdown", "json"}:
        raise typer.BadParameter("format must be markdown or json")
    settings = Settings.load(config)
    record = DecisionRepository(settings.resolve(settings.get("system.database_path"))).get(run_id)
    if record is None:
        raise typer.BadParameter("research run not found")
    package = build_stored_audit_package(record)
    content = (
        render_research_markdown(package) if format == "markdown" else audit_package_json(package)
    )
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
        console.print(f"Saved {output}")
    else:
        console.print(content)


@app.command("evidence")
def evidence_command(asset_scope: str = "etf", config: Path | None = None):
    """Show benchmark, statistical-model and prospective LLM ablation evidence."""
    if asset_scope not in {"etf", "stock", "convertible_bond"}:
        raise typer.BadParameter("invalid asset_scope")
    console.print_json(data=build_evidence_summary(Settings.load(config), asset_scope))


@app.command("evidence-report")
def evidence_report_command(
    asset_scope: str = "etf",
    output_dir: Path | None = None,
    config: Path | None = None,
):
    """Export the machine-graded profitability evidence as reviewable JSON and Markdown."""
    if asset_scope not in {"etf", "stock", "convertible_bond"}:
        raise typer.BadParameter("invalid asset_scope")
    console.print_json(
        data=export_profitability_evidence(Settings.load(config), asset_scope, output_dir)
    )


@app.command("paper-cycle")
def paper_cycle_command(
    as_of: str | None = None,
    run_research: bool = False,
    research_limit: int = 1,
    config: Path | None = None,
):
    """Run the prospective shadow portfolios and queue next-open paper orders."""
    output = run_paper_cycle(
        Settings.load(config),
        date.fromisoformat(as_of) if as_of else None,
        run_research,
        research_limit,
    )
    console.print_json(
        data={
            "as_of": output["as_of"],
            "fills": output["fills"],
            "queued_orders": output["queued_orders"],
            "warnings": output["warnings"],
            "degraded_sources": output["degraded_sources"],
            "scorecard": output["scorecard"],
        }
    )


@app.command("paper-scorecard")
def paper_scorecard_command(config: Path | None = None):
    """Show immutable prospective account curves and risk metrics."""
    console.print_json(data=paper_scorecard(Settings.load(config)))


@app.command("stock-paper-cycle")
def stock_paper_cycle_command(
    symbols: str,
    as_of: str | None = None,
    top_n: int = 3,
    max_correlation: float = 0.85,
    run_research: bool = False,
    research_limit: int = 2,
    config: Path | None = None,
):
    """Run A-share equal-weight, ranked and multi-agent prospective shadow accounts."""
    output = run_stock_paper_cycle(
        Settings.load(config),
        symbols,
        date.fromisoformat(as_of) if as_of else None,
        top_n=top_n,
        max_correlation=max_correlation,
        run_research=run_research,
        research_limit=research_limit,
    )
    console.print_json(data=output)


@app.command("today")
def today_command(as_of: str | None = None, config: Path | None = None):
    """Build the investor-facing daily command-center payload."""
    console.print_json(
        data=build_today_brief(Settings.load(config), date.fromisoformat(as_of) if as_of else None)
    )


@app.command("daily-cycle")
def daily_cycle_command(
    as_of: str | None = None,
    run_research: bool = False,
    config: Path | None = None,
):
    """Run paper trading, forecast learning and the daily investor brief."""
    output = run_daily_cycle(
        Settings.load(config),
        date.fromisoformat(as_of) if as_of else None,
        run_research,
    )
    console.print_json(data=output)


@app.command("historical-replay")
def historical_replay_command(
    start: str,
    end: str,
    horizon_days: int = 20,
    episodes: int = 3,
    save: bool = True,
    allow_large_run: bool = False,
    config: Path | None = None,
):
    """Run anonymized point-in-time LLM predictions and next-open paper trades."""
    output = run_historical_blind_replay(
        Settings.load(config),
        date.fromisoformat(start),
        date.fromisoformat(end),
        horizon_days=horizon_days,
        episodes=episodes,
        save=save,
        allow_large_run=allow_large_run,
        progress_callback=lambda item: console.print(
            "Replay "
            f"{item['completed']}/{item['requested']} completed: "
            f"{item['actual_as_of']} {item['action']} "
            f"review={item['reviewer_approved']} veto={item['council_veto']}"
        ),
    )
    console.print_json(
        data={
            "replay_id": output.get("replay_id"),
            "requested_range": output["requested_range"],
            "completed_episodes": output["completed_episodes"],
            "metrics": output["metrics"],
            "llm_validation": output["llm_validation"],
            "evidence_status": output["evidence_status"],
            "claim_boundary": output["claim_boundary"],
        }
    )


@app.command("historical-replays")
def historical_replays_command(limit: int = 20, config: Path | None = None):
    settings = Settings.load(config)
    console.print_json(
        data=HistoricalReplayRepository(
            settings.resolve(settings.get("system.database_path"))
        ).list(limit)
    )


@app.command("historical-replay-report")
def historical_replay_report_command(
    replay_id: int,
    output: Path | None = None,
    config: Path | None = None,
):
    settings = Settings.load(config)
    record = HistoricalReplayRepository(settings.resolve(settings.get("system.database_path"))).get(
        replay_id
    )
    if record is None:
        raise typer.BadParameter("historical replay not found")
    content = (
        audit_package_json(prepare_historical_replay_export(record["payload"]))
        if output and output.suffix.lower() == ".json"
        else render_historical_replay_markdown(record["payload"])
    )
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
        console.print(f"Saved {output}")
    else:
        console.print(content)


@app.command("decision-gate-scorecard")
def decision_gate_scorecard_command(
    replay_ids: str = "5,7,8,9",
    output_prefix: Path = Path("data/reports/decision-gate-scorecard-latest"),
    config: Path | None = None,
):
    """Aggregate frozen ETF gate experiments without mixing forecast horizons."""
    parsed_ids = [int(item.strip()) for item in replay_ids.split(",") if item.strip()]
    scorecard = build_decision_gate_scorecard(Settings.load(config), parsed_ids)
    json_path = output_prefix.with_suffix(".json")
    markdown_path = output_prefix.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(audit_package_json(scorecard), encoding="utf-8")
    markdown_path.write_text(render_decision_gate_scorecard_markdown(scorecard), encoding="utf-8")
    console.print_json(
        data={
            "json": str(json_path),
            "markdown": str(markdown_path),
            "promotion_status": scorecard["promotion_status"],
            "promotion_checks": scorecard["promotion_checks"],
        }
    )


@app.command("learning-bootstrap")
def learning_bootstrap(
    start: str = "2020-01-01",
    end: str | None = None,
    symbols: str | None = None,
    step: int = 5,
    asset_scope: str = "etf",
    config: Path | None = None,
):
    """Build leakage-safe historical factor samples for 5/20-day learning."""
    settings = Settings.load(config)
    output = bootstrap_learning_history(
        settings,
        date.fromisoformat(start),
        date.fromisoformat(end) if end else date.today(),
        [item.strip() for item in symbols.split(",")] if symbols else None,
        step,
        asset_scope,
    )
    console.print_json(data=output)


@app.command("learning-train")
def learning_train(
    horizon_days: int | None = None,
    asset_scope: str = "etf",
    force: bool = False,
    config: Path | None = None,
):
    """Train and register candidate probability models using chronological validation."""
    settings = Settings.load(config)
    console.print_json(data=train_learning_models(settings, horizon_days, asset_scope, force))


@app.command("learning-status")
def learning_status_command(config: Path | None = None, full_history: bool = False):
    settings = Settings.load(config)
    console.print_json(data=learning_status(settings, include_model_history=full_history))


@app.command("learning-event")
def learning_event(
    symbol: str,
    event_date: str,
    event_type: str,
    title: str,
    source: str,
    sentiment: float = 0.0,
    impact_score: float = 0.5,
    config: Path | None = None,
):
    """Record a dated news, financial or regulatory event for later attribution."""
    settings = Settings.load(config)
    event_id = LearningRepository(settings.resolve(settings.get("system.database_path"))).add_event(
        symbol,
        date.fromisoformat(event_date),
        event_type,
        title,
        source,
        sentiment,
        impact_score,
    )
    console.print_json(data={"status": "recorded", "event_id": event_id})


@app.command("learning-events")
def learning_events(
    symbol: str | None = None,
    limit: int = 50,
    config: Path | None = None,
):
    """List recorded news, financial and regulatory events."""
    settings = Settings.load(config)
    rows = LearningRepository(settings.resolve(settings.get("system.database_path"))).recent_events(
        symbol, limit
    )
    console.print_json(data=rows)


@app.command("learning-collect-events")
def learning_collect_events(
    symbol: str,
    start: str,
    end: str | None = None,
    config: Path | None = None,
):
    """Collect recent news and structured notices for forecast-error attribution."""
    settings = Settings.load(config)
    console.print_json(
        data=collect_all_events(
            settings,
            symbol,
            date.fromisoformat(start),
            date.fromisoformat(end) if end else date.today(),
        )
    )


@app.command("learning-cycle")
def learning_cycle(as_of: str | None = None, config: Path | None = None):
    """Settle forecasts, attribute errors, and retrain eligible scoped models."""
    settings = Settings.load(config)
    console.print_json(
        data=run_learning_cycle(settings, date.fromisoformat(as_of) if as_of else None)
    )


@app.command("worker")
def worker_command(
    worker_id: str = "quantlab-worker-1",
    once: bool = False,
    maximum_jobs: int = 100,
    poll_seconds: float = 1.0,
    config: Path | None = None,
):
    """Run the durable background worker with heartbeat, retry and crash recovery."""
    settings = Settings.load(config)
    worker = JobWorker(settings, worker_id=worker_id)
    if once:
        console.print_json(data={"jobs": worker.run_until_empty(maximum_jobs)})
        return
    console.print(f"QuantLab worker {worker_id} started; Ctrl+C to stop.")
    try:
        while True:
            result = worker.run_once()
            if result is not None:
                console.print_json(
                    data={
                        "job_id": result["job_id"],
                        "job_type": result["job_type"],
                        "status": result["status"],
                    }
                )
            else:
                time.sleep(max(0.1, min(30.0, poll_seconds)))
    except KeyboardInterrupt:
        console.print("Worker stopped.")


@app.command("scheduler-run")
def scheduler_run_command(
    run_date: str | None = None,
    backfill: bool = False,
    config: Path | None = None,
):
    """Submit the idempotent daily dependency graph."""
    settings = Settings.load(config)
    scheduler = RuntimeScheduler(settings)
    resolved_date = date.fromisoformat(run_date) if run_date else None
    output = (
        scheduler.backfill(resolved_date)
        if backfill and resolved_date
        else scheduler.tick(run_date=resolved_date)
    )
    console.print_json(data=output)


@app.command("runtime-component", hidden=True)
def runtime_component_command(
    component: str,
    config: Path | None = None,
):
    """Run one managed API/Worker/Scheduler/notification component."""
    console.print_json(
        data=run_runtime_component(Settings.load(config), component)
    )


@app.command("runtime-start")
def runtime_start_command(config: Path | None = None):
    """Start the four duplicate-safe local Windows runtime processes."""
    settings = Settings.load(config)
    console.print_json(
        data=RuntimeServiceController(settings, config_path=config).start()
    )


@app.command("runtime-stop")
def runtime_stop_command(
    grace_seconds: float = 15.0,
    config: Path | None = None,
):
    """Request cooperative shutdown, then signal processes that exceed the grace period."""
    settings = Settings.load(config)
    console.print_json(
        data=RuntimeServiceController(settings, config_path=config).stop(
            grace_seconds=grace_seconds
        )
    )


@app.command("runtime-status")
def runtime_status_command(config: Path | None = None):
    """Report database, data, process, schedule, notification, backup and experiment health."""
    settings = Settings.load(config)
    console.print_json(
        data=RuntimeServiceController(settings, config_path=config).status()
    )


@app.command("runtime-autostart-install")
def runtime_autostart_install_command(config: Path | None = None):
    """Explicitly install the per-user Windows Task Scheduler startup entry."""
    settings = Settings.load(config)
    console.print_json(
        data=RuntimeAutostartManager(settings, config_path=config).install()
    )


@app.command("runtime-autostart-status")
def runtime_autostart_status_command(config: Path | None = None):
    """Query the Windows Task Scheduler startup entry without changing it."""
    settings = Settings.load(config)
    console.print_json(
        data=RuntimeAutostartManager(settings, config_path=config).status()
    )


@app.command("runtime-autostart-disable")
def runtime_autostart_disable_command(config: Path | None = None):
    """Disable the Windows Task Scheduler startup entry."""
    settings = Settings.load(config)
    console.print_json(
        data=RuntimeAutostartManager(settings, config_path=config).disable()
    )


@app.command("runtime-autostart-remove")
def runtime_autostart_remove_command(config: Path | None = None):
    """Remove the Windows Task Scheduler startup entry and generated launcher."""
    settings = Settings.load(config)
    console.print_json(
        data=RuntimeAutostartManager(settings, config_path=config).remove()
    )


@app.command("runtime-soak-observe")
def runtime_soak_observe_command(config: Path | None = None):
    """Persist one bounded continuous-runtime observation."""
    console.print_json(data=capture_soak_observation(Settings.load(config), source="cli"))


@app.command("runtime-soak-report")
def runtime_soak_report_command(config: Path | None = None):
    """Report only the actual stored continuous-runtime observation interval."""
    console.print_json(data=soak_report(Settings.load(config)))


@app.command("database-backup")
def database_backup_command(
    label: str = "manual",
    config: Path | None = None,
):
    """Create an integrity-checkable SQLite online backup."""
    console.print_json(data=backup_database(Settings.load(config), label=label))


@app.command("database-migrate")
def database_migrate_command(config: Path | None = None):
    """Initialize a new database or apply registered component upgrades in order."""
    settings = Settings.load(config)
    console.print_json(
        data=initialize_or_upgrade_database(
            settings.resolve(settings.get("system.database_path"))
        )
    )


@app.command("database-backup-verify")
def database_backup_verify_command(
    backup_path: Path,
    expected_sha256: str | None = None,
    config: Path | None = None,
):
    """Verify checksum and SQLite integrity without modifying any database."""
    console.print_json(
        data=verify_database_backup(
            Settings.load(config),
            backup_path=backup_path,
            expected_sha256=expected_sha256,
        )
    )


@app.command("database-restore-dry-run")
def database_restore_dry_run_command(
    backup_path: Path,
    expected_sha256: str | None = None,
    config: Path | None = None,
):
    """Validate restore and migrations against a disposable database copy."""
    console.print_json(
        data=restore_database_dry_run(
            Settings.load(config),
            backup_path=backup_path,
            expected_sha256=expected_sha256,
        )
    )


@app.command("database-restore")
def database_restore_command(
    backup_path: Path,
    expected_sha256: str,
    confirm: bool = False,
    config: Path | None = None,
):
    """Restore a verified backup while Workers are stopped in maintenance mode."""
    console.print_json(
        data=restore_database(
            Settings.load(config),
            backup_path=backup_path,
            expected_sha256=expected_sha256,
            confirm=confirm,
            maintenance_mode=True,
        )
    )


if __name__ == "__main__":
    app()
