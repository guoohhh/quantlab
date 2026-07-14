from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quantlab.config import Settings
from quantlab.data import BaoStockProvider
from quantlab.execution import CostModel
from quantlab.learning import LearningRepository
from quantlab.workflows.stock_discovery import _bar_frame
from quantlab.workflows.stock_evidence import (
    _simulate_stock_portfolio,
)
from quantlab.workflows.stock_market_replay import (
    _market_outcome_return,
    _simulate_index_benchmark,
)
from quantlab.workflows.stock_strategy_lab import (
    _group_samples,
    _market_samples,
    a_share_ranking_policy_hash,
    score_a_share_ranking_policy,
)


PROTOCOL_VERSION = "a-share-defensive-reversal-v2"
SELECTED_CANDIDATE = "broad_contrarian_four"
CANDIDATE_POLICIES: dict[str, dict[str, Any]] = {
    "defensive_reversal_single": {
        "kind": "static_cross_section_v2",
        "name": "defensive_reversal_single",
        "portfolio": {"top_k": 1, "total_exposure": 0.15, "selection": "rank_top_k"},
    },
    "broad_contrarian_four": {
        "kind": "static_cross_section_v2",
        "name": "broad_contrarian_four",
        "portfolio": {"top_k": 4, "total_exposure": 0.40, "selection": "rank_top_k"},
    },
    "pure_low_vol_two": {
        "kind": "static_cross_section_v2",
        "name": "pure_low_vol_two",
        "portfolio": {"top_k": 2, "total_exposure": 0.30, "selection": "rank_top_k"},
    },
}


def run_a_share_strategy_lab_v2(
    settings: Settings,
    *,
    development_start: date = date(2018, 1, 1),
    development_end: date = date(2022, 12, 31),
    validation_start: date = date(2023, 1, 1),
    validation_end: date = date(2025, 12, 31),
    source_sample_horizon_days: int = 5,
    holding_horizon_days: int = 20,
    output_path: Path | None = None,
    provider: BaoStockProvider | None = None,
) -> dict[str, Any]:
    """Validate the frozen V2 A-share policy with full manual-execution constraints."""

    if not development_start <= development_end < validation_start <= validation_end:
        raise ValueError("strategy lab V2 requires non-overlapping ordered periods")
    if source_sample_horizon_days != 5 or holding_horizon_days != 20:
        raise ValueError("A-share strategy V2 is frozen to 5-day signal dates and 20-day holds")
    repository = LearningRepository(settings.resolve(settings.get("system.database_path")))
    samples, sample_audit = _market_samples(
        repository,
        development_start,
        validation_end,
        source_sample_horizon_days,
    )
    development_groups = _group_samples(
        [item for item in samples if development_start.isoformat() <= item["as_of"] <= development_end.isoformat()]
    )
    validation_groups = _group_samples(
        [item for item in samples if validation_start.isoformat() <= item["as_of"] <= validation_end.isoformat()]
    )
    if len(development_groups) < 30 or len(validation_groups) < 20:
        raise ValueError("strategy lab V2 has insufficient point-in-time market dates")

    source = provider or BaoStockProvider(
        cache_dir=settings.resolve(settings.get("system.data_dir")) / "cache" / "baostock"
    )
    symbols = sorted({item["symbol"] for item in samples} | {"sh000300"})
    bars = source.cached_bars(symbols) if hasattr(source, "cached_bars") else []
    available = {bar.symbol for bar in bars}
    missing = [symbol for symbol in symbols if symbol not in available]
    if missing:
        bars.extend(
            source.bars(
                missing,
                development_start - timedelta(days=10),
                validation_end + timedelta(days=90),
            )
        )
    frame = _bar_frame(bars)
    if frame.empty:
        raise ValueError("strategy lab V2 has no cached or provider execution history")
    cost_model = CostModel.from_dict(settings.get("costs.stock"))

    development_results = {
        name: _evaluate_v2_policy(
            development_groups,
            frame,
            policy,
            holding_horizon_days=holding_horizon_days,
            cost_model=cost_model,
        )
        for name, policy in CANDIDATE_POLICIES.items()
    }
    for result in development_results.values():
        result["development_admission"] = _development_admission(result)
    selected_development = development_results[SELECTED_CANDIDATE]
    if not selected_development["development_admission"]["passed"]:
        validation_result = None
    else:
        validation_result = _evaluate_v2_policy(
            validation_groups,
            frame,
            CANDIDATE_POLICIES[SELECTED_CANDIDATE],
            holding_horizon_days=holding_horizon_days,
            cost_model=cost_model,
        )
        validation_result["admission"] = _validation_admission(validation_result)

    protocol = {
        "version": PROTOCOL_VERSION,
        "registered_at": "2026-07-14",
        "development": [development_start.isoformat(), development_end.isoformat()],
        "validation": [validation_start.isoformat(), validation_end.isoformat()],
        "locked_holdout": ["2026-01-01", "2026-07-13"],
        "source_sample_horizon_days": source_sample_horizon_days,
        "holding_horizon_days": holding_horizon_days,
        "signal_schedule": "eight deterministic dates per year inherited from the 5-day PIT sample protocol",
        "selected_candidate_before_validation": SELECTED_CANDIDATE,
        "candidate_policies": CANDIDATE_POLICIES,
        "execution": "T+1 executable open to 20th benchmark trading-day close with A-share costs and lots",
    }
    protocol_hash = hashlib.sha256(
        json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    locked_ready = bool(validation_result and validation_result["admission"]["passed"])
    output = {
        "protocol": protocol,
        "protocol_hash": protocol_hash,
        "sample_audit": sample_audit,
        "data_coverage": {
            "requested_symbols": len(symbols),
            "available_symbols": int(frame["symbol"].nunique()),
            "market_rows": len(frame),
        },
        "development_results": development_results,
        "selected_candidate": SELECTED_CANDIDATE,
        "frozen_validation_policy": CANDIDATE_POLICIES[SELECTED_CANDIDATE],
        "validation_result": validation_result,
        "locked_holdout_ready": locked_ready,
        "status": (
            "validation_passed_holdout_locked"
            if locked_ready
            else "development_failed"
            if validation_result is None
            else "validation_failed"
        ),
        "claim_boundary": (
            "V2 was selected using 2018-2022 only; 2023-2025 is a frozen validation; "
            "2026 remains locked unless every validation gate passes"
        ),
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def freeze_a_share_v2_locked_holdout_policy(
    lab_result: dict[str, Any], *, output_path: Path | None = None
) -> dict[str, Any]:
    if not lab_result.get("locked_holdout_ready"):
        raise ValueError("V2 validation has not passed; locked holdout policy cannot be frozen")
    policy = json.loads(json.dumps(lab_result["frozen_validation_policy"]))
    policy["governance"] = {
        "protocol_version": PROTOCOL_VERSION,
        "protocol_hash": lab_result["protocol_hash"],
        "selected_candidate": lab_result["selected_candidate"],
        "validation_end": lab_result["protocol"]["validation"][1],
        "locked_holdout_start": "2026-01-01",
        "locked_holdout_end": "2026-07-13",
        "signal_schedule_horizon_days": 5,
        "holding_horizon_days": 20,
        "sample_protocol_hash": lab_result["sample_audit"]["selected_sample_protocol_hash"],
        "validation_passed": True,
    }
    policy["policy_hash"] = a_share_ranking_policy_hash(policy)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
    return policy


def evaluate_a_share_v2_locked_holdout(
    lab_result: dict[str, Any], replay: dict[str, Any]
) -> dict[str, Any]:
    if not lab_result.get("locked_holdout_ready"):
        raise ValueError("V2 validation has not passed")
    requested = replay.get("requested_range", {})
    if requested.get("start") != "2026-01-01" or requested.get("end") != "2026-07-13":
        raise ValueError("V2 locked holdout replay range does not match the preregistered period")
    metrics = replay["metrics"]["system_diversified_top_k"]
    benchmark = replay["metrics"]["benchmark_hs300_multi_name"]
    comparison = replay["paired_comparisons"]["benchmark_hs300_multi_name"]
    checks = {
        "positive_total_return": metrics["total_return"] > 0,
        "positive_same_exposure_hs300_excess": (
            metrics["total_return"] > benchmark["total_return"]
            and comparison["mean_excess_return"] > 0
        ),
        "bootstrap_probability_at_least_90pct": (
            comparison["probability_mean_excess_positive"] >= 0.90
        ),
        "maximum_drawdown_within_10pct": metrics["max_drawdown"] >= -0.10,
        "participation_at_least_70pct": metrics["participation_rate"] >= 0.70,
        "point_in_time_data_integrity": replay["evidence_qualification"]["snapshot_integrity"],
    }
    passed = all(checks.values())
    return {
        "passed": passed,
        "status": "positive_holdout_evidence" if passed else "holdout_failed",
        "checks": checks,
        "metrics": metrics,
        "benchmark_metrics": benchmark,
        "paired_comparison": comparison,
        "policy_hash": replay.get("ranking_policy_hash"),
        "claim_boundary": (
            "a passed historical holdout permits a prospective shadow challenge only; "
            "it does not promise future profit"
        ),
    }


def _evaluate_v2_policy(
    groups: dict[date, list[dict[str, Any]]],
    frame: pd.DataFrame,
    policy: dict[str, Any],
    *,
    holding_horizon_days: int,
    cost_model: CostModel,
) -> dict[str, Any]:
    benchmark_rows = frame[frame["symbol"] == "sh000300"].sort_values("date")
    benchmark_dates = [pd.Timestamp(value).date() for value in benchmark_rows["date"].tolist()]
    benchmark_index = {day: index for index, day in enumerate(benchmark_dates)}
    portfolio = policy["portfolio"]
    top_k = int(portfolio["top_k"])
    total_exposure = float(portfolio["total_exposure"])
    capital = 100_000.0
    benchmark_capital = 100_000.0
    peak = capital
    max_drawdown = 0.0
    returns = []
    benchmark_returns = []
    rank_ics = []
    episodes = []
    annual_returns: dict[int, list[float]] = defaultdict(list)
    annual_benchmark: dict[int, list[float]] = defaultdict(list)
    for signal_date in sorted(groups):
        index = benchmark_index.get(signal_date)
        if index is None or index + holding_horizon_days >= len(benchmark_dates):
            continue
        target_date = benchmark_dates[index + holding_horizon_days]
        items = groups[signal_date]
        scores = score_a_share_ranking_policy(items, policy)
        ranked = sorted(
            items,
            key=lambda item: (scores[item["symbol"]], item["symbol"]),
            reverse=True,
        )
        selected = ranked[:top_k]
        trade = _simulate_stock_portfolio(
            frame,
            [item["symbol"] for item in selected],
            signal_date,
            target_date,
            capital,
            total_exposure,
            cost_model,
        )
        benchmark_trade = _simulate_index_benchmark(
            frame,
            "sh000300",
            signal_date,
            target_date,
            benchmark_capital,
            total_exposure,
        )
        capital = float(trade["capital_after"])
        benchmark_capital = float(benchmark_trade["capital_after"])
        peak = max(peak, capital)
        max_drawdown = min(max_drawdown, capital / peak - 1)
        strategy_return = float(trade["net_return"])
        benchmark_return = float(benchmark_trade["net_return"])
        returns.append(strategy_return)
        benchmark_returns.append(benchmark_return)
        annual_returns[signal_date.year].append(strategy_return)
        annual_benchmark[signal_date.year].append(benchmark_return)
        outcomes = {
            item["symbol"]: _market_outcome_return(
                frame, item["symbol"], signal_date, target_date
            )
            for item in items
        }
        comparable = [symbol for symbol, value in outcomes.items() if value is not None]
        rank_ic = None
        if len(comparable) >= 2:
            score_ranks = pd.Series([scores[symbol] for symbol in comparable]).rank()
            return_ranks = pd.Series([outcomes[symbol] for symbol in comparable]).rank()
            value = score_ranks.corr(return_ranks)
            if pd.notna(value):
                rank_ic = float(value)
                rank_ics.append(rank_ic)
        episodes.append(
            {
                "signal_date": signal_date.isoformat(),
                "target_date": target_date.isoformat(),
                "selected_symbols": [item["symbol"] for item in selected],
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "traded_legs": int(trade["traded_legs"]),
                "requested_legs": top_k,
                "rank_ic": rank_ic,
            }
        )
    if not episodes:
        raise ValueError("strategy lab V2 produced no executable episodes")
    differences = np.asarray(returns) - np.asarray(benchmark_returns)
    rng = np.random.default_rng(20260714)
    bootstrapped = np.asarray(
        [float(rng.choice(differences, size=len(differences), replace=True).mean()) for _ in range(4000)]
    )
    annual = {}
    for year in sorted(annual_returns):
        strategy_year = float(np.prod(1 + np.asarray(annual_returns[year])) - 1)
        benchmark_year = float(np.prod(1 + np.asarray(annual_benchmark[year])) - 1)
        annual[str(year)] = {
            "strategy_return": strategy_year,
            "benchmark_return": benchmark_year,
            "excess_return": strategy_year - benchmark_year,
        }
    return {
        "policy": policy,
        "episodes": len(episodes),
        "top_k": top_k,
        "total_exposure": total_exposure,
        "total_return": capital / 100_000.0 - 1,
        "benchmark_total_return": benchmark_capital / 100_000.0 - 1,
        "average_episode_return": float(np.mean(returns)),
        "win_rate": float(np.mean(np.asarray(returns) > 0)),
        "max_drawdown": max_drawdown,
        "participation_rate": float(
            np.mean([item["traded_legs"] > 0 for item in episodes])
        ),
        "leg_fill_rate": float(
            sum(item["traded_legs"] for item in episodes)
            / sum(item["requested_legs"] for item in episodes)
        ),
        "mean_rank_ic": float(np.mean(rank_ics)) if rank_ics else 0.0,
        "positive_year_fraction": float(
            np.mean([item["strategy_return"] > 0 for item in annual.values()])
        ),
        "positive_excess_year_fraction": float(
            np.mean([item["excess_return"] > 0 for item in annual.values()])
        ),
        "annual": annual,
        "paired_comparison": {
            "mean_excess_return": float(differences.mean()),
            "median_excess_return": float(np.median(differences)),
            "positive_excess_rate": float(np.mean(differences > 0)),
            "probability_mean_excess_positive": float(np.mean(bootstrapped > 0)),
            "bootstrap_90pct_interval": [
                float(np.quantile(bootstrapped, 0.05)),
                float(np.quantile(bootstrapped, 0.95)),
            ],
        },
        "episode_details": episodes,
    }


def _development_admission(result: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "positive_total_return": result["total_return"] > 0,
        "positive_mean_excess": result["paired_comparison"]["mean_excess_return"] > 0,
        "mean_rank_ic_at_least_5pct": result["mean_rank_ic"] >= 0.05,
        "maximum_drawdown_within_15pct": result["max_drawdown"] >= -0.15,
        "positive_year_fraction_at_least_60pct": result["positive_year_fraction"] >= 0.60,
        "leg_fill_rate_at_least_85pct": result["leg_fill_rate"] >= 0.85,
    }
    return {"passed": all(checks.values()), "checks": checks}


def _validation_admission(result: dict[str, Any]) -> dict[str, Any]:
    comparison = result["paired_comparison"]
    annual = result["annual"]
    checks = {
        "positive_total_return": result["total_return"] > 0,
        "positive_same_exposure_excess": (
            result["total_return"] > result["benchmark_total_return"]
            and comparison["mean_excess_return"] > 0
        ),
        "bootstrap_probability_at_least_90pct": (
            comparison["probability_mean_excess_positive"] >= 0.90
        ),
        "bootstrap_lower_bound_not_below_minus_10bp": (
            comparison["bootstrap_90pct_interval"][0] >= -0.001
        ),
        "mean_rank_ic_at_least_3pct": result["mean_rank_ic"] >= 0.03,
        "maximum_drawdown_within_15pct": result["max_drawdown"] >= -0.15,
        "at_least_two_positive_years": sum(
            item["strategy_return"] > 0 for item in annual.values()
        )
        >= 2,
        "at_least_two_positive_excess_years": sum(
            item["excess_return"] > 0 for item in annual.values()
        )
        >= 2,
        "participation_at_least_90pct": result["participation_rate"] >= 0.90,
        "leg_fill_rate_at_least_85pct": result["leg_fill_rate"] >= 0.85,
    }
    return {"passed": all(checks.values()), "checks": checks}
