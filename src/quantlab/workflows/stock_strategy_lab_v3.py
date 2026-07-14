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
from quantlab.persistence import TerminalRepository
from quantlab.workflows.stock_discovery import _bar_frame
from quantlab.workflows.stock_evidence import _simulate_stock_portfolio
from quantlab.workflows.stock_market_replay import (
    _market_outcome_return,
    _simulate_index_benchmark,
)
from quantlab.workflows.stock_strategy_lab import (
    _group_samples,
    _market_samples,
    a_share_ranking_policy_hash,
    resolve_a_share_ranking_policy,
    score_a_share_ranking_policy,
)


PROTOCOL_VERSION = "a-share-market-regime-v3"
EXPECTED_SAMPLE_PROTOCOL_HASH = "6002d4c696b6c629"
FROZEN_POLICY: dict[str, Any] = {
    "kind": "market_regime_switch",
    "name": "hs300_trend_defensive_switch",
    "market_regime": {
        "benchmark_symbol": "sh000300",
        "trend_lookback_trading_days": 120,
        "risk_on_threshold": 0.0,
    },
    "branches": {
        "risk_on": {
            "ranking": {
                "kind": "static_cross_section_v2",
                "name": "broad_contrarian_four",
            },
            "portfolio": {
                "top_k": 4,
                "total_exposure": 0.40,
                "selection": "rank_top_k",
            },
        },
        "risk_off": {
            "ranking": {
                "kind": "static_cross_section_v2",
                "name": "pure_low_vol_two",
            },
            "portfolio": {
                "top_k": 2,
                "total_exposure": 0.30,
                "selection": "rank_top_k",
            },
        },
    },
}


def a_share_v3_protocol() -> dict[str, Any]:
    return {
        "version": PROTOCOL_VERSION,
        "registered_at": "2026-07-14",
        "development": ["2018-01-01", "2022-12-31"],
        "validation": ["2023-01-01", "2025-12-31"],
        "locked_holdout": ["2026-01-01", "2026-07-13"],
        "source_sample_horizon_days": 5,
        "holding_horizon_days": 20,
        "expected_sample_protocol_hash": EXPECTED_SAMPLE_PROTOCOL_HASH,
        "signal_schedule": "eight deterministic dates per year from the PIT market protocol",
        "policy": FROZEN_POLICY,
        "execution": (
            "T close rank; next executable open entry; twentieth benchmark trading-day close "
            "exit; A-share costs, price limits and 100-share lots"
        ),
    }


def a_share_v3_protocol_hash() -> str:
    return hashlib.sha256(
        json.dumps(
            a_share_v3_protocol(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()[:16]


def run_a_share_strategy_lab_v3_development(
    settings: Settings,
    *,
    output_path: Path | None = None,
    provider: BaoStockProvider | None = None,
) -> dict[str, Any]:
    groups, frame, sample_audit, source_name = _load_period(
        settings,
        date(2018, 1, 1),
        date(2022, 12, 31),
        provider,
    )
    result = _evaluate_v3_policy(
        groups,
        frame,
        FROZEN_POLICY,
        holding_horizon_days=20,
        cost_model=CostModel.from_dict(settings.get("costs.stock")),
    )
    result["admission"] = _development_admission(result)
    passed = bool(result["admission"]["passed"])
    output = {
        "status": "development_passed_validation_locked" if passed else "development_failed",
        "stage": "development",
        "protocol": a_share_v3_protocol(),
        "protocol_hash": a_share_v3_protocol_hash(),
        "policy_hash": a_share_ranking_policy_hash(FROZEN_POLICY),
        "sample_audit": sample_audit,
        "source": source_name,
        "development_result": result,
        "validation_opened": False,
        "locked_holdout_ready": False,
        "claim_boundary": (
            "Only 2018-2022 was evaluated. A pass permits one frozen 2023-2025 validation; "
            "it is not independent profitability evidence."
        ),
    }
    _write_report(output_path, output)
    return output


def run_a_share_strategy_lab_v3_validation(
    settings: Settings,
    development_report: dict[str, Any],
    *,
    output_path: Path | None = None,
    provider: BaoStockProvider | None = None,
) -> dict[str, Any]:
    _validate_development_report(development_report)
    if output_path is not None and output_path.exists():
        raise ValueError("V3 validation report already exists; the frozen validation is one-shot")
    groups, frame, sample_audit, source_name = _load_period(
        settings,
        date(2023, 1, 1),
        date(2025, 12, 31),
        provider,
    )
    development_report_hash = _report_hash(development_report)
    policy_hash = a_share_ranking_policy_hash(FROZEN_POLICY)
    consumption_key = hashlib.sha256(
        (
            f"{PROTOCOL_VERSION}|validation|{a_share_v3_protocol_hash()}|"
            f"{policy_hash}|{development_report_hash}"
        ).encode("utf-8")
    ).hexdigest()
    terminal = TerminalRepository(settings.resolve(settings.get("system.database_path")))
    if not terminal.claim_validation_once(
        consumption_key,
        a_share_v3_protocol_hash(),
        "validation",
        {
            "period": ["2023-01-01", "2025-12-31"],
            "policy_hash": policy_hash,
            "development_report_hash": development_report_hash,
        },
    ):
        raise ValueError("V3 frozen validation has already been consumed in this database")
    result = _evaluate_v3_policy(
        groups,
        frame,
        FROZEN_POLICY,
        holding_horizon_days=20,
        cost_model=CostModel.from_dict(settings.get("costs.stock")),
    )
    result["admission"] = _validation_admission(result)
    passed = bool(result["admission"]["passed"])
    output = {
        "status": "validation_passed_holdout_locked" if passed else "validation_failed",
        "stage": "validation",
        "protocol": a_share_v3_protocol(),
        "protocol_hash": a_share_v3_protocol_hash(),
        "policy_hash": policy_hash,
        "development_report_hash": development_report_hash,
        "validation_consumption_key": consumption_key,
        "development_result": development_report["development_result"],
        "validation_result": result,
        "sample_audit": sample_audit,
        "source": source_name,
        "validation_opened": True,
        "locked_holdout_ready": passed,
        "claim_boundary": (
            "This is the single frozen 2023-2025 validation. A pass permits one 2026 locked "
            "holdout; it does not promise future profit or authorize live trading."
        ),
    }
    _write_report(output_path, output)
    return output


def freeze_a_share_v3_locked_holdout_policy(
    validation_report: dict[str, Any],
    *,
    output_path: Path | None = None,
) -> dict[str, Any]:
    if not validation_report.get("locked_holdout_ready"):
        raise ValueError("V3 validation has not passed; locked holdout policy cannot be frozen")
    if validation_report.get("protocol_hash") != a_share_v3_protocol_hash():
        raise ValueError("V3 validation protocol hash does not match the frozen implementation")
    policy = json.loads(json.dumps(FROZEN_POLICY))
    policy["governance"] = {
        "protocol_version": PROTOCOL_VERSION,
        "protocol_hash": a_share_v3_protocol_hash(),
        "validation_report_hash": _report_hash(validation_report),
        "validation_end": "2025-12-31",
        "locked_holdout_start": "2026-01-01",
        "locked_holdout_end": "2026-07-13",
        "signal_schedule_horizon_days": 5,
        "holding_horizon_days": 20,
        "sample_protocol_hash": EXPECTED_SAMPLE_PROTOCOL_HASH,
        "validation_passed": True,
    }
    policy["policy_hash"] = a_share_ranking_policy_hash(policy)
    _write_report(output_path, policy)
    return policy


def evaluate_a_share_v3_locked_holdout(
    validation_report: dict[str, Any], replay: dict[str, Any]
) -> dict[str, Any]:
    if not validation_report.get("locked_holdout_ready"):
        raise ValueError("V3 validation has not passed")
    requested = replay.get("requested_range", {})
    if requested.get("start") != "2026-01-01" or requested.get("end") != "2026-07-13":
        raise ValueError("V3 locked holdout replay range does not match the protocol")
    metrics = replay["metrics"]["system_diversified_top_k"]
    benchmark = replay["metrics"]["benchmark_hs300_multi_name"]
    comparison = replay["paired_comparisons"]["benchmark_hs300_multi_name"]
    checks = {
        "positive_total_return": metrics["total_return"] > 0,
        "positive_same_exposure_hs300_excess": (
            metrics["total_return"] > benchmark["total_return"]
            and comparison["mean_excess_return"] > 0
        ),
        "bootstrap_probability_at_least_80pct": (
            comparison["probability_mean_excess_positive"] >= 0.80
        ),
        "maximum_drawdown_within_10pct": metrics["max_drawdown"] >= -0.10,
        "participation_at_least_70pct": metrics["participation_rate"] >= 0.70,
        "point_in_time_data_integrity": replay["evidence_qualification"][
            "snapshot_integrity"
        ],
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
            "A passed historical holdout permits a prospective shadow challenge only; it does "
            "not guarantee future returns."
        ),
    }


def render_a_share_strategy_lab_v3_markdown(output: dict[str, Any]) -> str:
    result = (
        output["validation_result"]
        if output.get("stage") == "validation"
        else output["development_result"]
    )
    admission = result["admission"]
    lines = [
        "# A股市场状态策略 V3",
        "",
        f"- 阶段：{output['stage']}",
        f"- 状态：{output['status']}",
        f"- 协议哈希：`{output['protocol_hash']}`",
        f"- 政策哈希：`{output['policy_hash']}`",
        f"- 累计收益：{result['total_return']:.2%}",
        f"- 同仓位沪深300：{result['benchmark_total_return']:.2%}",
        f"- 平均每回合超额：{result['paired_comparison']['mean_excess_return']:.3%}",
        f"- 超额为正概率：{result['paired_comparison']['probability_mean_excess_positive']:.1%}",
        f"- 平均 Rank IC：{result['mean_rank_ic']:.3f}",
        f"- 最大回撤：{result['max_drawdown']:.2%}",
        f"- 正收益年度：{result['positive_year_fraction']:.1%}",
        f"- 正超额年度：{result['positive_excess_year_fraction']:.1%}",
        f"- 准入：{admission['passed']}",
        "",
        "## 年度结果",
        "",
        "| 年度 | 策略 | 同仓位沪深300 | 超额 |",
        "|---|---:|---:|---:|",
    ]
    for year, item in result["annual"].items():
        lines.append(
            f"| {year} | {item['strategy_return']:.2%} | "
            f"{item['benchmark_return']:.2%} | {item['excess_return']:.2%} |"
        )
    lines.extend(["", "## 边界", "", output["claim_boundary"], ""])
    return "\n".join(lines)


def _load_period(
    settings: Settings,
    start: date,
    end: date,
    provider: BaoStockProvider | None,
) -> tuple[dict[date, list[dict[str, Any]]], pd.DataFrame, dict[str, Any], str]:
    repository = LearningRepository(settings.resolve(settings.get("system.database_path")))
    samples, sample_audit = _market_samples(
        repository,
        start,
        end,
        5,
        required_protocol_hash=EXPECTED_SAMPLE_PROTOCOL_HASH,
    )
    if sample_audit["selected_sample_protocol_hash"] != EXPECTED_SAMPLE_PROTOCOL_HASH:
        raise ValueError("V3 sample protocol hash does not match the preregistered dataset")
    groups = _group_samples(samples)
    if len(groups) < 20:
        raise ValueError("A-share strategy V3 has insufficient point-in-time market dates")
    source = provider or BaoStockProvider(
        cache_dir=settings.resolve(settings.get("system.data_dir")) / "cache" / "baostock"
    )
    benchmark = str(FROZEN_POLICY["market_regime"]["benchmark_symbol"])
    symbols = sorted({item["symbol"] for item in samples} | {benchmark})
    bars = source.cached_bars(symbols) if hasattr(source, "cached_bars") else []
    available = {bar.symbol for bar in bars}
    missing = [symbol for symbol in symbols if symbol not in available]
    if missing:
        bars.extend(source.bars(missing, start - timedelta(days=400), end + timedelta(days=90)))
    benchmark_rows = [bar for bar in bars if bar.symbol == benchmark]
    if not benchmark_rows or min(bar.date for bar in benchmark_rows) > start - timedelta(days=250):
        bars = [bar for bar in bars if bar.symbol != benchmark]
        bars.extend(source.bars([benchmark], start - timedelta(days=400), end + timedelta(days=90)))
    frame = _bar_frame(bars)
    if frame.empty:
        raise ValueError("A-share strategy V3 has no execution history")
    return groups, frame, sample_audit, source.name


def _evaluate_v3_policy(
    groups: dict[date, list[dict[str, Any]]],
    frame: pd.DataFrame,
    policy: dict[str, Any],
    *,
    holding_horizon_days: int,
    cost_model: CostModel,
    include_ranking_diagnostics: bool = True,
) -> dict[str, Any]:
    benchmark_symbol = str(policy["market_regime"]["benchmark_symbol"])
    benchmark_rows = frame[frame["symbol"] == benchmark_symbol].sort_values("date")
    benchmark_dates = [pd.Timestamp(value).date() for value in benchmark_rows["date"].tolist()]
    benchmark_index = {day: index for index, day in enumerate(benchmark_dates)}
    capital = 100_000.0
    benchmark_capital = 100_000.0
    peak = capital
    max_drawdown = 0.0
    returns: list[float] = []
    benchmark_returns: list[float] = []
    rank_ics: list[float] = []
    episodes = []
    annual_returns: dict[int, list[float]] = defaultdict(list)
    annual_benchmark: dict[int, list[float]] = defaultdict(list)
    regime_counts: dict[str, int] = defaultdict(int)
    for signal_date in sorted(groups):
        index = benchmark_index.get(signal_date)
        if index is None or index + holding_horizon_days >= len(benchmark_dates):
            continue
        target_date = benchmark_dates[index + holding_horizon_days]
        effective_policy, regime = resolve_a_share_ranking_policy(policy, frame, signal_date)
        portfolio = effective_policy["portfolio"]
        top_k = int(portfolio["top_k"])
        total_exposure = float(portfolio["total_exposure"])
        items = groups[signal_date]
        scores = score_a_share_ranking_policy(items, effective_policy)
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
            benchmark_symbol,
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
        regime_counts[str(regime["regime"])] += 1
        rank_ic = None
        if include_ranking_diagnostics:
            outcomes = {
                item["symbol"]: _market_outcome_return(
                    frame, item["symbol"], signal_date, target_date
                )
                for item in items
            }
            comparable = [symbol for symbol, value in outcomes.items() if value is not None]
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
                "regime": regime,
                "selected_symbols": [item["symbol"] for item in selected],
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "traded_legs": int(trade["traded_legs"]),
                "requested_legs": top_k,
                "rank_ic": rank_ic,
            }
        )
    if not episodes:
        raise ValueError("A-share strategy V3 produced no executable episodes")
    differences = np.asarray(returns) - np.asarray(benchmark_returns)
    rng = np.random.default_rng(20260714)
    bootstrapped = np.asarray(
        [
            float(rng.choice(differences, size=len(differences), replace=True).mean())
            for _ in range(4_000)
        ]
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
        "regime_counts": dict(regime_counts),
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
        "mean_rank_ic": (
            float(np.mean(rank_ics))
            if rank_ics
            else None
            if not include_ranking_diagnostics
            else 0.0
        ),
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
        "positive_excess_year_fraction_at_least_60pct": (
            result["positive_excess_year_fraction"] >= 0.60
        ),
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
        "bootstrap_probability_at_least_80pct": (
            comparison["probability_mean_excess_positive"] >= 0.80
        ),
        "bootstrap_lower_bound_not_below_minus_20bp": (
            comparison["bootstrap_90pct_interval"][0] >= -0.002
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


def _validate_development_report(report: dict[str, Any]) -> None:
    if report.get("status") != "development_passed_validation_locked":
        raise ValueError("V3 development has not passed; validation must remain locked")
    if report.get("protocol_hash") != a_share_v3_protocol_hash():
        raise ValueError("V3 development protocol hash does not match the frozen implementation")
    if report.get("policy_hash") != a_share_ranking_policy_hash(FROZEN_POLICY):
        raise ValueError("V3 development policy hash does not match the frozen policy")
    if report.get("sample_audit", {}).get("selected_sample_protocol_hash") != (
        EXPECTED_SAMPLE_PROTOCOL_HASH
    ):
        raise ValueError("V3 development sample protocol hash does not match")
    if not report.get("development_result", {}).get("admission", {}).get("passed"):
        raise ValueError("V3 development admission checks did not pass")


def _report_hash(report: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def _write_report(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
