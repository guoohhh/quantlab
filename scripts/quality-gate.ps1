$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Python environment not found: $python"
}

Push-Location $root
try {
    $workspaceTemp = Join-Path $root "data\quality-gate-temp"
    $resolvedRoot = [IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
    $resolvedWorkspaceTemp = [IO.Path]::GetFullPath($workspaceTemp)
    if (-not $resolvedWorkspaceTemp.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Quality-gate temp path escaped workspace: $resolvedWorkspaceTemp"
    }
    if (Test-Path -LiteralPath $resolvedWorkspaceTemp) {
        Remove-Item -LiteralPath $resolvedWorkspaceTemp -Recurse -Force -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Force -Path $workspaceTemp | Out-Null
    $env:TEMP = $workspaceTemp
    $env:TMP = $workspaceTemp
    $env:TMPDIR = $workspaceTemp

    & $python -m ruff check src tests dashboard
    if ($LASTEXITCODE -ne 0) { throw "Ruff failed" }

    & $python -m compileall -q src dashboard
    if ($LASTEXITCODE -ne 0) { throw "compileall failed" }

    & $python -m coverage erase
    if ($LASTEXITCODE -ne 0) { throw "coverage erase failed" }
    $testFiles = @(Get-ChildItem -Path (Join-Path $root "tests") -Filter "test_*.py" -File | Sort-Object Name)
    $shards = @(@(), @(), @())
    for ($index = 0; $index -lt $testFiles.Count; $index++) {
        $shards[$index % $shards.Count] += $testFiles[$index].FullName
    }
    $gateRunId = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffffffZ")
    for ($shardIndex = 0; $shardIndex -lt $shards.Count; $shardIndex++) {
        $shard = $shards[$shardIndex]
        $baseTemp = Join-Path $workspaceTemp "pytest-$gateRunId-$shardIndex"
        & $python -m coverage run --parallel-mode --source=quantlab -m pytest -q -p no:cacheprovider --basetemp $baseTemp @shard
        if ($LASTEXITCODE -ne 0) { throw "pytest coverage shard failed" }
    }
    & $python -m coverage combine
    if ($LASTEXITCODE -ne 0) { throw "coverage combine failed" }
    & $python -m coverage json -o data/reports/coverage.json
    if ($LASTEXITCODE -ne 0) { throw "coverage json generation failed" }
    & $python -m coverage report --fail-under=71
    if ($LASTEXITCODE -ne 0) { throw "pytest or total coverage gate failed" }

    @'
import json
from pathlib import Path

report = json.loads(Path("data/reports/coverage.json").read_text(encoding="utf-8"))
files = {name.replace("\\", "/"): item for name, item in report["files"].items()}
overall = float(report["totals"]["percent_covered"])
print(f"total coverage: {overall:.1f}% (minimum 71%)")
if overall + 1e-9 < 71:
    raise SystemExit(f"total coverage gate failed: {overall:.1f}%<71%")
thresholds = {
    "src/quantlab/agents/orchestrator.py": 90,
    "src/quantlab/agents/decision_gate.py": 90,
    "src/quantlab/api/app.py": 45,
    "src/quantlab/backtest/engine.py": 85,
    "src/quantlab/backtest/statistics.py": 85,
    "src/quantlab/data/akshare.py": 90,
    "src/quantlab/data/a_share_symbols.py": 95,
    "src/quantlab/data/baostock.py": 90,
    "src/quantlab/data/cache.py": 95,
    "src/quantlab/data/fallback.py": 75,
    "src/quantlab/data/quality.py": 90,
    "src/quantlab/data/provider_router.py": 80,
    "src/quantlab/factors/engine.py": 90,
    "src/quantlab/learning/trainer.py": 80,
    "src/quantlab/llm/providers.py": 70,
    "src/quantlab/market/calendar.py": 80,
    "src/quantlab/market/quotes.py": 80,
    "src/quantlab/portfolio/planner.py": 85,
    "src/quantlab/portfolio/smoothing.py": 85,
    "src/quantlab/persistence/jobs.py": 80,
    "src/quantlab/persistence/round5.py": 80,
    "src/quantlab/persistence/round6.py": 80,
    "src/quantlab/persistence/round7.py": 80,
    "src/quantlab/persistence/round8.py": 80,
    "src/quantlab/persistence/round9.py": 80,
    "src/quantlab/persistence/simulator.py": 80,
    "src/quantlab/persistence/strategy_evidence.py": 75,
    "src/quantlab/persistence/universe.py": 90,
    "src/quantlab/reporting.py": 90,
    "src/quantlab/risk/engine.py": 90,
    "src/quantlab/security.py": 90,
    "src/quantlab/strategies/adaptive_etf.py": 90,
    "src/quantlab/strategies/adaptive_etf_v2.py": 90,
    "src/quantlab/strategies/adaptive_etf_v3.py": 90,
    "src/quantlab/strategies/convertible_bond.py": 90,
    "src/quantlab/strategies/etf_rotation.py": 90,
    "src/quantlab/strategies/stock_reversal.py": 90,
    "src/quantlab/runtime/notification_delivery.py": 70,
    "src/quantlab/runtime/operations.py": 80,
    "src/quantlab/runtime/readiness.py": 80,
    "src/quantlab/runtime/scheduler.py": 85,
    "src/quantlab/runtime/service.py": 80,
    "src/quantlab/runtime/autostart.py": 80,
    "src/quantlab/runtime/soak.py": 80,
    "src/quantlab/runtime/summaries.py": 50,
    "src/quantlab/runtime/worker.py": 80,
    "src/quantlab/workflows/capital_flow.py": 80,
    "src/quantlab/workflows/chat.py": 80,
    "src/quantlab/workflows/chat_jobs.py": 85,
    "src/quantlab/workflows/context.py": 80,
    "src/quantlab/workflows/convertible_bond_evidence.py": 90,
    "src/quantlab/workflows/etf_point_in_time.py": 85,
    "src/quantlab/workflows/experiment_recorder.py": 80,
    "src/quantlab/workflows/forward_ablation.py": 80,
    "src/quantlab/workflows/forward_experiment.py": 80,
    "src/quantlab/workflows/investor_portfolio.py": 80,
    "src/quantlab/workflows/investment_thesis.py": 80,
    "src/quantlab/workflows/decision_lifecycle.py": 80,
    "src/quantlab/workflows/decision_tasks.py": 80,
    "src/quantlab/workflows/point_in_time.py": 75,
    "src/quantlab/workflows/product.py": 80,
    "src/quantlab/workflows/product_demo.py": 80,
    "src/quantlab/workflows/stock_strategy_lab_v4.py": 85,
    "src/quantlab/workflows/shadow_trading.py": 80,
    "src/quantlab/workflows/simulator.py": 80,
    "src/quantlab/workflows/trusted_data.py": 80,
    "src/quantlab/workflows/trusted_data_adapters.py": 80,
    "src/quantlab/workflows/paper.py": 75,
    "src/quantlab/workflows/replay.py": 90,
    "src/quantlab/workflows/reflection.py": 80,
    "src/quantlab/workflows/gate_scorecard.py": 80,
    "src/quantlab/workflows/tournament.py": 85,
    "src/quantlab/workflows/stock_discovery.py": 85,
    "src/quantlab/workflows/stock_market_replay.py": 85,
    "src/quantlab/workflows/universe.py": 80,
    "src/quantlab/workflows/strategy_lab.py": 70,
    "src/quantlab/stock_reporting.py": 80,
}
failed = []
for name, minimum in thresholds.items():
    actual = float(files[name]["summary"]["percent_covered"])
    print(f"critical coverage {name}: {actual:.1f}% (minimum {minimum}%)")
    if actual + 1e-9 < minimum:
        failed.append(f"{name}={actual:.1f}%<{minimum}%")
if failed:
    raise SystemExit("critical coverage gate failed: " + ", ".join(failed))
'@ | & $python -
    if ($LASTEXITCODE -ne 0) { throw "critical-module coverage gate failed" }

    @'
import json
import os
import gc
from pathlib import Path

from streamlit.testing.v1 import AppTest

database_path = Path(os.environ["TEMP"]) / "streamlit-quality-gate.db"
os.environ["QUANTLAB_DATABASE_PATH"] = str(database_path)
app = AppTest.from_file("dashboard/app.py", default_timeout=30).run(timeout=30)
if app.exception:
    raise RuntimeError(app.exception)
expected_labels = [
    "\N{CJK UNIFIED IDEOGRAPH-4ECA}\N{CJK UNIFIED IDEOGRAPH-65E5}",
    "\N{CJK UNIFIED IDEOGRAPH-5E02}\N{CJK UNIFIED IDEOGRAPH-573A}"
    "\N{CJK UNIFIED IDEOGRAPH-4E0E}\N{CJK UNIFIED IDEOGRAPH-53D1}"
    "\N{CJK UNIFIED IDEOGRAPH-73B0}",
    "\N{CJK UNIFIED IDEOGRAPH-7814}\N{CJK UNIFIED IDEOGRAPH-7A76}"
    "\N{CJK UNIFIED IDEOGRAPH-53F0}",
    "\N{CJK UNIFIED IDEOGRAPH-7EC4}\N{CJK UNIFIED IDEOGRAPH-5408}"
    "\N{CJK UNIFIED IDEOGRAPH-4E0E}\N{CJK UNIFIED IDEOGRAPH-4EA4}"
    "\N{CJK UNIFIED IDEOGRAPH-6613}",
    "\N{CJK UNIFIED IDEOGRAPH-51B3}\N{CJK UNIFIED IDEOGRAPH-7B56}"
    "\N{CJK UNIFIED IDEOGRAPH-590D}\N{CJK UNIFIED IDEOGRAPH-76D8}",
    "\N{CJK UNIFIED IDEOGRAPH-4E13}\N{CJK UNIFIED IDEOGRAPH-4E1A}"
    "\N{CJK UNIFIED IDEOGRAPH-7A7A}\N{CJK UNIFIED IDEOGRAPH-95F4}",
    "\N{CJK UNIFIED IDEOGRAPH-5E2E}\N{CJK UNIFIED IDEOGRAPH-52A9}"
    "\N{CJK UNIFIED IDEOGRAPH-4E2D}\N{CJK UNIFIED IDEOGRAPH-5FC3}",
]
if app.segmented_control or len(app.radio) != 1:
    raise RuntimeError("normal mode must expose the vNext sidebar navigation")
navigation = app.radio[0]
if navigation.options != expected_labels or navigation.value != expected_labels[0]:
    raise RuntimeError(
        f"vNext product navigation mismatch: options={navigation.options}, "
        f"active={navigation.value}"
    )
if app.tabs:
    raise RuntimeError("normal mode must not render primary workspaces through tabs")
page_exceptions = {}
for page in expected_labels:
    app.radio[0].set_value(page).run(timeout=30)
    page_exceptions[page] = len(app.exception)
    if app.exception:
        raise RuntimeError(f"normal product page failed: {page}: {app.exception}")
app.radio[0].set_value(expected_labels[5]).run(timeout=30)
audit_button = next(
    (button for button in app.button if button.label == "\N{CJK UNIFIED IDEOGRAPH-8FDB}"
     "\N{CJK UNIFIED IDEOGRAPH-5165}\N{CJK UNIFIED IDEOGRAPH-5DE5}"
     "\N{CJK UNIFIED IDEOGRAPH-7A0B}\N{CJK UNIFIED IDEOGRAPH-5BA1}"
     "\N{CJK UNIFIED IDEOGRAPH-8BA1}"),
    None,
)
if audit_button is None:
    raise RuntimeError("professional workspace lost the engineering audit transition")
audit_button.click().run(timeout=30)
if app.exception:
    raise RuntimeError(app.exception)
if len(app.tabs) < 15:
    raise RuntimeError("advanced/audit mode lost historical engineering capabilities")
advanced_tabs = len(app.tabs)
result = {
    "status": "passed",
    "navigation_entries": len(expected_labels),
    "primary_entries": expected_labels,
    "initial_page": expected_labels[0],
    "normal_mode_tabs": 0,
    "page_exceptions": page_exceptions,
    "advanced_tabs": advanced_tabs,
    "exceptions": 0,
}
del app
gc.collect()
Path("data/reports/streamlit-apptest.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(
    f"Streamlit AppTest passed: entries={result['navigation_entries']}, "
    f"exceptions={result['exceptions']}"
)
'@ | & $python -
    if ($LASTEXITCODE -ne 0) { throw "Streamlit AppTest failed" }

    $extensions = @(".py", ".md", ".toml", ".yaml", ".yml", ".json", ".txt", ".ps1", ".example")
    $textFiles = Get-ChildItem -Path $root -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
        $_.FullName -notlike "*\.venv\*" -and
        $_.FullName -notlike "*\__pycache__\*" -and
        $_.FullName -notlike "*\.pytest_cache\*" -and
        $_.Name -ne ".env" -and
        ($extensions -contains $_.Extension -or $_.Name -eq ".env.example")
    }
    $keyMatches = $textFiles | Select-String -Pattern "(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{12,}" -CaseSensitive:$false -ErrorAction SilentlyContinue
    if (@($keyMatches).Count -gt 0) {
        throw "hard-coded API-key pattern detected outside .env"
    }

    $reportFiles = Get-ChildItem -Path (Join-Path $root "data\reports") -File -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -ne "coverage.json"
    }
    $sensitiveFields = $reportFiles | Select-String -Pattern '"(api_key|authorization|system_prompt|user_prompt|raw_request|access_token|client_secret|password|secret|token)"\s*:' -CaseSensitive:$false -ErrorAction SilentlyContinue
    if (@($sensitiveFields).Count -gt 0) {
        throw "sensitive field detected in exported reports"
    }

    foreach ($artifact in @(
        "data\reports\latest-demo.md",
        "data\reports\latest-demo.json",
        "data\reports\historical-replay-3.md",
        "data\reports\historical-replay-3.json",
        "data\reports\historical-replay-7.md",
        "data\reports\historical-replay-7.json",
        "data\reports\historical-replay-8.md",
        "data\reports\historical-replay-8.json",
        "data\reports\historical-replay-9.md",
        "data\reports\historical-replay-9.json",
        "data\reports\decision-gate-scorecard-latest.md",
        "data\reports\decision-gate-scorecard-latest.json",
        "data\reports\profitability-evidence-latest.md",
        "data\reports\profitability-evidence-latest.json",
        "data\reports\etf-core-protocol-validation-latest.md",
        "data\reports\etf-core-protocol-validation-latest.json",
        "data\reports\strategy-lab-latest.md",
        "data\reports\strategy-lab-latest.json",
        "data\reports\adaptive-v2-diagnostic-latest.md",
        "data\reports\adaptive-v2-diagnostic-latest.json",
        "data\reports\strategy-robustness-audit-latest.md",
        "data\reports\strategy-robustness-audit-latest.json",
        "data\reports\strategy-v3-diagnostic-latest.md",
        "data\reports\strategy-v3-diagnostic-latest.json",
        "data\reports\stock-market-replay-7.md",
        "data\reports\stock-market-replay-7.json",
        "data\reports\a-share-strategy-lab-v3-development.md",
        "data\reports\a-share-strategy-lab-v3-development.json",
        "data\reports\a-share-strategy-lab-v3-validation.md",
        "data\reports\a-share-strategy-lab-v3-validation.json",
        "docs\BACKEND_ROUND1.md",
        "docs\BACKEND_ROUND2.md",
        "docs\BACKEND_ROUND3.md",
        "docs\BACKEND_ROUND4.md",
        "docs\BACKEND_ROUND5.md",
        "docs\BACKEND_ROUND6.md",
        "docs\BACKEND_ROUND7.md",
        "docs\BACKEND_ROUND8.md",
        "docs\BACKEND_ROUND9.md",
        "docs\HACKATHON_DEMO.md",
        "docs\DATA_SOURCE_STATUS.md",
        "docs\FIVE_ENTRY_USER_FLOW.md",
        "docs\WINDOWS_AUTOSTART.md",
        "docs\CONTINUOUS_RUNTIME_STATUS.md",
        "docs\BACKEND_ROUNDS_1_4_AUDIT.md"
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $root $artifact))) {
            throw "required review artifact is missing: $artifact"
        }
    }

    @'
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from quantlab.config import Settings
from quantlab.runtime.readiness import quality_source_fingerprint

coverage = json.loads(Path("data/reports/coverage.json").read_text(encoding="utf-8"))
replay = json.loads(Path("data/reports/historical-replay-9.json").read_text(encoding="utf-8"))
decision_gate = json.loads(
    Path("data/reports/decision-gate-scorecard-latest.json").read_text(encoding="utf-8")
)
profitability = json.loads(
    Path("data/reports/profitability-evidence-latest.json").read_text(encoding="utf-8")
)
strategy_lab = json.loads(
    Path("data/reports/strategy-lab-latest.json").read_text(encoding="utf-8")
)
adaptive_v2 = json.loads(
    Path("data/reports/adaptive-v2-diagnostic-latest.json").read_text(encoding="utf-8")
)
strategy_audit = json.loads(
    Path("data/reports/strategy-robustness-audit-latest.json").read_text(encoding="utf-8")
)
adaptive_v3 = json.loads(
    Path("data/reports/strategy-v3-diagnostic-latest.json").read_text(encoding="utf-8")
)
stock_market_replay = json.loads(
    Path("data/reports/stock-market-replay-7.json").read_text(encoding="utf-8")
)
streamlit_test = json.loads(
    Path("data/reports/streamlit-apptest.json").read_text(encoding="utf-8")
)
database_path = Path("data/quantlab.db")
formal_forward_samples = 0
if database_path.is_file():
    with sqlite3.connect(database_path) as db:
        has_round5 = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='forward_registration_runs'"
        ).fetchone()
        if has_round5:
            formal_forward_samples = int(
                db.execute(
                    """SELECT COALESCE(SUM(r.registered_samples),0)
                       FROM forward_registration_runs r
                       JOIN forward_experiment_protocols e
                         ON e.experiment_id=r.experiment_id
                       WHERE e.is_primary=1"""
                ).fetchone()[0]
                or 0
            )
a_share_v3 = profitability.get("a_share_strategy_v3", {})
a_share_v3_validation = a_share_v3.get("validation_result", {})
scorecard = {
    "status": "passed",
    "generated_at": datetime.now(UTC).isoformat(),
    "source_fingerprint": quality_source_fingerprint(Settings.load()),
    "self_assigned_score": None,
    "quality_gates": {
        "ruff": "passed",
        "compileall": "passed",
        "pytest": "passed",
        "total_coverage_pct": coverage["totals"]["percent_covered"],
        "total_coverage_minimum_pct": 71,
        "critical_module_coverage": "passed",
        "streamlit_app_test": streamlit_test,
        "hardcoded_key_scan": "passed",
        "report_sensitive_field_scan": "passed",
    },
    "latest_live_replay": {
        "replay_id": replay.get("replay_id", 9),
        "evidence_status": replay["evidence_status"],
        "evidence_qualified": replay.get("evidence_qualification", {}).get(
            "qualified", False
        ),
        "completed_episodes": replay["completed_episodes"],
        "successful_role_outputs": replay["llm_validation"][
            "successful_non_mock_role_outputs"
        ],
        "required_role_outputs": replay["llm_validation"].get(
            "required_role_outputs", 22
        ),
        "fallback_errors": len(replay["llm_validation"]["fallback_errors"]),
        "strategy_total_return": replay["metrics"]["strategy_only"]["total_return"],
        "full_system_total_return": replay["metrics"]["full_system"]["total_return"],
        "benchmark_total_return": replay["metrics"]["hs300_same_risk_budget"][
            "total_return"
        ],
    },
    "profitability_evidence": {
        "grade": profitability["profitability_assessment"]["grade"],
        "score": profitability["profitability_assessment"]["score"],
        "strategy_admission_passed": profitability["profitability_assessment"].get(
            "admission_passed", False
        ),
        "claims_allowed": profitability["profitability_assessment"]["claims_allowed"],
        "experiment_id": profitability["strategy_validation"].get("reproducibility", {}).get(
            "experiment_payload_sha256"
        ),
        "evidence_first_portfolio": profitability.get("evidence_first_portfolio"),
        "production_core_validation": profitability.get("production_core_validation"),
        "a_share_v3": {
            "status": a_share_v3.get("status"),
            "locked_holdout_ready": a_share_v3.get("locked_holdout_ready", False),
            "validation_total_return": a_share_v3_validation.get("total_return"),
            "validation_benchmark_return": a_share_v3_validation.get(
                "benchmark_total_return"
            ),
            "validation_admission": a_share_v3_validation.get("admission"),
        },
    },
    "adaptive_strategy_candidate": {
        "status": strategy_lab["status"],
        "selected_candidate": strategy_lab["selected_candidate"]["name"],
        "formal_strategy_changed": strategy_lab["formal_strategy_changed"],
        "holdout_admission_passed": strategy_lab["locked_holdout"]["admission"]["passed"],
        "experiment_id": strategy_lab["experiment_id"],
    },
    "adaptive_v2_diagnostic": {
        "status": adaptive_v2["status"],
        "strategy_variant": adaptive_v2["strategy_variant"],
        "research_only": adaptive_v2["research_only"],
        "period": adaptive_v2["period"],
        "total_return": adaptive_v2["metrics"]["strategy"]["total_return"],
        "sharpe": adaptive_v2["metrics"]["strategy"]["sharpe"],
        "max_drawdown": adaptive_v2["metrics"]["strategy"]["max_drawdown"],
        "relative_to_equal_weight": adaptive_v2["relative_to_equal_weight"],
    },
    "strategy_robustness_audit": {
        "status": strategy_audit["status"],
        "experiment_id": strategy_audit["experiment_id"],
        "adaptive_v2_stability": strategy_audit["stability"]["adaptive_v2_full"],
        "diagnosis": strategy_audit["diagnosis"],
    },
    "adaptive_v3_candidate": {
        "status": adaptive_v3["status"],
        "experiment_id": adaptive_v3["experiment_id"],
        "formal_strategy_changed": adaptive_v3["formal_strategy_changed"],
        "v3_vs_v2": adaptive_v3["v3_vs_v2"],
        "exploratory_screen": adaptive_v3["exploratory_screen"],
    },
    "a_share_market_replay": {
        "replay_id": stock_market_replay.get("replay_id", 7),
        "evidence_status": stock_market_replay["evidence_status"],
        "evidence_qualified": stock_market_replay["evidence_qualification"]["qualified"],
        "strategy_admission": stock_market_replay["strategy_admission"],
        "completed_episodes": stock_market_replay["completed_episodes"],
        "minimum_exchange_master_jaccard": stock_market_replay["evidence_qualification"][
            "minimum_exchange_master_jaccard"
        ],
        "top_rank_total_return": stock_market_replay["metrics"]["system_top_rank"][
            "total_return"
        ],
        "diversified_top_k_total_return": stock_market_replay["metrics"][
            "system_diversified_top_k"
        ]["total_return"],
        "same_exposure_hs300_total_return": stock_market_replay["metrics"][
            "benchmark_hs300_multi_name"
        ]["total_return"],
    },
    "decision_gate_evidence": {
        "promotion_status": decision_gate["promotion_status"],
        "v2_new_episodes": decision_gate["v2_new_episodes"],
        "model_driven_risk_reductions": decision_gate[
            "v2_model_driven_risk_reductions"
        ],
        "promotion_checks": decision_gate["promotion_checks"],
        "five_day_probability_ablation": decision_gate["v2_horizon_challenges"].get(
            "5", {}
        ).get("forecast_ablation"),
        "twenty_day_probability_ablation": decision_gate[
            "v2_horizon_challenges"
        ].get("20", {}).get("forecast_ablation"),
        "conclusion": decision_gate["conclusion"],
    },
    "claim_boundary": replay["claim_boundary"],
    "known_limitations_document": "AI_REVIEW_GUIDE.md",
    "backend_rounds": {
        "round1": {
            "status": "completed",
            "report": "docs/BACKEND_ROUND1.md",
            "simulator_schema_version": 6,
        },
        "round2": {
            "status": "completed",
            "report": "docs/BACKEND_ROUND2.md",
            "context_schema": "2.0",
            "evidence_schema_version": 5,
        },
        "round3": {
            "status": "completed_engineering_waiting_for_real_time_evidence",
            "report": "docs/BACKEND_ROUND3.md",
            "protocol": "STRATEGY_ROUND3_PREREGISTRATION.md",
            "runtime_schema_version": 3,
        },
        "round4": {
            "status": "completed_trust_boundary_and_reliability_closure",
            "report": "docs/BACKEND_ROUND4.md",
            "quote_boundary": "server_authoritative",
            "forward_settlement": "worker_due_trading_day",
            "migration_registry": "component_checksummed",
        },
        "round5": {
            "status": "engineering_complete_waiting_for_real_time_evidence",
            "report": "docs/BACKEND_ROUND5.md",
            "round5_schema_version": 2,
            "evidence_schema_version": 7,
            "strategy_evidence_schema_version": 6,
            "primary_protocol": "primary-forward-v2",
            "formal_forward_samples": formal_forward_samples,
            "shadow_account_variants": 7,
        },
        "round6": {
            "status": "engineering_complete_runtime_readiness_fail_closed",
            "report": "docs/BACKEND_ROUND6.md",
            "round6_schema_version": 1,
            "runtime_supervisor": "api_worker_scheduler_notification_worker",
            "product_navigation": "vnext_seven_workspace_with_advanced_audit_mode",
            "trusted_free_source_level": "server_observed_unverified_no_sla",
        },
        "round7": {
            "status": "engineering_complete_real_time_evidence_continues",
            "report": "docs/BACKEND_ROUND7.md",
            "round7_schema_version": 1,
            "production_data_chain": "baostock_akshare_eastmoney_akshare_sina_unavailable",
            "historical_demo": "isolated_research_only_database",
            "windows_autostart": "explicit_task_scheduler_management",
            "soak_reporting": "actual_observation_interval_only",
        },
        "round8": {
            "status": "engineering_complete_waiting_for_real_trading_day_and_matured_outcomes",
            "report": "docs/BACKEND_ROUND8.md",
            "round8_schema_version": 1,
            "provider_routing": "capability_based_single_flight",
            "decision_lifecycle": "recommendation_adoption_thesis_check_reflection",
            "experiment_ledger": "unified_run_checkpoint_artifact_links",
        },
        "round9": {
            "status": "engineering_complete_waiting_for_real_trading_day_and_natural_maturity",
            "report": "docs/BACKEND_ROUND9.md",
            "round9_schema_version": 4,
            "reflection_boundary": "authoritative_settled_outcomes_only",
            "checkpointing": "pre_call_atomic_claim_and_structured_resume",
            "decision_run": "single_traceable_lifecycle_with_sanitized_audit_export",
            "historical_demo": "research_only_not_forward_evidence",
        },
        "audit": {
            "status": "passed",
            "report": "docs/BACKEND_ROUNDS_1_4_AUDIT.md",
            "contract_test": "tests/test_backend_rounds_contract.py",
        },
    },
}
Path("data/reports/quality-gate-latest.json").write_text(
    json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8"
)
print("Wrote data/reports/quality-gate-latest.json")
'@ | & $python -
    if ($LASTEXITCODE -ne 0) { throw "quality scorecard generation failed" }

    Write-Host "QUALITY GATE PASSED" -ForegroundColor Green
}
finally {
    if ($resolvedWorkspaceTemp -and (Test-Path -LiteralPath $resolvedWorkspaceTemp)) {
        Remove-Item -LiteralPath $resolvedWorkspaceTemp -Recurse -Force -ErrorAction SilentlyContinue
    }
    Pop-Location
}
