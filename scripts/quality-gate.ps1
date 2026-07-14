$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Python environment not found: $python"
}

Push-Location $root
try {
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
    foreach ($shard in $shards) {
        & $python -m coverage run --parallel-mode --source=quantlab -m pytest -q @shard
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
    "src/quantlab/factors/engine.py": 90,
    "src/quantlab/learning/trainer.py": 80,
    "src/quantlab/llm/providers.py": 70,
    "src/quantlab/portfolio/planner.py": 85,
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
    "src/quantlab/workflows/paper.py": 75,
    "src/quantlab/workflows/replay.py": 90,
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
from pathlib import Path

from streamlit.testing.v1 import AppTest

app = AppTest.from_file("dashboard/app.py", default_timeout=30)
app.run(timeout=30)
if app.exception:
    raise RuntimeError(app.exception)
result = {"status": "passed", "tabs": len(app.tabs), "exceptions": len(app.exception)}
Path("data/reports/streamlit-apptest.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(
    f"Streamlit AppTest passed: tabs={result['tabs']}, "
    f"exceptions={result['exceptions']}"
)
'@ | & $python -
    if ($LASTEXITCODE -ne 0) { throw "Streamlit AppTest failed" }

    $extensions = @(".py", ".md", ".toml", ".yaml", ".yml", ".json", ".txt", ".ps1", ".example")
    $textFiles = Get-ChildItem -Path $root -Recurse -File | Where-Object {
        $_.FullName -notlike "*\.venv\*" -and
        $_.FullName -notlike "*\__pycache__\*" -and
        $_.Name -ne ".env" -and
        ($extensions -contains $_.Extension -or $_.Name -eq ".env.example")
    }
    $keyMatches = $textFiles | Select-String -Pattern "sk-[A-Za-z0-9]{12,}" -CaseSensitive:$false -ErrorAction SilentlyContinue
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
        "data\reports\a-share-strategy-lab-v3-validation.json"
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $root $artifact))) {
            throw "required review artifact is missing: $artifact"
        }
    }

    @'
import json
from datetime import UTC, datetime
from pathlib import Path

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
a_share_v3 = profitability.get("a_share_strategy_v3", {})
a_share_v3_validation = a_share_v3.get("validation_result", {})
scorecard = {
    "status": "passed",
    "generated_at": datetime.now(UTC).isoformat(),
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
    Pop-Location
}
