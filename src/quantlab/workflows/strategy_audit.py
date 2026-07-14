from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from quantlab import __version__
from quantlab.backtest import (
    calculate_equity_metrics,
    equity_return_series,
    paired_block_bootstrap,
    sharpe_significance,
)
from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.workflows.etf import (
    resolve_etf_variant_config,
    run_etf_backtest,
    run_etf_static_backtest,
)


AUDIT_START = date(2015, 1, 5)
AUDIT_END = date(2026, 6, 30)
CALENDAR_PERIODS: tuple[tuple[str, date, date], ...] = (
    ("2015_2017", date(2015, 1, 5), date(2017, 12, 29)),
    ("2018_2019", date(2018, 1, 2), date(2019, 12, 31)),
    ("2020_2022", date(2020, 1, 2), date(2022, 12, 30)),
    ("2023_2024", date(2023, 1, 3), date(2024, 12, 31)),
    ("2025_2026h1", date(2025, 1, 2), date(2026, 6, 30)),
)
BOOTSTRAP_SEEDS = (11, 29, 47, 71, 101)


def run_etf_strategy_robustness_audit(
    settings: Settings,
    save: bool = True,
) -> dict[str, Any]:
    base = dict(settings.get("strategies.etf_rotation"))
    symbols = list(base["universe"])
    fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
    provider = CachedProvider(
        fallback,
        settings.resolve(settings.get("system.data_dir")) / "cache",
    )
    bars = provider.bars(symbols, AUDIT_START - timedelta(days=500), AUDIT_END)
    frame = pd.DataFrame([bar.model_dump() for bar in bars])
    if frame.empty:
        raise ValueError("no ETF data returned for robustness audit")
    frame["date"] = pd.to_datetime(frame["date"])
    signal_frame = frame.copy()
    signal_frame["close"] = signal_frame["adjusted_close"].fillna(signal_frame["close"])
    configs = _audit_configs(settings)
    equal_weight_targets = {
        symbol: float(settings.get("risk.max_total_exposure")) / len(symbols)
        for symbol in symbols
    }

    full_results = {
        name: run_etf_backtest(
            settings,
            bars,
            signal_frame,
            symbols,
            config,
            AUDIT_START,
            AUDIT_END,
        )
        for name, config in configs.items()
    }
    full_results["equal_weight_buy_hold"] = run_etf_static_backtest(
        settings,
        bars,
        symbols,
        equal_weight_targets,
        AUDIT_START,
        AUDIT_END,
    )
    stressed_results = {
        name: run_etf_backtest(
            settings,
            bars,
            signal_frame,
            symbols,
            config,
            AUDIT_START,
            AUDIT_END,
            cost_multiplier=2.0,
        )
        for name, config in configs.items()
    }

    full_metrics = {
        name: _window_metrics(result, AUDIT_START, AUDIT_END)
        for name, result in full_results.items()
    }
    two_x_cost_metrics = {
        name: _window_metrics(result, AUDIT_START, AUDIT_END)
        for name, result in stressed_results.items()
    }
    calendar = _calendar_period_results(
        settings,
        bars,
        signal_frame,
        symbols,
        configs,
        equal_weight_targets,
    )
    common_dates = _common_dates(frame, symbols, AUDIT_START, AUDIT_END)
    rolling_windows = _rolling_window_results(full_results, common_dates)
    paired = {
        name: _paired_daily_returns(result, full_results["equal_weight_buy_hold"])
        for name, result in full_results.items()
        if name != "equal_weight_buy_hold"
    }
    inference = {
        name: {
            "paired_block_bootstrap": paired_block_bootstrap(
                (strategy for _, strategy, _ in values),
                (benchmark for _, _, benchmark in values),
                block_size=20,
                simulations=2_000,
                seed=20260714,
            ),
            "sharpe_significance": sharpe_significance(
                (strategy for _, strategy, _ in values),
                tested_variants=len(configs),
            ),
        }
        for name, values in paired.items()
    }
    repeated_bootstrap = [
        paired_block_bootstrap(
            (strategy for _, strategy, _ in paired["adaptive_v2_full"]),
            (benchmark for _, _, benchmark in paired["adaptive_v2_full"]),
            block_size=20,
            simulations=2_000,
            seed=seed,
        )
        for seed in BOOTSTRAP_SEEDS
    ]
    attribution = {
        name: _return_attribution(values)
        for name, values in paired.items()
        if name in {"adaptive_v2_full", "adaptive_v1_defensive", "legacy_fixed"}
    }
    stability = _stability_summary(calendar, rolling_windows)
    ablation = _ablation_summary(calendar, rolling_windows)
    diagnosis = _diagnose(stability, ablation, attribution)
    data_fingerprint = _data_fingerprint(signal_frame, symbols)
    experiment_payload = {
        "version": __version__,
        "data_fingerprint": data_fingerprint,
        "range": [AUDIT_START.isoformat(), AUDIT_END.isoformat()],
        "calendar_periods": [
            [name, start.isoformat(), end.isoformat()]
            for name, start, end in CALENDAR_PERIODS
        ],
        "configs": configs,
        "rolling_window_days": 504,
        "rolling_step_days": 126,
        "bootstrap_seeds": BOOTSTRAP_SEEDS,
    }
    output = {
        "status": "retrospective_diagnostic_only",
        "experiment_id": hashlib.sha256(
            json.dumps(experiment_payload, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "research_only": True,
        "source": provider.name,
        "degraded_sources": list(fallback.last_degraded_from),
        "period": {"start": AUDIT_START.isoformat(), "end": AUDIT_END.isoformat()},
        "variants": list(configs) + ["equal_weight_buy_hold"],
        "full_period_metrics": full_metrics,
        "two_x_cost_metrics": two_x_cost_metrics,
        "calendar_periods": calendar,
        "rolling_504_day_windows": rolling_windows,
        "stability": stability,
        "ablation": ablation,
        "inference": inference,
        "adaptive_v2_repeated_bootstrap": {
            "seeds": list(BOOTSTRAP_SEEDS),
            "runs": repeated_bootstrap,
            "probability_alpha_positive_range": [
                min(float(item["probability_alpha_positive"]) for item in repeated_bootstrap),
                max(float(item["probability_alpha_positive"]) for item in repeated_bootstrap),
            ],
        },
        "market_state_attribution": attribution,
        "diagnosis": diagnosis,
        "reproducibility": {
            "quantlab_version": __version__,
            "data_fingerprint_sha256": data_fingerprint,
            "protocol": "STRATEGY_ROBUSTNESS_AUDIT_PROTOCOL.md",
            "experiment_payload": experiment_payload,
        },
        "claim_boundary": (
            "All audited history was previously researched. Repeated windows and bootstrap runs "
            "diagnose stability but do not create independent confirmation or strategy admission."
        ),
    }
    if save:
        output["reports"] = _export_audit(settings, output)
    return output


def run_etf_v3_candidate_audit(settings: Settings, save: bool = True) -> dict[str, Any]:
    base = dict(settings.get("strategies.etf_rotation"))
    symbols = list(base["universe"])
    fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
    provider = CachedProvider(
        fallback,
        settings.resolve(settings.get("system.data_dir")) / "cache",
    )
    bars = provider.bars(symbols, AUDIT_START - timedelta(days=500), AUDIT_END)
    frame = pd.DataFrame([bar.model_dump() for bar in bars])
    if frame.empty:
        raise ValueError("no ETF data returned for V3 candidate audit")
    frame["date"] = pd.to_datetime(frame["date"])
    signal_frame = frame.copy()
    signal_frame["close"] = signal_frame["adjusted_close"].fillna(signal_frame["close"])
    configs = {
        "adaptive_v3_candidate": resolve_etf_variant_config(settings, "adaptive_v3"),
        "adaptive_v2_full": resolve_etf_variant_config(settings, "adaptive_v2"),
    }
    equal_weight_targets = {
        symbol: float(settings.get("risk.max_total_exposure")) / len(symbols)
        for symbol in symbols
    }
    full_results = {
        name: run_etf_backtest(
            settings,
            bars,
            signal_frame,
            symbols,
            config,
            AUDIT_START,
            AUDIT_END,
        )
        for name, config in configs.items()
    }
    full_results["equal_weight_buy_hold"] = run_etf_static_backtest(
        settings,
        bars,
        symbols,
        equal_weight_targets,
        AUDIT_START,
        AUDIT_END,
    )
    stressed_results = {
        name: run_etf_backtest(
            settings,
            bars,
            signal_frame,
            symbols,
            config,
            AUDIT_START,
            AUDIT_END,
            cost_multiplier=2.0,
        )
        for name, config in configs.items()
    }
    full_metrics = {
        name: _window_metrics(result, AUDIT_START, AUDIT_END)
        for name, result in full_results.items()
    }
    two_x_cost_metrics = {
        name: _window_metrics(result, AUDIT_START, AUDIT_END)
        for name, result in stressed_results.items()
    }
    calendar = _calendar_period_results(
        settings,
        bars,
        signal_frame,
        symbols,
        configs,
        equal_weight_targets,
    )
    common_dates = _common_dates(frame, symbols, AUDIT_START, AUDIT_END)
    rolling = _rolling_window_results(full_results, common_dates)
    paired_to_equal = {
        name: _paired_daily_returns(result, full_results["equal_weight_buy_hold"])
        for name, result in full_results.items()
        if name != "equal_weight_buy_hold"
    }
    paired_v3_v2 = _paired_daily_returns(
        full_results["adaptive_v3_candidate"],
        full_results["adaptive_v2_full"],
    )
    repeated_equal = [
        paired_block_bootstrap(
            (strategy for _, strategy, _ in paired_to_equal["adaptive_v3_candidate"]),
            (benchmark for _, _, benchmark in paired_to_equal["adaptive_v3_candidate"]),
            block_size=20,
            simulations=2_000,
            seed=seed,
        )
        for seed in BOOTSTRAP_SEEDS
    ]
    repeated_v2 = [
        paired_block_bootstrap(
            (strategy for _, strategy, _ in paired_v3_v2),
            (benchmark for _, _, benchmark in paired_v3_v2),
            block_size=20,
            simulations=2_000,
            seed=seed,
        )
        for seed in BOOTSTRAP_SEEDS
    ]
    stability = _stability_summary(calendar, rolling)
    v3_vs_v2 = _candidate_comparison(calendar, rolling, "adaptive_v3_candidate", "adaptive_v2_full")
    attribution = {
        name: _return_attribution(values)
        for name, values in paired_to_equal.items()
    }
    criteria = {
        "rolling_return_win_rate_vs_v2": v3_vs_v2["rolling_return_win_rate"],
        "rolling_sharpe_win_rate_vs_v2": v3_vs_v2["rolling_sharpe_win_rate"],
        "rolling_drawdown_win_rate_vs_v2": v3_vs_v2["rolling_drawdown_win_rate"],
        "up_capture_improvement": (
            float(attribution["adaptive_v3_candidate"]["up_capture"])
            - float(attribution["adaptive_v2_full"]["up_capture"])
        ),
        "down_capture_change": (
            float(attribution["adaptive_v3_candidate"]["down_capture"])
            - float(attribution["adaptive_v2_full"]["down_capture"])
        ),
        "two_x_cost_sharpe": two_x_cost_metrics["adaptive_v3_candidate"]["sharpe"],
    }
    exploratory_passed = bool(
        criteria["rolling_return_win_rate_vs_v2"] >= 0.50
        and criteria["rolling_sharpe_win_rate_vs_v2"] >= 0.50
        and criteria["up_capture_improvement"] > 0
        and criteria["two_x_cost_sharpe"] > 0
    )
    experiment_payload = {
        "version": __version__,
        "data_fingerprint": _data_fingerprint(signal_frame, symbols),
        "configs": configs,
        "calendar_periods": [
            [name, start.isoformat(), end.isoformat()]
            for name, start, end in CALENDAR_PERIODS
        ],
        "bootstrap_seeds": BOOTSTRAP_SEEDS,
        "protocol": "STRATEGY_V3_RESEARCH_PROTOCOL.md",
    }
    output = {
        "status": (
            "eligible_for_prospective_shadow_observation"
            if exploratory_passed
            else "retrospective_candidate_rejected"
        ),
        "experiment_id": hashlib.sha256(
            json.dumps(experiment_payload, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "research_only": True,
        "formal_strategy_changed": False,
        "source": provider.name,
        "degraded_sources": list(fallback.last_degraded_from),
        "period": {"start": AUDIT_START.isoformat(), "end": AUDIT_END.isoformat()},
        "full_period_metrics": full_metrics,
        "two_x_cost_metrics": two_x_cost_metrics,
        "calendar_periods": calendar,
        "rolling_504_day_windows": rolling,
        "stability_vs_equal_weight": stability,
        "v3_vs_v2": v3_vs_v2,
        "market_state_attribution": attribution,
        "repeated_bootstrap_vs_equal_weight": {
            "runs": repeated_equal,
            "probability_alpha_positive_range": _probability_range(repeated_equal),
        },
        "repeated_bootstrap_vs_v2": {
            "runs": repeated_v2,
            "probability_alpha_positive_range": _probability_range(repeated_v2),
        },
        "exploratory_screen": {"passed": exploratory_passed, "criteria": criteria},
        "reproducibility": {"experiment_payload": experiment_payload},
        "claim_boundary": (
            "V3 was designed after diagnosing already-seen history. Passing this exploratory "
            "screen only permits a new prospective shadow account, not strategy admission."
        ),
    }
    if save:
        output["reports"] = _export_v3_audit(settings, output)
    return output


def _audit_configs(settings: Settings) -> dict[str, dict[str, Any]]:
    full = resolve_etf_variant_config(settings, "adaptive_v2")
    v1 = resolve_etf_variant_config(settings, "adaptive_v1")
    legacy = resolve_etf_variant_config(settings, "legacy")
    return {
        "adaptive_v2_full": full,
        "adaptive_v2_no_correlation": {**full, "correlation_penalty": 0.0},
        "adaptive_v2_diagonal_covariance": {**full, "covariance_shrinkage": 1.0},
        "adaptive_v2_no_stress_overlay": {
            **full,
            "breadth_risk_off": 0.0,
            "breadth_risk_on": 0.01,
            "volatility_stress_warning": 100.0,
            "volatility_stress_stop": 101.0,
            "drawdown_warning": -1.0,
            "drawdown_stop": -2.0,
        },
        "adaptive_v2_fixed_core": {
            **full,
            "minimum_core_weight": 0.50,
            "maximum_core_weight": 0.50,
        },
        "adaptive_v2_no_rebalance_tolerance": {
            **full,
            "rebalance_tolerance_weight": 0.0,
        },
        "adaptive_v1_defensive": v1,
        "legacy_fixed": {**legacy, "core_weight": 0.0},
    }


def _candidate_comparison(
    calendar: list[dict],
    rolling: list[dict],
    candidate: str,
    incumbent: str,
) -> dict[str, float | int]:
    return {
        "calendar_periods": len(calendar),
        "calendar_return_win_rate": _positive_rate(
            item["metrics"][candidate]["total_return"]
            - item["metrics"][incumbent]["total_return"]
            for item in calendar
        ),
        "calendar_sharpe_win_rate": _positive_rate(
            item["metrics"][candidate]["sharpe"] - item["metrics"][incumbent]["sharpe"]
            for item in calendar
        ),
        "calendar_drawdown_win_rate": _positive_rate(
            item["metrics"][candidate]["max_drawdown"]
            - item["metrics"][incumbent]["max_drawdown"]
            for item in calendar
        ),
        "rolling_windows": len(rolling),
        "rolling_return_win_rate": _positive_rate(
            item["metrics"][candidate]["total_return"]
            - item["metrics"][incumbent]["total_return"]
            for item in rolling
        ),
        "rolling_sharpe_win_rate": _positive_rate(
            item["metrics"][candidate]["sharpe"] - item["metrics"][incumbent]["sharpe"]
            for item in rolling
        ),
        "rolling_drawdown_win_rate": _positive_rate(
            item["metrics"][candidate]["max_drawdown"]
            - item["metrics"][incumbent]["max_drawdown"]
            for item in rolling
        ),
    }


def _calendar_period_results(
    settings: Settings,
    bars,
    signal_frame: pd.DataFrame,
    symbols: list[str],
    configs: dict[str, dict[str, Any]],
    equal_weight_targets: dict[str, float],
) -> list[dict[str, Any]]:
    periods = []
    for name, start, end in CALENDAR_PERIODS:
        metrics = {
            variant: _window_metrics(
                run_etf_backtest(
                    settings,
                    bars,
                    signal_frame,
                    symbols,
                    config,
                    start,
                    end,
                ),
                start,
                end,
            )
            for variant, config in configs.items()
        }
        metrics["equal_weight_buy_hold"] = _window_metrics(
            run_etf_static_backtest(
                settings,
                bars,
                symbols,
                equal_weight_targets,
                start,
                end,
            ),
            start,
            end,
        )
        periods.append(
            {
                "name": name,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "metrics": metrics,
                "relative_to_equal_weight": _relative_metrics(metrics),
            }
        )
    return periods


def _rolling_window_results(full_results: dict[str, Any], dates: list[date]) -> list[dict]:
    windows = []
    for start_index in range(0, len(dates) - 504 + 1, 126):
        start = dates[start_index]
        end = dates[start_index + 503]
        metrics = {
            name: _window_metrics(result, start, end)
            for name, result in full_results.items()
        }
        windows.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "metrics": metrics,
                "relative_to_equal_weight": _relative_metrics(metrics),
            }
        )
    return windows


def _relative_metrics(metrics: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    benchmark = metrics["equal_weight_buy_hold"]
    return {
        name: {
            "total_return_delta": values["total_return"] - benchmark["total_return"],
            "sharpe_delta": values["sharpe"] - benchmark["sharpe"],
            "max_drawdown_delta": values["max_drawdown"] - benchmark["max_drawdown"],
        }
        for name, values in metrics.items()
        if name != "equal_weight_buy_hold"
    }


def _stability_summary(calendar: list[dict], rolling: list[dict]) -> dict[str, Any]:
    names = list(calendar[0]["relative_to_equal_weight"])
    return {
        name: {
            "calendar_periods": len(calendar),
            "calendar_return_win_rate": _positive_rate(
                item["relative_to_equal_weight"][name]["total_return_delta"]
                for item in calendar
            ),
            "calendar_sharpe_win_rate": _positive_rate(
                item["relative_to_equal_weight"][name]["sharpe_delta"]
                for item in calendar
            ),
            "calendar_drawdown_win_rate": _positive_rate(
                item["relative_to_equal_weight"][name]["max_drawdown_delta"]
                for item in calendar
            ),
            "rolling_windows": len(rolling),
            "rolling_return_win_rate": _positive_rate(
                item["relative_to_equal_weight"][name]["total_return_delta"]
                for item in rolling
            ),
            "rolling_sharpe_win_rate": _positive_rate(
                item["relative_to_equal_weight"][name]["sharpe_delta"]
                for item in rolling
            ),
            "rolling_drawdown_win_rate": _positive_rate(
                item["relative_to_equal_weight"][name]["max_drawdown_delta"]
                for item in rolling
            ),
        }
        for name in names
    }


def _ablation_summary(calendar: list[dict], rolling: list[dict]) -> dict[str, Any]:
    full_name = "adaptive_v2_full"
    ablations = [
        name
        for name in calendar[0]["metrics"]
        if name.startswith("adaptive_v2_") and name != full_name
    ]
    return {
        name: {
            "feature_value_positive_means_full_is_better": {
                "mean_calendar_return_delta": _mean_difference(
                    calendar, full_name, name, "total_return"
                ),
                "mean_calendar_sharpe_delta": _mean_difference(
                    calendar, full_name, name, "sharpe"
                ),
                "mean_calendar_drawdown_delta": _mean_difference(
                    calendar, full_name, name, "max_drawdown"
                ),
                "mean_calendar_turnover_reduction": -_mean_difference(
                    calendar, full_name, name, "turnover_count"
                ),
                "mean_rolling_return_delta": _mean_difference(
                    rolling, full_name, name, "total_return"
                ),
                "mean_rolling_sharpe_delta": _mean_difference(
                    rolling, full_name, name, "sharpe"
                ),
                "mean_rolling_drawdown_delta": _mean_difference(
                    rolling, full_name, name, "max_drawdown"
                ),
            }
        }
        for name in ablations
    }


def _paired_daily_returns(strategy_result, benchmark_result) -> list[tuple[date, float, float]]:
    strategy = dict(equity_return_series(strategy_result.equity_curve, AUDIT_START, AUDIT_END))
    benchmark = dict(equity_return_series(benchmark_result.equity_curve, AUDIT_START, AUDIT_END))
    return [
        (day, strategy[day], benchmark[day])
        for day in sorted(strategy.keys() & benchmark.keys())
    ]


def _return_attribution(paired: list[tuple[date, float, float]]) -> dict[str, Any]:
    frame = pd.DataFrame(paired, columns=["date", "strategy", "benchmark"]).set_index("date")
    frame.index = pd.to_datetime(frame.index)
    excess = frame["strategy"] - frame["benchmark"]
    monthly = (1.0 + excess).groupby(frame.index.to_period("M")).prod() - 1.0
    benchmark_trend = (1.0 + frame["benchmark"]).rolling(60).apply(np.prod, raw=True) - 1.0
    short_volatility = frame["benchmark"].rolling(20).std(ddof=1)
    long_volatility = frame["benchmark"].rolling(120).std(ddof=1)
    volatility_ratio = short_volatility / long_volatility.replace(0, np.nan)
    regimes = {
        "bull_normal": (benchmark_trend >= 0) & (volatility_ratio <= 1.25),
        "bull_stress": (benchmark_trend >= 0) & (volatility_ratio > 1.25),
        "bear_normal": (benchmark_trend < 0) & (volatility_ratio <= 1.25),
        "bear_stress": (benchmark_trend < 0) & (volatility_ratio > 1.25),
    }
    return {
        "monthly_excess_positive_rate": float((monthly > 0).mean()),
        "up_capture": _capture_ratio(frame, frame["benchmark"] > 0),
        "down_capture": _capture_ratio(frame, frame["benchmark"] < 0),
        "regimes": {
            name: _regime_metrics(frame, mask)
            for name, mask in regimes.items()
        },
    }


def _capture_ratio(frame: pd.DataFrame, mask: pd.Series) -> float | None:
    bounded = frame[mask]
    if bounded.empty or abs(float(bounded["benchmark"].mean())) <= 1e-12:
        return None
    return float(bounded["strategy"].mean() / bounded["benchmark"].mean())


def _regime_metrics(frame: pd.DataFrame, mask: pd.Series) -> dict[str, float | int]:
    bounded = frame[mask.fillna(False)]
    if bounded.empty:
        return {"observations": 0, "strategy_annualized": 0.0, "benchmark_annualized": 0.0, "annualized_alpha": 0.0}
    return {
        "observations": len(bounded),
        "strategy_annualized": float(bounded["strategy"].mean() * 252),
        "benchmark_annualized": float(bounded["benchmark"].mean() * 252),
        "annualized_alpha": float((bounded["strategy"] - bounded["benchmark"]).mean() * 252),
    }


def _diagnose(stability: dict, ablation: dict, attribution: dict) -> dict[str, Any]:
    full = stability["adaptive_v2_full"]
    issues = []
    if (
        full["rolling_return_win_rate"] < 0.50
        and full["rolling_sharpe_win_rate"] >= 0.50
        and full["rolling_drawdown_win_rate"] >= 0.50
    ):
        issues.append("risk_budget_or_bull_market_participation")
    if attribution["adaptive_v2_full"]["up_capture"] is not None and float(
        attribution["adaptive_v2_full"]["up_capture"]
    ) < 0.90:
        issues.append("low_upside_capture")
    feature_findings = {
        name: values["feature_value_positive_means_full_is_better"]
        for name, values in ablation.items()
    }
    harmful_features = [
        name
        for name, values in feature_findings.items()
        if values["mean_rolling_return_delta"] < 0
        and values["mean_rolling_sharpe_delta"] < 0
    ]
    return {
        "primary_issues": issues or ["historical_alpha_is_not_stable"],
        "harmful_or_unproven_features": harmful_features,
        "feature_findings": feature_findings,
        "next_design_rule": (
            "Any modification must be registered as V3 and evaluated prospectively; do not tune "
            "V2 on these already-seen windows."
        ),
    }


def _window_metrics(result, start: date, end: date) -> dict[str, float]:
    curve = [(day, value) for day, value in result.equity_curve if start <= day <= end]
    turnover = sum(1 for fill in result.fills if start <= fill.trade_date <= end)
    return calculate_equity_metrics(curve, turnover)


def _common_dates(frame: pd.DataFrame, symbols: list[str], start: date, end: date) -> list[date]:
    bounded = frame[
        frame["symbol"].isin(symbols)
        & frame["date"].between(pd.Timestamp(start), pd.Timestamp(end))
    ]
    counts = bounded.groupby("date")["symbol"].nunique()
    return [timestamp.date() for timestamp, count in counts.items() if count == len(symbols)]


def _positive_rate(values) -> float:
    bounded = np.asarray(list(values), dtype=float)
    return float(np.mean(bounded > 0)) if len(bounded) else 0.0


def _mean_difference(
    periods: list[dict],
    first: str,
    second: str,
    metric: str,
) -> float:
    return float(
        np.mean(
            [
                item["metrics"][first][metric] - item["metrics"][second][metric]
                for item in periods
            ]
        )
    )


def _data_fingerprint(frame: pd.DataFrame, symbols: list[str]) -> str:
    bounded = frame[frame["symbol"].isin(symbols)][["symbol", "date", "close", "volume"]].copy()
    bounded["date"] = bounded["date"].dt.strftime("%Y-%m-%d")
    payload = bounded.sort_values(["date", "symbol"]).to_csv(
        index=False, float_format="%.10g", lineterminator="\n"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _probability_range(runs: list[dict]) -> list[float]:
    values = [float(item["probability_alpha_positive"]) for item in runs]
    return [min(values), max(values)]


def _export_audit(settings: Settings, output: dict[str, Any]) -> dict[str, str]:
    reports_dir = settings.resolve(settings.get("system.data_dir")) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "strategy-robustness-audit-latest.json"
    markdown_path = reports_dir / "strategy-robustness-audit-latest.md"
    full = output["full_period_metrics"]["adaptive_v2_full"]
    stability = output["stability"]["adaptive_v2_full"]
    bootstrap_range = output["adaptive_v2_repeated_bootstrap"][
        "probability_alpha_positive_range"
    ]
    lines = [
        "# ETF 策略多轮稳健性审查",
        "",
        f"- 实验 ID：{output['experiment_id']}",
        f"- 状态：{output['status']}",
        f"- V2 全期收益：{full['total_return']:.2%}",
        f"- V2 全期 Sharpe：{full['sharpe']:.3f}",
        f"- V2 全期最大回撤：{full['max_drawdown']:.2%}",
        f"- 日历区间收益胜率：{stability['calendar_return_win_rate']:.1%}",
        f"- 滚动窗口收益胜率：{stability['rolling_return_win_rate']:.1%}",
        f"- 滚动窗口 Sharpe 胜率：{stability['rolling_sharpe_win_rate']:.1%}",
        f"- 滚动窗口回撤胜率：{stability['rolling_drawdown_win_rate']:.1%}",
        (
            "- 多种子 alpha 为正概率范围："
            f"{bootstrap_range[0]:.1%} 至 {bootstrap_range[1]:.1%}"
        ),
        f"- 主要问题：{', '.join(output['diagnosis']['primary_issues'])}",
        "",
        f"> {output['claim_boundary']}",
    ]
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def _export_v3_audit(settings: Settings, output: dict[str, Any]) -> dict[str, str]:
    reports_dir = settings.resolve(settings.get("system.data_dir")) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "strategy-v3-diagnostic-latest.json"
    markdown_path = reports_dir / "strategy-v3-diagnostic-latest.md"
    metrics = output["full_period_metrics"]
    v3 = metrics["adaptive_v3_candidate"]
    v2 = metrics["adaptive_v2_full"]
    screen = output["exploratory_screen"]
    lines = [
        "# Adaptive ETF V3 回顾性候选诊断",
        "",
        f"- 实验 ID：{output['experiment_id']}",
        f"- 状态：{output['status']}",
        f"- V3 全期收益：{v3['total_return']:.2%}",
        f"- V2 全期收益：{v2['total_return']:.2%}",
        f"- V3 Sharpe：{v3['sharpe']:.3f}",
        f"- V2 Sharpe：{v2['sharpe']:.3f}",
        f"- V3 最大回撤：{v3['max_drawdown']:.2%}",
        f"- V2 最大回撤：{v2['max_drawdown']:.2%}",
        f"- 探索筛选：{screen['passed']}",
        (
            "- V3 相对等权 alpha 为正概率范围："
            f"{output['repeated_bootstrap_vs_equal_weight']['probability_alpha_positive_range'][0]:.1%} "
            "至 "
            f"{output['repeated_bootstrap_vs_equal_weight']['probability_alpha_positive_range'][1]:.1%}"
        ),
        "",
        f"> {output['claim_boundary']}",
    ]
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}
