from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from quantlab import __version__
from quantlab.backtest import (
    aggregate_fold_metrics,
    equity_return_series,
    paired_block_bootstrap,
    sharpe_significance,
)
from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.persistence import TerminalRepository
from quantlab.workflows.etf import run_etf_backtest, run_etf_static_backtest


DEVELOPMENT_START = date(2015, 1, 5)
DEVELOPMENT_END = date(2022, 12, 30)
LOCKED_HOLDOUT_START = date(2023, 1, 3)
LOCKED_HOLDOUT_END = date(2026, 6, 30)


ADAPTIVE_ETF_CANDIDATES: tuple[dict[str, Any], ...] = (
    {
        "name": "adaptive_balanced",
        "core_weight": 0.60,
        "top_k": 2,
        "breadth_threshold": 0.50,
        "risk_off_multiplier": 0.25,
        "target_volatility": 0.12,
    },
    {
        "name": "adaptive_core",
        "core_weight": 0.75,
        "top_k": 2,
        "breadth_threshold": 0.40,
        "risk_off_multiplier": 0.50,
        "target_volatility": 0.14,
    },
    {
        "name": "adaptive_defensive",
        "core_weight": 0.50,
        "top_k": 3,
        "breadth_threshold": 0.60,
        "risk_off_multiplier": 0.0,
        "target_volatility": 0.10,
    },
    {
        "name": "adaptive_concentrated",
        "core_weight": 0.50,
        "top_k": 1,
        "breadth_threshold": 0.50,
        "risk_off_multiplier": 0.25,
        "target_volatility": 0.12,
    },
)


def run_adaptive_etf_candidate_lab(settings: Settings, save: bool = True) -> dict[str, Any]:
    base_cfg = dict(settings.get("strategies.etf_rotation"))
    symbols = list(base_cfg["universe"])
    fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
    provider = CachedProvider(
        fallback,
        settings.resolve(settings.get("system.data_dir")) / "cache",
    )
    bars = provider.bars(
        symbols,
        DEVELOPMENT_START - timedelta(days=400),
        LOCKED_HOLDOUT_END,
    )
    frame = pd.DataFrame([bar.model_dump() for bar in bars])
    if frame.empty:
        raise ValueError("no ETF data returned for adaptive strategy lab")
    frame["date"] = pd.to_datetime(frame["date"])
    signal_frame = frame.copy()
    signal_frame["close"] = signal_frame["adjusted_close"].fillna(signal_frame["close"])
    coverage = _coverage(signal_frame, symbols)
    if not coverage["fresh_enough"]:
        raise ValueError("adaptive strategy lab requires fresh complete ETF coverage")

    candidates = []
    development_dates = _common_dates(
        signal_frame,
        symbols,
        DEVELOPMENT_START,
        DEVELOPMENT_END,
    )
    development_segments = _segments(development_dates, 6)
    for specification in ADAPTIVE_ETF_CANDIDATES:
        config = _candidate_config(base_cfg, specification)
        result = run_etf_backtest(
            settings,
            bars,
            signal_frame,
            symbols,
            config,
            DEVELOPMENT_START,
            DEVELOPMENT_END,
        )
        segment_metrics = [
            _window_metrics(result, start, end) for start, end in development_segments
        ]
        aggregate = aggregate_fold_metrics(segment_metrics)
        score = _development_selection_score(aggregate)
        candidates.append(
            {
                "name": specification["name"],
                "config": _public_config(config),
                "development": aggregate,
                "development_segments": [
                    {
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "metrics": metrics,
                    }
                    for (start, end), metrics in zip(development_segments, segment_metrics)
                ],
                "selection_score": score,
            }
        )
    candidates.sort(key=lambda item: (-item["selection_score"], item["name"]))
    best_score = candidates[0]["selection_score"]
    near_best = [item for item in candidates if item["selection_score"] >= best_score - 0.05]
    selected = max(
        near_best,
        key=lambda item: (
            float(item["config"]["core_weight"]),
            int(item["config"]["top_k"]),
            item["name"],
        ),
    )
    selected_config = _candidate_config(base_cfg, selected["config"] | {"name": selected["name"]})

    selected_result = run_etf_backtest(
        settings,
        bars,
        signal_frame,
        symbols,
        selected_config,
        LOCKED_HOLDOUT_START,
        LOCKED_HOLDOUT_END,
    )
    legacy_config = {**base_cfg, "core_weight": float(base_cfg.get("core_weight", 0.0))}
    legacy_result = run_etf_backtest(
        settings,
        bars,
        signal_frame,
        symbols,
        legacy_config,
        LOCKED_HOLDOUT_START,
        LOCKED_HOLDOUT_END,
    )
    exposure = float(settings.get("risk.max_total_exposure"))
    benchmark_targets = {
        "hs300_buy_hold": {"sh510300": exposure},
        "equal_weight_buy_hold": {symbol: exposure / len(symbols) for symbol in symbols},
    }
    benchmark_results = {
        name: run_etf_static_backtest(
            settings,
            bars,
            symbols,
            targets,
            LOCKED_HOLDOUT_START,
            LOCKED_HOLDOUT_END,
        )
        for name, targets in benchmark_targets.items()
    }
    selected_metrics = _window_metrics(
        selected_result, LOCKED_HOLDOUT_START, LOCKED_HOLDOUT_END
    )
    legacy_metrics = _window_metrics(legacy_result, LOCKED_HOLDOUT_START, LOCKED_HOLDOUT_END)
    benchmark_metrics = {
        name: _window_metrics(result, LOCKED_HOLDOUT_START, LOCKED_HOLDOUT_END)
        for name, result in benchmark_results.items()
    }
    stress_result = run_etf_backtest(
        settings,
        bars,
        signal_frame,
        symbols,
        selected_config,
        LOCKED_HOLDOUT_START,
        LOCKED_HOLDOUT_END,
        cost_multiplier=2.0,
    )
    stress_metrics = _window_metrics(stress_result, LOCKED_HOLDOUT_START, LOCKED_HOLDOUT_END)
    paired = _paired_returns(selected_result, benchmark_results["equal_weight_buy_hold"])
    inference = paired_block_bootstrap(
        [item[1] for item in paired],
        [item[2] for item in paired],
        block_size=int(settings.get("backtest.bootstrap_block_days", 20)),
        simulations=int(settings.get("backtest.bootstrap_simulations", 2_000)),
    )
    sharpe = sharpe_significance(
        [item[1] for item in paired],
        tested_variants=len(ADAPTIVE_ETF_CANDIDATES),
    )
    relative = {
        name: {
            "total_return_delta": selected_metrics["total_return"] - metrics["total_return"],
            "sharpe_delta": selected_metrics["sharpe"] - metrics["sharpe"],
            "max_drawdown_delta": (
                selected_metrics["max_drawdown"] - metrics["max_drawdown"]
            ),
        }
        for name, metrics in benchmark_metrics.items()
    }
    admission = _candidate_admission(
        selected_metrics,
        relative["equal_weight_buy_hold"],
        stress_metrics,
        inference,
        sharpe,
        len(paired),
    )
    reproducibility = {
        "quantlab_version": __version__,
        "protocol": "STRATEGY_RESEARCH_PROTOCOL.md",
        "candidate_count": len(ADAPTIVE_ETF_CANDIDATES),
        "data_fingerprint_sha256": _data_fingerprint(signal_frame, symbols),
        "code_fingerprint_sha256": _code_fingerprint(),
    }
    experiment_id = hashlib.sha256(
        json.dumps(
            {
                "reproducibility": reproducibility,
                "development": [DEVELOPMENT_START.isoformat(), DEVELOPMENT_END.isoformat()],
                "holdout": [LOCKED_HOLDOUT_START.isoformat(), LOCKED_HOLDOUT_END.isoformat()],
                "candidates": ADAPTIVE_ETF_CANDIDATES,
                "selected": selected["name"],
                "cost_model": dict(settings.get("costs.etf")),
                "exposure": exposure,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    output = {
        "experiment_id": experiment_id,
        "strategy": "adaptive_etf_rotation_candidate",
        "status": (
            "eligible_for_prospective_observation"
            if admission["passed"]
            else "historical_holdout_rejected"
        ),
        "research_only": True,
        "formal_strategy_changed": False,
        "protocol": {
            "development": {
                "start": DEVELOPMENT_START.isoformat(),
                "end": DEVELOPMENT_END.isoformat(),
            },
            "locked_historical_holdout": {
                "start": LOCKED_HOLDOUT_START.isoformat(),
                "end": LOCKED_HOLDOUT_END.isoformat(),
                "classification": "semi_confirmatory_due_to_prior_market_research",
            },
            "selection_tolerance": 0.05,
        },
        "source": provider.name,
        "degraded_sources": list(fallback.last_degraded_from),
        "data_coverage": coverage,
        "development_candidates": candidates,
        "selected_candidate": selected,
        "locked_holdout": {
            "selected_candidate_metrics": selected_metrics,
            "legacy_rotation_metrics": legacy_metrics,
            "benchmarks": benchmark_metrics,
            "relative_to_benchmarks": relative,
            "paired_oos_days": len(paired),
            "excess_return_inference": inference,
            "sharpe_significance": sharpe,
            "two_x_cost_metrics": stress_metrics,
            "admission": admission,
        },
        "reproducibility": reproducibility,
        "claim_boundary": (
            "the historical holdout was locked before candidate execution, but prior research on the "
            "same market prevents treating it as pristine confirmation; prospective paper evidence is required"
        ),
    }
    if save:
        output["validation_id"] = TerminalRepository(
            settings.resolve(settings.get("system.database_path"))
        ).save_strategy_validation(
            "adaptive_etf_rotation_candidate",
            DEVELOPMENT_START,
            LOCKED_HOLDOUT_END,
            len(development_dates),
            len(paired),
            output,
        )
        output["reports"] = export_strategy_lab_report(settings, output)
    return output


def export_strategy_lab_report(settings: Settings, output: dict[str, Any]) -> dict[str, str]:
    target = settings.resolve("data/reports")
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "strategy-lab-latest.json"
    markdown_path = target / "strategy-lab-latest.md"
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    holdout = output["locked_holdout"]
    selected = output["selected_candidate"]
    metrics = holdout["selected_candidate_metrics"]
    relative = holdout["relative_to_benchmarks"]["equal_weight_buy_hold"]
    lines = [
        "# QuantLab ETF 策略候选实验",
        "",
        f"- 实验 ID：{output['experiment_id']}",
        f"- 开发期冠军：{selected['name']}",
        f"- 状态：{output['status']}",
        f"- 留出收益：{metrics['total_return']:.2%}",
        f"- 留出 Sharpe：{metrics['sharpe']:.3f}",
        f"- 留出最大回撤：{metrics['max_drawdown']:.2%}",
        f"- 相对 ETF 等权收益：{relative['total_return_delta']:+.2%}",
        f"- 2倍成本收益：{holdout['two_x_cost_metrics']['total_return']:.2%}",
        f"- 准入：{holdout['admission']['passed']}",
        "",
        f"> {output['claim_boundary']}",
    ]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def _candidate_config(base: dict, specification: dict) -> dict:
    return {
        **base,
        **specification,
        "strategy_variant": "adaptive_v1",
        "lookbacks": (20, 60, 120),
        "momentum_weights": (0.50, 0.30, 0.20),
        "volatility_lookback": 60,
        "trend_lookback": 120,
        "rank_buffer": 1,
    }


def _public_config(config: dict) -> dict:
    return {
        key: config[key]
        for key in (
            "strategy_variant",
            "lookbacks",
            "momentum_weights",
            "top_k",
            "core_weight",
            "breadth_threshold",
            "risk_off_multiplier",
            "target_volatility",
            "volatility_lookback",
            "trend_lookback",
            "rank_buffer",
            "defensive_symbol",
        )
    }


def _development_selection_score(metrics: dict[str, float]) -> float:
    return float(
        metrics["mean_sharpe"]
        + 0.50 * metrics["mean_annualized_return"]
        - 2.0 * abs(metrics["worst_max_drawdown"])
        + 0.20 * metrics["positive_fold_rate"]
        + 0.10 * max(-2.0, min(2.0, metrics["stability_score"]))
    )


def _candidate_admission(
    metrics: dict[str, float],
    relative: dict[str, float],
    stress: dict[str, float],
    inference: dict[str, Any],
    sharpe: dict[str, Any],
    observations: int,
) -> dict[str, Any]:
    benchmark_passed = bool(
        (
            relative["total_return_delta"] >= 0
            and relative["sharpe_delta"] >= 0
        )
        or (
            relative["total_return_delta"] >= -0.02
            and relative["sharpe_delta"] >= 0
            and relative["max_drawdown_delta"] >= 0.02
        )
    )
    gates = {
        "absolute": metrics["sharpe"] > 0 and metrics["max_drawdown"] > -0.25,
        "benchmark": benchmark_passed,
        "statistical": (
            observations >= 504
            and float(inference.get("probability_alpha_positive", 0)) >= 0.90
            and float(sharpe.get("multiple_testing_adjusted_psr", 0)) >= 0.70
        ),
        "cost_stress": stress["sharpe"] > 0 and stress["max_drawdown"] > -0.30,
    }
    return {
        "passed": all(gates.values()),
        "gates": gates,
        "criteria": {
            "minimum_holdout_days": 504,
            "minimum_alpha_positive_probability": 0.90,
            "minimum_four_candidate_adjusted_psr": 0.70,
            "maximum_base_drawdown": -0.25,
            "maximum_two_x_cost_drawdown": -0.30,
        },
    }


def _window_metrics(result, start: date, end: date) -> dict[str, float]:
    from quantlab.backtest import calculate_equity_metrics

    curve = [(day, value) for day, value in result.equity_curve if start <= day <= end]
    turnover = sum(1 for fill in result.fills if start <= fill.trade_date <= end)
    return calculate_equity_metrics(curve, turnover)


def _common_dates(frame: pd.DataFrame, symbols: list[str], start: date, end: date) -> list[date]:
    common = frame[frame["symbol"].isin(symbols)].groupby("date")["symbol"].nunique()
    return [
        timestamp.date()
        for timestamp, count in common.items()
        if count == len(symbols) and start <= timestamp.date() <= end
    ]


def _segments(dates: list[date], count: int) -> list[tuple[date, date]]:
    if len(dates) < count:
        raise ValueError("not enough development dates for stability segments")
    boundaries = [round(index * len(dates) / count) for index in range(count + 1)]
    return [(dates[boundaries[index]], dates[boundaries[index + 1] - 1]) for index in range(count)]


def _paired_returns(strategy_result, benchmark_result) -> list[tuple[date, float, float]]:
    strategy = dict(
        equity_return_series(
            strategy_result.equity_curve,
            LOCKED_HOLDOUT_START,
            LOCKED_HOLDOUT_END,
        )
    )
    benchmark = dict(
        equity_return_series(
            benchmark_result.equity_curve,
            LOCKED_HOLDOUT_START,
            LOCKED_HOLDOUT_END,
        )
    )
    return [
        (day, strategy[day], benchmark[day])
        for day in sorted(set(strategy) & set(benchmark))
    ]


def _coverage(frame: pd.DataFrame, symbols: list[str]) -> dict[str, Any]:
    per_symbol = {
        symbol: {
            "first": group["date"].min().date().isoformat(),
            "last": group["date"].max().date().isoformat(),
            "bars": int(len(group)),
        }
        for symbol, group in frame.groupby("symbol")
    }
    common = _common_dates(frame, symbols, DEVELOPMENT_START, LOCKED_HOLDOUT_END)
    gap = (LOCKED_HOLDOUT_END - max(common)).days if common else 10_000
    return {
        "per_symbol": per_symbol,
        "common_start": min(common).isoformat() if common else None,
        "common_end": max(common).isoformat() if common else None,
        "common_trading_days": len(common),
        "requested_end_gap_days": gap,
        "fresh_enough": set(symbols) <= set(per_symbol) and gap <= 15,
    }


def _data_fingerprint(frame: pd.DataFrame, symbols: list[str]) -> str:
    bounded = frame[frame["symbol"].isin(symbols)][["symbol", "date", "close", "volume"]].copy()
    bounded["date"] = bounded["date"].dt.strftime("%Y-%m-%d")
    payload = bounded.sort_values(["date", "symbol"]).to_csv(
        index=False, float_format="%.10g", lineterminator="\n"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _code_fingerprint() -> str:
    package_root = Path(__file__).resolve().parents[1]
    relative_paths = (
        "strategies/adaptive_etf.py",
        "workflows/strategy_lab.py",
        "workflows/etf.py",
        "backtest/engine.py",
        "backtest/statistics.py",
        "execution/costs.py",
    )
    digest = hashlib.sha256()
    for relative in relative_paths:
        path = package_root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
