from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quantlab.config import Settings
from quantlab.data import BaoStockProvider
from quantlab.learning import LearningRepository
from quantlab.workflows.stock_discovery import _bar_frame


PROTOCOL_VERSION = "a-share-cross-section-v1"
STATIC_CANDIDATES = (
    "reversal_low_vol",
    "quality_momentum",
    "momentum_reversal_blend",
    "low_vol_trend",
    "trend_pullback",
)
STATIC_V2_CANDIDATES = (
    "defensive_reversal_single",
    "broad_contrarian_four",
    "pure_low_vol_two",
)
RIDGE_FEATURES = (
    "factor_momentum_20",
    "factor_momentum_60",
    "factor_momentum_acceleration",
    "factor_path_quality_60",
    "factor_volume_asymmetry_20",
    "factor_return_skewness_60",
    "factor_price_position_60",
    "factor_ma_spread_5_20",
    "factor_rsi_14",
    "factor_composite",
    "mtf_consensus",
    "pullback_strength",
    "cross_section_momentum_20_rank",
    "cross_section_momentum_60_rank",
    "cross_section_momentum_120_rank",
    "cross_section_volatility_20_rank",
    "cross_section_relative_return_20",
)


def score_a_share_ranking_policy(
    candidates: list[dict[str, Any]], policy: dict[str, Any]
) -> dict[str, float]:
    items = [
        {
            "symbol": item["symbol"],
            "features": item.get("learning_features") or item.get("features") or {},
        }
        for item in candidates
    ]
    if policy.get("kind") == "static":
        name = str(policy.get("name"))
        if name not in STATIC_CANDIDATES:
            raise ValueError(f"unknown static A-share ranking policy: {name}")
        return _static_scores(items, name)
    if policy.get("kind") == "static_cross_section_v2":
        name = str(policy.get("name"))
        if name not in STATIC_V2_CANDIDATES:
            raise ValueError(f"unknown static A-share V2 ranking policy: {name}")
        return _static_v2_scores(items, name)
    if policy.get("kind") == "ridge_rank":
        return _ridge_scores(items, policy)
    if policy.get("kind") == "market_regime_switch":
        raise ValueError(
            "market_regime_switch must be resolved with point-in-time benchmark history first"
        )
    raise ValueError("unsupported A-share ranking policy")


def resolve_a_share_ranking_policy(
    policy: dict[str, Any],
    frame: pd.DataFrame,
    signal_date: date,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a frozen dynamic policy using only data observable on the signal date."""

    if policy.get("kind") != "market_regime_switch":
        return policy, {"regime": "static", "reason": "policy_is_not_dynamic"}
    regime = dict(policy.get("market_regime") or {})
    branches = dict(policy.get("branches") or {})
    benchmark_symbol = str(regime.get("benchmark_symbol", "sh000300"))
    lookback = int(regime.get("trend_lookback_trading_days", 120))
    threshold = float(regime.get("risk_on_threshold", 0.0))
    if lookback < 2:
        raise ValueError("market regime trend lookback must be at least two trading days")
    frame_dates = pd.to_datetime(frame["date"])
    benchmark = frame[
        (frame["symbol"] == benchmark_symbol)
        & (frame_dates <= pd.Timestamp(signal_date))
    ].copy()
    benchmark["date"] = pd.to_datetime(benchmark["date"])
    benchmark = benchmark.sort_values("date")
    benchmark = benchmark.drop_duplicates(subset=["date"], keep="last")
    history_available = len(benchmark) > lookback
    trend_return = None
    if history_available:
        current_close = float(benchmark.iloc[-1]["close"])
        prior_close = float(benchmark.iloc[-(lookback + 1)]["close"])
        if prior_close > 0:
            trend_return = current_close / prior_close - 1
    regime_name = (
        "risk_on"
        if trend_return is not None and trend_return > threshold
        else "risk_off"
    )
    branch = dict(branches.get(regime_name) or {})
    ranking = dict(branch.get("ranking") or {})
    portfolio = dict(branch.get("portfolio") or {})
    if not ranking or not portfolio:
        raise ValueError(f"market regime policy is missing the {regime_name} branch")
    effective = {**ranking, "portfolio": portfolio}
    return effective, {
        "regime": regime_name,
        "benchmark_symbol": benchmark_symbol,
        "trend_lookback_trading_days": lookback,
        "risk_on_threshold": threshold,
        "trend_return": trend_return,
        "history_available": history_available,
        "ranking_policy": ranking.get("name") or ranking.get("version"),
        "top_k": int(portfolio["top_k"]),
        "total_exposure": float(portfolio["total_exposure"]),
    }


def a_share_ranking_policy_hash(policy: dict[str, Any]) -> str:
    canonical = {key: value for key, value in policy.items() if key != "policy_hash"}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def run_a_share_strategy_lab(
    settings: Settings,
    *,
    development_start: date = date(2018, 1, 1),
    development_end: date = date(2022, 12, 31),
    validation_start: date = date(2023, 1, 1),
    validation_end: date = date(2025, 12, 31),
    horizon_days: int = 5,
    exposure: float = 0.15,
    round_trip_cost: float = 0.0035,
    ridge_l2: float = 10.0,
    output_path: Path | None = None,
    provider: BaoStockProvider | None = None,
) -> dict[str, Any]:
    if not development_start <= development_end < validation_start <= validation_end:
        raise ValueError("strategy lab requires non-overlapping ordered development and validation")
    if horizon_days != 5:
        raise ValueError("A-share strategy protocol v1 is frozen to a 5-day horizon")
    repository = LearningRepository(settings.resolve(settings.get("system.database_path")))
    samples, sample_audit = _market_samples(
        repository,
        development_start,
        validation_end,
        horizon_days,
    )
    development = [
        item for item in samples if development_start.isoformat() <= item["as_of"] <= development_end.isoformat()
    ]
    validation = [
        item for item in samples if validation_start.isoformat() <= item["as_of"] <= validation_end.isoformat()
    ]
    development_groups = _group_samples(development)
    validation_groups = _group_samples(validation)
    if len(development_groups) < 15 or len(validation_groups) < 10:
        raise ValueError("strategy lab has insufficient point-in-time market dates")
    all_dates = sorted(set(development_groups) | set(validation_groups))
    benchmark_returns = _benchmark_returns(
        provider
        or BaoStockProvider(
            cache_dir=settings.resolve(settings.get("system.data_dir")) / "cache" / "baostock"
        ),
        all_dates,
        horizon_days,
    )

    ridge_walk_forward, ridge_walk_audit = _ridge_walk_forward_scores(
        development_groups,
        l2=ridge_l2,
        minimum_training_dates=12,
    )
    comparison_dates = sorted(ridge_walk_forward)
    if len(comparison_dates) < 8:
        raise ValueError("strategy lab has insufficient ridge walk-forward dates")
    development_results: dict[str, dict[str, Any]] = {}
    for candidate in STATIC_CANDIDATES:
        scores = {
            day: _static_scores(development_groups[day], candidate) for day in comparison_dates
        }
        development_results[candidate] = _evaluate_scores(
            development_groups,
            scores,
            benchmark_returns,
            exposure=exposure,
            round_trip_cost=round_trip_cost,
        )
    development_results["ridge_rank_v1"] = _evaluate_scores(
        development_groups,
        ridge_walk_forward,
        benchmark_returns,
        exposure=exposure,
        round_trip_cost=round_trip_cost,
    )
    _attach_development_admission(development_results)
    selected = _select_development_candidate(development_results)

    validation_result = None
    frozen_policy = None
    if selected is not None:
        if selected == "ridge_rank_v1":
            frozen_policy = _fit_ridge(development_groups, l2=ridge_l2)
            validation_scores = {
                day: _ridge_scores(items, frozen_policy)
                for day, items in validation_groups.items()
            }
        else:
            frozen_policy = {"kind": "static", "name": selected}
            validation_scores = {
                day: _static_scores(items, selected) for day, items in validation_groups.items()
            }
        validation_result = _evaluate_scores(
            validation_groups,
            validation_scores,
            benchmark_returns,
            exposure=exposure,
            round_trip_cost=round_trip_cost,
        )
        validation_result["admission"] = _validation_admission(validation_result)

    protocol_payload = {
        "version": PROTOCOL_VERSION,
        "development": [development_start.isoformat(), development_end.isoformat()],
        "validation": [validation_start.isoformat(), validation_end.isoformat()],
        "locked_holdout": ["2026-01-01", "2026-07-13"],
        "horizon_days": horizon_days,
        "exposure": exposure,
        "round_trip_cost": round_trip_cost,
        "ridge_l2": ridge_l2,
        "static_candidates": list(STATIC_CANDIDATES),
        "ridge_features": list(RIDGE_FEATURES),
    }
    output = {
        "protocol": protocol_payload,
        "protocol_hash": hashlib.sha256(
            json.dumps(protocol_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16],
        "sample_audit": sample_audit,
        "benchmark_symbol": "sh000300",
        "development_results": development_results,
        "ridge_walk_forward_audit": ridge_walk_audit,
        "selected_candidate": selected,
        "frozen_policy": frozen_policy,
        "validation_result": validation_result,
        "locked_holdout_ready": bool(
            validation_result and validation_result["admission"]["passed"]
        ),
        "status": (
            "validation_passed_holdout_locked"
            if validation_result and validation_result["admission"]["passed"]
            else "development_failed"
            if selected is None
            else "validation_failed"
        ),
        "claim_boundary": (
            "development compares preregistered candidates; validation runs only the development "
            "winner; the 2026 locked holdout remains unopened by this workflow"
        ),
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.suffix.lower() == ".json":
            output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            output_path.write_text(render_a_share_strategy_lab_markdown(output), encoding="utf-8")
    return output


def freeze_a_share_locked_holdout_policy(
    settings: Settings,
    lab_result: dict[str, Any],
    *,
    output_path: Path | None = None,
) -> dict[str, Any]:
    if not lab_result.get("locked_holdout_ready"):
        raise ValueError("validation has not passed; locked holdout policy cannot be frozen")
    selected = str(lab_result["selected_candidate"])
    protocol = lab_result["protocol"]
    training_end = date.fromisoformat(protocol["validation"][1])
    if selected == "ridge_rank_v1":
        repository = LearningRepository(settings.resolve(settings.get("system.database_path")))
        samples, audit = _market_samples(
            repository,
            date.fromisoformat(protocol["development"][0]),
            training_end,
            int(protocol["horizon_days"]),
        )
        policy = _fit_ridge(_group_samples(samples), l2=float(protocol["ridge_l2"]))
    else:
        audit = lab_result["sample_audit"]
        policy = {"kind": "static", "name": selected}
    policy["governance"] = {
        "protocol_version": PROTOCOL_VERSION,
        "protocol_hash": lab_result["protocol_hash"],
        "selected_candidate": selected,
        "training_end": training_end.isoformat(),
        "locked_holdout_start": "2026-01-01",
        "locked_holdout_end": "2026-07-13",
        "sample_protocol_hash": audit["selected_sample_protocol_hash"],
        "validation_passed": True,
    }
    policy["policy_hash"] = a_share_ranking_policy_hash(policy)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
    return policy


def evaluate_a_share_locked_holdout(
    lab_result: dict[str, Any], replay: dict[str, Any]
) -> dict[str, Any]:
    if not lab_result.get("locked_holdout_ready"):
        raise ValueError("validation has not passed")
    requested = replay.get("requested_range", {})
    if requested.get("start") != "2026-01-01" or requested.get("end") != "2026-07-13":
        raise ValueError("locked holdout replay range does not match the preregistered period")
    metrics = replay["metrics"]["system_top_rank"]
    benchmark = replay["metrics"]["benchmark_hs300"]
    comparison = replay["paired_comparisons"]["benchmark_hs300"]
    checks = {
        "positive_total_return": metrics["total_return"] > 0,
        "positive_same_exposure_hs300_excess": (
            metrics["total_return"] > benchmark["total_return"]
            and comparison["mean_excess_return"] > 0
        ),
        "bootstrap_probability_at_least_90pct": (
            comparison["probability_mean_excess_positive"] >= 0.90
        ),
        "maximum_drawdown_within_8pct": metrics["max_drawdown"] >= -0.08,
        "participation_at_least_70pct": metrics["participation_rate"] >= 0.70,
        "point_in_time_data_integrity": replay["evidence_qualification"][
            "snapshot_integrity"
        ],
    }
    return {
        "passed": all(checks.values()),
        "status": "positive_holdout_evidence" if all(checks.values()) else "holdout_failed",
        "checks": checks,
        "metrics": metrics,
        "benchmark_metrics": benchmark,
        "paired_comparison": comparison,
        "policy_hash": replay.get("ranking_policy_hash"),
        "claim_boundary": (
            "a passed historical locked holdout permits only an independent prospective shadow "
            "challenge; it does not authorize live execution or promise future profit"
        ),
    }


def render_a_share_strategy_lab_markdown(output: dict[str, Any]) -> str:
    lines = [
        "# A股横截面策略实验室 V1",
        "",
        f"- 状态：{output['status']}",
        f"- 协议哈希：`{output['protocol_hash']}`",
        f"- 样本协议：`{output['sample_audit']['selected_sample_protocol_hash']}`",
        f"- 开发期冠军：{output['selected_candidate']}",
        f"- 是否允许打开锁定留出：{output['locked_holdout_ready']}",
        "",
        "## 开发期",
        "",
        "| 候选 | 收益 | 基准 | 平均超额 | Rank IC | 回撤 | 正收益年度 | 准入 | 选择分 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, item in output["development_results"].items():
        lines.append(
            f"| {name} | {item['total_return']:.2%} | {item['benchmark_total_return']:.2%} | "
            f"{item['paired_comparison']['mean_excess_return']:.3%} | "
            f"{item['mean_rank_ic']:.3f} | {item['max_drawdown']:.2%} | "
            f"{item['positive_year_fraction']:.1%} | {item['development_admission']} | "
            f"{item.get('selection_score', 0):.3f} |"
        )
    validation = output.get("validation_result")
    lines.extend(["", "## 验证期", ""])
    if validation is None:
        lines.append("没有开发期候选通过基础门槛，因此未运行验证期。")
    else:
        lines.extend(
            [
                f"- 候选：{output['selected_candidate']}",
                f"- 收益：{validation['total_return']:.2%}",
                f"- 同暴露沪深300：{validation['benchmark_total_return']:.2%}",
                f"- 平均超额：{validation['paired_comparison']['mean_excess_return']:.3%}",
                f"- 超额为正概率：{validation['paired_comparison']['probability_mean_excess_positive']:.1%}",
                f"- 90%区间：{validation['paired_comparison']['bootstrap_90pct_interval']}",
                f"- Rank IC：{validation['mean_rank_ic']:.3f}",
                f"- 最大回撤：{validation['max_drawdown']:.2%}",
                f"- 准入：{validation['admission']['passed']}",
            ]
        )
    lines.extend(["", "## 边界", "", output["claim_boundary"], ""])
    return "\n".join(lines)


def _market_samples(
    repository: LearningRepository,
    start: date,
    end: date,
    horizon_days: int,
    required_protocol_hash: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    eligible = [
        item
        for item in repository.completed_samples(horizon_days, "stock")
        if item["source"] == "stock_market_point_in_time"
        and item.get("context", {}).get("training_eligible") is True
        and start.isoformat() <= item["as_of"] <= end.isoformat()
        and item.get("realized_return_pct") is not None
    ]
    protocols: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in eligible:
        protocol_hash = str(item.get("context", {}).get("sample_protocol_hash") or "unknown")
        protocols[protocol_hash].append(item)
    if not protocols:
        raise ValueError("no eligible point-in-time stock market samples are available")
    if required_protocol_hash is not None:
        selected = protocols.get(required_protocol_hash)
        if not selected:
            raise ValueError(
                f"required point-in-time sample protocol is unavailable: {required_protocol_hash}"
            )
        selected_hash = required_protocol_hash
    else:
        selected_hash, selected = max(
            protocols.items(),
            key=lambda pair: (
                len({item["as_of"] for item in pair[1]}),
                len(pair[1]),
                pair[0],
            ),
        )
    deduplicated = {
        (item["as_of"], item["symbol"]): item
        for item in selected
    }
    output = sorted(deduplicated.values(), key=lambda item: (item["as_of"], item["symbol"]))
    return output, {
        "selected_sample_protocol_hash": selected_hash,
        "available_protocols": {
            key: {
                "samples": len(items),
                "dates": len({item["as_of"] for item in items}),
            }
            for key, items in sorted(protocols.items())
        },
        "selected_samples": len(output),
        "selected_dates": len({item["as_of"] for item in output}),
        "date_range": [output[0]["as_of"], output[-1]["as_of"]],
    }


def _group_samples(samples: list[dict[str, Any]]) -> dict[date, list[dict[str, Any]]]:
    groups: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for item in samples:
        groups[date.fromisoformat(item["as_of"])].append(item)
    return {
        day: sorted(items, key=lambda item: item["symbol"])
        for day, items in sorted(groups.items())
        if len(items) >= 12
    }


def _static_scores(items: list[dict[str, Any]], candidate: str) -> dict[str, float]:
    output = {}
    for item in items:
        feature = item["features"]
        reversal_20 = 1 - _unit(feature.get("cross_section_momentum_20_rank"))
        momentum_60 = _unit(feature.get("cross_section_momentum_60_rank"))
        momentum_120 = _unit(feature.get("cross_section_momentum_120_rank"))
        low_vol = 1 - _unit(feature.get("cross_section_volatility_20_rank"))
        path_quality = _signed_unit(feature.get("factor_path_quality_60"))
        volume_asymmetry = _signed_unit(feature.get("factor_volume_asymmetry_20"))
        price_position = _signed_unit(feature.get("factor_price_position_60"))
        factor_composite = _signed_unit(feature.get("factor_composite"))
        pullback = _unit(feature.get("pullback_strength"))
        formulas = {
            "reversal_low_vol": (
                0.45 * reversal_20 + 0.25 * low_vol + 0.20 * momentum_120 + 0.10 * path_quality
            ),
            "quality_momentum": (
                0.30 * momentum_60
                + 0.25 * momentum_120
                + 0.20 * path_quality
                + 0.15 * low_vol
                + 0.10 * volume_asymmetry
            ),
            "momentum_reversal_blend": (
                0.30 * momentum_60
                + 0.20 * momentum_120
                + 0.25 * reversal_20
                + 0.15 * low_vol
                + 0.10 * path_quality
            ),
            "low_vol_trend": (
                0.35 * low_vol
                + 0.25 * momentum_120
                + 0.20 * path_quality
                + 0.10 * price_position
                + 0.10 * factor_composite
            ),
            "trend_pullback": (
                0.30 * momentum_120
                + 0.25 * momentum_60
                + 0.20 * reversal_20
                + 0.15 * pullback
                + 0.10 * low_vol
            ),
        }
        output[item["symbol"]] = float(formulas[candidate])
    return output


def _static_v2_scores(items: list[dict[str, Any]], candidate: str) -> dict[str, float]:
    """Rank robust 20-day A-share effects inside the current point-in-time cross-section."""

    if not items:
        return {}
    ranks = {
        name: _cross_section_rank(items, name)
        for name in (
            "cross_section_volatility_20_rank",
            "cross_section_momentum_20_rank",
            "cross_section_momentum_60_rank",
            "cross_section_momentum_120_rank",
            "factor_path_quality_60",
            "factor_rsi_14",
            "factor_volume_asymmetry_20",
            "factor_composite",
            "pullback_strength",
        )
    }
    output = {}
    for item in items:
        symbol = item["symbol"]
        low_volatility = 1 - ranks["cross_section_volatility_20_rank"][symbol]
        reversal_20 = 1 - ranks["cross_section_momentum_20_rank"][symbol]
        reversal_60 = 1 - ranks["cross_section_momentum_60_rank"][symbol]
        reversal_120 = 1 - ranks["cross_section_momentum_120_rank"][symbol]
        path_reversal = 1 - ranks["factor_path_quality_60"][symbol]
        rsi_reversal = 1 - ranks["factor_rsi_14"][symbol]
        volume_reversal = 1 - ranks["factor_volume_asymmetry_20"][symbol]
        composite_reversal = 1 - ranks["factor_composite"][symbol]
        pullback = ranks["pullback_strength"][symbol]
        formulas = {
            "defensive_reversal_single": (
                0.40 * low_volatility
                + 0.25 * reversal_20
                + 0.10 * reversal_60
                + 0.10 * reversal_120
                + 0.10 * path_reversal
                + 0.05 * pullback
            ),
            "broad_contrarian_four": (
                0.30 * low_volatility
                + 0.20 * reversal_20
                + 0.10 * reversal_60
                + 0.10 * reversal_120
                + 0.10 * path_reversal
                + 0.10 * rsi_reversal
                + 0.05 * volume_reversal
                + 0.05 * composite_reversal
            ),
            "pure_low_vol_two": low_volatility,
        }
        output[symbol] = float(formulas[candidate])
    return output


def _cross_section_rank(
    items: list[dict[str, Any]], feature_name: str
) -> dict[str, float]:
    values = pd.Series(
        {
            item["symbol"]: float(item["features"].get(feature_name, 0.0))
            for item in items
        },
        dtype=float,
    )
    ranked = values.rank(method="average", pct=True)
    return {symbol: float(value) for symbol, value in ranked.items()}


def _ridge_walk_forward_scores(
    groups: dict[date, list[dict[str, Any]]],
    *,
    l2: float,
    minimum_training_dates: int,
) -> tuple[dict[date, dict[str, float]], dict[str, Any]]:
    output = {}
    audits = []
    dates = sorted(groups)
    for index, day in enumerate(dates):
        if index < minimum_training_dates:
            continue
        training = {item: groups[item] for item in dates[:index]}
        policy = _fit_ridge(training, l2=l2)
        output[day] = _ridge_scores(groups[day], policy)
        audits.append(
            {
                "prediction_date": day.isoformat(),
                "training_dates": index,
                "training_samples": policy["training_samples"],
            }
        )
    return output, {
        "minimum_training_dates": minimum_training_dates,
        "predicted_dates": len(output),
        "episodes": audits,
    }


def _fit_ridge(
    groups: dict[date, list[dict[str, Any]]],
    *,
    l2: float,
) -> dict[str, Any]:
    rows = []
    labels = []
    for items in groups.values():
        realized = pd.Series(
            [float(item["realized_return_pct"]) for item in items],
            index=[item["symbol"] for item in items],
        )
        ranked = realized.rank(method="average", pct=True) - 0.5
        for item in items:
            rows.append([float(item["features"].get(name, 0.0)) for name in RIDGE_FEATURES])
            labels.append(float(ranked.loc[item["symbol"]]))
    matrix = np.asarray(rows, dtype=float)
    target = np.asarray(labels, dtype=float)
    mean = matrix.mean(axis=0)
    scale = matrix.std(axis=0)
    scale[scale < 1e-8] = 1.0
    normalized = (matrix - mean) / scale
    penalty = np.eye(normalized.shape[1]) * float(l2)
    coefficients = np.linalg.solve(normalized.T @ normalized + penalty, normalized.T @ target)
    return {
        "kind": "ridge_rank",
        "version": "ridge_rank_v1",
        "feature_names": list(RIDGE_FEATURES),
        "mean": mean.tolist(),
        "scale": scale.tolist(),
        "coefficients": coefficients.tolist(),
        "l2": float(l2),
        "training_samples": len(target),
        "training_dates": len(groups),
    }


def _ridge_scores(items: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, float]:
    feature_names = policy["feature_names"]
    mean = np.asarray(policy["mean"], dtype=float)
    scale = np.asarray(policy["scale"], dtype=float)
    coefficients = np.asarray(policy["coefficients"], dtype=float)
    output = {}
    for item in items:
        vector = np.asarray(
            [float(item["features"].get(name, 0.0)) for name in feature_names], dtype=float
        )
        output[item["symbol"]] = float(((vector - mean) / scale) @ coefficients)
    return output


def _evaluate_scores(
    groups: dict[date, list[dict[str, Any]]],
    scores: dict[date, dict[str, float]],
    benchmark_returns: dict[date, float],
    *,
    exposure: float,
    round_trip_cost: float,
) -> dict[str, Any]:
    capital = 1.0
    benchmark_capital = 1.0
    peak = 1.0
    max_drawdown = 0.0
    episode_returns = []
    benchmark_episode_returns = []
    rank_ics = []
    episodes = []
    yearly_returns: dict[int, list[float]] = defaultdict(list)
    yearly_benchmark: dict[int, list[float]] = defaultdict(list)
    for day in sorted(scores):
        if day not in groups or day not in benchmark_returns:
            continue
        items = groups[day]
        day_scores = scores[day]
        comparable = [item for item in items if item["symbol"] in day_scores]
        if len(comparable) < 2:
            continue
        selected = max(comparable, key=lambda item: (day_scores[item["symbol"]], item["symbol"]))
        gross = float(selected["realized_return_pct"]) / 100
        strategy_return = exposure * max(-1.0, gross - round_trip_cost)
        benchmark_return = exposure * float(benchmark_returns[day])
        capital *= 1 + strategy_return
        benchmark_capital *= 1 + benchmark_return
        peak = max(peak, capital)
        max_drawdown = min(max_drawdown, capital / peak - 1)
        episode_returns.append(strategy_return)
        benchmark_episode_returns.append(benchmark_return)
        yearly_returns[day.year].append(strategy_return)
        yearly_benchmark[day.year].append(benchmark_return)
        score_ranks = pd.Series(
            [day_scores[item["symbol"]] for item in comparable], dtype=float
        ).rank()
        return_ranks = pd.Series(
            [float(item["realized_return_pct"]) for item in comparable], dtype=float
        ).rank()
        rank_ic = score_ranks.corr(return_ranks)
        if pd.notna(rank_ic):
            rank_ics.append(float(rank_ic))
        episodes.append(
            {
                "signal_date": day.isoformat(),
                "selected_symbol": selected["symbol"],
                "score": float(day_scores[selected["symbol"]]),
                "gross_stock_return": gross,
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "rank_ic": float(rank_ic) if pd.notna(rank_ic) else None,
            }
        )
    if not episodes:
        raise ValueError("candidate evaluation produced no comparable episodes")
    differences = np.asarray(episode_returns) - np.asarray(benchmark_episode_returns)
    rng = np.random.default_rng(20260714)
    bootstrapped = np.asarray(
        [float(rng.choice(differences, size=len(differences), replace=True).mean()) for _ in range(2000)]
    )
    annual = {}
    for year in sorted(yearly_returns):
        strategy_year = float(np.prod(1 + np.asarray(yearly_returns[year])) - 1)
        benchmark_year = float(np.prod(1 + np.asarray(yearly_benchmark[year])) - 1)
        annual[str(year)] = {
            "strategy_return": strategy_year,
            "benchmark_return": benchmark_year,
            "excess_return": strategy_year - benchmark_year,
        }
    return {
        "episodes": len(episodes),
        "total_return": capital - 1,
        "benchmark_total_return": benchmark_capital - 1,
        "average_episode_return": float(np.mean(episode_returns)),
        "win_rate": float(np.mean(np.asarray(episode_returns) > 0)),
        "max_drawdown": max_drawdown,
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


def _attach_development_admission(results: dict[str, dict[str, Any]]) -> None:
    admitted = []
    for name, item in results.items():
        passed = bool(
            item["total_return"] > 0
            and item["paired_comparison"]["mean_excess_return"] > 0
            and item["mean_rank_ic"] > 0
            and item["positive_year_fraction"] >= 0.60
        )
        item["development_admission"] = passed
        if passed:
            admitted.append(name)
    if not admitted:
        for item in results.values():
            item["selection_score"] = 0.0
        return
    metrics = {
        "mean_excess": {name: results[name]["paired_comparison"]["mean_excess_return"] for name in admitted},
        "rank_ic": {name: results[name]["mean_rank_ic"] for name in admitted},
        "drawdown": {name: results[name]["max_drawdown"] for name in admitted},
        "stability": {name: results[name]["positive_excess_year_fraction"] for name in admitted},
    }
    weights = {"mean_excess": 0.35, "rank_ic": 0.30, "drawdown": 0.20, "stability": 0.15}
    scores = Counter()
    for metric, values in metrics.items():
        ranks = pd.Series(values).rank(method="average", pct=True)
        for name, value in ranks.items():
            scores[name] += weights[metric] * float(value)
    for name, item in results.items():
        item["selection_score"] = float(scores.get(name, 0.0))


def _select_development_candidate(results: dict[str, dict[str, Any]]) -> str | None:
    admitted = [name for name, item in results.items() if item["development_admission"]]
    if not admitted:
        return None
    return max(
        admitted,
        key=lambda name: (
            results[name]["selection_score"],
            name != "ridge_rank_v1",
            name,
        ),
    )


def _validation_admission(result: dict[str, Any]) -> dict[str, Any]:
    comparison = result["paired_comparison"]
    annual = result["annual"]
    checks = {
        "positive_total_return": result["total_return"] > 0,
        "positive_mean_excess": comparison["mean_excess_return"] > 0,
        "bootstrap_probability_at_least_90pct": (
            comparison["probability_mean_excess_positive"] >= 0.90
        ),
        "bootstrap_lower_bound_not_below_minus_10bp": (
            comparison["bootstrap_90pct_interval"][0] >= -0.001
        ),
        "positive_mean_rank_ic": result["mean_rank_ic"] > 0,
        "maximum_drawdown_within_10pct": result["max_drawdown"] >= -0.10,
        "at_least_two_positive_years": sum(
            item["strategy_return"] > 0 for item in annual.values()
        )
        >= 2,
        "at_least_two_positive_excess_years": sum(
            item["excess_return"] > 0 for item in annual.values()
        )
        >= 2,
    }
    return {"passed": all(checks.values()), "checks": checks}


def _benchmark_returns(
    provider: BaoStockProvider,
    signal_dates: list[date],
    horizon_days: int,
) -> dict[date, float]:
    bars = provider.bars(
        ["sh000300"],
        min(signal_dates) - timedelta(days=200),
        max(signal_dates) + timedelta(days=45),
    )
    frame = _bar_frame(bars).sort_values("date")
    dates = [pd.Timestamp(value).date() for value in frame["date"].tolist()]
    closes = frame["signal_close"].astype(float).tolist()
    index = {day: position for position, day in enumerate(dates)}
    output = {}
    for day in signal_dates:
        position = index.get(day)
        if position is None or position + horizon_days >= len(dates):
            continue
        output[day] = closes[position + horizon_days] / closes[position] - 1
    return output


def _unit(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return float(np.clip(number, 0, 1))


def _signed_unit(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return float(np.clip((number + 1) / 2, 0, 1))
