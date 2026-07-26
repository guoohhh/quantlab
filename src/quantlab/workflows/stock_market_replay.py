from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Any, Callable

import numpy as np
import pandas as pd

from quantlab.config import Settings
from quantlab.data import BaoStockProvider
from quantlab.execution import CostModel
from quantlab.learning import LearningRepository
from quantlab.persistence import (
    AShareUniverseRepository,
    StockRankingReplayRepository,
)
from quantlab.workflows.stock_discovery import (
    SCREEN_WEIGHTS,
    _bar_frame,
    _correlation_matrix,
    _diversified_shortlist,
    _screen_frame,
)
from quantlab.workflows.stock_evidence import (
    _blind_candidate_id,
    _mean_optional,
    _paired_comparison,
    _path_metrics,
    _rank_information_coefficient,
    _return_label,
    _simulate_stock_portfolio,
)
from quantlab.workflows.stock_strategy_lab import (
    a_share_ranking_policy_hash,
    resolve_a_share_ranking_policy,
    score_a_share_ranking_policy,
)
from quantlab.workflows.universe import (
    capture_point_in_time_universe,
    refresh_a_share_security_master,
    select_stratified_point_in_time_sample,
)


def run_market_wide_stock_replay(
    settings: Settings,
    start: date,
    end: date,
    *,
    horizon_days: int = 5,
    episodes: int = 12,
    sample_size: int = 60,
    top_k: int = 3,
    max_correlation: float | None = None,
    seed: str | None = None,
    save: bool = True,
    record_learning_samples: bool = True,
    provider: BaoStockProvider | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ranking_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay ranking on deterministic stratified samples of each historical A-share market."""

    if start >= end:
        raise ValueError("market-wide stock replay start must be before end")
    if horizon_days not in {5, 20}:
        raise ValueError("horizon_days must be 5 or 20")
    if not 1 <= episodes <= 60:
        raise ValueError("episodes must be between 1 and 60")
    if not 12 <= sample_size <= 200:
        raise ValueError("sample_size must be between 12 and 200")
    policy_portfolio = (ranking_policy or {}).get("portfolio", {})
    policy_branch_portfolios = [
        dict(branch.get("portfolio") or {})
        for branch in (ranking_policy or {}).get("branches", {}).values()
        if branch.get("portfolio")
    ]
    if policy_branch_portfolios:
        top_k = max(int(item.get("top_k", top_k)) for item in policy_branch_portfolios)
    elif policy_portfolio:
        top_k = int(policy_portfolio.get("top_k", top_k))
    if not 1 <= top_k <= 5:
        raise ValueError("top_k must be between 1 and 5")
    correlation_limit = float(
        max_correlation
        if max_correlation is not None
        else settings.get("strategies.stock_market_replay.max_correlation", 0.85)
    )
    if not 0 <= correlation_limit <= 1:
        raise ValueError("max_correlation must be between 0 and 1")
    protocol_seed = seed or str(
        settings.get("strategies.stock_market_replay.seed", "quantlab-a-share-pit-v1")
    )
    ranking_policy_hash = (
        a_share_ranking_policy_hash(ranking_policy) if ranking_policy is not None else None
    )
    if (
        ranking_policy is not None
        and ranking_policy.get("policy_hash")
        and ranking_policy["policy_hash"] != ranking_policy_hash
    ):
        raise ValueError("ranking policy hash does not match its embedded policy_hash")
    schedule_horizon_days = int(
        (ranking_policy or {})
        .get("governance", {})
        .get("signal_schedule_horizon_days", horizon_days)
    )
    if schedule_horizon_days not in {5, 20}:
        raise ValueError("ranking policy signal schedule horizon must be 5 or 20 days")
    benchmark = str(settings.get("strategies.stock_market_replay.benchmark_symbol", "sh000300"))
    minimum_turnover = float(
        settings.get("strategies.stock_market_replay.minimum_average_turnover", 50_000_000)
    )
    source = provider or BaoStockProvider(
        cache_dir=settings.resolve(settings.get("system.data_dir")) / "cache" / "baostock"
    )
    database_path = settings.resolve(settings.get("system.database_path"))
    universe_repository = AShareUniverseRepository(database_path)
    if universe_repository.latest_master_build() is None:
        refresh_a_share_security_master(settings)

    outcome_end = min(
        end + timedelta(days=max(45, horizon_days * 4)),
        date.today() - timedelta(days=1),
    )
    benchmark_bars = source.bars(
        [benchmark],
        start - timedelta(days=700),
        outcome_end,
    )
    benchmark_frame = _bar_frame(benchmark_bars)
    if benchmark_frame.empty:
        raise ValueError("market-wide replay returned no benchmark history")
    benchmark_dates = [
        pd.Timestamp(value).date() for value in benchmark_frame["date"].sort_values().unique()
    ]
    dates_in_requested_range = [day for day in benchmark_dates if start <= day <= end]
    if not dates_in_requested_range:
        raise ValueError(
            f"benchmark {benchmark} has no history in requested range {start}..{end}; "
            "BaoStock ETF history can be incomplete, so use the sh000300 index comparator"
        )
    selected_dates = _select_market_dates(
        benchmark_dates,
        start,
        end,
        schedule_horizon_days,
        episodes,
    )
    if not selected_dates:
        raise ValueError("no non-overlapping market-wide replay dates are available")

    samples_by_date: dict[date, dict[str, Any]] = {}
    snapshot_audits = []
    all_symbols = {benchmark}
    for signal_date in selected_dates:
        snapshot = capture_point_in_time_universe(settings, signal_date, provider=source)
        sample = select_stratified_point_in_time_sample(
            snapshot["records"],
            sample_size,
            seed=protocol_seed,
            snapshot_date=signal_date,
        )
        samples_by_date[signal_date] = sample
        snapshot_audits.append(
            {
                "signal_date": signal_date.isoformat(),
                "source": snapshot["source"],
                "capture_attempts": snapshot.get("capture_attempts", 0),
                "securities": snapshot["securities"],
                "tradable": snapshot["tradable"],
                "sample_size": sample["sample_size"],
                "strata": sample["strata"],
                "cross_validation": snapshot["cross_validation"],
            }
        )
        all_symbols.update(item["symbol"] for item in sample["records"])

    date_index = {day: index for index, day in enumerate(benchmark_dates)}
    stock_outcome_end = max(
        benchmark_dates[date_index[signal_date] + horizon_days] for signal_date in selected_dates
    )
    bars = source.bars(
        sorted(all_symbols),
        start - timedelta(days=700),
        stock_outcome_end,
    )
    frame = _bar_frame(bars)
    if frame.empty:
        raise ValueError("market-wide replay returned no sampled stock history")
    universe_repository.save_daily_status(
        [
            {
                "symbol": bar.symbol,
                "trade_date": bar.date,
                "trade_status": not bar.suspended,
                "is_st": bar.is_st,
                "source": bar.source,
            }
            for bar in bars
            if bar.symbol != benchmark
        ]
    )

    initial_capital = float(settings.get("system.initial_capital"))
    single_budget = min(
        float(settings.get("risk.max_single_position")),
        float(settings.get("risk.max_total_exposure")),
    )
    stock_cost = CostModel.from_dict(settings.get("costs.stock"))
    capital = {
        "system_top_rank": initial_capital,
        "simple_momentum": initial_capital,
        "pool_equal_weight": initial_capital,
        "benchmark_hs300": initial_capital,
        "benchmark_hs300_multi_name": initial_capital,
        "system_diversified_top_k": initial_capital,
    }
    master = {item["symbol"]: item for item in universe_repository.master_records()}
    master_build = universe_repository.latest_master_build()
    cross_validation_values = [
        item["cross_validation"].get("jaccard")
        for item in snapshot_audits
        if item["cross_validation"].get("jaccard") is not None
    ]
    mean_master_jaccard = _mean_optional(cross_validation_values)
    minimum_master_jaccard = min(cross_validation_values) if cross_validation_values else None
    failed_snapshot_integrity_count = sum(value < 0.90 for value in cross_validation_values)
    snapshot_sources = {item["source"] for item in snapshot_audits}
    snapshot_integrity = bool(
        snapshot_sources == {"baostock"}
        and master_build
        and minimum_master_jaccard is not None
        and minimum_master_jaccard >= 0.90
        and len(cross_validation_values) == len(snapshot_audits)
    )
    learning_repository = LearningRepository(database_path)
    protocol_definition = {
        "method": "stratified_point_in_time_market_sample_v1",
        "seed": protocol_seed,
        "sample_size": sample_size,
        "horizon_days": horizon_days,
        "top_k": top_k,
        "max_correlation": correlation_limit,
        "minimum_turnover": minimum_turnover,
        "master_version": master_build["version_hash"] if master_build else None,
        "ranking_policy_hash": ranking_policy_hash,
    }
    if policy_branch_portfolios:
        protocol_definition["dynamic_portfolios"] = policy_branch_portfolios
    if schedule_horizon_days != horizon_days:
        protocol_definition["signal_schedule_horizon_days"] = schedule_horizon_days
    protocol_hash = hashlib.sha256(
        json.dumps(
            protocol_definition,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    rows = []
    applied_multi_name_budgets: list[float] = []
    applied_top_k_values: list[int] = []
    for episode_number, signal_date in enumerate(selected_dates, start=1):
        target_date = benchmark_dates[date_index[signal_date] + horizon_days]
        sample = samples_by_date[signal_date]
        sample_records = sample["records"]
        sample_symbols = [item["symbol"] for item in sample_records]
        point_in_time = frame[
            (frame["date"] <= pd.Timestamp(signal_date)) & (frame["symbol"].isin(sample_symbols))
        ].copy()
        metadata = {}
        st_from_daily_status = 0
        for item in sample_records:
            status_rows = frame[
                (frame["symbol"] == item["symbol"]) & (frame["date"] == pd.Timestamp(signal_date))
            ]
            is_st = (
                bool(status_rows.iloc[-1].get("is_st", False)) if not status_rows.empty else False
            )
            st_from_daily_status += int(is_st)
            metadata[item["symbol"]] = {
                "name": item["name"],
                "board": item["board"],
                "is_st": is_st,
            }
        screened = _screen_frame(sample_symbols, point_in_time, signal_date, metadata)
        candidates = [
            item
            for item in screened
            if item.get("status") == "ok"
            and item.get("eligible")
            and item.get("as_of") == signal_date.isoformat()
            and float(item.get("average_turnover_20") or 0) >= minimum_turnover
        ]
        effective_policy = ranking_policy
        policy_diagnostics = {"regime": "default", "reason": "no_ranking_policy"}
        episode_policy_portfolio = policy_portfolio
        episode_top_k = top_k
        if ranking_policy is not None:
            effective_policy, policy_diagnostics = resolve_a_share_ranking_policy(
                ranking_policy, frame, signal_date
            )
            episode_policy_portfolio = effective_policy.get("portfolio", {})
            if episode_policy_portfolio:
                episode_top_k = int(episode_policy_portfolio.get("top_k", episode_top_k))
            if not 1 <= episode_top_k <= 5:
                raise ValueError("resolved ranking policy top_k must be between 1 and 5")
            policy_scores = score_a_share_ranking_policy(candidates, effective_policy)
            for candidate in candidates:
                candidate["baseline_screen_score"] = float(candidate["screen_score"])
                candidate["screen_score"] = float(policy_scores[candidate["symbol"]])
        candidates.sort(key=lambda item: (-float(item["screen_score"]), item["symbol"]))
        for rank, candidate in enumerate(candidates, start=1):
            candidate["screen_rank"] = rank
        if len(candidates) < 2:
            continue
        correlations = _correlation_matrix(point_in_time, [item["symbol"] for item in candidates])
        if episode_policy_portfolio.get("selection") == "rank_top_k":
            shortlist = candidates[:episode_top_k]
        else:
            shortlist = _diversified_shortlist(
                candidates, correlations, correlation_limit
            )[:episode_top_k]
        if not shortlist:
            shortlist = [candidates[0]]
        top = candidates[0]
        momentum = max(
            candidates,
            key=lambda item: (
                float(item.get("return_20_pct") or float("-inf")),
                item["symbol"],
            ),
        )
        if episode_policy_portfolio:
            requested_budget = float(
                episode_policy_portfolio.get(
                    "total_exposure", single_budget * len(shortlist)
                )
            )
            maximum_total = float(settings.get("risk.max_total_exposure"))
            if not 0 < requested_budget <= maximum_total:
                raise ValueError("ranking policy total exposure exceeds the configured risk limit")
            if requested_budget / max(1, len(shortlist)) > float(
                settings.get("risk.max_single_position")
            ):
                raise ValueError("ranking policy per-position exposure exceeds the risk limit")
            top_k_budget = requested_budget
        else:
            top_k_budget = min(
                float(settings.get("risk.max_total_exposure")),
                single_budget * len(shortlist),
            )
        applied_multi_name_budgets.append(top_k_budget)
        applied_top_k_values.append(episode_top_k)
        trades = {
            "system_top_rank": _simulate_stock_portfolio(
                frame,
                [top["symbol"]],
                signal_date,
                target_date,
                capital["system_top_rank"],
                single_budget,
                stock_cost,
            ),
            "simple_momentum": _simulate_stock_portfolio(
                frame,
                [momentum["symbol"]],
                signal_date,
                target_date,
                capital["simple_momentum"],
                single_budget,
                stock_cost,
            ),
            "pool_equal_weight": _simulate_stock_portfolio(
                frame,
                [item["symbol"] for item in candidates],
                signal_date,
                target_date,
                capital["pool_equal_weight"],
                top_k_budget,
                stock_cost,
            ),
            "benchmark_hs300": _simulate_index_benchmark(
                frame,
                benchmark,
                signal_date,
                target_date,
                capital["benchmark_hs300"],
                single_budget,
            ),
            "benchmark_hs300_multi_name": _simulate_index_benchmark(
                frame,
                benchmark,
                signal_date,
                target_date,
                capital["benchmark_hs300_multi_name"],
                top_k_budget,
            ),
            "system_diversified_top_k": _simulate_stock_portfolio(
                frame,
                [item["symbol"] for item in shortlist],
                signal_date,
                target_date,
                capital["system_diversified_top_k"],
                top_k_budget,
                stock_cost,
            ),
        }
        for name, trade in trades.items():
            capital[name] = float(trade["capital_after"])

        candidate_results = []
        sampled_eventual_delisted = 0
        delisted_within_horizon = 0
        for candidate in candidates:
            master_record = master.get(candidate["symbol"], {})
            delisting_date = _optional_date(master_record.get("delisting_date"))
            sampled_eventual_delisted += int(master_record.get("status") == "delisted")
            delisted_within_horizon += int(
                delisting_date is not None and signal_date < delisting_date <= target_date
            )
            realized = _market_outcome_return(
                frame,
                candidate["symbol"],
                signal_date,
                target_date,
            )
            candidate_results.append(
                {
                    "blind_candidate_id": _blind_candidate_id(
                        protocol_hash, episode_number, candidate["symbol"]
                    ),
                    "symbol": candidate["symbol"],
                    "screen_rank": int(candidate["screen_rank"]),
                    "screen_score": float(candidate["screen_score"]),
                    "baseline_screen_score": candidate.get("baseline_screen_score"),
                    "board": metadata[candidate["symbol"]]["board"],
                    "eventual_master_status": master_record.get("status", "unknown"),
                    "delisting_date": delisting_date.isoformat() if delisting_date else None,
                    "realized_adjusted_return_pct": realized,
                }
            )
            if record_learning_samples and realized is not None:
                learning_repository.upsert_sample(
                    sample_key=(
                        f"stock-market-pit:{protocol_hash}:{signal_date.isoformat()}:"
                        f"{candidate['symbol']}:{horizon_days}"
                    ),
                    run_id=None,
                    source="stock_market_point_in_time",
                    asset_scope="stock",
                    symbol=candidate["symbol"],
                    as_of=signal_date,
                    horizon_days=horizon_days,
                    features=candidate.get("learning_features", {}),
                    outcome=_return_label(
                        realized,
                        float(settings.get("calibration.flat_threshold_pct", 1.0)),
                    ),
                    realized_return_pct=realized,
                    evaluated_at=target_date,
                    context={
                        "training_eligible": snapshot_integrity,
                        "universe_provenance": "baostock_point_in_time_stratified_market_sample",
                        "sample_protocol_hash": protocol_hash,
                        "snapshot_date": signal_date.isoformat(),
                        "master_version_hash": (
                            master_build["version_hash"] if master_build else None
                        ),
                        "survivorship_bias_control": True,
                        "historical_st_status_control": True,
                        "ranking_policy_hash": ranking_policy_hash,
                    },
                    origin="historical_research",
                    evidence_stage="point_in_time_training",
                    training_eligible=snapshot_integrity,
                )
        best = max(
            candidate_results,
            key=lambda item: item["realized_adjusted_return_pct"],
            default=None,
        )
        row = {
            "episode": episode_number,
            "signal_date": signal_date.isoformat(),
            "target_outcome_date": target_date.isoformat(),
            "full_market_securities": snapshot_audits[episode_number - 1]["securities"],
            "sampled_securities": sample["sample_size"],
            "eligible_candidates": len(candidates),
            "st_excluded_by_daily_status": st_from_daily_status,
            "sampled_eventual_delisted": sampled_eventual_delisted,
            "delisted_within_horizon": delisted_within_horizon,
            "top_ranked_symbol": top["symbol"],
            "simple_momentum_symbol": momentum["symbol"],
            "diversified_top_k": [item["symbol"] for item in shortlist],
            "ranking_policy_diagnostics": policy_diagnostics,
            "applied_top_k": episode_top_k,
            "applied_total_exposure": top_k_budget,
            "candidate_results": candidate_results,
            "top_ranked_winner": bool(best and best["symbol"] == top["symbol"]),
            "rank_information_coefficient": _rank_information_coefficient(candidate_results),
            "trades": trades,
        }
        rows.append(row)
        if progress_callback:
            progress_callback(
                {
                    "completed": len(rows),
                    "requested": len(selected_dates),
                    "signal_date": signal_date.isoformat(),
                    "full_market_securities": row["full_market_securities"],
                    "eligible_candidates": len(candidates),
                    "top_ranked_symbol": top["symbol"],
                }
            )
    if not rows:
        raise ValueError("market-wide stock replay produced no analyzable episodes")

    metrics = {name: _path_metrics(rows, name, initial_capital) for name in capital}
    comparisons = {
        "simple_momentum": _paired_comparison(rows, "system_top_rank", "simple_momentum"),
        "pool_equal_weight": _paired_comparison(
            rows, "system_diversified_top_k", "pool_equal_weight"
        ),
        "benchmark_hs300": _paired_comparison(rows, "system_top_rank", "benchmark_hs300"),
        "benchmark_hs300_multi_name": _paired_comparison(
            rows, "system_diversified_top_k", "benchmark_hs300_multi_name"
        ),
    }
    evidence_status = (
        "measured" if len(rows) >= 30 else "preliminary" if len(rows) >= 10 else "illustrative"
    )
    evidence_qualified = bool(len(rows) >= 30 and snapshot_integrity)
    mean_rank_ic = _mean_optional([row["rank_information_coefficient"] for row in rows])
    maximum_drawdown = float(settings.get("risk.max_portfolio_drawdown", 0.15))
    strategy_admission = _strategy_admission(
        evidence_qualified=evidence_qualified,
        metrics=metrics,
        comparisons=comparisons,
        mean_rank_ic=mean_rank_ic,
        maximum_drawdown=maximum_drawdown,
    )
    output = {
        "method": "stratified point-in-time A-share market replay",
        "universe_scope": "historical_market_snapshot_stratified_sample",
        "requested_range": {"start": start.isoformat(), "end": end.isoformat()},
        "horizon_days": horizon_days,
        "signal_schedule_horizon_days": schedule_horizon_days,
        "requested_episodes": episodes,
        "completed_episodes": len(rows),
        "sample_size_per_episode": sample_size,
        "universe_hash": protocol_hash,
        "benchmark_symbol": benchmark,
        "benchmark_type": "non_tradeable_index_comparator",
        "benchmark_execution": (
            "two same-exposure comparators (single-name and multi-name budgets); "
            "next trading open to horizon close; no fees"
        ),
        "source": source.name,
        "degraded_sources": [],
        "master_build": master_build,
        "snapshot_audits": snapshot_audits,
        "ranking_formula": SCREEN_WEIGHTS,
        "ranking_policy": ranking_policy,
        "ranking_policy_hash": ranking_policy_hash,
        "selection_rule": (
            "query the actual A-share list on each signal date; remove only same-date non-trading "
            "and special-treatment names; draw an exchange/board-stratified deterministic hash "
            "sample rotated by calendar year without using future returns or future listing status"
        ),
        "execution_contract": (
            "rank at T close; buy at the first later executable open; enforce historical ST, "
            "suspension, one-price limit, 100-share lot and stock costs; if a selected stock "
            "disappears before the horizon, use its last sellable close or a zero terminal recovery; "
            "the HS300 comparison is a non-tradeable index return scaled to the same exposure"
        ),
        "blinding": {
            "future_constituents_used": False,
            "future_delisting_status_used_for_selection": False,
            "future_returns_used_for_sampling": False,
            "same_date_st_and_trade_status_used": True,
            "delisted_stocks_can_enter_before_delisting": True,
        },
        "sampling_contract": {
            "seed": protocol_seed,
            "rotation": "calendar_year",
            "sample_size": sample_size,
            "strata": ["SH_main", "SZ_main", "star", "chinext"],
            "minimum_average_turnover": minimum_turnover,
        },
        "budget_contract": {
            "single_name_budget": single_budget,
            "multi_name_budget": (
                applied_multi_name_budgets[0]
                if len(set(applied_multi_name_budgets)) == 1
                else {
                    "minimum": min(applied_multi_name_budgets),
                    "maximum": max(applied_multi_name_budgets),
                }
            ),
            "applied_top_k": (
                applied_top_k_values[0]
                if len(set(applied_top_k_values)) == 1
                else {
                    "minimum": min(applied_top_k_values),
                    "maximum": max(applied_top_k_values),
                }
            ),
            "ranking_policy_total_exposure": (
                float(policy_portfolio["total_exposure"])
                if policy_portfolio and "total_exposure" in policy_portfolio
                else sorted(
                    {
                        float(item["total_exposure"])
                        for item in policy_branch_portfolios
                        if "total_exposure" in item
                    }
                )
                if policy_branch_portfolios
                else None
            ),
        },
        "metrics": metrics,
        "paired_comparisons": comparisons,
        "ranking_metrics": {
            "top_rank_win_rate": float(np.mean([row["top_ranked_winner"] for row in rows])),
            "mean_rank_information_coefficient": mean_rank_ic,
        },
        "survivorship_audit": {
            "sampled_eventual_delisted_observations": sum(
                row["sampled_eventual_delisted"] for row in rows
            ),
            "delisted_within_horizon_observations": sum(
                row["delisted_within_horizon"] for row in rows
            ),
            "historical_st_exclusions": sum(row["st_excluded_by_daily_status"] for row in rows),
        },
        "episodes": rows,
        "learning_samples": {
            "recorded": record_learning_samples,
            "count": sum(len(row["candidate_results"]) for row in rows)
            if record_learning_samples
            else 0,
            "training_eligible": snapshot_integrity,
            "universe_provenance": "point_in_time_stratified_market_sample",
        },
        "evidence_status": evidence_status,
        "strategy_admission": strategy_admission,
        "evidence_qualification": {
            "qualified": evidence_qualified,
            "qualification_scope": "stratified_point_in_time_a_share_market_sample",
            "qualified_for_market_wide_evidence_claim": evidence_qualified,
            "qualified_for_market_wide_selection_claim": strategy_admission["passed"],
            "minimum_measured_episodes": 30,
            "point_in_time_market_universe_available": True,
            "historical_st_status_available": True,
            "delisted_price_history_available": True,
            "snapshot_sources": sorted(snapshot_sources),
            "mean_exchange_master_jaccard": mean_master_jaccard,
            "minimum_exchange_master_jaccard": minimum_master_jaccard,
            "failed_snapshot_integrity_count": failed_snapshot_integrity_count,
            "snapshot_integrity": snapshot_integrity,
        },
        "claim_boundary": (
            "this removes current-constituent survivorship bias and tests deterministic stratified "
            "samples of each historical A-share market; it is not an exhaustive daily ranking of "
            "every listed stock; evidence qualification describes data rigor, while strategy "
            "deployment additionally requires the separate performance admission gate"
        ),
    }
    if save:
        output["replay_id"] = StockRankingReplayRepository(database_path).save(
            start,
            end,
            horizon_days,
            len(rows),
            protocol_hash,
            "ok" if snapshot_integrity else "degraded",
            output,
        )
    return output


def _select_market_dates(
    benchmark_dates: list[date],
    start: date,
    end: date,
    horizon_days: int,
    episodes: int,
) -> list[date]:
    index_by_date = {day: index for index, day in enumerate(benchmark_dates)}
    eligible = [
        day
        for day in benchmark_dates
        if start <= day <= end and index_by_date[day] + horizon_days < len(benchmark_dates)
    ]
    non_overlapping = []
    last_index = -10_000
    for day in eligible:
        index = index_by_date[day]
        if index - last_index >= horizon_days + 2:
            non_overlapping.append(day)
            last_index = index
    if len(non_overlapping) <= episodes:
        return non_overlapping
    positions = np.linspace(0, len(non_overlapping) - 1, episodes)
    return [non_overlapping[int(round(position))] for position in positions]


def _market_outcome_return(
    frame: pd.DataFrame,
    symbol: str,
    signal_date: date,
    target_date: date,
) -> float | None:
    rows = frame[frame["symbol"] == symbol].sort_values("date")
    start_rows = rows[rows["date"] <= pd.Timestamp(signal_date)]
    if start_rows.empty:
        return None
    start_price = float(start_rows.iloc[-1]["signal_close"])
    if start_price <= 0:
        return None
    target_rows = rows[rows["date"] >= pd.Timestamp(target_date)]
    if not target_rows.empty:
        end_price = float(target_rows.iloc[0]["signal_close"])
        return (end_price / start_price - 1) * 100
    terminal_rows = rows[rows["date"] > pd.Timestamp(signal_date)]
    if terminal_rows.empty:
        return -100.0
    terminal_price = float(terminal_rows.iloc[-1]["signal_close"])
    return (terminal_price / start_price - 1) * 100


def _simulate_index_benchmark(
    frame: pd.DataFrame,
    symbol: str,
    signal_date: date,
    target_date: date,
    capital: float,
    exposure: float,
) -> dict[str, Any]:
    rows = frame[frame["symbol"] == symbol].sort_values("date")
    entries = rows[rows["date"] > pd.Timestamp(signal_date)]
    exits = rows[rows["date"] >= pd.Timestamp(target_date)]
    if entries.empty or exits.empty:
        return {
            "symbols": [symbol],
            "target_weight": exposure,
            "traded_legs": 0,
            "net_pnl": 0.0,
            "net_return": 0.0,
            "capital_before": capital,
            "capital_after": capital,
            "legs": [{"symbol": symbol, "traded": False, "reason": "missing index bars"}],
        }
    entry = entries.iloc[0]
    exit_row = exits.iloc[0]
    entry_price = float(entry.get("adjusted_open") or entry["open"])
    exit_price = float(exit_row["signal_close"])
    if entry_price <= 0 or exit_price <= 0 or exit_row["date"] <= entry["date"]:
        return {
            "symbols": [symbol],
            "target_weight": exposure,
            "traded_legs": 0,
            "net_pnl": 0.0,
            "net_return": 0.0,
            "capital_before": capital,
            "capital_after": capital,
            "legs": [{"symbol": symbol, "traded": False, "reason": "invalid index bars"}],
        }
    index_return = exit_price / entry_price - 1
    net_pnl = capital * max(0.0, exposure) * index_return
    return {
        "symbols": [symbol],
        "target_weight": exposure,
        "traded_legs": 1,
        "net_pnl": net_pnl,
        "net_return": net_pnl / capital if capital else 0.0,
        "capital_before": capital,
        "capital_after": capital + net_pnl,
        "legs": [
            {
                "symbol": symbol,
                "traded": True,
                "entry_date": entry["date"].date().isoformat(),
                "exit_date": exit_row["date"].date().isoformat(),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "fees": 0.0,
                "net_pnl": net_pnl,
                "index_return": index_return,
            }
        ],
    }


def _optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return date.fromisoformat(str(value)[:10])


def _strategy_admission(
    *,
    evidence_qualified: bool,
    metrics: dict[str, dict[str, Any]],
    comparisons: dict[str, dict[str, Any]],
    mean_rank_ic: float | None,
    maximum_drawdown: float,
) -> dict[str, Any]:
    top = metrics["system_top_rank"]
    diversified = metrics["system_diversified_top_k"]
    top_benchmark = comparisons["benchmark_hs300"]
    top_momentum = comparisons["simple_momentum"]
    diversified_pool = comparisons["pool_equal_weight"]
    diversified_benchmark = comparisons["benchmark_hs300_multi_name"]

    def check(name: str, passed: bool, observed: Any, requirement: str) -> dict[str, Any]:
        return {
            "name": name,
            "passed": bool(passed),
            "observed": observed,
            "requirement": requirement,
        }

    ranking_check = check(
        "positive_mean_rank_information_coefficient",
        mean_rank_ic is not None and mean_rank_ic > 0,
        mean_rank_ic,
        "> 0",
    )
    top_checks = [
        check("measured_point_in_time_evidence", evidence_qualified, evidence_qualified, "true"),
        check("positive_total_return", top["total_return"] > 0, top["total_return"], "> 0"),
        check(
            "drawdown_within_system_limit",
            top["max_drawdown"] >= -maximum_drawdown,
            top["max_drawdown"],
            f">= {-maximum_drawdown}",
        ),
        check(
            "beats_simple_momentum_with_90pct_interval",
            top_momentum["bootstrap_90pct_interval"][0] > 0,
            top_momentum["bootstrap_90pct_interval"],
            "lower bound > 0",
        ),
        check(
            "beats_same_exposure_hs300_with_90pct_interval",
            top_benchmark["bootstrap_90pct_interval"][0] > 0,
            top_benchmark["bootstrap_90pct_interval"],
            "lower bound > 0",
        ),
        ranking_check,
    ]
    diversified_checks = [
        check("measured_point_in_time_evidence", evidence_qualified, evidence_qualified, "true"),
        check(
            "positive_total_return",
            diversified["total_return"] > 0,
            diversified["total_return"],
            "> 0",
        ),
        check(
            "drawdown_within_system_limit",
            diversified["max_drawdown"] >= -maximum_drawdown,
            diversified["max_drawdown"],
            f">= {-maximum_drawdown}",
        ),
        check(
            "beats_candidate_pool_with_90pct_interval",
            diversified_pool["bootstrap_90pct_interval"][0] > 0,
            diversified_pool["bootstrap_90pct_interval"],
            "lower bound > 0",
        ),
        check(
            "beats_same_exposure_hs300_with_90pct_interval",
            diversified_benchmark["bootstrap_90pct_interval"][0] > 0,
            diversified_benchmark["bootstrap_90pct_interval"],
            "lower bound > 0",
        ),
        ranking_check,
    ]
    variants = {
        "top_rank_single_name": {
            "passed": all(item["passed"] for item in top_checks),
            "checks": top_checks,
        },
        "diversified_top_k": {
            "passed": all(item["passed"] for item in diversified_checks),
            "checks": diversified_checks,
        },
    }
    passed = any(item["passed"] for item in variants.values())
    return {
        "passed": passed,
        "status": "admitted" if passed else "research_only",
        "preferred_variant": next(
            (name for name, item in variants.items() if item["passed"]),
            None,
        ),
        "variants": variants,
        "deployment_recommendation": (
            "eligible for paper-trading challenge" if passed else "do not promote to live recommendation"
        ),
    }
