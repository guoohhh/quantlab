from __future__ import annotations

from typing import Any

from quantlab.config import Settings
from quantlab.domain.strategy_evidence import EvidenceStage, PointInTimePoolSnapshot
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
from quantlab.workflows.point_in_time import ROUND3_PROTOCOL_VERSION


def run_point_in_time_etf_replay(
    settings: Settings,
    *,
    episodes: list[dict[str, Any]],
    top_k: int = 3,
    total_exposure: float = 0.80,
    save: bool = True,
) -> dict[str, Any]:
    if top_k < 1 or not 0 <= total_exposure <= 1:
        raise ValueError("invalid ETF point-in-time replay portfolio policy")
    evaluated: list[dict[str, Any]] = []
    previous_weights: dict[str, float] = {}
    cost_rate = _cost_rate(settings)
    for episode in sorted(episodes, key=lambda item: str(item["as_of"])):
        snapshot = PointInTimePoolSnapshot.model_validate(episode["snapshot"])
        if snapshot.snapshot_type != "etf":
            raise ValueError("ETF replay requires ETF point-in-time snapshots")
        if str(episode["as_of"])[:10] != snapshot.snapshot_date.isoformat():
            raise ValueError("ETF replay episode date must match the point-in-time snapshot")
        signal_scores = {str(key): float(value) for key, value in episode["signal_scores"].items()}
        returns = {str(key): float(value) for key, value in episode.get("forward_returns", {}).items()}
        available_representatives = [
            member
            for member in snapshot.members
            if member.eligible and member.representative and member.symbol in signal_scores
        ]
        selected = sorted(
            available_representatives,
            key=lambda member: (-signal_scores[member.symbol], member.symbol),
        )[:top_k]
        selected_symbols = [item.symbol for item in selected]
        missing_returns = [symbol for symbol in selected_symbols if symbol not in returns]
        if missing_returns:
            evaluated.append(
                {
                    "as_of": snapshot.snapshot_date.isoformat(),
                    "status": "unsettled",
                    "selected": selected_symbols,
                    "missing_returns": missing_returns,
                    "snapshot_fingerprint": snapshot.fingerprint,
                }
            )
            continue
        weight = total_exposure / len(selected_symbols) if selected_symbols else 0.0
        target_weights = {symbol: weight for symbol in selected_symbols}
        turnover = 0.5 * sum(
            abs(target_weights.get(symbol, 0.0) - previous_weights.get(symbol, 0.0))
            for symbol in set(previous_weights) | set(target_weights)
        )
        gross_return = sum(returns[symbol] * weight for symbol in selected_symbols)
        cost_pct = turnover * cost_rate * 100.0
        net_return = gross_return - cost_pct
        benchmark = float(episode.get("benchmark_return_pct", 0.0)) * total_exposure
        evaluated.append(
            {
                "as_of": snapshot.snapshot_date.isoformat(),
                "status": "matured_research_replay",
                "selected": selected_symbols,
                "selected_categories": [item.category for item in selected],
                "investable_count": len(available_representatives),
                "gross_return_pct": gross_return,
                "transaction_cost_pct": cost_pct,
                "net_return_pct": net_return,
                "same_exposure_benchmark_return_pct": benchmark,
                "excess_return_pct": net_return - benchmark,
                "turnover": turnover,
                "snapshot_fingerprint": snapshot.fingerprint,
                "snapshot_source_version": snapshot.source_version,
            }
        )
        previous_weights = target_weights
    matured = [item for item in evaluated if item["status"] == "matured_research_replay"]
    returns = [float(item["net_return_pct"]) for item in matured]
    excess = [float(item["excess_return_pct"]) for item in matured]
    passed = bool(matured) and (_compound_return(returns) or 0.0) > 0
    output = {
        "status": "research_passed" if passed else "research_failed",
        "evidence_stage": EvidenceStage.RESEARCH_REPLAY.value,
        "protocol_version": ROUND3_PROTOCOL_VERSION,
        "episodes": evaluated,
        "metrics": {
            "matured_episodes": len(matured),
            "total_return_pct": _compound_return(returns),
            "same_exposure_excess_pct": _compound_return(excess),
            "maximum_drawdown_pct": _maximum_drawdown(returns),
            "average_turnover": _mean([float(item["turnover"]) for item in matured]),
        },
        "used_today_frozen_universe": False,
        "production_admitted": False,
        "claim_boundary": (
            "Every episode is constrained by its own versioned ETF snapshot. Historical reruns "
            "remain research evidence and are never promoted into the forward cohort."
        ),
    }
    if save:
        output["persistence"] = StrategyEvidenceRepository(
            settings.resolve(settings.get("system.database_path"))
        ).record_research_run(
            protocol_version=ROUND3_PROTOCOL_VERSION,
            strategy_type="point_in_time_etf_replay",
            requested_range={
                "start": matured[0]["as_of"] if matured else None,
                "end": matured[-1]["as_of"] if matured else None,
            },
            status=output["status"],
            passed=passed,
            payload=output,
        )
    return output


def _cost_rate(settings: Settings) -> float:
    costs = settings.get("costs.etf", {})
    return (
        float(costs.get("commission_rate", 0.0001)) * 2
        + float(costs.get("slippage_bps", 5.0)) * 2 / 10_000.0
    )


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _compound_return(values: list[float]) -> float | None:
    if not values:
        return None
    wealth = 1.0
    for value in values:
        wealth *= 1 + value / 100.0
    return (wealth - 1) * 100.0


def _maximum_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    wealth = 1.0
    peak = 1.0
    worst = 0.0
    for value in values:
        wealth *= 1 + value / 100.0
        peak = max(peak, wealth)
        worst = min(worst, wealth / peak - 1.0)
    return worst * 100.0


__all__ = ["run_point_in_time_etf_replay"]
