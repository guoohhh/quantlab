from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from quantlab.backtest import (
    aggregate_fold_metrics,
    calculate_equity_metrics,
    equity_return_series,
    paired_block_bootstrap,
    robust_selection_score,
    selection_overfit_diagnostics,
    sharpe_significance,
    walk_forward_splits,
)
from quantlab import __version__
from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.persistence import TerminalRepository
from quantlab.workflows.etf import (
    run_etf_backtest,
    run_etf_core_protocol_backtest,
    run_etf_static_backtest,
)


def run_etf_walk_forward(
    settings: Settings,
    start: date,
    end: date,
    train_days: int | None = None,
    test_days: int | None = None,
    save: bool = True,
) -> dict[str, Any]:
    base_cfg = dict(settings.get("strategies.etf_rotation"))
    symbols = list(base_cfg["universe"])
    train_size = int(train_days or settings.get("backtest.walk_forward_train_days", 756))
    test_size = int(test_days or settings.get("backtest.walk_forward_test_days", 252))
    embargo_size = int(settings.get("backtest.walk_forward_embargo_days", 1))
    parameter_grid = _parameter_grid(base_cfg)
    max_lookback = max(max(item["lookbacks"]) for item in parameter_grid)

    fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
    provider = CachedProvider(
        fallback,
        settings.resolve(settings.get("system.data_dir")) / "cache",
    )
    bars = provider.bars(
        symbols,
        start - timedelta(days=max_lookback * 3),
        end,
    )
    frame = pd.DataFrame([bar.model_dump() for bar in bars])
    if frame.empty:
        raise ValueError("no ETF data returned for walk-forward validation")
    frame["date"] = pd.to_datetime(frame["date"])
    coverage = {
        symbol: {
            "first": group["date"].min().date().isoformat(),
            "last": group["date"].max().date().isoformat(),
            "bars": int(len(group)),
        }
        for symbol, group in frame.groupby("symbol")
    }
    signal_frame = frame.copy()
    signal_frame["close"] = signal_frame["adjusted_close"].fillna(signal_frame["close"])
    common = signal_frame[signal_frame["symbol"].isin(symbols)].groupby("date")["symbol"].nunique()
    dates = [
        timestamp.date()
        for timestamp, count in common.items()
        if count == len(symbols) and start <= timestamp.date() <= end
    ]
    if not dates:
        raise ValueError("ETF universe has no complete common trading dates")
    data_coverage = {
        "symbols_requested": symbols,
        "symbols_covered": sorted(coverage),
        "per_symbol": coverage,
        "common_start": min(dates).isoformat(),
        "common_end": max(dates).isoformat(),
        "common_trading_days": len(dates),
        "requested_end_gap_days": (end - max(dates)).days,
        "fresh_enough": (end - max(dates)).days <= 15,
    }
    folds = walk_forward_splits(
        dates,
        train_size,
        test_size,
        embargo_days=embargo_size,
    )
    if not folds:
        raise ValueError(f"not enough common trading days for train={train_size}, test={test_size}")

    fold_results = []
    benchmark_metrics: dict[str, list[dict[str, float]]] = {
        "hs300_buy_hold": [],
        "equal_weight_buy_hold": [],
        "defensive_buy_hold": [],
    }
    sensitivity_metrics: dict[str, list[dict[str, float]]] = {
        _config_key(config): [] for config in parameter_grid
    }
    selection_audit = []
    paired_oos_returns: dict[date, tuple[float, float]] = {}
    cost_multipliers = (1.0, 1.5, 2.0)
    cost_stress_metrics: dict[float, dict[str, list[dict[str, float]]]] = {
        multiplier: {"strategy": [], "equal_weight_buy_hold": []}
        for multiplier in cost_multipliers
    }
    for fold in folds:
        training = []
        for config in parameter_grid:
            result = run_etf_backtest(
                settings,
                bars,
                signal_frame,
                symbols,
                config,
                fold.train_start,
                fold.train_end,
            )
            metrics = _window_metrics(result, fold.train_start, fold.train_end)
            training.append(
                {
                    "config": config,
                    "metrics": metrics,
                    "selection_score": robust_selection_score(metrics),
                }
            )
        selected = max(training, key=lambda item: item["selection_score"])
        selected_config_key = _config_key(selected["config"])
        tests = []
        selected_test = None
        selected_result = None
        for config in parameter_grid:
            result = run_etf_backtest(
                settings,
                bars,
                signal_frame,
                symbols,
                config,
                fold.test_start,
                fold.test_end,
            )
            metrics = _window_metrics(result, fold.test_start, fold.test_end)
            sensitivity_metrics[_config_key(config)].append(metrics)
            tests.append((config, metrics))
            if _config_key(config) == selected_config_key:
                selected_test = metrics
                selected_result = result
        if selected_test is None or selected_result is None:
            raise RuntimeError("selected ETF configuration was not evaluated in the test fold")
        selection_audit.append(
            {
                "fold": fold.fold,
                "selected_key": selected_config_key,
                "candidates": [
                    {
                        "key": _config_key(config),
                        "train_score": next(
                            item["selection_score"]
                            for item in training
                            if _config_key(item["config"]) == _config_key(config)
                        ),
                        "test_score": robust_selection_score(metrics),
                    }
                    for config, metrics in tests
                ],
            }
        )
        exposure = float(settings.get("risk.max_total_exposure"))
        benchmark_targets = {
            "hs300_buy_hold": {"sh510300": exposure},
            "equal_weight_buy_hold": {symbol: exposure / len(symbols) for symbol in symbols},
            "defensive_buy_hold": {base_cfg["defensive_symbol"]: exposure},
        }
        fold_benchmarks = {}
        fold_benchmark_results = {}
        for benchmark_name, targets in benchmark_targets.items():
            benchmark_result = run_etf_static_backtest(
                settings,
                bars,
                symbols,
                targets,
                fold.test_start,
                fold.test_end,
            )
            metrics = _window_metrics(benchmark_result, fold.test_start, fold.test_end)
            benchmark_metrics[benchmark_name].append(metrics)
            fold_benchmarks[benchmark_name] = metrics
            fold_benchmark_results[benchmark_name] = benchmark_result
        cost_stress_metrics[1.0]["strategy"].append(selected_test)
        cost_stress_metrics[1.0]["equal_weight_buy_hold"].append(
            fold_benchmarks["equal_weight_buy_hold"]
        )
        fold_cost_stress = {
            "1.0": {
                "strategy": selected_test,
                "equal_weight_buy_hold": fold_benchmarks["equal_weight_buy_hold"],
            }
        }
        for multiplier in cost_multipliers[1:]:
            stressed_strategy = run_etf_backtest(
                settings,
                bars,
                signal_frame,
                symbols,
                selected["config"],
                fold.test_start,
                fold.test_end,
                cost_multiplier=multiplier,
            )
            stressed_benchmark = run_etf_static_backtest(
                settings,
                bars,
                symbols,
                benchmark_targets["equal_weight_buy_hold"],
                fold.test_start,
                fold.test_end,
                cost_multiplier=multiplier,
            )
            stressed_strategy_metrics = _window_metrics(
                stressed_strategy, fold.test_start, fold.test_end
            )
            stressed_benchmark_metrics = _window_metrics(
                stressed_benchmark, fold.test_start, fold.test_end
            )
            cost_stress_metrics[multiplier]["strategy"].append(stressed_strategy_metrics)
            cost_stress_metrics[multiplier]["equal_weight_buy_hold"].append(
                stressed_benchmark_metrics
            )
            fold_cost_stress[f"{multiplier:.1f}"] = {
                "strategy": stressed_strategy_metrics,
                "equal_weight_buy_hold": stressed_benchmark_metrics,
            }
        strategy_returns = dict(
            equity_return_series(
                selected_result.equity_curve,
                fold.test_start,
                fold.test_end,
            )
        )
        benchmark_returns = dict(
            equity_return_series(
                fold_benchmark_results["equal_weight_buy_hold"].equity_curve,
                fold.test_start,
                fold.test_end,
            )
        )
        for day in sorted(set(strategy_returns) & set(benchmark_returns)):
            paired_oos_returns[day] = (strategy_returns[day], benchmark_returns[day])
        fold_results.append(
            {
                **fold.to_dict(),
                "selected_config": _serializable_config(selected["config"]),
                "train_metrics": selected["metrics"],
                "train_selection_score": selected["selection_score"],
                "test_metrics": selected_test,
                "benchmarks": fold_benchmarks,
                "cost_stress": fold_cost_stress,
            }
        )

    sensitivity = []
    for config in parameter_grid:
        key = _config_key(config)
        aggregate = aggregate_fold_metrics(sensitivity_metrics[key])
        sensitivity.append(
            {
                "config": _serializable_config(config),
                "oos": aggregate,
                "robust_score": (
                    aggregate["mean_sharpe"]
                    + 0.5 * aggregate["mean_annualized_return"]
                    - 2 * abs(aggregate["worst_max_drawdown"])
                ),
            }
        )
    sensitivity.sort(key=lambda item: item["robust_score"], reverse=True)
    selected_oos = aggregate_fold_metrics([item["test_metrics"] for item in fold_results])
    benchmark_oos = {
        name: aggregate_fold_metrics(metrics) for name, metrics in benchmark_metrics.items()
    }
    relative_to_benchmarks = {
        name: {
            "compounded_return_delta": (
                selected_oos["compounded_return"] - metrics["compounded_return"]
            ),
            "mean_sharpe_delta": selected_oos["mean_sharpe"] - metrics["mean_sharpe"],
            "worst_drawdown_delta": (
                selected_oos["worst_max_drawdown"] - metrics["worst_max_drawdown"]
            ),
        }
        for name, metrics in benchmark_oos.items()
    }
    paired_days = sorted(paired_oos_returns)
    strategy_daily = [paired_oos_returns[day][0] for day in paired_days]
    benchmark_daily = [paired_oos_returns[day][1] for day in paired_days]
    bootstrap_simulations = int(settings.get("backtest.bootstrap_simulations", 2_000))
    bootstrap_block_days = int(settings.get("backtest.bootstrap_block_days", 20))
    cost_stress_oos = {}
    for multiplier in cost_multipliers:
        strategy_metrics = aggregate_fold_metrics(cost_stress_metrics[multiplier]["strategy"])
        reference_metrics = aggregate_fold_metrics(
            cost_stress_metrics[multiplier]["equal_weight_buy_hold"]
        )
        cost_stress_oos[f"{multiplier:.1f}"] = {
            "strategy": strategy_metrics,
            "equal_weight_buy_hold": reference_metrics,
            "compounded_return_delta": (
                strategy_metrics["compounded_return"] - reference_metrics["compounded_return"]
            ),
            "mean_sharpe_delta": strategy_metrics["mean_sharpe"]
            - reference_metrics["mean_sharpe"],
        }
    robustness = {
        "data_coverage": data_coverage,
        "oos_daily_observations": len(paired_days),
        "excess_return_inference": paired_block_bootstrap(
            strategy_daily,
            benchmark_daily,
            block_size=bootstrap_block_days,
            simulations=bootstrap_simulations,
        ),
        "sharpe_significance": sharpe_significance(
            strategy_daily,
            tested_variants=len(parameter_grid),
        ),
        "selection_overfit": selection_overfit_diagnostics(selection_audit),
        "cost_stress": cost_stress_oos,
    }
    admission = evaluate_strategy_admission(
        selected_oos,
        relative_to_benchmarks,
        robustness,
    )
    data_fingerprint = _data_fingerprint(signal_frame, symbols)
    code_fingerprint = _code_fingerprint()
    experiment_payload = {
        "strategy": "etf_rotation",
        "data_fingerprint": data_fingerprint,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "train_days": train_size,
        "test_days": test_size,
        "embargo_days": embargo_size,
        "parameter_grid": [_serializable_config(item) for item in parameter_grid],
        "cost_multipliers": cost_multipliers,
        "initial_capital": float(settings.get("system.initial_capital")),
        "maximum_total_exposure": float(settings.get("risk.max_total_exposure")),
        "cost_model": dict(settings.get("costs.etf")),
        "code_fingerprint_sha256": code_fingerprint,
        "version": __version__,
    }
    experiment_id = hashlib.sha256(
        json.dumps(experiment_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    output = {
        "experiment_id": experiment_id,
        "strategy": "etf_rotation",
        "method": (
            "embargoed rolling walk-forward; parameters selected on each training fold only; "
            "paired OOS inference and execution-cost stress"
        ),
        "source": provider.name,
        "degraded_sources": list(fallback.last_degraded_from),
        "data_coverage": data_coverage,
        "requested_range": {"start": start.isoformat(), "end": end.isoformat()},
        "train_days": train_size,
        "test_days": test_size,
        "embargo_days": embargo_size,
        "folds": fold_results,
        "selected_oos": selected_oos,
        "benchmark_oos": benchmark_oos,
        "relative_to_benchmarks": relative_to_benchmarks,
        "admission": admission,
        "robustness": robustness,
        "parameter_sensitivity": sensitivity,
        "reproducibility": {
            "quantlab_version": __version__,
            "data_fingerprint_sha256": data_fingerprint,
            "code_fingerprint_sha256": code_fingerprint,
            "experiment_payload_sha256": experiment_id,
            "tested_parameter_variants": len(parameter_grid),
            "bootstrap_seed": 20260714,
            "bootstrap_simulations": bootstrap_simulations,
            "bootstrap_block_days": bootstrap_block_days,
            "oos_return_series_sha256": hashlib.sha256(
                json.dumps(
                    [
                        [day.isoformat(), strategy, benchmark]
                        for day, (strategy, benchmark) in sorted(paired_oos_returns.items())
                    ],
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "execution_parameters": {
                "initial_capital": float(settings.get("system.initial_capital")),
                "maximum_total_exposure": float(settings.get("risk.max_total_exposure")),
                "cost_model": dict(settings.get("costs.etf")),
            },
        },
        "oos_daily_returns": [
            {
                "date": day.isoformat(),
                "strategy": paired_oos_returns[day][0],
                "equal_weight_buy_hold": paired_oos_returns[day][1],
            }
            for day in paired_days
        ],
        "guardrails": [
            "signals use data available on or before each trade date",
            "each test fold starts from cash and excludes training-fold positions",
            f"a {embargo_size}-trading-day embargo separates each training and test fold",
            "T-day close signals execute at the next available open with configured costs",
            "reference portfolios buy once at the next open, use the same ETF cost model and hold",
            "training winners are audited against every parameter variant in untouched OOS folds",
            "paired moving-block bootstrap preserves short-run dependence in excess returns",
            "configured ETF membership is not claimed to be a historical point-in-time universe",
            "all requested symbols must cover the requested tail within 15 calendar days",
        ],
    }
    if save:
        output["validation_id"] = TerminalRepository(
            settings.resolve(settings.get("system.database_path"))
        ).save_strategy_validation(
            "etf_rotation",
            start,
            end,
            train_size,
            test_size,
            output,
        )
    return output


def run_etf_core_protocol_validation(
    settings: Settings,
    start: date,
    end: date,
    save: bool = True,
) -> dict[str, Any]:
    """Validate the frozen production ETF core without the walk-forward memory footprint."""

    cfg = dict(settings.get("strategies.etf_rotation"))
    symbols = list(cfg["universe"])
    fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
    provider = CachedProvider(
        fallback,
        settings.resolve(settings.get("system.data_dir")) / "cache",
    )
    bars = provider.bars(symbols, start, end)
    frame = pd.DataFrame([bar.model_dump() for bar in bars])
    if frame.empty:
        raise ValueError("no ETF data returned for production core validation")
    frame["date"] = pd.to_datetime(frame["date"])
    common = frame[frame["symbol"].isin(symbols)].groupby("date")["symbol"].nunique()
    dates = [
        timestamp.date()
        for timestamp, count in common.items()
        if count == len(symbols) and start <= timestamp.date() <= end
    ]
    if len(dates) < 504:
        raise ValueError("ETF core validation requires at least 504 common trading days")
    effective_start = min(dates)
    effective_end = max(dates)
    evidence = _production_core_protocol_evidence(
        settings,
        bars,
        symbols,
        effective_start,
        effective_end,
    )
    signal_frame = frame.copy()
    signal_frame["close"] = signal_frame["adjusted_close"].fillna(signal_frame["close"])
    data_fingerprint = _data_fingerprint(signal_frame, symbols)
    code_fingerprint = _code_fingerprint()
    experiment_payload = {
        "strategy": "etf_equal_weight_core",
        "start": effective_start.isoformat(),
        "end": effective_end.isoformat(),
        "data_fingerprint": data_fingerprint,
        "code_fingerprint": code_fingerprint,
        "protocol": evidence["protocol"],
        "cost_model": dict(settings.get("costs.etf")),
    }
    experiment_id = hashlib.sha256(
        json.dumps(experiment_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    metrics = evidence["metrics"]
    output = {
        "experiment_id": experiment_id,
        "strategy": "etf_equal_weight_core",
        "method": "full-history frozen production protocol with 1x and 2x execution costs",
        "source": provider.name,
        "degraded_sources": list(fallback.last_degraded_from),
        "requested_range": {"start": start.isoformat(), "end": end.isoformat()},
        "data_coverage": {
            "symbols_requested": symbols,
            "symbols_covered": sorted(frame["symbol"].unique().tolist()),
            "common_start": effective_start.isoformat(),
            "common_end": effective_end.isoformat(),
            "common_trading_days": len(dates),
        },
        "production_core_protocol": evidence,
        "historical_protocol_metrics": metrics,
        "admission": {
            "passed": evidence["passed"],
            "checks": evidence["checks"],
            "scope": "frozen current six-ETF basket production protocol",
        },
        "reproducibility": {
            "quantlab_version": __version__,
            "data_fingerprint_sha256": data_fingerprint,
            "code_fingerprint_sha256": code_fingerprint,
            "experiment_payload_sha256": experiment_id,
        },
        "guardrails": [
            "T-day close targets execute at the next available open",
            "semiannual rebalance and 2% weight tolerance match the portfolio planner",
            "weights are adjusted only to keep 100-share lots executable under the single-name cap",
            "LLM decisions and ATR stops cannot alter this core protocol",
            "configured ETF membership is not claimed to be a historical point-in-time universe",
        ],
        "claim_boundary": evidence["claim_boundary"],
    }
    if save:
        output["validation_id"] = TerminalRepository(
            settings.resolve(settings.get("system.database_path"))
        ).save_strategy_validation(
            "etf_equal_weight_core",
            effective_start,
            effective_end,
            0,
            0,
            output,
        )
        output["reports"] = _write_etf_core_protocol_report(settings, output)
    return output


def evaluate_strategy_admission(
    selected_oos: dict[str, float],
    relative_to_benchmarks: dict[str, dict[str, float]],
    robustness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    absolute_gate = {
        "passed": bool(
            selected_oos.get("folds", 0) >= 3
            and selected_oos.get("positive_fold_rate", 0) >= 0.50
            and selected_oos.get("mean_sharpe", 0) > 0
            and selected_oos.get("worst_max_drawdown", -1) > -0.25
        ),
        "criteria": {
            "minimum_folds": 3,
            "minimum_positive_fold_rate": 0.50,
            "minimum_mean_sharpe": 0.0,
            "minimum_worst_drawdown": -0.25,
        },
    }
    benchmark_reference = "equal_weight_buy_hold"
    benchmark_delta = relative_to_benchmarks[benchmark_reference]
    benchmark_gate = {
        "passed": bool(
            (
                benchmark_delta["compounded_return_delta"] >= 0
                and benchmark_delta["mean_sharpe_delta"] >= 0
            )
            or (
                benchmark_delta["compounded_return_delta"] >= -0.02
                and benchmark_delta["mean_sharpe_delta"] >= 0
                and benchmark_delta["worst_drawdown_delta"] >= 0.02
            )
        ),
        "reference": benchmark_reference,
        "criteria": (
            "outperform return and Sharpe, or stay within 2% compounded return while "
            "improving Sharpe and worst drawdown by at least 2 percentage points"
        ),
        "observed": benchmark_delta,
    }
    robustness = robustness or {}
    inference = robustness.get("excess_return_inference", {})
    sharpe = robustness.get("sharpe_significance", {})
    overfit = robustness.get("selection_overfit", {})
    data_coverage = robustness.get("data_coverage", {})
    oos_observations = int(robustness.get("oos_daily_observations", 0))
    statistical_gate = {
        "passed": bool(
            oos_observations >= 504
            and float(inference.get("probability_alpha_positive", 0)) >= 0.95
            and float(inference.get("one_sided_p_value", 1)) <= 0.10
            and float(sharpe.get("multiple_testing_adjusted_psr", 0)) >= 0.80
            and float(overfit.get("overfit_fold_rate", 1)) <= 0.50
        ),
        "criteria": {
            "minimum_oos_daily_observations": 504,
            "minimum_probability_alpha_positive": 0.95,
            "maximum_one_sided_bootstrap_p_value": 0.10,
            "minimum_multiple_testing_adjusted_psr": 0.80,
            "maximum_overfit_fold_rate": 0.50,
        },
        "observed": {
            "oos_daily_observations": oos_observations,
            "probability_alpha_positive": inference.get("probability_alpha_positive"),
            "one_sided_p_value": inference.get("one_sided_p_value"),
            "multiple_testing_adjusted_psr": sharpe.get(
                "multiple_testing_adjusted_psr"
            ),
            "overfit_fold_rate": overfit.get("overfit_fold_rate"),
        },
    }
    two_x = robustness.get("cost_stress", {}).get("2.0", {}).get("strategy", {})
    stress_gate = {
        "passed": bool(
            two_x
            and float(two_x.get("mean_sharpe", 0)) > 0
            and float(two_x.get("worst_max_drawdown", -1)) > -0.30
        ),
        "criteria": {
            "cost_multiplier": 2.0,
            "minimum_mean_sharpe": 0.0,
            "minimum_worst_drawdown": -0.30,
        },
        "observed": two_x,
    }
    data_gate = {
        "passed": bool(
            data_coverage.get("fresh_enough")
            and set(data_coverage.get("symbols_requested", []))
            <= set(data_coverage.get("symbols_covered", []))
        ),
        "criteria": {
            "all_requested_symbols_present": True,
            "maximum_requested_end_gap_days": 15,
        },
        "observed": data_coverage,
    }
    passed = bool(
        absolute_gate["passed"]
        and benchmark_gate["passed"]
        and statistical_gate["passed"]
        and stress_gate["passed"]
        and data_gate["passed"]
    )
    return {
        "passed": passed,
        "absolute_gate": absolute_gate,
        "benchmark_gate": benchmark_gate,
        "statistical_gate": statistical_gate,
        "cost_stress_gate": stress_gate,
        "data_coverage_gate": data_gate,
        "consequence": (
            "eligible for dynamic budget"
            if passed
            else "restricted to low exploratory budget"
        ),
    }


def _parameter_grid(base_cfg: dict) -> list[dict]:
    base = tuple(int(value) for value in base_cfg["lookbacks"])
    lookback_sets = {
        tuple(max(5, int(round(value * scale / 5) * 5)) for value in base)
        for scale in (0.75, 1.0, 1.25)
    }
    top_values = sorted({1, int(base_cfg["top_k"]), min(3, len(base_cfg["universe"]))})
    core_weights = (0.0, 0.50, 0.75)
    return [
        {
            **base_cfg,
            "lookbacks": lookbacks,
            "top_k": top_k,
            "core_weight": core_weight,
        }
        for lookbacks in sorted(lookback_sets)
        for top_k in top_values
        for core_weight in core_weights
    ]


def _window_metrics(result, start: date, end: date) -> dict[str, float]:
    curve = [(day, value) for day, value in result.equity_curve if start <= day <= end]
    turnover = sum(1 for fill in result.fills if start <= fill.trade_date <= end)
    return calculate_equity_metrics(curve, turnover)


def _production_core_protocol_evidence(
    settings: Settings,
    bars,
    symbols: list[str],
    start: date,
    end: date,
) -> dict[str, Any]:
    target_exposure = min(
        float(settings.get("risk.max_total_exposure")),
        float(settings.get("strategies.etf_core.target_exposure", 0.45)),
    )
    rebalance_frequency = str(
        settings.get("strategies.etf_core.rebalance_frequency", "semiannual")
    )
    rebalance_tolerance = float(
        settings.get("strategies.etf_core.rebalance_tolerance_weight", 0.02)
    )
    drawdown_limit = float(settings.get("risk.max_portfolio_drawdown", 0.15))
    protocol = {
        "name": "semiannual_equal_weight_core_v1",
        "rebalance_frequency": rebalance_frequency,
        "rebalance_tolerance_weight": rebalance_tolerance,
        "trade_lot": 100,
        "target_exposure": target_exposure,
        "maximum_total_exposure": float(settings.get("risk.max_total_exposure")),
        "maximum_single_position": float(settings.get("risk.max_single_position")),
        "signal_at": "close",
        "execute_at": "next_open",
        "lot_aware": True,
        "llm_order_override": False,
        "atr_stop": False,
    }
    one_x = run_etf_core_protocol_backtest(
        settings,
        bars,
        symbols,
        start,
        end,
        rebalance_frequency=rebalance_frequency,
        rebalance_tolerance_weight=rebalance_tolerance,
        target_exposure=target_exposure,
    )
    two_x = run_etf_core_protocol_backtest(
        settings,
        bars,
        symbols,
        start,
        end,
        rebalance_frequency=rebalance_frequency,
        rebalance_tolerance_weight=rebalance_tolerance,
        target_exposure=target_exposure,
        cost_multiplier=2.0,
    )
    hs300 = run_etf_static_backtest(
        settings,
        bars,
        symbols,
        {"sh510300": target_exposure},
        start,
        end,
    )
    metrics = _window_metrics(one_x, start, end)
    two_x_metrics = _window_metrics(two_x, start, end)
    hs300_metrics = _window_metrics(hs300, start, end)
    checks = {
        "positive_total_return": metrics["total_return"] > 0,
        "positive_sharpe": metrics["sharpe"] > 0,
        "drawdown_within_configured_limit": metrics["max_drawdown"] >= -drawdown_limit,
        "positive_return_at_2x_cost": two_x_metrics["total_return"] > 0,
        "beats_hs300_total_return": metrics["total_return"] > hs300_metrics["total_return"],
    }
    return {
        "status": "historically_supported" if all(checks.values()) else "research_only",
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "protocol": protocol,
        "metrics": metrics,
        "two_x_cost_metrics": two_x_metrics,
        "hs300_buy_hold_metrics": hs300_metrics,
        "checks": checks,
        "passed": all(checks.values()),
        "claim_boundary": (
            "Cost-aware historical evidence for the frozen current six-ETF basket and matching "
            "production execution protocol; ETF membership is not a historical point-in-time universe "
            "and future returns are not guaranteed."
        ),
    }


def _write_etf_core_protocol_report(
    settings: Settings,
    output: dict[str, Any],
) -> dict[str, str]:
    reports_dir = settings.resolve(settings.get("system.data_dir")) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "etf-core-protocol-validation-latest.json"
    markdown_path = reports_dir / "etf-core-protocol-validation-latest.md"
    evidence = output["production_core_protocol"]
    metrics = evidence["metrics"]
    two_x = evidence["two_x_cost_metrics"]
    hs300 = evidence["hs300_buy_hold_metrics"]
    markdown = "\n".join(
        [
            "# ETF 核心生产协议验证",
            "",
            f"- 状态：{evidence['status']}",
            f"- 实验 ID：`{output['experiment_id']}`",
            f"- 区间：{evidence['period']['start']} 至 {evidence['period']['end']}",
            f"- 协议：{evidence['protocol']['name']}",
            f"- 累计收益：{metrics['total_return']:.2%}",
            f"- 年化收益：{metrics['annualized_return']:.2%}",
            f"- Sharpe：{metrics['sharpe']:.3f}",
            f"- 最大回撤：{metrics['max_drawdown']:.2%}",
            f"- 成交腿数：{int(metrics['turnover_count'])}",
            f"- 2倍成本收益：{two_x['total_return']:.2%}",
            f"- 沪深300同暴露收益：{hs300['total_return']:.2%}",
            "",
            "## 边界",
            "",
            output["claim_boundary"],
            "",
        ]
    )
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def _config_key(config: dict) -> str:
    return (
        f"{','.join(str(value) for value in config['lookbacks'])}|"
        f"{config['top_k']}|{float(config.get('core_weight', 0.0)):.2f}"
    )


def _serializable_config(config: dict) -> dict:
    return {
        "lookbacks": list(config["lookbacks"]),
        "top_k": int(config["top_k"]),
        "core_weight": float(config.get("core_weight", 0.0)),
        "defensive_symbol": config["defensive_symbol"],
    }


def _data_fingerprint(frame: pd.DataFrame, symbols: list[str]) -> str:
    columns = [column for column in ("symbol", "date", "close", "volume") if column in frame]
    bounded = frame[frame["symbol"].isin(symbols)][columns].copy()
    bounded["date"] = pd.to_datetime(bounded["date"]).dt.strftime("%Y-%m-%d")
    bounded = bounded.sort_values(["date", "symbol"])
    payload = bounded.to_csv(index=False, float_format="%.10g", lineterminator="\n")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _code_fingerprint() -> str:
    package_root = Path(__file__).resolve().parents[1]
    relative_paths = (
        "strategies/etf_rotation.py",
        "workflows/etf.py",
        "workflows/validation.py",
        "backtest/engine.py",
        "backtest/statistics.py",
        "execution/costs.py",
        "portfolio/etf_core.py",
        "portfolio/planner.py",
    )
    digest = hashlib.sha256()
    for relative in relative_paths:
        path = package_root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
