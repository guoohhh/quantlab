from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import date, datetime
from typing import Any

from quantlab.config import Settings
from quantlab.domain.strategy_evidence import EvidenceStage
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
from quantlab.workflows.point_in_time import (
    A_SHARE_V4_PROTOCOL_VERSION,
    a_share_v4_protocol,
    build_a_share_v4_candidates,
)


def run_a_share_strategy_lab_v4(
    settings: Settings,
    *,
    episodes: list[dict[str, Any]],
    source: str,
    source_version: str,
    bootstrap_simulations: int | None = None,
    save: bool = True,
) -> dict[str, Any]:
    """Evaluate the pre-registered V4 policy using point-in-time episode inputs.

    This is deliberately labelled research replay. Re-running it can never create forward or
    independent holdout evidence.
    """

    protocol = a_share_v4_protocol()
    simulations = int(
        bootstrap_simulations
        or settings.get("backtest.bootstrap_simulations", 2_000)
    )
    simulations = max(100, min(20_000, simulations))
    evaluated: list[dict[str, Any]] = []
    previous_selected: set[str] = set()
    cost_rate = _round_trip_cost_rate(settings)
    for episode in sorted(episodes, key=lambda item: str(item["as_of"])):
        as_of = _date(episode["as_of"])
        cutoff_at = _datetime(episode.get("cutoff_at") or f"{as_of.isoformat()}T15:00:00+08:00")
        correlations = _parse_correlations(episode.get("correlations", {}))
        risk_on = bool(episode.get("risk_on", True))
        snapshot = build_a_share_v4_candidates(
            snapshot_date=as_of,
            cutoff_at=cutoff_at,
            records=list(episode.get("records", [])),
            correlations=correlations,
            risk_on=risk_on,
            source=source,
            source_version=source_version,
            stage=EvidenceStage.RESEARCH_REPLAY,
        )
        by_symbol = {str(item["symbol"]): item for item in episode.get("records", [])}
        selected = [item for item in snapshot.members if item.representative]
        selected_symbols = {item.symbol for item in selected}
        missing_outcomes = [
            symbol
            for symbol in selected_symbols
            if by_symbol.get(symbol, {}).get("future_return_pct") is None
        ]
        if missing_outcomes:
            evaluated.append(
                {
                    "as_of": as_of.isoformat(),
                    "status": "unsettled",
                    "selected": sorted(selected_symbols),
                    "missing_outcomes": missing_outcomes,
                    "snapshot_fingerprint": snapshot.fingerprint,
                }
            )
            previous_selected = selected_symbols
            continue
        exposure = float(
            protocol["risk_on_exposure"] if risk_on else protocol["risk_off_exposure"]
        )
        gross_return = (
            exposure
            * sum(float(by_symbol[symbol]["future_return_pct"]) for symbol in selected_symbols)
            / len(selected_symbols)
            if selected_symbols
            else 0.0
        )
        turnover = _set_turnover(previous_selected, selected_symbols, exposure)
        transaction_cost = turnover * cost_rate * 100.0
        net_return = gross_return - transaction_cost
        benchmark_raw = float(episode.get("benchmark_return_pct", 0.0))
        same_exposure_benchmark = benchmark_raw * exposure
        eligible_with_outcomes = [
            item
            for item in snapshot.members
            if not item.exclusion_reasons
            and by_symbol.get(item.symbol, {}).get("future_return_pct") is not None
        ]
        rank_ic = _spearman(
            [float(item.payload.get("score", 0.0)) for item in eligible_with_outcomes],
            [
                float(by_symbol[item.symbol]["future_return_pct"])
                for item in eligible_with_outcomes
            ],
        )
        evaluated.append(
            {
                "as_of": as_of.isoformat(),
                "status": "matured_research_replay",
                "risk_on": risk_on,
                "mother_pool_count": len(snapshot.members),
                "eligible_count": sum(not item.exclusion_reasons for item in snapshot.members),
                "selected": sorted(selected_symbols),
                "gross_return_pct": gross_return,
                "transaction_cost_pct": transaction_cost,
                "net_return_pct": net_return,
                "same_exposure_hs300_return_pct": same_exposure_benchmark,
                "same_exposure_excess_pct": net_return - same_exposure_benchmark,
                "turnover": turnover,
                "rank_ic": rank_ic,
                "industry_exposure": _selected_exposure(selected, "industry", exposure),
                "market_cap_exposure": _selected_exposure(
                    selected, "market_cap_bucket", exposure
                ),
                "snapshot_fingerprint": snapshot.fingerprint,
                "known_gaps": snapshot.known_gaps,
            }
        )
        previous_selected = selected_symbols
    matured = [item for item in evaluated if item["status"] == "matured_research_replay"]
    returns = [float(item["net_return_pct"]) for item in matured]
    excess = [float(item["same_exposure_excess_pct"]) for item in matured]
    rank_ics = [float(item["rank_ic"]) for item in matured if item["rank_ic"] is not None]
    bootstrap = _block_bootstrap(
        returns,
        simulations=simulations,
        block_size=max(1, int(settings.get("backtest.bootstrap_block_days", 20)) // 5),
        seed=20260717,
    )
    metrics = {
        "episodes": len(matured),
        "unsettled_episodes": len(evaluated) - len(matured),
        "total_return_pct": _compound_return(returns),
        "same_exposure_excess_pct": _compound_return(excess),
        "maximum_drawdown_pct": _maximum_drawdown(returns),
        "average_turnover": _mean([float(item["turnover"]) for item in matured]),
        "mean_rank_ic": _mean(rank_ics),
        "rank_ic_positive_fraction": (
            sum(value > 0 for value in rank_ics) / len(rank_ics) if rank_ics else None
        ),
        "bootstrap_total_return_pct": bootstrap,
    }
    checks = {
        "has_matured_episodes": len(matured) > 0,
        "positive_costed_return": (metrics["total_return_pct"] or 0.0) > 0,
        "positive_same_exposure_excess": (metrics["same_exposure_excess_pct"] or 0.0) > 0,
        "positive_rank_ic": (metrics["mean_rank_ic"] or 0.0) > 0,
        "bootstrap_lower_bound_positive": (
            bootstrap["lower_95_pct"] is not None and bootstrap["lower_95_pct"] > 0
        ),
    }
    passed = all(checks.values())
    output = {
        "status": "research_passed" if passed else "research_failed",
        "evidence_stage": EvidenceStage.RESEARCH_REPLAY.value,
        "protocol": protocol,
        "protocol_hash": hashlib.sha256(
            json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "source": source,
        "source_version": source_version,
        "episodes": evaluated,
        "metrics": metrics,
        "checks": checks,
        "production_admitted": False,
        "claim_boundary": (
            "All results are point-in-time historical research replays. They preserve failures "
            "but cannot be relabelled as independent validation or post-freeze forward evidence."
        ),
    }
    if save:
        output["persistence"] = StrategyEvidenceRepository(
            settings.resolve(settings.get("system.database_path"))
        ).record_research_run(
            protocol_version=A_SHARE_V4_PROTOCOL_VERSION,
            strategy_type="a_share_v4",
            requested_range={
                "start": matured[0]["as_of"] if matured else None,
                "end": matured[-1]["as_of"] if matured else None,
            },
            status=output["status"],
            passed=passed,
            payload=output,
        )
    return output


def _round_trip_cost_rate(settings: Settings) -> float:
    costs = settings.get("costs.stock", {})
    return (
        float(costs.get("commission_rate", 0.00025)) * 2
        + float(costs.get("stamp_duty_rate", 0.0005))
        + float(costs.get("transfer_fee_rate", 0.00001)) * 2
        + float(costs.get("slippage_bps", 10.0)) * 2 / 10_000.0
    )


def _selected_exposure(
    selected: list[Any], field: str, total_exposure: float
) -> dict[str, float]:
    if not selected:
        return {}
    weight = total_exposure / len(selected)
    output: dict[str, float] = {}
    for item in selected:
        key = str(item.payload.get(field) or item.category)
        output[key] = output.get(key, 0.0) + weight
    return output


def _set_turnover(previous: set[str], current: set[str], exposure: float) -> float:
    if not previous and not current:
        return 0.0
    previous_weight = exposure / len(previous) if previous else 0.0
    current_weight = exposure / len(current) if current else 0.0
    symbols = previous | current
    return 0.5 * sum(
        abs((current_weight if symbol in current else 0.0) - (previous_weight if symbol in previous else 0.0))
        for symbol in symbols
    )


def _spearman(scores: list[float], outcomes: list[float]) -> float | None:
    if len(scores) < 3 or len(scores) != len(outcomes):
        return None
    first = _ranks(scores)
    second = _ranks(outcomes)
    first_mean = sum(first) / len(first)
    second_mean = sum(second) / len(second)
    numerator = sum((x - first_mean) * (y - second_mean) for x, y in zip(first, second))
    denominator = math.sqrt(
        sum((x - first_mean) ** 2 for x in first)
        * sum((y - second_mean) ** 2 for y in second)
    )
    return numerator / denominator if denominator else None


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index + 1
        while end < len(order) and values[order[end]] == values[order[index]]:
            end += 1
        rank = (index + 1 + end) / 2.0
        for position in order[index:end]:
            ranks[position] = rank
        index = end
    return ranks


def _block_bootstrap(
    returns: list[float], *, simulations: int, block_size: int, seed: int
) -> dict[str, float | None]:
    if not returns:
        return {"lower_95_pct": None, "median_pct": None, "upper_95_pct": None}
    generator = random.Random(seed)
    results = []
    for _ in range(simulations):
        sample: list[float] = []
        while len(sample) < len(returns):
            start = generator.randrange(len(returns))
            sample.extend(returns[(start + offset) % len(returns)] for offset in range(block_size))
        results.append(_compound_return(sample[: len(returns)]))
    results.sort()
    return {
        "lower_95_pct": _percentile(results, 0.025),
        "median_pct": _percentile(results, 0.50),
        "upper_95_pct": _percentile(results, 0.975),
    }


def _percentile(values: list[float], quantile: float) -> float:
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


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


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _parse_correlations(value: dict[Any, Any]) -> dict[tuple[str, str], float]:
    output: dict[tuple[str, str], float] = {}
    for key, correlation in value.items():
        if isinstance(key, tuple) and len(key) == 2:
            output[(str(key[0]), str(key[1]))] = float(correlation)
        else:
            parts = str(key).split("|")
            if len(parts) == 2:
                output[(parts[0], parts[1])] = float(correlation)
    return output


def _date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _datetime(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


__all__ = ["run_a_share_strategy_lab_v4"]
