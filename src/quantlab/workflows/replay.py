from __future__ import annotations

import asyncio
import math
from collections import Counter
from datetime import date, timedelta
from typing import Any, Callable

import numpy as np
import pandas as pd

from quantlab.agents import MultiAgentDecisionSystem, ResearchContext
from quantlab.agents.decision_gate import (
    ETF_GATE_POLICY_CATALOG,
    ETF_GATE_POLICY_VERSION,
    evaluate_etf_gate_policies,
)
from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.domain.models import Bar, OrderRequest, Side, StrategySignal
from quantlab.execution import CostModel
from quantlab.factors import MomentumFactorEngine
from quantlab.learning import (
    LearningRepository,
    build_point_in_time_predictor,
    cross_sectional_features,
)
from quantlab.llm import LLMProvider, await_with_provider_close, build_provider
from quantlab.persistence import HistoricalReplayRepository
from quantlab.strategies import EtfRotationStrategy


LABELS = ("up", "flat", "down")


def run_historical_blind_replay(
    settings: Settings,
    start: date,
    end: date,
    *,
    horizon_days: int = 20,
    episodes: int = 3,
    save: bool = True,
    allow_large_run: bool = False,
    bars: list[Bar] | None = None,
    provider_factory: Callable[[], LLMProvider] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if horizon_days not in {5, 20}:
        raise ValueError("horizon_days must be 5 or 20")
    if not 1 <= episodes <= 60:
        raise ValueError("episodes must be between 1 and 60")
    if episodes > 12 and not allow_large_run:
        raise ValueError(
            "episodes above 12 require allow_large_run=true after reviewing estimated LLM cost"
        )
    if start >= end:
        raise ValueError("historical replay start must be before end")

    cfg = settings.get("strategies.etf_rotation")
    symbols = list(cfg["universe"])
    degraded_sources: list[str] = []
    source = "provided_bars"
    if bars is None:
        fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
        provider = CachedProvider(
            fallback,
            settings.resolve(settings.get("system.data_dir")) / "cache",
        )
        requested_outcome_end = end + timedelta(days=max(60, horizon_days * 5))
        latest_completed_date = date.today() - timedelta(days=1)
        bars = provider.bars(
            symbols,
            start - timedelta(days=700),
            min(requested_outcome_end, latest_completed_date),
        )
        degraded_sources = list(fallback.last_degraded_from)
        source = provider.name
    frame = pd.DataFrame([bar.model_dump() for bar in bars])
    if frame.empty:
        raise ValueError("historical replay data returned no bars")
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"], keep="last")
    signal_frame = frame.copy()
    signal_frame["close"] = signal_frame["adjusted_close"].fillna(signal_frame["close"])
    common = frame.groupby("date")["symbol"].nunique()
    common_dates = [
        timestamp.date() for timestamp, count in common.items() if count == len(symbols)
    ]
    selected_dates = _select_replay_dates(
        common_dates,
        start,
        end,
        max(max(cfg["lookbacks"]), 200),
        horizon_days,
        episodes,
    )
    if not selected_dates:
        raise ValueError("no non-overlapping replay dates satisfy history and outcome requirements")

    strategy = EtfRotationStrategy(cfg["lookbacks"], int(cfg["top_k"]), cfg["defensive_symbol"])
    learning_repository = LearningRepository(settings.resolve(settings.get("system.database_path")))
    cost_model = CostModel.from_dict(settings.get("costs.etf"))
    initial_capital = float(settings.get("system.initial_capital"))
    strategy_weight_cap = min(
        float(settings.get("risk.max_single_position")),
        float(settings.get("risk.max_total_exposure")),
    )
    rows = []
    path_capital = {
        "strategy_trade": initial_capital,
        "full_system_trade": initial_capital,
        "benchmark_trade": initial_capital,
    }
    gate_policy_names = tuple(ETF_GATE_POLICY_CATALOG)
    gate_path_capital = {name: initial_capital for name in gate_policy_names}
    for episode_index, actual_as_of in enumerate(selected_dates, start=1):
        signals = strategy.generate(actual_as_of, signal_frame)
        if not signals:
            continue
        actual_signal = signals[0]
        actual_symbol = actual_signal.symbol
        factor_report = MomentumFactorEngine().analyze(actual_symbol, signal_frame, actual_as_of)
        cross_section = cross_sectional_features(signal_frame, actual_as_of, actual_symbol)
        blind_symbol = f"ETF_CANDIDATE_{episode_index:02d}"
        blind_date = date(2000, 1, 3) + timedelta(days=episode_index * 31)
        blind_signal = StrategySignal(
            **{
                **actual_signal.model_dump(),
                "symbol": blind_symbol,
                "as_of": blind_date,
            }
        )
        blind_factors = factor_report.model_dump(mode="json")
        blind_factors["symbol"] = blind_symbol
        blind_factors["as_of"] = blind_date.isoformat()
        normalized_path = _normalized_price_path(
            signal_frame, actual_symbol, actual_as_of, observations=120
        )
        blind_factors["normalized_price_path_120"] = normalized_path
        point_in_time_predictor, pit_audit = build_point_in_time_predictor(
            learning_repository,
            "etf",
            actual_as_of,
            minimum_samples=int(settings.get("learning.minimum_samples", 100)),
            minimum_validation_samples=int(settings.get("learning.minimum_validation_samples", 20)),
            validation_fraction=float(settings.get("learning.validation_fraction", 0.20)),
            validation_folds=int(settings.get("learning.validation_folds", 3)),
            minimum_fold_pass_rate=float(settings.get("learning.minimum_fold_pass_rate", 0.67)),
            maximum_weight=float(settings.get("learning.maximum_statistical_weight", 0.50)),
        )
        llm = provider_factory() if provider_factory else build_provider(settings.section("llm"))
        blind_context = ResearchContext(
            symbol=blind_symbol,
            as_of=blind_date,
            price=100.0,
            price_is_executable=False,
            price_semantics="normalized_index_latest_observation_100",
            decision_mode="historical_blind_replay",
            execution_evidence_required=False,
            maximum_final_weight=strategy_weight_cap,
            strategy_signals=[blind_signal],
            quant_factors=blind_factors,
            price_history=_blinded_price_history_evidence(
                normalized_path, blind_symbol, blind_date
            ),
            cross_section_factors=cross_section,
            asset_type="etf",
            market_regime=factor_report.regime.value,
            data_quality=0.8 if degraded_sources else 1.0,
            degraded_sources=degraded_sources,
        )
        expected_role_keys = MultiAgentDecisionSystem.expected_llm_role_keys(blind_context)
        run = asyncio.run(
            await_with_provider_close(
                llm,
                MultiAgentDecisionSystem(llm, point_in_time_predictor).run(blind_context),
            )
        )
        actual_index = common_dates.index(actual_as_of)
        entry_date = common_dates[actual_index + 1]
        outcome_date = common_dates[actual_index + horizon_days]
        entry_bar = _bar(frame, actual_symbol, entry_date)
        outcome_bar = _bar(frame, actual_symbol, outcome_date)
        signal_bar = _bar(frame, actual_symbol, actual_as_of)
        benchmark_entry = _bar(frame, "sh510300", entry_date)
        benchmark_outcome = _bar(frame, "sh510300", outcome_date)

        strategy_weight = min(
            strategy_weight_cap,
            float(actual_signal.target_weight) * float(settings.get("risk.max_total_exposure")),
        )
        approved_buy = (
            run.decision.action in {"buy", "add"}
            and not run.decision.requires_human_review
            and run.reports["reviewer"].approved
        )
        full_weight = (
            min(strategy_weight, float(run.decision.target_weight)) if approved_buy else 0.0
        )
        strategy_trade = _simulate_trade(
            cost_model,
            path_capital["strategy_trade"],
            actual_symbol,
            strategy_weight,
            entry_date,
            entry_bar.open,
            outcome_date,
            outcome_bar.close,
        )
        full_trade = _simulate_trade(
            cost_model,
            path_capital["full_system_trade"],
            actual_symbol,
            full_weight,
            entry_date,
            entry_bar.open,
            outcome_date,
            outcome_bar.close,
        )
        benchmark_trade = _simulate_trade(
            cost_model,
            path_capital["benchmark_trade"],
            "sh510300",
            strategy_weight,
            entry_date,
            benchmark_entry.open,
            outcome_date,
            benchmark_outcome.close,
        )
        gate_allocations = evaluate_etf_gate_policies(
            decision=run.decision,
            decision_trace=run.decision_trace,
            reviewer=run.reports["reviewer"],
            forecast=next(item for item in run.forecasts if item.horizon_days == horizon_days),
            strategy_weight=strategy_weight,
        )
        gate_counterfactuals = {}
        for policy_name in gate_policy_names:
            allocation = gate_allocations[policy_name]
            trade = _simulate_trade(
                cost_model,
                gate_path_capital[policy_name],
                actual_symbol,
                float(allocation["target_weight"]),
                entry_date,
                entry_bar.open,
                outcome_date,
                outcome_bar.close,
            )
            gate_path_capital[policy_name] = trade["capital_after"]
            gate_counterfactuals[policy_name] = {
                **allocation,
                "trade": trade,
                "effect": _gate_effect(strategy_trade, trade),
            }
        current_trade = gate_counterfactuals["current_strict"]["trade"]
        if not math.isclose(
            float(current_trade["capital_after"]),
            float(full_trade["capital_after"]),
            rel_tol=1e-12,
            abs_tol=0.01,
        ):
            raise ValueError("current_strict counterfactual diverged from the production gate")
        path_capital["strategy_trade"] = strategy_trade["capital_after"]
        path_capital["full_system_trade"] = full_trade["capital_after"]
        path_capital["benchmark_trade"] = benchmark_trade["capital_after"]
        realized_return_pct = (outcome_bar.signal_close / signal_bar.signal_close - 1) * 100
        outcome = _outcome(
            realized_return_pct,
            float(settings.get("calibration.flat_threshold_pct", 1.0)),
        )
        forecast = next(item for item in run.forecasts if item.horizon_days == horizon_days)
        if strategy_trade["net_return"] < 0 and full_weight == 0:
            gate_effect = "avoided_loss"
        elif strategy_trade["net_return"] > 0 and full_weight == 0:
            gate_effect = "missed_gain"
        else:
            gate_effect = "participated"
        rows.append(
            {
                "episode": episode_index,
                "actual_as_of": actual_as_of.isoformat(),
                "blind_symbol": blind_symbol,
                "blind_date": blind_date.isoformat(),
                "actual_symbol": actual_symbol,
                "entry_date": entry_date.isoformat(),
                "outcome_date": outcome_date.isoformat(),
                "strategy_signal": actual_signal.model_dump(mode="json"),
                "market_regime": factor_report.regime.value,
                "decision": run.decision.model_dump(mode="json"),
                "decision_trace": run.decision_trace,
                "reviewer": run.reports["reviewer"].model_dump(mode="json"),
                "council": run.reports["council"].model_dump(mode="json"),
                "forecast": forecast.model_dump(mode="json"),
                "realized_return_pct": realized_return_pct,
                "outcome": outcome,
                "strategy_weight": strategy_weight,
                "full_system_weight": full_weight,
                "strategy_trade": strategy_trade,
                "full_system_trade": full_trade,
                "benchmark_trade": benchmark_trade,
                "gate_effect": gate_effect,
                "gate_counterfactuals": gate_counterfactuals,
                "point_in_time_model": pit_audit,
                "llm_audit": run.llm_audit,
                "llm_validation": _episode_llm_validation(run.llm_audit, expected_role_keys),
            }
        )
        if progress_callback:
            progress_callback(
                {
                    "completed": len(rows),
                    "requested": len(selected_dates),
                    "actual_as_of": actual_as_of.isoformat(),
                    "actual_symbol": actual_symbol,
                    "action": run.decision.action,
                    "reviewer_approved": run.reports["reviewer"].approved,
                    "council_veto": run.reports["council"].veto_triggered,
                }
            )
    if not rows:
        raise ValueError("historical replay produced no analyzable episodes")

    strategy_metrics = _trade_metrics(rows, "strategy_trade", initial_capital)
    full_metrics = _trade_metrics(rows, "full_system_trade", initial_capital)
    benchmark_metrics = _trade_metrics(rows, "benchmark_trade", initial_capital)
    gate_counterfactual_metrics = {
        policy_name: _gate_policy_metrics(
            rows,
            policy_name,
            initial_capital,
            current_total_return=full_metrics["total_return"],
            strategy_total_return=strategy_metrics["total_return"],
            strategy_max_drawdown=strategy_metrics["max_drawdown"],
        )
        for policy_name in gate_policy_names
    }
    forecast_metrics = {
        "final_ensemble": _forecast_metrics(rows, "final"),
        "raw_llm": _forecast_metrics(rows, "raw_llm"),
        "point_in_time_statistical": _forecast_metrics(rows, "statistical"),
    }
    episode_llm_validation = [row["llm_validation"] for row in rows]
    successful_role_outputs = sum(
        item["successful_non_mock_role_outputs"] for item in episode_llm_validation
    )
    recorded_endpoint_attempts = sum(
        item["recorded_endpoint_attempts"] for item in episode_llm_validation
    )
    required_role_outputs = sum(item["required_role_outputs"] for item in episode_llm_validation)
    per_episode_requirements = {item["required_role_outputs"] for item in episode_llm_validation}
    fallback_errors = [
        error for item in episode_llm_validation for error in item["fallback_errors"]
    ]
    live_llm_complete = bool(episode_llm_validation) and all(
        item["complete"] for item in episode_llm_validation
    )
    evidence_status = (
        "measured" if len(rows) >= 30 else "preliminary" if len(rows) >= 10 else "illustrative"
    )
    evidence_qualification = {
        "sample_size_status": evidence_status,
        "live_llm_complete": live_llm_complete,
        "clean_data_path": not degraded_sources,
        "qualified": live_llm_complete and not degraded_sources and len(rows) >= 30,
        "limitations": [
            limitation
            for condition, limitation in (
                (not live_llm_complete, "one or more required live LLM roles are missing"),
                (bool(degraded_sources), "one or more preferred data sources degraded"),
                (len(rows) < 30, "fewer than 30 non-overlapping episodes"),
            )
            if condition
        ],
    }
    output = {
        "method": "anonymized, non-overlapping, point-in-time historical blind replay",
        "requested_range": {"start": start.isoformat(), "end": end.isoformat()},
        "horizon_days": horizon_days,
        "requested_episodes": episodes,
        "completed_episodes": len(rows),
        "selection_rule": (
            "eligible common trading dates with >=200 prior observations; non-overlapping "
            "episodes selected evenly across the requested range without using outcomes"
        ),
        "source": source,
        "degraded_sources": degraded_sources,
        "blinding": {
            "actual_symbol_supplied_to_llm": False,
            "actual_date_supplied_to_llm": False,
            "price_normalized_to": 100.0,
            "normalized_price_path_120_supplied": True,
            "normalized_price_path_contains_absolute_prices": False,
            "actual_identity_stored_only_outside_llm_context": True,
        },
        "statistical_model_contract": (
            "each episode refits using samples whose as_of and evaluated_at are before the signal date"
        ),
        "execution_contract": (
            "T close signal, T+1 open buy, horizon-day close sell, ETF costs, whole lots, "
            "and path-dependent capital per comparison arm"
        ),
        "episodes": rows,
        "metrics": {
            "strategy_only": strategy_metrics,
            "full_system": full_metrics,
            "hs300_same_risk_budget": benchmark_metrics,
            "full_minus_strategy_total_return": (
                full_metrics["total_return"] - strategy_metrics["total_return"]
            ),
            "forecast_ablation": forecast_metrics,
            "gate_effects": {
                name: sum(row["gate_effect"] == name for row in rows)
                for name in ("avoided_loss", "missed_gain", "participated")
            },
            "decision_gate_counterfactuals": gate_counterfactual_metrics,
        },
        "decision_gate_audit": {
            "policy_version": ETF_GATE_POLICY_VERSION,
            "protocol": "DECISION_GATE_AUDIT_PROTOCOL.md",
            "production_policy_changed": False,
            "policies": ETF_GATE_POLICY_CATALOG,
            "promotion_boundary": (
                "counterfactual results may qualify a candidate only for a frozen prospective "
                "shadow account; they never replace the production gate automatically"
            ),
        },
        "llm_validation": {
            "required_role_outputs": required_role_outputs,
            "required_role_outputs_per_episode": (
                next(iter(per_episode_requirements)) if len(per_episode_requirements) == 1 else None
            ),
            "successful_non_mock_role_outputs": successful_role_outputs,
            "recorded_endpoint_attempts": recorded_endpoint_attempts,
            "fallback_errors": fallback_errors,
            "episodes": episode_llm_validation,
            "live_llm_complete": live_llm_complete,
        },
        "evidence_status": evidence_status,
        "evidence_qualification": evidence_qualification,
        "claim_boundary": (
            "a short replay is illustrative and cannot prove future profitability; expand the fixed "
            "window and prospective paper sample before making performance claims"
        ),
    }
    if save:
        output["replay_id"] = HistoricalReplayRepository(
            settings.resolve(settings.get("system.database_path"))
        ).save(
            start,
            end,
            horizon_days,
            len(rows),
            "ok" if output["llm_validation"]["live_llm_complete"] else "degraded",
            output,
        )
    return output


def _normalized_price_path(
    frame: pd.DataFrame,
    symbol: str,
    as_of: date,
    *,
    observations: int,
) -> list[float]:
    history = frame[
        (frame["symbol"] == symbol) & (frame["date"] <= pd.Timestamp(as_of))
    ].sort_values("date")
    closes = history["close"].astype(float).tail(observations)
    if closes.empty or closes.iloc[-1] <= 0:
        return []
    return [round(float(value / closes.iloc[-1] * 100), 6) for value in closes]


def _blinded_price_history_evidence(
    normalized_path: list[float],
    blind_symbol: str,
    blind_date: date,
) -> dict[str, Any]:
    """Expose trend evidence while withholding identity, real dates and absolute prices."""

    if not normalized_path:
        return {}
    series = pd.Series(normalized_path, dtype=float)
    daily_returns = series.pct_change(fill_method=None).dropna().tail(120)
    rolling_peak = series.cummax()
    moving_averages = {
        f"ma_{window}": (
            round(float(series.tail(window).mean()), 6) if len(series) >= window else None
        )
        for window in (5, 20, 60, 120)
    }
    return {
        "evidence_type": "blinded_normalized_price_history",
        "symbol": blind_symbol,
        "cutoff_date": blind_date.isoformat(),
        "observations": int(len(series)),
        "blinding": {
            "identity_blinded": True,
            "actual_dates_included": False,
            "absolute_prices_included": False,
            "raw_ohlc_intentionally_withheld": True,
        },
        "price_semantics": {
            "series": "back-adjusted signal close normalized to the latest observation",
            "normalization": "latest_observation=100",
            "executable_price_levels_available": False,
        },
        "normalized_adjusted_close_path_120": {
            "normalization": "latest_observation=100",
            "observations": int(len(series)),
            "relative_start_index": 1 - len(series),
            "relative_end_index": 0,
            "values": [round(float(value), 6) for value in series],
        },
        "recent_normalized_closes_30": [round(float(value), 6) for value in series.tail(30)],
        "returns_adjusted_pct": {
            f"{window}_trading_days": _normalized_window_return(series, window)
            for window in (20, 60, 120)
        },
        "risk_adjusted_pct": {
            "annualized_volatility_last_120_returns": (
                round(float(daily_returns.std(ddof=1) * (252**0.5) * 100), 6)
                if len(daily_returns) >= 2
                else None
            ),
            "maximum_drawdown_last_120_prices": round(
                float((series / rolling_peak - 1).min() * 100), 6
            ),
            "daily_return_observations": int(len(daily_returns)),
        },
        "moving_averages_normalized": moving_averages,
        "latest_normalized_close_vs_moving_averages": {
            name: _normalized_level_relationship(series.iloc[-1], average)
            for name, average in moving_averages.items()
        },
        "normalized_range_120": {
            "high": round(float(series.max()), 6),
            "low": round(float(series.min()), 6),
        },
    }


def _normalized_window_return(series: pd.Series, window: int) -> float | None:
    if len(series) <= window or series.iloc[-window - 1] <= 0:
        return None
    return round(float((series.iloc[-1] / series.iloc[-window - 1] - 1) * 100), 6)


def _normalized_level_relationship(
    latest: float,
    reference: float | None,
) -> dict[str, float | str | None]:
    if reference is None or reference <= 0:
        return {
            "latest_normalized_close": round(float(latest), 6),
            "moving_average": reference,
            "relation": "unknown",
            "distance_pct": None,
        }
    distance = (latest / reference - 1) * 100
    relation = "equal" if abs(distance) < 1e-9 else "above" if distance > 0 else "below"
    return {
        "latest_normalized_close": round(float(latest), 6),
        "moving_average": reference,
        "relation": relation,
        "distance_pct": round(float(distance), 6),
    }


def _episode_llm_validation(llm_audit: dict, expected_role_keys: list[str]) -> dict:
    calls = list(llm_audit.get("calls", []))
    successful = [
        call for call in calls if call.get("status") == "ok" and call.get("provider") != "mock"
    ]
    fallback_errors = [
        {
            "endpoint_id": call.get("endpoint_id"),
            "provider": call.get("provider"),
            "model": call.get("model"),
            "schema": call.get("schema"),
            "routing_key": call.get("routing_key"),
            "error_type": call.get("error_type"),
        }
        for call in calls
        if call.get("status") == "error"
    ]
    expected_counts = Counter(expected_role_keys)
    successful_counts = Counter(
        str(call.get("routing_key")) for call in successful if call.get("routing_key")
    )
    missing_roles = {
        role: required - successful_counts.get(role, 0)
        for role, required in expected_counts.items()
        if successful_counts.get(role, 0) < required
    }
    unexpected_success_roles = {
        role: count - expected_counts.get(role, 0)
        for role, count in successful_counts.items()
        if count > expected_counts.get(role, 0)
    }
    return {
        "required_role_outputs": len(expected_role_keys),
        "expected_role_counts": dict(expected_counts),
        "successful_role_counts": dict(successful_counts),
        "missing_roles": missing_roles,
        "unexpected_success_roles": unexpected_success_roles,
        "successful_non_mock_role_outputs": len(successful),
        "recorded_endpoint_attempts": len(calls),
        "fallback_errors": fallback_errors,
        "complete": not missing_roles,
    }


def _select_replay_dates(
    common_dates: list[date],
    start: date,
    end: date,
    minimum_history: int,
    horizon_days: int,
    episodes: int,
) -> list[date]:
    index_by_date = {day: index for index, day in enumerate(common_dates)}
    eligible = [
        day
        for day in common_dates
        if start <= day <= end
        and index_by_date[day] >= minimum_history
        and index_by_date[day] + horizon_days < len(common_dates)
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
    indices = np.linspace(0, len(non_overlapping) - 1, episodes)
    return [non_overlapping[int(round(index))] for index in indices]


def _bar(frame: pd.DataFrame, symbol: str, day: date) -> Bar:
    row = frame[(frame["symbol"] == symbol) & (frame["date"] == pd.Timestamp(day))]
    if row.empty:
        raise ValueError(f"missing {symbol} bar for {day}")
    payload = row.iloc[-1].to_dict()
    for key in (
        "adjusted_open",
        "adjusted_high",
        "adjusted_low",
        "adjusted_close",
        "prev_close",
        "available_at",
    ):
        if key in payload and pd.isna(payload[key]):
            payload[key] = None
    return Bar.model_validate(payload)


def _simulate_trade(
    cost_model: CostModel,
    capital: float,
    symbol: str,
    weight: float,
    entry_date: date,
    entry_open: float,
    exit_date: date,
    exit_close: float,
) -> dict[str, Any]:
    quantity = int(max(0.0, weight) * capital / entry_open / 100) * 100
    if quantity <= 0:
        return {
            "traded": False,
            "weight": weight,
            "quantity": 0,
            "net_pnl": 0.0,
            "net_return": 0.0,
            "capital_before": capital,
            "capital_after": capital,
        }
    buy = cost_model.fill(
        OrderRequest(
            symbol=symbol,
            side=Side.BUY,
            quantity=quantity,
            signal_date=entry_date,
            reason="historical blind replay entry",
        ),
        entry_open,
        entry_date,
    )
    sell = cost_model.fill(
        OrderRequest(
            symbol=symbol,
            side=Side.SELL,
            quantity=quantity,
            signal_date=exit_date,
            reason="historical blind replay exit",
        ),
        exit_close,
        exit_date,
    )
    buy_total = buy.gross_value + buy.commission + buy.transfer_fee
    sell_net = sell.gross_value - sell.commission - sell.stamp_duty - sell.transfer_fee
    pnl = sell_net - buy_total
    return {
        "traded": True,
        "weight": weight,
        "quantity": quantity,
        "entry_price": buy.price,
        "exit_price": sell.price,
        "entry_date": entry_date.isoformat(),
        "exit_date": exit_date.isoformat(),
        "fees": buy.commission
        + buy.transfer_fee
        + sell.commission
        + sell.stamp_duty
        + sell.transfer_fee,
        "slippage": buy.slippage + sell.slippage,
        "net_pnl": pnl,
        "net_return": pnl / capital,
        "capital_before": capital,
        "capital_after": capital + pnl,
    }


def _trade_metrics(
    rows: list[dict[str, Any]], field: str, initial_capital: float
) -> dict[str, Any]:
    equity = initial_capital
    peak = equity
    max_drawdown = 0.0
    episode_returns = []
    traded_returns = []
    for row in rows:
        trade = _nested_value(row, field)
        result = float(trade["net_return"])
        episode_returns.append(result)
        if trade["traded"]:
            traded_returns.append(result)
        capital_before = float(trade.get("capital_before", equity))
        if not math.isclose(capital_before, equity, rel_tol=1e-9, abs_tol=0.01):
            raise ValueError(f"{field} capital path is discontinuous")
        equity = float(trade.get("capital_after", equity * (1 + result)))
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    return {
        "episodes": len(rows),
        "trades": len(traded_returns),
        "participation_rate": len(traded_returns) / len(rows),
        "total_return": equity / initial_capital - 1,
        "average_episode_return": sum(episode_returns) / len(episode_returns),
        "trade_win_rate": (
            sum(value > 0 for value in traded_returns) / len(traded_returns)
            if traded_returns
            else None
        ),
        "max_drawdown": max_drawdown,
        "final_equity": equity,
    }


def _gate_policy_metrics(
    rows: list[dict[str, Any]],
    policy_name: str,
    initial_capital: float,
    *,
    current_total_return: float,
    strategy_total_return: float,
    strategy_max_drawdown: float,
) -> dict[str, Any]:
    field = f"gate_counterfactuals.{policy_name}.trade"
    metrics = _trade_metrics(rows, field, initial_capital)
    episode_returns = [float(_nested_value(row, field)["net_return"]) for row in rows]
    positive_returns = [value for value in episode_returns if value > 0]
    leave_one_out = [
        math.prod(1 + value for index, value in enumerate(episode_returns) if index != omitted) - 1
        for omitted in range(len(episode_returns))
    ]
    effect_counts = dict(
        Counter(row["gate_counterfactuals"][policy_name]["effect"] for row in rows)
    )
    largest_positive_share = (
        max(positive_returns) / sum(positive_returns) if positive_returns else None
    )
    screen_checks = {
        "minimum_12_episodes": len(rows) >= 12,
        "positive_increment_vs_current": metrics["total_return"] > current_total_return,
        "drawdown_not_worse_than_strategy": metrics["max_drawdown"] >= strategy_max_drawdown,
        "participation_at_least_20pct": metrics["participation_rate"] >= 0.20,
        "largest_positive_episode_below_80pct": (
            largest_positive_share is not None and largest_positive_share < 0.80
        ),
        "positive_leave_one_out_minimum": bool(leave_one_out) and min(leave_one_out) > 0,
    }
    status = (
        "insufficient_sample"
        if len(rows) < 12
        else "retrospective_screen_pass"
        if all(screen_checks.values())
        else "retrospective_screen_fail"
    )
    return {
        **metrics,
        "incremental_vs_current_total_return": metrics["total_return"] - current_total_return,
        "incremental_vs_strategy_total_return": metrics["total_return"] - strategy_total_return,
        "effect_counts": effect_counts,
        "worst_episode_return": min(episode_returns),
        "best_episode_return": max(episode_returns),
        "largest_positive_episode_share": largest_positive_share,
        "leave_one_out_min_total_return": min(leave_one_out) if leave_one_out else None,
        "screen_checks": screen_checks,
        "screening_status": status,
        "promotion_status": "research_only_not_promoted",
    }


def _nested_value(payload: dict[str, Any], dotted: str) -> Any:
    value: Any = payload
    for part in dotted.split("."):
        value = value[part]
    return value


def _gate_effect(strategy_trade: dict[str, Any], candidate_trade: dict[str, Any]) -> str:
    strategy_return = float(strategy_trade["net_return"])
    candidate_return = float(candidate_trade["net_return"])
    if strategy_return > 0:
        if not candidate_trade["traded"]:
            return "missed_gain"
        if candidate_return + 1e-12 < strategy_return:
            return "reduced_gain"
        return "participated_gain"
    if strategy_return < 0:
        if not candidate_trade["traded"]:
            return "avoided_loss"
        if candidate_return > strategy_return + 1e-12:
            return "reduced_loss"
        return "participated_loss"
    return "flat_outcome"


def _forecast_metrics(rows: list[dict[str, Any]], variant: str) -> dict[str, Any]:
    values = []
    invalid_samples = 0
    for row in rows:
        forecast = row["forecast"]
        if variant == "final":
            probabilities = [
                forecast["up_probability"],
                forecast["flat_probability"],
                forecast["down_probability"],
            ]
        elif variant == "raw_llm":
            probabilities = [
                forecast.get("raw_llm_up_probability"),
                forecast.get("raw_llm_flat_probability"),
                forecast.get("raw_llm_down_probability"),
            ]
        else:
            probabilities = [
                forecast.get("statistical_up_probability"),
                forecast.get("statistical_flat_probability"),
                forecast.get("statistical_down_probability"),
            ]
        if any(value is None for value in probabilities):
            continue
        converted = [float(value) for value in probabilities]
        if not all(
            math.isfinite(value) and 0 <= value <= 1 for value in converted
        ) or not math.isclose(sum(converted), 1.0, rel_tol=0.0, abs_tol=1e-6):
            invalid_samples += 1
            continue
        values.append((converted, row["outcome"]))
    if not values:
        return {
            "samples": 0,
            "invalid_samples": invalid_samples,
            "brier_score": None,
            "log_loss": None,
            "accuracy": None,
        }
    brier = 0.0
    log_loss = 0.0
    correct = 0
    for probabilities, outcome in values:
        target_index = LABELS.index(outcome)
        actual = [1.0 if index == target_index else 0.0 for index in range(3)]
        brier += (
            sum((probability - target) ** 2 for probability, target in zip(probabilities, actual))
            / 3
        )
        log_loss -= math.log(max(1e-12, probabilities[target_index]))
        correct += int(max(range(3), key=probabilities.__getitem__) == target_index)
    return {
        "samples": len(values),
        "invalid_samples": invalid_samples,
        "brier_score": brier / len(values),
        "log_loss": log_loss / len(values),
        "accuracy": correct / len(values),
    }


def _outcome(realized_return_pct: float, flat_threshold_pct: float) -> str:
    if realized_return_pct > flat_threshold_pct:
        return "up"
    if realized_return_pct < -flat_threshold_pct:
        return "down"
    return "flat"
