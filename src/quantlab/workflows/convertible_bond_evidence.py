from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any

from quantlab.config import Settings
from quantlab.domain.strategy_evidence import (
    EvidenceStage,
    PointInTimeSecurity,
    PointInTimeTradeStatus,
)
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
from quantlab.workflows.point_in_time import (
    CONVERTIBLE_BOND_PROTOCOL_VERSION,
    build_point_in_time_convertible_bond_pool,
    convertible_bond_protocol,
)


def run_convertible_bond_point_in_time_evidence(
    settings: Settings,
    *,
    episodes: list[dict[str, Any]],
    source_version: str,
    selection_count: int = 10,
    save: bool = True,
) -> dict[str, Any]:
    protocol = convertible_bond_protocol()
    evaluated: list[dict[str, Any]] = []
    for episode in sorted(episodes, key=lambda item: str(item["as_of"])):
        as_of = _date(episode["as_of"])
        cutoff_at = _datetime(episode.get("cutoff_at") or f"{as_of}T15:00:00+08:00")
        raw_records = list(episode.get("records", []))
        master = [_master_record(item) for item in raw_records]
        statuses = [_status_record(item, as_of) for item in raw_records]
        snapshot = build_point_in_time_convertible_bond_pool(
            snapshot_date=as_of,
            cutoff_at=cutoff_at,
            master_records=master,
            trade_statuses=statuses,
            source_version=source_version,
            minimum_remaining_balance=float(
                settings.get(
                    "strategies.convertible_bond_pit.minimum_remaining_balance",
                    100_000_000.0,
                )
            ),
            minimum_amount=float(
                settings.get("strategies.convertible_bond_pit.minimum_amount", 10_000_000.0)
            ),
            stage=EvidenceStage.RESEARCH_REPLAY,
        )
        by_symbol = {str(item["symbol"]): item for item in raw_records}
        full_candidates = [
            item
            for item in snapshot.members
            if item.eligible
            and by_symbol[item.symbol].get("price") is not None
            and by_symbol[item.symbol].get("premium_pct") is not None
        ]
        simple_candidates = [
            item
            for item in raw_records
            if bool(item.get("trade_status", False))
            and not bool(item.get("suspended", False))
            and _date(item.get("listing_date", as_of)) <= as_of
            and (
                item.get("delisting_date") is None
                or _date(item["delisting_date"]) > as_of
            )
            and item.get("price") is not None
            and item.get("premium_pct") is not None
        ]
        full_selected = sorted(
            full_candidates,
            key=lambda item: (
                float(by_symbol[item.symbol]["price"])
                + float(by_symbol[item.symbol]["premium_pct"]),
                item.symbol,
            ),
        )[:selection_count]
        simple_selected = sorted(
            simple_candidates,
            key=lambda item: (
                float(item["price"]) + float(item["premium_pct"]),
                str(item["symbol"]),
            ),
        )[:selection_count]
        missing_full = [
            item.symbol
            for item in full_selected
            if by_symbol[item.symbol].get("future_return_pct") is None
        ]
        missing_simple = [
            str(item["symbol"])
            for item in simple_selected
            if item.get("future_return_pct") is None
        ]
        if missing_full or missing_simple:
            evaluated.append(
                {
                    "as_of": as_of.isoformat(),
                    "status": "unsettled",
                    "missing_outcomes": sorted(set(missing_full + missing_simple)),
                    "snapshot_fingerprint": snapshot.fingerprint,
                }
            )
            continue
        cost_model = _cost_model(settings)
        cost_pct = float(cost_model["round_trip_pct"])
        full_return = _mean(
            [float(by_symbol[item.symbol]["future_return_pct"]) for item in full_selected]
        )
        simple_return = _mean(
            [float(item["future_return_pct"]) for item in simple_selected]
        )
        evaluated.append(
            {
                "as_of": as_of.isoformat(),
                "status": "matured_research_replay",
                "point_in_time_pool_count": len(snapshot.members),
                "eligible_count": len(full_candidates),
                "full_selected": [item.symbol for item in full_selected],
                "simple_double_low_selected": [str(item["symbol"]) for item in simple_selected],
                "full_net_return_pct": (full_return or 0.0) - cost_pct,
                "simple_double_low_net_return_pct": (simple_return or 0.0) - cost_pct,
                "incremental_return_pct": (full_return or 0.0) - (simple_return or 0.0),
                "cost_model": cost_model,
                "snapshot_fingerprint": snapshot.fingerprint,
                "known_gaps": snapshot.known_gaps,
            }
        )
    matured = [item for item in evaluated if item["status"] == "matured_research_replay"]
    full_returns = [float(item["full_net_return_pct"]) for item in matured]
    simple_returns = [float(item["simple_double_low_net_return_pct"]) for item in matured]
    walk_forward = _walk_forward_blocks(matured)
    checks = {
        "point_in_time_episodes_available": bool(matured),
        "positive_full_return": (_compound_return(full_returns) or 0.0) > 0,
        "beats_simple_double_low": (
            (_compound_return(full_returns) or 0.0)
            > (_compound_return(simple_returns) or 0.0)
        ),
        "all_walk_forward_blocks_positive": bool(walk_forward)
        and all((item["full_return_pct"] or 0.0) > 0 for item in walk_forward),
    }
    passed = all(checks.values())
    output = {
        "status": "research_passed_shadow_only" if passed else "research_failed",
        "evidence_stage": EvidenceStage.RESEARCH_REPLAY.value,
        "protocol": protocol,
        "protocol_hash": hashlib.sha256(
            json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "episodes": evaluated,
        "walk_forward": walk_forward,
        "metrics": {
            "matured_episodes": len(matured),
            "full_total_return_pct": _compound_return(full_returns),
            "simple_double_low_total_return_pct": _compound_return(simple_returns),
            "incremental_total_return_pct": _compound_return(
                [float(item["incremental_return_pct"]) for item in matured]
            ),
            "full_maximum_drawdown_pct": _maximum_drawdown(full_returns),
        },
        "checks": checks,
        "cost_model": _cost_model(settings),
        "production_budget": 0.0,
        "production_admitted": False,
        "forward_shadow_required": True,
        "claim_boundary": (
            "Historical walk-forward results remain research evidence. Production budget stays "
            "at zero until the separately frozen forward shadow account matures."
        ),
    }
    if save:
        output["persistence"] = StrategyEvidenceRepository(
            settings.resolve(settings.get("system.database_path"))
        ).record_research_run(
            protocol_version=CONVERTIBLE_BOND_PROTOCOL_VERSION,
            strategy_type="convertible_bond_point_in_time",
            requested_range={
                "start": matured[0]["as_of"] if matured else None,
                "end": matured[-1]["as_of"] if matured else None,
            },
            status=output["status"],
            passed=passed,
            payload=output,
        )
    return output


def _master_record(item: dict[str, Any]) -> PointInTimeSecurity:
    return PointInTimeSecurity(
        symbol=str(item["symbol"]),
        name=str(item.get("name") or item["symbol"]),
        security_type="convertible_bond",
        exchange=str(item.get("exchange") or str(item["symbol"])[:2]),
        listing_date=_date(item["listing_date"]),
        delisting_date=_date(item["delisting_date"]) if item.get("delisting_date") else None,
        asset_class="convertible_bond",
        category=str(item.get("category") or "convertible_bond"),
        source=str(item.get("source") or "unknown"),
        source_version=str(item.get("source_version") or "unknown"),
        available_at=_datetime(item["master_available_at"]),
        payload={"underlying_symbol": item.get("underlying_symbol")},
    )


def _status_record(item: dict[str, Any], as_of: date) -> PointInTimeTradeStatus:
    return PointInTimeTradeStatus(
        symbol=str(item["symbol"]),
        trade_date=as_of,
        trade_status=bool(item.get("trade_status", False)),
        suspended=bool(item.get("suspended", False)),
        amount=item.get("amount"),
        remaining_balance=item.get("remaining_balance"),
        redeem_status=item.get("redeem_status"),
        rating=item.get("rating"),
        source=str(item.get("status_source") or item.get("source") or "unknown"),
        methodology=str(item.get("status_methodology") or "point_in_time_daily"),
        available_at=_datetime(item["status_available_at"]),
        payload={"rating_change": item.get("rating_change")},
    )


def _walk_forward_blocks(items: list[dict[str, Any]], blocks: int = 3) -> list[dict[str, Any]]:
    if not items:
        return []
    block_size = max(1, len(items) // blocks)
    output = []
    for index in range(0, len(items), block_size):
        values = items[index : index + block_size]
        output.append(
            {
                "start": values[0]["as_of"],
                "end": values[-1]["as_of"],
                "episodes": len(values),
                "full_return_pct": _compound_return(
                    [float(item["full_net_return_pct"]) for item in values]
                ),
                "simple_return_pct": _compound_return(
                    [float(item["simple_double_low_net_return_pct"]) for item in values]
                ),
            }
        )
    return output


def _cost_pct(settings: Settings) -> float:
    return float(_cost_model(settings)["round_trip_pct"])


def _cost_model(settings: Settings) -> dict[str, Any]:
    costs = settings.get("costs.convertible_bond", {})
    commission_rate = float(costs.get("commission_rate", 0.0001))
    slippage_bps = float(costs.get("slippage_bps", 10.0))
    round_trip_pct = (
        commission_rate * 2
        + slippage_bps * 2 / 10_000.0
    ) * 100.0
    return {
        "source": "costs.convertible_bond",
        "methodology": "round-trip commission rate plus two-sided slippage estimate",
        "commission_rate": commission_rate,
        "minimum_commission": float(costs.get("minimum_commission", 0.0)),
        "stamp_duty_rate": float(costs.get("stamp_duty_rate", 0.0)),
        "transfer_fee_rate": float(costs.get("transfer_fee_rate", 0.0)),
        "slippage_bps": slippage_bps,
        "trade_lot": int(costs.get("trade_lot", 10)),
        "round_trip_pct": round(round_trip_pct, 8),
        "claim_boundary": (
            "This is a configurable research cost estimate, not a broker-confirmed fill cost."
        ),
    }


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


def _date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _datetime(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


__all__ = ["run_convertible_bond_point_in_time_evidence"]
