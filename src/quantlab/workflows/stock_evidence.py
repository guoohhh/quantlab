from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.domain.models import Bar, OrderRequest, Side
from quantlab.execution import CostModel
from quantlab.learning import LearningRepository
from quantlab.persistence import StockRankingReplayRepository
from quantlab.workflows.stock_discovery import (
    _bar_frame,
    parse_stock_symbols,
    screen_selected_stocks,
)


def run_stock_ranking_replay(
    settings: Settings,
    symbols: list[str] | str,
    start: date,
    end: date,
    *,
    horizon_days: int = 20,
    episodes: int = 12,
    top_k: int = 3,
    max_correlation: float = 0.85,
    benchmark_symbol: str | None = None,
    save: bool = True,
    record_learning_samples: bool = True,
    bars: list[Bar] | list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Measure A-share ranking value with a frozen universe and point-in-time inputs.

    The replay deliberately does not reconstruct a historical index constituent universe.  It
    measures whether the ranking rule adds value *inside the user-supplied frozen research pool*.
    """

    universe = parse_stock_symbols(symbols, maximum=20)
    if len(universe) < 2:
        raise ValueError("stock ranking replay requires at least two A-share symbols")
    if start >= end:
        raise ValueError("stock ranking replay start must be before end")
    if horizon_days not in {5, 20}:
        raise ValueError("horizon_days must be 5 or 20")
    if not 1 <= episodes <= 60:
        raise ValueError("episodes must be between 1 and 60")
    if not 1 <= top_k <= min(5, len(universe)):
        raise ValueError("top_k must be between 1 and min(5, universe size)")
    if not 0 <= max_correlation <= 1:
        raise ValueError("max_correlation must be between 0 and 1")

    benchmark = benchmark_symbol or str(
        settings.get("strategies.stock_evidence.benchmark_symbol", "sh510300")
    )
    requested_symbols = list(dict.fromkeys([*universe, benchmark]))
    degraded_sources: list[str] = []
    source = "provided_bars"
    if bars is None:
        fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
        provider = CachedProvider(
            fallback,
            settings.resolve(settings.get("system.data_dir")) / "cache",
        )
        outcome_end = min(
            end + timedelta(days=max(45, horizon_days * 4)),
            date.today() - timedelta(days=1),
        )
        bars = provider.bars(requested_symbols, start - timedelta(days=700), outcome_end)
        degraded_sources = list(fallback.last_degraded_from)
        source = provider.name
    frame = _bar_frame(bars)
    if frame.empty:
        raise ValueError("stock ranking replay returned no market history")
    missing = sorted(set(requested_symbols) - set(frame["symbol"].unique()))
    if missing:
        raise ValueError(f"stock ranking replay is missing bars for: {', '.join(missing)}")

    benchmark_dates = [
        pd.Timestamp(timestamp).date()
        for timestamp in frame.loc[frame["symbol"] == benchmark, "date"].sort_values().unique()
    ]
    selected_dates = _select_stock_replay_dates(
        frame,
        universe,
        benchmark_dates,
        start,
        end,
        horizon_days,
        episodes,
    )
    if not selected_dates:
        raise ValueError("no non-overlapping stock replay dates satisfy history and outcome rules")

    initial_capital = float(settings.get("system.initial_capital"))
    single_budget = min(
        float(settings.get("risk.max_single_position")),
        float(settings.get("risk.max_total_exposure")),
    )
    stock_cost = CostModel.from_dict(settings.get("costs.stock"))
    benchmark_cost = CostModel.from_dict(settings.get("costs.etf"))
    universe_hash = hashlib.sha256("|".join(sorted(universe)).encode("utf-8")).hexdigest()[:16]
    date_index = {day: index for index, day in enumerate(benchmark_dates)}
    capital = {
        "system_top_rank": initial_capital,
        "simple_momentum": initial_capital,
        "pool_equal_weight": initial_capital,
        "benchmark_hs300": initial_capital,
        "system_diversified_top_k": initial_capital,
    }
    rows: list[dict[str, Any]] = []
    learning_repository = LearningRepository(settings.resolve(settings.get("system.database_path")))

    for episode_number, signal_date in enumerate(selected_dates, start=1):
        point_in_time = frame[frame["date"] <= pd.Timestamp(signal_date)].copy()
        stock_point_in_time = point_in_time[point_in_time["symbol"].isin(universe)]
        screen = screen_selected_stocks(
            settings,
            universe,
            signal_date,
            top_n=len(universe),
            max_correlation=max_correlation,
            save=False,
            bars=stock_point_in_time.to_dict("records"),
            run_type="historical_fixed_universe_replay",
        )
        candidates = [
            item
            for item in screen["candidates"]
            if item.get("status") == "ok"
            and item.get("eligible")
            and item.get("as_of") == signal_date.isoformat()
        ]
        if len(candidates) < 2:
            continue
        top = candidates[0]
        momentum = max(
            candidates,
            key=lambda item: (
                float(item.get("return_20_pct") or float("-inf")),
                item["symbol"],
            ),
        )
        shortlist = [
            item
            for item in screen["diversified_shortlist"]
            if item["symbol"] in {candidate["symbol"] for candidate in candidates}
        ][:top_k]
        if not shortlist:
            shortlist = [top]
        target_date = benchmark_dates[date_index[signal_date] + horizon_days]
        top_k_budget = min(
            float(settings.get("risk.max_total_exposure")),
            single_budget * len(shortlist),
        )
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
            "benchmark_hs300": _simulate_stock_portfolio(
                frame,
                [benchmark],
                signal_date,
                target_date,
                capital["benchmark_hs300"],
                single_budget,
                benchmark_cost,
                asset_type="etf",
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
        for candidate in candidates:
            realized = _adjusted_return(frame, candidate["symbol"], signal_date, target_date)
            candidate_results.append(
                {
                    "blind_candidate_id": _blind_candidate_id(
                        universe_hash, episode_number, candidate["symbol"]
                    ),
                    "symbol": candidate["symbol"],
                    "screen_rank": int(candidate["screen_rank"]),
                    "screen_score": float(candidate["screen_score"]),
                    "simple_momentum_rank_input_pct": candidate.get("return_20_pct"),
                    "realized_adjusted_return_pct": realized,
                }
            )
            if record_learning_samples and realized is not None:
                learning_repository.upsert_sample(
                    sample_key=(
                        f"stock-ranking-replay:{universe_hash}:{signal_date.isoformat()}:"
                        f"{candidate['symbol']}:{horizon_days}"
                    ),
                    run_id=None,
                    source="stock_fixed_universe_replay",
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
                        "training_eligible": False,
                        "universe_provenance": "user_supplied_fixed_universe",
                        "selection_bias_warning": (
                            "research-only until a historical point-in-time universe is available"
                        ),
                        "universe_hash": universe_hash,
                    },
                )
        rank_ic = _rank_information_coefficient(candidate_results)
        best = max(
            (
                item
                for item in candidate_results
                if item["realized_adjusted_return_pct"] is not None
            ),
            key=lambda item: item["realized_adjusted_return_pct"],
            default=None,
        )
        rows.append(
            {
                "episode": episode_number,
                "signal_date": signal_date.isoformat(),
                "target_outcome_date": target_date.isoformat(),
                "universe_hash": universe_hash,
                "eligible_candidates": len(candidates),
                "top_ranked_symbol": top["symbol"],
                "simple_momentum_symbol": momentum["symbol"],
                "diversified_top_k": [item["symbol"] for item in shortlist],
                "candidate_results": candidate_results,
                "top_ranked_winner": bool(best and best["symbol"] == top["symbol"]),
                "rank_information_coefficient": rank_ic,
                "trades": trades,
            }
        )

    if not rows:
        raise ValueError("stock ranking replay produced no analyzable episodes")
    metrics = {name: _path_metrics(rows, name, initial_capital) for name in capital}
    comparisons = {
        "simple_momentum": _paired_comparison(rows, "system_top_rank", "simple_momentum"),
        "pool_equal_weight": _paired_comparison(
            rows, "system_diversified_top_k", "pool_equal_weight"
        ),
        "benchmark_hs300": _paired_comparison(rows, "system_top_rank", "benchmark_hs300"),
    }
    evidence_status = (
        "measured" if len(rows) >= 30 else "preliminary" if len(rows) >= 10 else "illustrative"
    )
    output = {
        "method": "fixed-universe point-in-time A-share ranking replay",
        "requested_range": {"start": start.isoformat(), "end": end.isoformat()},
        "horizon_days": horizon_days,
        "requested_episodes": episodes,
        "completed_episodes": len(rows),
        "universe": universe,
        "universe_hash": universe_hash,
        "benchmark_symbol": benchmark,
        "source": source,
        "degraded_sources": degraded_sources,
        "ranking_formula": screen["ranking_formula"],
        "selection_rule": (
            "freeze the supplied symbols before replay; select non-overlapping benchmark trading "
            "dates evenly without reading future returns; require at least 120 prior observations"
        ),
        "execution_contract": (
            "rank at T close, buy at the first tradable later open, evaluate at the first tradable "
            "close on/after T+5 or T+20; use 100-share lots, stock fees, stamp duty and slippage"
        ),
        "blinding": {
            "current_stock_screener_snapshot_used": False,
            "company_names_used": False,
            "news_or_future_financials_used": False,
            "ranking_inputs_cut_off_at_signal_date": True,
            "blind_candidate_ids_recorded": True,
        },
        "budget_contract": {
            "top_rank_weight": single_budget,
            "simple_momentum_weight": single_budget,
            "pool_equal_weight_total_weight": min(
                float(settings.get("risk.max_total_exposure")), single_budget * top_k
            ),
            "benchmark_total_weight": single_budget,
            "diversified_top_k_max_weight": min(
                float(settings.get("risk.max_total_exposure")), single_budget * top_k
            ),
        },
        "metrics": metrics,
        "paired_comparisons": comparisons,
        "ranking_metrics": {
            "top_rank_win_rate": float(np.mean([row["top_ranked_winner"] for row in rows])),
            "mean_rank_information_coefficient": _mean_optional(
                [row["rank_information_coefficient"] for row in rows]
            ),
        },
        "episodes": rows,
        "learning_samples": {
            "recorded": record_learning_samples,
            "count": sum(len(row["candidate_results"]) for row in rows)
            if record_learning_samples
            else 0,
            "training_eligible": False,
            "reason": "fixed current symbols are not a historical point-in-time market universe",
        },
        "evidence_status": evidence_status,
        "evidence_qualification": {
            "qualified": len(rows) >= 30 and not degraded_sources,
            "qualification_scope": "fixed_user_supplied_universe_only",
            "qualified_for_market_wide_selection_claim": False,
            "minimum_measured_episodes": 30,
            "clean_data_path": not degraded_sources,
            "point_in_time_market_universe_available": False,
        },
        "claim_boundary": (
            "this measures ranking value inside a frozen research pool and does not remove the pool's "
            "selection/survivorship bias; it is not evidence that the system can select from all A-shares"
        ),
    }
    if save:
        output["replay_id"] = StockRankingReplayRepository(
            settings.resolve(settings.get("system.database_path"))
        ).save(
            start,
            end,
            horizon_days,
            len(rows),
            universe_hash,
            "ok" if not degraded_sources else "degraded",
            output,
        )
    return output


def _select_stock_replay_dates(
    frame: pd.DataFrame,
    universe: list[str],
    benchmark_dates: list[date],
    start: date,
    end: date,
    horizon_days: int,
    episodes: int,
) -> list[date]:
    index_by_date = {day: index for index, day in enumerate(benchmark_dates)}
    history = {
        symbol: list(frame.loc[frame["symbol"] == symbol, "date"].sort_values())
        for symbol in universe
    }
    eligible = []
    for day in benchmark_dates:
        index = index_by_date[day]
        if not (start <= day <= end) or index + horizon_days >= len(benchmark_dates):
            continue
        available = sum(
            pd.Series(days).le(pd.Timestamp(day)).sum() >= 120 for days in history.values()
        )
        if available >= 2:
            eligible.append(day)
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


def _simulate_stock_portfolio(
    frame: pd.DataFrame,
    symbols: list[str],
    signal_date: date,
    target_date: date,
    capital: float,
    total_weight: float,
    cost_model: CostModel,
    *,
    asset_type: str = "stock",
) -> dict[str, Any]:
    unique = list(dict.fromkeys(symbols))
    weight = total_weight / len(unique) if unique else 0.0
    legs = [
        _simulate_leg(
            frame,
            symbol,
            signal_date,
            target_date,
            capital,
            weight,
            cost_model,
            asset_type=asset_type,
        )
        for symbol in unique
    ]
    net_pnl = sum(float(leg["net_pnl"]) for leg in legs)
    return {
        "symbols": unique,
        "target_weight": total_weight,
        "traded_legs": sum(bool(leg["traded"]) for leg in legs),
        "net_pnl": net_pnl,
        "net_return": net_pnl / capital if capital else 0.0,
        "capital_before": capital,
        "capital_after": capital + net_pnl,
        "legs": legs,
    }


def _simulate_leg(
    frame: pd.DataFrame,
    symbol: str,
    signal_date: date,
    target_date: date,
    capital: float,
    weight: float,
    cost_model: CostModel,
    *,
    asset_type: str,
) -> dict[str, Any]:
    rows = frame[frame["symbol"] == symbol].sort_values("date")
    entry_candidates = rows[rows["date"] > pd.Timestamp(signal_date)]
    entry = next(
        (
            row
            for _, row in entry_candidates.iterrows()
            if not bool(row.get("suspended", False))
            and not (asset_type == "stock" and bool(row.get("limit_up", False)))
        ),
        None,
    )
    exit_candidates = rows[rows["date"] >= pd.Timestamp(target_date)]
    exit_row = next(
        (
            row
            for _, row in exit_candidates.iterrows()
            if not bool(row.get("suspended", False))
            and not (asset_type == "stock" and bool(row.get("limit_down", False)))
        ),
        None,
    )
    terminal_exit = False
    if entry is not None and exit_row is None:
        suspended = (
            rows["suspended"].astype(bool)
            if "suspended" in rows
            else pd.Series(False, index=rows.index)
        )
        limit_down = (
            rows["limit_down"].astype(bool)
            if "limit_down" in rows
            else pd.Series(False, index=rows.index)
        )
        terminal_candidates = rows[
            (rows["date"] > entry["date"])
            & (rows["date"] < pd.Timestamp(target_date))
            & ~suspended
            & ~limit_down
        ]
        if not terminal_candidates.empty:
            exit_row = terminal_candidates.iloc[-1]
            terminal_exit = True
    if entry is None or (exit_row is not None and exit_row["date"] <= entry["date"]):
        return {
            "symbol": symbol,
            "traded": False,
            "quantity": 0,
            "net_pnl": 0.0,
            "reason": "no executable entry",
        }
    quantity = int(max(0.0, weight) * capital / float(entry["open"]) / 100) * 100
    if quantity <= 0:
        return {
            "symbol": symbol,
            "traded": False,
            "quantity": 0,
            "net_pnl": 0.0,
            "reason": "allocation is smaller than one 100-share lot",
        }
    buy = cost_model.fill(
        OrderRequest(
            symbol=symbol,
            side=Side.BUY,
            quantity=quantity,
            signal_date=signal_date,
            reason="stock ranking replay entry",
        ),
        float(entry["open"]),
        entry["date"].date(),
    )
    buy_total = buy.gross_value + buy.commission + buy.transfer_fee
    if exit_row is None:
        return {
            "symbol": symbol,
            "traded": True,
            "quantity": quantity,
            "entry_date": entry["date"].date().isoformat(),
            "exit_date": None,
            "entry_price": buy.price,
            "exit_price": 0.0,
            "fees": buy.commission + buy.transfer_fee,
            "slippage": buy.slippage,
            "net_pnl": -buy_total,
            "position_net_return": -1.0,
            "terminal_exit": True,
            "terminal_exit_reason": "no_sellable_bar_before_delisting_or_history_end",
        }
    sell = cost_model.fill(
        OrderRequest(
            symbol=symbol,
            side=Side.SELL,
            quantity=quantity,
            signal_date=target_date,
            reason="stock ranking replay exit",
        ),
        float(exit_row["close"]),
        exit_row["date"].date(),
    )
    sell_net = sell.gross_value - sell.commission - sell.stamp_duty - sell.transfer_fee
    pnl = sell_net - buy_total
    return {
        "symbol": symbol,
        "traded": True,
        "quantity": quantity,
        "entry_date": entry["date"].date().isoformat(),
        "exit_date": exit_row["date"].date().isoformat(),
        "entry_price": buy.price,
        "exit_price": sell.price,
        "fees": (
            buy.commission
            + buy.transfer_fee
            + sell.commission
            + sell.stamp_duty
            + sell.transfer_fee
        ),
        "slippage": buy.slippage + sell.slippage,
        "net_pnl": pnl,
        "position_net_return": pnl / buy_total if buy_total else 0.0,
        "terminal_exit": terminal_exit,
        "terminal_exit_reason": (
            "last_sellable_close_before_delisting_or_history_end" if terminal_exit else None
        ),
    }


def _adjusted_return(
    frame: pd.DataFrame, symbol: str, signal_date: date, target_date: date
) -> float | None:
    rows = frame[frame["symbol"] == symbol].sort_values("date")
    start_rows = rows[rows["date"] <= pd.Timestamp(signal_date)]
    end_rows = rows[rows["date"] >= pd.Timestamp(target_date)]
    if start_rows.empty or end_rows.empty:
        return None
    start_price = float(start_rows.iloc[-1]["signal_close"])
    end_price = float(end_rows.iloc[0]["signal_close"])
    if start_price <= 0:
        return None
    return (end_price / start_price - 1) * 100


def _path_metrics(
    rows: list[dict[str, Any]], strategy: str, initial_capital: float
) -> dict[str, Any]:
    equity = initial_capital
    peak = equity
    max_drawdown = 0.0
    returns = []
    traded = 0
    for row in rows:
        trade = row["trades"][strategy]
        if abs(float(trade["capital_before"]) - equity) > 0.01:
            raise ValueError(f"{strategy} replay capital path is discontinuous")
        returns.append(float(trade["net_return"]))
        traded += int(trade["traded_legs"] > 0)
        equity = float(trade["capital_after"])
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    return {
        "episodes": len(rows),
        "episodes_with_trades": traded,
        "participation_rate": traded / len(rows),
        "total_return": equity / initial_capital - 1,
        "average_episode_return": float(np.mean(returns)),
        "episode_win_rate": float(np.mean([value > 0 for value in returns])),
        "max_drawdown": max_drawdown,
        "final_equity": equity,
    }


def _paired_comparison(
    rows: list[dict[str, Any]], system_name: str, baseline_name: str
) -> dict[str, Any]:
    differences = np.asarray(
        [
            float(row["trades"][system_name]["net_return"])
            - float(row["trades"][baseline_name]["net_return"])
            for row in rows
        ],
        dtype=float,
    )
    rng = np.random.default_rng(20260714)
    bootstrapped = np.asarray(
        [
            float(rng.choice(differences, size=len(differences), replace=True).mean())
            for _ in range(2000)
        ]
    )
    return {
        "system": system_name,
        "baseline": baseline_name,
        "samples": len(differences),
        "mean_excess_return": float(differences.mean()),
        "median_excess_return": float(np.median(differences)),
        "positive_excess_rate": float(np.mean(differences > 0)),
        "probability_mean_excess_positive": float(np.mean(bootstrapped > 0)),
        "bootstrap_90pct_interval": [
            float(np.quantile(bootstrapped, 0.05)),
            float(np.quantile(bootstrapped, 0.95)),
        ],
    }


def _rank_information_coefficient(results: list[dict[str, Any]]) -> float | None:
    comparable = [item for item in results if item["realized_adjusted_return_pct"] is not None]
    if len(comparable) < 2:
        return None
    scores = pd.Series([item["screen_score"] for item in comparable]).rank()
    returns = pd.Series([item["realized_adjusted_return_pct"] for item in comparable]).rank()
    value = scores.corr(returns)
    return float(value) if pd.notna(value) else None


def _blind_candidate_id(universe_hash: str, episode: int, symbol: str) -> str:
    digest = hashlib.sha256(f"{universe_hash}:{episode}:{symbol}".encode("utf-8")).hexdigest()
    return f"STOCK_CANDIDATE_{digest[:10].upper()}"


def _return_label(realized_return_pct: float, threshold: float) -> str:
    if realized_return_pct > threshold:
        return "up"
    if realized_return_pct < -threshold:
        return "down"
    return "flat"


def _mean_optional(values: list[float | None]) -> float | None:
    available = [float(value) for value in values if value is not None]
    return float(np.mean(available)) if available else None
