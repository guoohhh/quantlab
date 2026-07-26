from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from typing import Any

from quantlab.config import Settings
from quantlab.domain.strategy_evidence import (
    EvidenceStage,
    PointInTimePoolMember,
    PointInTimePoolSnapshot,
    PointInTimeSecurity,
    PointInTimeTradeStatus,
)
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository


ROUND3_PROTOCOL_VERSION = "round3-pit-forward-v1"
A_SHARE_V4_PROTOCOL_VERSION = "a-share-v4-pit-topk-v1"
CONVERTIBLE_BOND_PROTOCOL_VERSION = "convertible-bond-pit-shadow-v1"


def round3_protocol() -> dict[str, Any]:
    return {
        "version": ROUND3_PROTOCOL_VERSION,
        "frozen_at": "2026-07-17T00:00:00+08:00",
        "forward_start": "2026-07-17",
        "minimum_matured_samples": 30,
        "horizons": [5, 20],
        "ablation_variants": [
            "simple_baseline",
            "quant_only",
            "raw_llm",
            "statistical_model",
            "llm_stat_fusion",
            "llm_trade_gate",
            "full_system",
        ],
        "historical_runs_are_forward_evidence": False,
        "etf_representative_rule": (
            "within each asset category, rank eligible funds by log1p(amount)+log1p(size); "
            "break exact ties by symbol"
        ),
        "a_share_v4": a_share_v4_protocol(),
        "convertible_bond": convertible_bond_protocol(),
    }


def a_share_v4_protocol() -> dict[str, Any]:
    return {
        "version": A_SHARE_V4_PROTOCOL_VERSION,
        "stage": "research_replay_until_forward_samples_mature",
        "known_seen_periods": ["2018-2022", "2023-2025", "2026-01-01/2026-07-16"],
        "independent_holdout_claim_allowed": False,
        "top_k": 8,
        "minimum_average_amount": 50_000_000.0,
        "maximum_industry_fraction": 0.25,
        "maximum_market_cap_bucket_fraction": 0.40,
        "maximum_pair_correlation": 0.85,
        "risk_on_exposure": 0.60,
        "risk_off_exposure": 0.0,
        "risk_off_asset": "cash",
        "benchmark": "sh000300_same_exposure",
        "metrics": [
            "rank_ic",
            "net_return",
            "maximum_drawdown",
            "turnover",
            "block_bootstrap",
        ],
        "failed_runs_are_append_only": True,
    }


def convertible_bond_protocol() -> dict[str, Any]:
    return {
        "version": CONVERTIBLE_BOND_PROTOCOL_VERSION,
        "stage": "forward_shadow",
        "production_budget": 0.0,
        "required_point_in_time_fields": [
            "listing_date",
            "maturity_or_delisting_date",
            "trade_status",
            "remaining_balance",
            "redeem_status",
            "rating",
            "liquidity",
        ],
        "benchmark": "simple_double_low",
        "validation": "walk_forward_then_real_forward_shadow",
    }


def protocol_hash(payload: dict[str, Any] | None = None) -> str:
    value = payload or round3_protocol()
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def register_round3_protocol(settings: Settings) -> dict[str, Any]:
    repository = StrategyEvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    payload = round3_protocol()
    return repository.register_protocol(
        protocol_version=ROUND3_PROTOCOL_VERSION,
        protocol_type="point_in_time_and_forward_evidence",
        payload=payload,
        frozen_at=datetime.fromisoformat(payload["frozen_at"]),
    )


def build_point_in_time_etf_pool(
    *,
    snapshot_date: date,
    cutoff_at: datetime,
    master_records: list[PointInTimeSecurity],
    trade_statuses: list[PointInTimeTradeStatus],
    source_version: str,
    minimum_amount: float = 0.0,
    minimum_fund_size: float = 0.0,
    stage: EvidenceStage = EvidenceStage.RESEARCH_REPLAY,
    created_at: datetime | None = None,
) -> PointInTimePoolSnapshot:
    cutoff = _aware(cutoff_at)
    status_map = _unique_status_map(trade_statuses, cutoff, snapshot_date)
    members: list[PointInTimePoolMember] = []
    known_gaps: set[str] = set()
    for security in sorted(master_records, key=lambda item: item.symbol):
        if security.security_type != "etf":
            continue
        missing: list[str] = []
        reasons: list[str] = []
        if _aware(security.available_at) > cutoff:
            continue
        if security.listing_date > snapshot_date:
            reasons.append("not_yet_listed")
        if security.delisting_date and security.delisting_date <= snapshot_date:
            reasons.append("delisted")
        status = status_map.get(security.symbol)
        if status is None:
            reasons.append("daily_trade_status_unavailable")
            missing.append("daily_trade_status")
        else:
            if not status.trade_status or status.suspended:
                reasons.append("not_tradeable")
            if status.amount is None:
                missing.append("amount")
            elif status.amount < minimum_amount:
                reasons.append("insufficient_liquidity")
            if status.fund_size is None:
                missing.append("fund_size")
            elif status.fund_size < minimum_fund_size:
                reasons.append("insufficient_fund_size")
            if security.asset_class in {"overseas_equity", "overseas_asset"}:
                if status.premium_discount_pct is None:
                    missing.append("premium_discount_pct")
                if status.overseas_market_date is None:
                    missing.append("overseas_market_date")
        if missing:
            known_gaps.update(f"{security.symbol}:{field}" for field in missing)
        amount = status.amount if status else None
        fund_size = status.fund_size if status else None
        liquidity_score = _liquidity_score(amount, fund_size)
        members.append(
            PointInTimePoolMember(
                symbol=security.symbol,
                name=security.name or security.symbol,
                asset_class=security.asset_class,
                category=security.category,
                eligible=not reasons,
                exclusion_reasons=reasons,
                amount=amount,
                fund_size=fund_size,
                liquidity_score=liquidity_score,
                premium_discount_pct=status.premium_discount_pct if status else None,
                overseas_market_date=status.overseas_market_date if status else None,
                source=f"{security.source}|{status.source if status else 'unavailable'}",
                available_at=max(
                    _aware(security.available_at),
                    _aware(status.available_at) if status else _aware(security.available_at),
                ),
                data_quality="degraded" if missing else "available",
                missing_fields=missing,
                payload={
                    "listing_date": security.listing_date.isoformat(),
                    "delisting_date": security.delisting_date.isoformat()
                    if security.delisting_date
                    else None,
                    "status_methodology": status.methodology if status else None,
                    "overseas_time_lag_days": (
                        (snapshot_date - status.overseas_market_date).days
                        if status and status.overseas_market_date
                        else None
                    ),
                },
            )
        )
    _mark_category_representatives(members)
    sources = sorted({item.source for item in members})
    return PointInTimePoolSnapshot(
        snapshot_type="etf",
        snapshot_date=snapshot_date,
        cutoff_at=cutoff,
        protocol_version=ROUND3_PROTOCOL_VERSION,
        source=";".join(sources) if sources else "unavailable",
        source_version=source_version,
        stage=stage,
        members=members,
        known_gaps=sorted(known_gaps),
        created_at=_aware(created_at or datetime.now(UTC)),
    )


def build_a_share_v4_candidates(
    *,
    snapshot_date: date,
    cutoff_at: datetime,
    records: list[dict[str, Any]],
    correlations: dict[tuple[str, str], float] | None = None,
    risk_on: bool,
    source: str,
    source_version: str,
    stage: EvidenceStage = EvidenceStage.RESEARCH_REPLAY,
    policy: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> PointInTimePoolSnapshot:
    policy = policy or a_share_v4_protocol()
    cutoff = _aware(cutoff_at)
    top_k = int(policy["top_k"])
    max_industry = max(1, math.floor(top_k * float(policy["maximum_industry_fraction"])))
    max_cap_bucket = max(
        1, math.floor(top_k * float(policy["maximum_market_cap_bucket_fraction"]))
    )
    maximum_correlation = float(policy["maximum_pair_correlation"])
    minimum_amount = float(policy["minimum_average_amount"])
    normalized: list[dict[str, Any]] = []
    known_gaps: set[str] = set()
    for raw in records:
        item = dict(raw)
        available_at = _aware(_datetime(item.get("available_at")))
        if available_at > cutoff:
            continue
        reasons: list[str] = []
        missing: list[str] = []
        listing_date = _date(item.get("listing_date"))
        delisting_date = _date(item.get("delisting_date"))
        if listing_date is None:
            reasons.append("listing_date_unavailable")
            missing.append("listing_date")
        elif listing_date > snapshot_date:
            reasons.append("not_yet_listed")
        if delisting_date and delisting_date <= snapshot_date:
            reasons.append("delisted")
        if not bool(item.get("trade_status", False)) or bool(item.get("suspended", False)):
            reasons.append("not_tradeable")
        if bool(item.get("is_st", False)):
            reasons.append("historical_st")
        amount = _float(item.get("average_amount"))
        if amount is None:
            reasons.append("liquidity_unavailable")
            missing.append("average_amount")
        elif amount < minimum_amount:
            reasons.append("insufficient_liquidity")
        industry = str(item.get("industry") or "unknown")
        cap_bucket = str(item.get("market_cap_bucket") or "unknown")
        if industry == "unknown":
            missing.append("industry")
        if cap_bucket == "unknown":
            missing.append("market_cap_bucket")
        code_history = item.get("code_history") or []
        canonical_symbol = str(item.get("canonical_symbol") or item["symbol"])
        score = float(item.get("score", 0.0))
        if missing:
            known_gaps.update(f"{canonical_symbol}:{field}" for field in missing)
        normalized.append(
            {
                **item,
                "symbol": canonical_symbol,
                "source_symbol": item["symbol"],
                "available_at": available_at,
                "reasons": reasons,
                "missing": missing,
                "industry": industry,
                "market_cap_bucket": cap_bucket,
                "average_amount": amount,
                "score": score,
                "code_history": code_history,
            }
        )
    selected: list[str] = []
    industry_counts: Counter[str] = Counter()
    cap_counts: Counter[str] = Counter()
    correlations = correlations or {}
    if risk_on:
        for item in sorted(normalized, key=lambda value: (-value["score"], value["symbol"])):
            if item["reasons"]:
                continue
            if industry_counts[item["industry"]] >= max_industry:
                item["reasons"].append("industry_exposure_limit")
                continue
            if cap_counts[item["market_cap_bucket"]] >= max_cap_bucket:
                item["reasons"].append("market_cap_exposure_limit")
                continue
            if any(
                abs(_correlation(correlations, item["symbol"], other)) > maximum_correlation
                for other in selected
            ):
                item["reasons"].append("correlation_limit")
                continue
            selected.append(item["symbol"])
            industry_counts[item["industry"]] += 1
            cap_counts[item["market_cap_bucket"]] += 1
            if len(selected) >= top_k:
                break
    members = [
        PointInTimePoolMember(
            symbol=item["symbol"],
            name=str(item.get("name") or item["symbol"]),
            asset_class="a_share_equity",
            category=item["industry"],
            eligible=not item["reasons"],
            exclusion_reasons=item["reasons"],
            representative=item["symbol"] in selected,
            representative_rank=(selected.index(item["symbol"]) + 1)
            if item["symbol"] in selected
            else None,
            amount=item["average_amount"],
            fund_size=_float(item.get("market_cap")),
            liquidity_score=_liquidity_score(item["average_amount"], item.get("market_cap")),
            source=source,
            available_at=item["available_at"],
            data_quality="degraded" if item["missing"] else "available",
            missing_fields=item["missing"],
            payload={
                "score": item["score"],
                "industry": item["industry"],
                "market_cap_bucket": item["market_cap_bucket"],
                "source_symbol": item["source_symbol"],
                "code_history": item["code_history"],
                "risk_on": risk_on,
                "target_total_exposure": float(policy["risk_on_exposure"])
                if risk_on
                else float(policy["risk_off_exposure"]),
            },
        )
        for item in sorted(normalized, key=lambda value: value["symbol"])
    ]
    if not risk_on:
        known_gaps.add("risk_off:cash_selected_by_protocol")
    return PointInTimePoolSnapshot(
        snapshot_type="a_share",
        snapshot_date=snapshot_date,
        cutoff_at=cutoff,
        protocol_version=A_SHARE_V4_PROTOCOL_VERSION,
        source=source,
        source_version=source_version,
        stage=stage,
        members=members,
        known_gaps=sorted(known_gaps),
        created_at=_aware(created_at or datetime.now(UTC)),
    )


def build_point_in_time_convertible_bond_pool(
    *,
    snapshot_date: date,
    cutoff_at: datetime,
    master_records: list[PointInTimeSecurity],
    trade_statuses: list[PointInTimeTradeStatus],
    source_version: str,
    minimum_remaining_balance: float = 100_000_000.0,
    minimum_amount: float = 10_000_000.0,
    stage: EvidenceStage = EvidenceStage.RESEARCH_REPLAY,
    created_at: datetime | None = None,
) -> PointInTimePoolSnapshot:
    cutoff = _aware(cutoff_at)
    status_map = _unique_status_map(trade_statuses, cutoff, snapshot_date)
    members: list[PointInTimePoolMember] = []
    known_gaps: set[str] = set()
    for security in sorted(master_records, key=lambda item: item.symbol):
        if security.security_type != "convertible_bond":
            continue
        reasons: list[str] = []
        missing: list[str] = []
        if _aware(security.available_at) > cutoff:
            continue
        if security.listing_date > snapshot_date:
            reasons.append("not_yet_listed")
        if security.delisting_date and security.delisting_date <= snapshot_date:
            reasons.append("matured_or_delisted")
        status = status_map.get(security.symbol)
        if status is None:
            reasons.append("daily_trade_status_unavailable")
            missing.append("daily_trade_status")
        else:
            if not status.trade_status or status.suspended:
                reasons.append("not_tradeable")
            if status.remaining_balance is None:
                reasons.append("remaining_balance_unavailable")
                missing.append("remaining_balance")
            elif status.remaining_balance < minimum_remaining_balance:
                reasons.append("insufficient_remaining_balance")
            if status.redeem_status is None:
                reasons.append("redeem_status_unavailable")
                missing.append("redeem_status")
            elif status.redeem_status.lower() in {
                "announced",
                "triggered",
                "redeeming",
                "forced_redemption",
            }:
                reasons.append("forced_redemption_risk")
            if status.rating is None:
                reasons.append("rating_unavailable")
                missing.append("rating")
            if status.amount is None:
                reasons.append("liquidity_unavailable")
                missing.append("amount")
            elif status.amount < minimum_amount:
                reasons.append("insufficient_liquidity")
        if missing:
            known_gaps.update(f"{security.symbol}:{field}" for field in missing)
        members.append(
            PointInTimePoolMember(
                symbol=security.symbol,
                name=security.name or security.symbol,
                asset_class="convertible_bond",
                category=security.category,
                eligible=not reasons,
                exclusion_reasons=reasons,
                amount=status.amount if status else None,
                fund_size=status.remaining_balance if status else None,
                liquidity_score=_liquidity_score(
                    status.amount if status else None,
                    status.remaining_balance if status else None,
                ),
                source=f"{security.source}|{status.source if status else 'unavailable'}",
                available_at=max(
                    _aware(security.available_at),
                    _aware(status.available_at) if status else _aware(security.available_at),
                ),
                data_quality="degraded" if missing else "available",
                missing_fields=missing,
                payload={
                    "remaining_balance": status.remaining_balance if status else None,
                    "redeem_status": status.redeem_status if status else None,
                    "rating": status.rating if status else None,
                    "production_budget": 0.0,
                },
            )
        )
    return PointInTimePoolSnapshot(
        snapshot_type="convertible_bond",
        snapshot_date=snapshot_date,
        cutoff_at=cutoff,
        protocol_version=CONVERTIBLE_BOND_PROTOCOL_VERSION,
        source=";".join(sorted({item.source for item in members})) or "unavailable",
        source_version=source_version,
        stage=stage,
        members=members,
        known_gaps=sorted(known_gaps),
        created_at=_aware(created_at or datetime.now(UTC)),
    )


def persist_point_in_time_pool(
    settings: Settings, snapshot: PointInTimePoolSnapshot
) -> dict[str, Any]:
    return StrategyEvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    ).save_pool_snapshot(snapshot)


def _mark_category_representatives(members: list[PointInTimePoolMember]) -> None:
    grouped: dict[str, list[PointInTimePoolMember]] = defaultdict(list)
    for member in members:
        if member.eligible:
            grouped[member.category].append(member)
    for items in grouped.values():
        ranked = sorted(
            items,
            key=lambda item: (-(item.liquidity_score or 0.0), item.symbol),
        )
        for rank, member in enumerate(ranked, start=1):
            member.representative_rank = rank
            member.representative = rank == 1


def _unique_status_map(
    statuses: list[PointInTimeTradeStatus], cutoff: datetime, snapshot_date: date
) -> dict[str, PointInTimeTradeStatus]:
    output: dict[str, PointInTimeTradeStatus] = {}
    claims: dict[str, tuple[str, str]] = {}
    for status in statuses:
        if status.trade_date != snapshot_date or _aware(status.available_at) > cutoff:
            continue
        claim = (status.source, status.methodology)
        previous = claims.get(status.symbol)
        if previous is not None and previous != claim:
            raise ValueError(
                "multiple point-in-time sources or methodologies require an explicit isolated pool"
            )
        claims[status.symbol] = claim
        output[status.symbol] = status
    return output


def _liquidity_score(amount: Any, size: Any) -> float | None:
    amount_value = _float(amount)
    size_value = _float(size)
    if amount_value is None and size_value is None:
        return None
    return math.log1p(amount_value or 0.0) + math.log1p(size_value or 0.0)


def _correlation(values: dict[tuple[str, str], float], first: str, second: str) -> float:
    return float(values.get((first, second), values.get((second, first), 0.0)))


def _date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value)[:10])


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if value is None:
        raise ValueError("available_at is required for point-in-time evidence")
    return datetime.fromisoformat(str(value))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("point-in-time timestamps must include a timezone")
    return value.astimezone(UTC)


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    result = float(value)
    return result if math.isfinite(result) else None


__all__ = [
    "A_SHARE_V4_PROTOCOL_VERSION",
    "CONVERTIBLE_BOND_PROTOCOL_VERSION",
    "ROUND3_PROTOCOL_VERSION",
    "a_share_v4_protocol",
    "build_a_share_v4_candidates",
    "build_point_in_time_convertible_bond_pool",
    "build_point_in_time_etf_pool",
    "convertible_bond_protocol",
    "persist_point_in_time_pool",
    "protocol_hash",
    "register_round3_protocol",
    "round3_protocol",
]
