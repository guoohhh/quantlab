from __future__ import annotations

from datetime import date
from math import sqrt
from typing import Any, Callable

import numpy as np
import pandas as pd

from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.domain.models import Bar
from quantlab.domain import ResearchProvenance
from quantlab.persistence import DecisionRepository, TerminalRepository
from quantlab.reporting import research_persistence_context
from quantlab.security import safe_error_detail
from quantlab.workflows.candidates import scan_etf_rotation
from quantlab.workflows.radar import ETF_METADATA, calculate_market_radar
from quantlab.workflows.research import analyze_symbol


TOURNAMENT_WEIGHTS = {
    "decision_composite": 0.30,
    "forecast_20d": 0.20,
    "forecast_5d": 0.10,
    "factor_composite": 0.15,
    "radar_strength": 0.15,
    "action_quality": 0.10,
}

ACTION_QUALITY = {
    "buy": 1.0,
    "add": 0.8,
    "watch": 0.25,
    "hold": 0.0,
    "reduce": -0.6,
    "sell": -1.0,
    "review_required": -0.75,
}

SCENARIO_SHOCKS = {
    "equity_selloff": {
        "A股宽基": -0.10,
        "A股成长": -0.15,
        "A股红利": -0.07,
        "海外权益": -0.12,
        "黄金": 0.04,
        "债券": 0.015,
        "其他": -0.08,
    },
    "rates_up": {
        "A股宽基": -0.05,
        "A股成长": -0.08,
        "A股红利": -0.03,
        "海外权益": -0.07,
        "黄金": -0.05,
        "债券": -0.04,
        "其他": -0.05,
    },
    "inflation_shock": {
        "A股宽基": -0.06,
        "A股成长": -0.09,
        "A股红利": -0.03,
        "海外权益": -0.05,
        "黄金": 0.08,
        "债券": -0.06,
        "其他": -0.05,
    },
    "liquidity_crunch": {
        "A股宽基": -0.11,
        "A股成长": -0.16,
        "A股红利": -0.08,
        "海外权益": -0.14,
        "黄金": -0.03,
        "债券": -0.02,
        "其他": -0.10,
    },
}


def run_candidate_tournament(
    settings: Settings,
    as_of: date | None = None,
    *,
    candidate_limit: int = 3,
    shortlist_size: int = 2,
    max_correlation: float = 0.80,
    save: bool = True,
    bars: list[Bar] | None = None,
    radar: dict[str, Any] | None = None,
    analyzer: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not 2 <= candidate_limit <= 6:
        raise ValueError("candidate_limit must be between 2 and 6")
    if not 1 <= shortlist_size <= candidate_limit:
        raise ValueError("shortlist_size must be between 1 and candidate_limit")
    if not 0.0 <= max_correlation <= 1.0:
        raise ValueError("max_correlation must be between 0 and 1")

    requested_as_of = as_of or date.today()
    degraded_sources: list[str] = []
    if bars is None:
        scan = scan_etf_rotation(settings, requested_as_of)
        bars = scan.bars
        degraded_sources.extend(scan.degraded_sources)
    if not bars:
        raise ValueError("candidate tournament requires ETF history")
    symbols = list(settings.get("strategies.etf_rotation.universe", ETF_METADATA))
    if radar is None:
        radar = calculate_market_radar(
            bars,
            symbols=symbols,
            requested_as_of=requested_as_of,
            source="candidate_tournament",
            degraded_sources=degraded_sources,
        )
    effective_as_of = date.fromisoformat(radar["as_of"])
    candidates = [
        item for item in radar["instruments"] if int(item.get("observations") or 0) >= 120
    ][:candidate_limit]
    candidates = [
        {**candidate, "radar_rank": rank} for rank, candidate in enumerate(candidates, start=1)
    ]
    if len(candidates) < 2:
        raise ValueError("candidate tournament requires at least two analyzable instruments")

    analyze = analyzer or analyze_symbol
    research_rows = []
    decision_repository = DecisionRepository(settings.resolve(settings.get("system.database_path")))
    for radar_row in candidates:
        symbol = radar_row["symbol"]
        try:
            output = analyze(
                settings,
                symbol,
                effective_as_of,
                asset_type="etf",
                include_events=False,
            )
            if save:
                decision_repository.save(
                    output["decision_run"],
                    research_persistence_context(output),
                    provenance=ResearchProvenance(
                        origin="historical_research",
                        requested_as_of=as_of,
                        evidence_stage="candidate_tournament",
                    ),
                )
            research_rows.append(_candidate_record(output, radar_row))
        except Exception as exc:
            research_rows.append(
                {
                    "symbol": symbol,
                    "name": radar_row.get("name", symbol),
                    "category": radar_row.get("category", "其他"),
                    "risk_bucket": radar_row.get("risk_bucket", "balanced"),
                    "radar_rank": radar_row.get("radar_rank"),
                    "status": "error",
                    "error": safe_error_detail(exc),
                    "tournament_score": 0.0,
                    "action": "review_required",
                    "actionable": False,
                    "review_eligible": False,
                }
            )
            continue

    ranked = rank_tournament_candidates(
        research_rows,
        bars,
        shortlist_size=shortlist_size,
        max_correlation=max_correlation,
    )
    comparison_weights = _comparison_weights(ranked["diversified_shortlist"], total=0.30)
    categories = {
        item["symbol"]: {
            "category": item.get("category", "其他"),
            "risk_bucket": item.get("risk_bucket", "balanced"),
        }
        for item in research_rows
    }
    stress = stress_test_portfolio(
        comparison_weights,
        bars,
        capital=float(settings.get("system.initial_capital")),
        metadata=categories,
    )
    output = {
        "as_of": effective_as_of.isoformat(),
        "candidate_limit": candidate_limit,
        "shortlist_size": shortlist_size,
        "max_correlation": max_correlation,
        "ranking_formula": TOURNAMENT_WEIGHTS,
        "candidates": ranked["ranked_candidates"],
        "diversified_shortlist": ranked["diversified_shortlist"],
        "correlation_matrix": ranked["correlation_matrix"],
        "comparison_portfolio": {
            "weights": comparison_weights,
            "hypothetical_only": True,
            "note": (
                "equal-weight 30% exposure for scenario comparison only; actionable orders still "
                "require buy/add approval and the portfolio planner"
            ),
        },
        "stress_test": stress,
        "degraded_sources": list(
            dict.fromkeys(degraded_sources + list(radar.get("degraded_sources", [])))
        ),
        "manual_execution_only": True,
    }
    if save:
        output["tournament_id"] = TerminalRepository(
            settings.resolve(settings.get("system.database_path"))
        ).save_candidate_tournament(effective_as_of, output)
    return output


def _candidate_record(output: dict[str, Any], radar_row: dict[str, Any]) -> dict[str, Any]:
    run = output["decision_run"]
    decision = run.decision
    reviewer = run.reports["reviewer"]
    council = run.reports["council"]
    forecasts = {item.horizon_days: item for item in run.forecasts}
    forecast_5 = _forecast_direction(forecasts[5])
    forecast_20 = _forecast_direction(forecasts[20])
    factor_score = float(output["report"].composite_score)
    composite = float(run.decision_trace["composite_score"])
    radar_score = (float(radar_row["strength_score"]) - 50.0) / 50.0
    action_quality = ACTION_QUALITY.get(decision.action, -1.0)
    raw_score = (
        TOURNAMENT_WEIGHTS["decision_composite"] * composite
        + TOURNAMENT_WEIGHTS["forecast_20d"] * forecast_20
        + TOURNAMENT_WEIGHTS["forecast_5d"] * forecast_5
        + TOURNAMENT_WEIGHTS["factor_composite"] * factor_score
        + TOURNAMENT_WEIGHTS["radar_strength"] * radar_score
        + TOURNAMENT_WEIGHTS["action_quality"] * action_quality
    )
    penalty = (
        0.35 * float(decision.requires_human_review)
        + 0.35 * float(council.veto_triggered)
        + min(0.15, 0.05 * len(decision.degraded_sources))
        + 0.25 * float(not reviewer.approved)
    )
    score = float(np.clip(50 + 50 * (raw_score - penalty), 0, 100))
    review_eligible = bool(
        reviewer.approved
        and not decision.requires_human_review
        and not council.veto_triggered
        and decision.action in {"buy", "add", "watch", "hold"}
    )
    actionable = bool(review_eligible and decision.action in {"buy", "add"})
    return {
        "symbol": decision.symbol,
        "name": radar_row.get("name", decision.symbol),
        "category": radar_row.get("category", "其他"),
        "risk_bucket": radar_row.get("risk_bucket", "balanced"),
        "radar_rank": radar_row.get("radar_rank"),
        "status": "ok",
        "action": decision.action,
        "actionable": actionable,
        "review_eligible": review_eligible,
        "requires_human_review": decision.requires_human_review,
        "reviewer_approved": reviewer.approved,
        "veto_triggered": council.veto_triggered,
        "confidence": decision.confidence,
        "target_weight_cap": decision.target_weight,
        "tournament_score": round(score, 4),
        "score_trace": {
            "raw_score": raw_score,
            "penalty": penalty,
            "decision_composite": composite,
            "forecast_5d": forecast_5,
            "forecast_20d": forecast_20,
            "factor_composite": factor_score,
            "radar_strength": radar_score,
            "action_quality": action_quality,
        },
        "forecast_5d": forecasts[5].model_dump(mode="json"),
        "forecast_20d": forecasts[20].model_dump(mode="json"),
        "radar": radar_row,
        "run_id": run.run_id,
        "degraded_sources": list(decision.degraded_sources),
    }


def rank_tournament_candidates(
    candidates: list[dict[str, Any]],
    bars: list[Bar] | list[dict[str, Any]],
    *,
    shortlist_size: int,
    max_correlation: float,
) -> dict[str, Any]:
    if shortlist_size < 1:
        raise ValueError("shortlist_size must be positive")
    if not np.isfinite(max_correlation) or not 0 <= max_correlation <= 1:
        raise ValueError("max_correlation must be between 0 and 1")
    ranked = sorted(
        candidates,
        key=lambda item: (-float(item.get("tournament_score", 0)), item.get("symbol", "")),
    )
    for rank, item in enumerate(ranked, start=1):
        item["tournament_rank"] = rank
    symbols = [item["symbol"] for item in ranked if item.get("status") == "ok"]
    correlation = _correlation_matrix(bars, symbols)
    selected = []
    for item in ranked:
        if not item.get("review_eligible"):
            item["diversification_status"] = "not_eligible"
            continue
        if len(selected) >= shortlist_size:
            item["diversification_status"] = "shortlist_full"
            continue
        correlated_with = next(
            (
                chosen["symbol"]
                for chosen in selected
                if correlation.get(item["symbol"], {}).get(chosen["symbol"]) is not None
                and correlation[item["symbol"]][chosen["symbol"]] > max_correlation
            ),
            None,
        )
        if correlated_with:
            item["diversification_status"] = f"excluded_high_correlation:{correlated_with}"
            continue
        item["diversification_status"] = "selected"
        selected.append(item)
    return {
        "ranked_candidates": ranked,
        "diversified_shortlist": selected,
        "correlation_matrix": correlation,
    }


def stress_test_portfolio(
    weights: dict[str, float],
    bars: list[Bar] | list[dict[str, Any]],
    *,
    capital: float,
    metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not np.isfinite(capital) or capital <= 0:
        raise ValueError("stress-test capital must be positive")
    if any(not np.isfinite(weight) or weight < 0 for weight in weights.values()):
        raise ValueError("stress-test weights must be finite and non-negative")
    if sum(weights.values()) > 1 + 1e-9:
        raise ValueError("stress-test weights must be non-negative and sum to at most one")
    metadata = metadata or {}
    symbols = list(weights)
    returns = _return_matrix(bars, symbols)
    historical = {
        "observations": 0,
        "requested_symbols": symbols,
        "available_symbols": [],
        "missing_symbols": symbols,
        "annualized_volatility": None,
        "one_day_var_95_pct": None,
        "one_day_cvar_95_pct": None,
        "var_95_amount": None,
        "cvar_95_amount": None,
        "maximum_historical_drawdown": None,
        "variance_contribution": {},
    }
    available_symbols = [symbol for symbol in symbols if symbol in returns.columns]
    if not returns.empty and available_symbols:
        vector = np.asarray([weights[symbol] for symbol in available_symbols], dtype=float)
        portfolio_returns = returns[available_symbols].to_numpy() @ vector
        threshold = float(np.quantile(portfolio_returns, 0.05))
        tail = portfolio_returns[portfolio_returns <= threshold]
        cvar = float(tail.mean()) if len(tail) else threshold
        annualized_volatility = float(np.std(portfolio_returns, ddof=0) * sqrt(252))
        equity_curve = np.cumprod(1 + portfolio_returns)
        peaks = np.maximum.accumulate(equity_curve)
        maximum_drawdown = float(np.min(equity_curve / peaks - 1))
        covariance = returns[available_symbols].cov().to_numpy() * 252
        portfolio_variance = float(vector @ covariance @ vector)
        contributions = (
            vector * (covariance @ vector) / portfolio_variance
            if portfolio_variance > 1e-12
            else np.zeros(len(vector))
        )
        historical = {
            "observations": len(portfolio_returns),
            "requested_symbols": symbols,
            "available_symbols": available_symbols,
            "missing_symbols": [symbol for symbol in symbols if symbol not in available_symbols],
            "annualized_volatility": annualized_volatility,
            "one_day_var_95_pct": max(0.0, -threshold),
            "one_day_cvar_95_pct": max(0.0, -cvar),
            "var_95_amount": max(0.0, -threshold) * capital,
            "cvar_95_amount": max(0.0, -cvar) * capital,
            "maximum_historical_drawdown": maximum_drawdown,
            "variance_contribution": {
                symbol: float(contribution)
                for symbol, contribution in zip(available_symbols, contributions)
            },
        }
    scenarios = []
    for scenario, category_shocks in SCENARIO_SHOCKS.items():
        contributions = {}
        for symbol, weight in weights.items():
            category = metadata.get(symbol, {}).get("category", "其他")
            shock = float(category_shocks.get(category, category_shocks["其他"]))
            contributions[symbol] = weight * shock
        shock_return = sum(contributions.values())
        scenarios.append(
            {
                "scenario": scenario,
                "portfolio_return": shock_return,
                "pnl_amount": shock_return * capital,
                "ending_equity": capital * (1 + shock_return),
                "contributions": contributions,
            }
        )
    worst = min(scenarios, key=lambda item: item["portfolio_return"]) if scenarios else None
    return {
        "capital": capital,
        "invested_weight": sum(weights.values()),
        "cash_weight": 1 - sum(weights.values()),
        "historical_risk": historical,
        "scenarios": scenarios,
        "worst_scenario": worst,
        "methodology": [
            "historical VaR/CVaR use up to 252 aligned daily adjusted-close returns",
            "scenario shocks are transparent deterministic assumptions, not forecasts",
            "cash is assumed unchanged and no leverage or short positions are allowed",
        ],
    }


def _forecast_direction(forecast) -> float:
    return float((forecast.up_probability - forecast.down_probability) * forecast.confidence)


def _correlation_matrix(
    bars: list[Bar] | list[dict[str, Any]], symbols: list[str]
) -> dict[str, dict[str, float | None]]:
    returns = _return_matrix(bars, symbols)
    if returns.empty:
        return {symbol: {other: None for other in symbols} for symbol in symbols}
    correlation = returns.corr()
    return {
        symbol: {
            other: (
                round(float(correlation.loc[symbol, other]), 6)
                if symbol in correlation.index
                and other in correlation.columns
                and pd.notna(correlation.loc[symbol, other])
                else None
            )
            for other in symbols
        }
        for symbol in symbols
    }


def _return_matrix(bars: list[Bar] | list[dict[str, Any]], symbols: list[str]) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    records = [item.model_dump() if isinstance(item, Bar) else item for item in bars]
    frame = pd.DataFrame(records)
    if frame.empty or not {"symbol", "date", "close"}.issubset(frame.columns):
        return pd.DataFrame()
    frame = frame[frame["symbol"].isin(symbols)].copy()
    if frame.empty:
        return pd.DataFrame()
    frame["date"] = pd.to_datetime(frame["date"])
    frame["signal_close"] = frame.get("adjusted_close", frame["close"])
    if "adjusted_close" in frame:
        frame["signal_close"] = frame["adjusted_close"].fillna(frame["close"])
    frame["signal_close"] = pd.to_numeric(frame["signal_close"], errors="coerce")
    frame = frame.dropna(subset=["date", "signal_close"])
    pivot = frame.pivot_table(
        index="date", columns="symbol", values="signal_close", aggfunc="last"
    ).sort_index()
    available = [symbol for symbol in symbols if symbol in pivot.columns]
    if not available:
        return pd.DataFrame()
    return pivot[available].pct_change(fill_method=None).dropna().tail(252)


def _comparison_weights(shortlist: list[dict[str, Any]], total: float) -> dict[str, float]:
    if not shortlist:
        return {}
    weight = total / len(shortlist)
    return {item["symbol"]: weight for item in shortlist}


def settle_candidate_tournaments(
    settings: Settings,
    as_of: date | None = None,
    *,
    limit: int = 20,
    bars: list[Bar] | list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not 1 <= limit <= 200:
        raise ValueError("tournament settlement limit must be between 1 and 200")
    repository = TerminalRepository(settings.resolve(settings.get("system.database_path")))
    tournaments = repository.candidate_tournament_records(limit)
    if not tournaments:
        return {
            "settled": [],
            "pending": [],
            "not_comparable": [],
            "degraded_sources": [],
            "scorecard": candidate_tournament_scorecard(settings),
        }

    cutoff = as_of or date.today()
    pending_tournaments = [
        item
        for item in tournaments
        if date.fromisoformat(item["as_of"]) < cutoff
        and not {"5", "20"}.issubset(
            {
                horizon
                for horizon, result in item.get("settlement", {}).items()
                if result.get("status") in {"settled", "not_comparable"}
            }
        )
    ]
    if not pending_tournaments:
        return {
            "settled": [],
            "pending": [],
            "not_comparable": [],
            "degraded_sources": [],
            "scorecard": candidate_tournament_scorecard(settings),
        }

    preclassified = []
    comparable_tournaments = []
    for tournament in pending_tournaments:
        successful = [
            item for item in tournament.get("candidates", []) if item.get("status") == "ok"
        ]
        if len(successful) >= 2:
            comparable_tournaments.append(tournament)
            continue
        settlement = dict(tournament.get("settlement", {}))
        for horizon in (5, 20):
            settlement[str(horizon)] = {
                "status": "not_comparable",
                "horizon_days": horizon,
                "reason": "fewer than two candidates completed research",
                "candidate_count": len(successful),
                "missing_symbols": [],
            }
        tournament["settlement"] = settlement
        repository.update_candidate_tournament(tournament["tournament_id"], tournament)
        preclassified.append(
            {
                "tournament_id": tournament["tournament_id"],
                "as_of": tournament["as_of"],
                "settled_horizons": [],
            }
        )
    pending_tournaments = comparable_tournaments
    if not pending_tournaments:
        return {
            "settled": [],
            "pending": [],
            "not_comparable": preclassified,
            "degraded_sources": [],
            "scorecard": candidate_tournament_scorecard(settings),
        }

    degraded_sources: list[str] = []
    if bars is None:
        symbols = sorted(
            {
                candidate["symbol"]
                for tournament in pending_tournaments
                for candidate in tournament.get("candidates", [])
                if candidate.get("status") == "ok"
            }
        )
        start = min(date.fromisoformat(item["as_of"]) for item in pending_tournaments)
        fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
        provider = CachedProvider(
            fallback,
            settings.resolve(settings.get("system.data_dir")) / "cache",
        )
        bars = provider.bars(symbols, start, cutoff)
        degraded_sources.extend(fallback.last_degraded_from)

    frame = _settlement_price_frame(bars or [], cutoff)
    settled = []
    pending = []
    not_comparable = list(preclassified)
    for tournament in pending_tournaments:
        settlement = dict(tournament.get("settlement", {}))
        for horizon in (5, 20):
            existing = settlement.get(str(horizon), {})
            if existing.get("status") in {"settled", "not_comparable"}:
                continue
            settlement[str(horizon)] = _settle_tournament_horizon(
                tournament,
                frame,
                horizon,
            )
        tournament["settlement"] = settlement
        repository.update_candidate_tournament(tournament["tournament_id"], tournament)
        complete_horizons = [
            int(horizon)
            for horizon, result in settlement.items()
            if result.get("status") == "settled"
        ]
        terminal_horizons = [
            int(horizon)
            for horizon, result in settlement.items()
            if result.get("status") in {"settled", "not_comparable"}
        ]
        record = {
            "tournament_id": tournament["tournament_id"],
            "as_of": tournament["as_of"],
            "settled_horizons": sorted(complete_horizons),
        }
        if {5, 20}.issubset(set(complete_horizons)):
            settled.append(record)
        elif {5, 20}.issubset(set(terminal_horizons)):
            not_comparable.append(record)
        else:
            pending.append(record)
    return {
        "settled": settled,
        "pending": pending,
        "not_comparable": not_comparable,
        "degraded_sources": list(dict.fromkeys(degraded_sources)),
        "scorecard": candidate_tournament_scorecard(settings),
    }


def candidate_tournament_scorecard(settings: Settings, limit: int = 100) -> dict[str, Any]:
    repository = TerminalRepository(settings.resolve(settings.get("system.database_path")))
    tournaments = repository.candidate_tournament_records(limit)
    horizons = {}
    for horizon in (5, 20):
        results = [
            item.get("settlement", {}).get(str(horizon), {})
            for item in tournaments
            if item.get("settlement", {}).get(str(horizon), {}).get("status") == "settled"
        ]
        samples = len(results)
        horizons[str(horizon)] = {
            "samples": samples,
            "evidence_status": "measured" if samples >= 30 else "illustrative",
            "minimum_measured_samples": 30,
            "top_rank_win_rate": _mean_or_none(
                [float(item["top_ranked_winner"]) for item in results]
            ),
            "positive_excess_vs_radar_rate": _mean_or_none(
                [float(item["agent_rank_excess_vs_radar_pct"] > 0) for item in results]
            ),
            "mean_agent_rank_excess_vs_radar_pct": _mean_or_none(
                [item["agent_rank_excess_vs_radar_pct"] for item in results]
            ),
            "positive_excess_vs_equal_weight_rate": _mean_or_none(
                [float(item["top_ranked_excess_vs_equal_weight_pct"] > 0) for item in results]
            ),
            "mean_top_ranked_excess_vs_equal_weight_pct": _mean_or_none(
                [item["top_ranked_excess_vs_equal_weight_pct"] for item in results]
            ),
            "mean_shortlist_excess_vs_all_pct": _mean_or_none(
                [item["shortlist_excess_vs_all_pct"] for item in results]
            ),
            "mean_rank_information_coefficient": _mean_or_none(
                [
                    item["rank_information_coefficient"]
                    for item in results
                    if item["rank_information_coefficient"] is not None
                ]
            ),
        }
    return {
        "tournaments": len(tournaments),
        "horizons": horizons,
        "claim_boundary": (
            "settled tournament outcomes measure ranking behavior on recorded runs; "
            "they do not guarantee future returns"
        ),
    }


def _settlement_price_frame(bars: list[Bar] | list[dict[str, Any]], cutoff: date) -> pd.DataFrame:
    records = [item.model_dump() if isinstance(item, Bar) else item for item in bars]
    frame = pd.DataFrame(records)
    if frame.empty or not {"symbol", "date", "close"}.issubset(frame.columns):
        return pd.DataFrame(columns=["symbol", "date", "signal_close"])
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame[frame["date"] <= pd.Timestamp(cutoff)].copy()
    frame["signal_close"] = frame["close"]
    if "adjusted_close" in frame.columns:
        frame["signal_close"] = frame["adjusted_close"].fillna(frame["close"])
    frame["signal_close"] = pd.to_numeric(frame["signal_close"], errors="coerce")
    return (
        frame.dropna(subset=["symbol", "date", "signal_close"])
        .sort_values(["symbol", "date"])
        .drop_duplicates(["symbol", "date"], keep="last")
    )


def _settle_tournament_horizon(
    tournament: dict[str, Any], frame: pd.DataFrame, horizon: int
) -> dict[str, Any]:
    origin = date.fromisoformat(tournament["as_of"])
    candidates = [item for item in tournament.get("candidates", []) if item.get("status") == "ok"]
    if len(candidates) < 2:
        return {
            "status": "not_comparable",
            "horizon_days": horizon,
            "reason": "fewer than two candidates completed research",
            "candidate_count": len(candidates),
            "missing_symbols": [],
        }
    derived_radar_rank = {
        candidate["symbol"]: rank
        for rank, candidate in enumerate(
            sorted(
                candidates,
                key=lambda item: (
                    -float(item.get("radar", {}).get("strength_score", float("-inf"))),
                    item["symbol"],
                ),
            ),
            start=1,
        )
    }
    shortlist = {item["symbol"] for item in tournament.get("diversified_shortlist", [])}
    results = []
    missing_symbols = []
    for candidate in candidates:
        symbol = candidate["symbol"]
        rows = frame[
            (frame["symbol"] == symbol) & (frame["date"] >= pd.Timestamp(origin))
        ].sort_values("date")
        if len(rows) <= horizon or rows.empty or rows.iloc[0]["date"].date() != origin:
            missing_symbols.append(symbol)
            continue
        start_price = float(rows.iloc[0]["signal_close"])
        end_row = rows.iloc[horizon]
        realized = (float(end_row["signal_close"]) / start_price - 1) * 100
        results.append(
            {
                "symbol": symbol,
                "tournament_rank": int(candidate["tournament_rank"]),
                "radar_rank": candidate.get("radar_rank")
                or derived_radar_rank[candidate["symbol"]],
                "tournament_score": float(candidate["tournament_score"]),
                "action": candidate.get("action"),
                "in_shortlist": symbol in shortlist,
                "realized_return_pct": realized,
                "evaluated_at": end_row["date"].date().isoformat(),
            }
        )
    if missing_symbols or len(results) < 2:
        return {
            "status": "pending",
            "horizon_days": horizon,
            "available_results": results,
            "missing_symbols": missing_symbols,
        }

    by_tournament = sorted(results, key=lambda item: item["tournament_rank"])
    by_radar = sorted(
        results,
        key=lambda item: (
            item["radar_rank"] if item["radar_rank"] is not None else 10_000,
            item["symbol"],
        ),
    )
    top = by_tournament[0]
    radar_leader = by_radar[0]
    best = max(results, key=lambda item: item["realized_return_pct"])
    all_average = float(np.mean([item["realized_return_pct"] for item in results]))
    shortlist_results = [item for item in results if item["in_shortlist"]]
    shortlist_average = (
        float(np.mean([item["realized_return_pct"] for item in shortlist_results]))
        if shortlist_results
        else all_average
    )
    score_ranks = pd.Series([item["tournament_score"] for item in results]).rank()
    return_ranks = pd.Series([item["realized_return_pct"] for item in results]).rank()
    rank_ic = score_ranks.corr(return_ranks)
    return {
        "status": "settled",
        "horizon_days": horizon,
        "candidate_results": results,
        "top_ranked_symbol": top["symbol"],
        "top_ranked_return_pct": top["realized_return_pct"],
        "radar_leader_symbol": radar_leader["symbol"],
        "radar_leader_return_pct": radar_leader["realized_return_pct"],
        "agent_rank_excess_vs_radar_pct": (
            top["realized_return_pct"] - radar_leader["realized_return_pct"]
        ),
        "candidate_equal_weight_return_pct": all_average,
        "top_ranked_excess_vs_equal_weight_pct": top["realized_return_pct"] - all_average,
        "shortlist_equal_weight_return_pct": shortlist_average,
        "shortlist_excess_vs_all_pct": shortlist_average - all_average,
        "best_symbol": best["symbol"],
        "best_return_pct": best["realized_return_pct"],
        "top_ranked_regret_pct": best["realized_return_pct"] - top["realized_return_pct"],
        "top_ranked_winner": top["symbol"] == best["symbol"],
        "rank_information_coefficient": (float(rank_ic) if pd.notna(rank_ic) else None),
        "evaluated_at": max(item["evaluated_at"] for item in results),
        "missing_symbols": [],
    }


def _mean_or_none(values: list[float]) -> float | None:
    return float(np.mean(values)) if values else None
