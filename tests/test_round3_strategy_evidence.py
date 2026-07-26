from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from quantlab.domain.strategy_evidence import (
    ABLATION_VARIANTS,
    PointInTimeSecurity,
    PointInTimeTradeStatus,
    VariantPrediction,
)
from quantlab.persistence import strategy_evidence as strategy_evidence_module
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
from quantlab.portfolio.smoothing import BudgetSmoothingPolicy, plan_smoothed_rebalance
from quantlab.workflows.point_in_time import (
    build_a_share_v4_candidates,
    build_point_in_time_convertible_bond_pool,
    build_point_in_time_etf_pool,
)
from quantlab.workflows.convertible_bond_evidence import (
    run_convertible_bond_point_in_time_evidence,
)
from quantlab.workflows.etf_point_in_time import run_point_in_time_etf_replay
from quantlab.workflows.stock_strategy_lab_v4 import run_a_share_strategy_lab_v4


def _security(
    symbol: str,
    *,
    security_type: str = "etf",
    category: str = "broad_equity",
    asset_class: str = "equity",
) -> PointInTimeSecurity:
    return PointInTimeSecurity(
        symbol=symbol,
        name=symbol,
        security_type=security_type,
        exchange=symbol[:2],
        listing_date=date(2020, 1, 1),
        asset_class=asset_class,
        category=category,
        source="licensed_source",
        source_version="v1",
        available_at=datetime(2020, 1, 2, tzinfo=UTC),
    )


def _status(
    symbol: str,
    *,
    trade_date: date,
    amount: float = 100_000_000,
    fund_size: float = 1_000_000_000,
    available_at: datetime | None = None,
    remaining_balance: float | None = None,
    redeem_status: str | None = None,
    rating: str | None = None,
) -> PointInTimeTradeStatus:
    return PointInTimeTradeStatus(
        symbol=symbol,
        trade_date=trade_date,
        trade_status=True,
        amount=amount,
        fund_size=fund_size,
        remaining_balance=remaining_balance,
        redeem_status=redeem_status,
        rating=rating,
        source="licensed_source",
        methodology="official_daily_status",
        available_at=available_at
        or datetime.combine(trade_date, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=10),
    )


def test_etf_pool_is_point_in_time_and_representative_switches():
    first = _security("sh510001")
    second = _security("sh510002")
    day = date(2026, 7, 17)
    cutoff = datetime(2026, 7, 17, 12, tzinfo=UTC)
    future = _status(
        "sh510003",
        trade_date=day,
        available_at=datetime(2026, 7, 17, 13, tzinfo=UTC),
    )
    snapshot = build_point_in_time_etf_pool(
        snapshot_date=day,
        cutoff_at=cutoff,
        master_records=[first, second, _security("sh510003")],
        trade_statuses=[
            _status("sh510001", trade_date=day, amount=100, fund_size=100),
            _status("sh510002", trade_date=day, amount=200, fund_size=200),
            future,
        ],
        source_version="daily-v1",
    )
    by_symbol = {item.symbol: item for item in snapshot.members}
    assert by_symbol["sh510002"].representative is True
    assert by_symbol["sh510003"].eligible is False
    assert "daily_trade_status" in by_symbol["sh510003"].missing_fields

    next_day = day + timedelta(days=1)
    switched = build_point_in_time_etf_pool(
        snapshot_date=next_day,
        cutoff_at=cutoff + timedelta(days=1),
        master_records=[first, second],
        trade_statuses=[
            _status("sh510001", trade_date=next_day, amount=1_000, fund_size=1_000),
            _status("sh510002", trade_date=next_day, amount=100, fund_size=100),
        ],
        source_version="daily-v1",
    )
    assert next(item for item in switched.members if item.symbol == "sh510001").representative
    assert snapshot.fingerprint != switched.fingerprint


def test_etf_pool_rejects_mixed_methodologies():
    day = date(2026, 7, 17)
    statuses = [_status("sh510001", trade_date=day), _status("sh510001", trade_date=day)]
    statuses[1].methodology = "vendor_estimate"
    with pytest.raises(ValueError, match="multiple point-in-time sources"):
        build_point_in_time_etf_pool(
            snapshot_date=day,
            cutoff_at=datetime(2026, 7, 17, 12, tzinfo=UTC),
            master_records=[_security("sh510001")],
            trade_statuses=statuses,
            source_version="v1",
        )


def test_a_share_v4_controls_exposure_correlation_and_risk_off():
    cutoff = datetime(2026, 7, 17, 10, tzinfo=UTC)
    records = []
    for index in range(20):
        records.append(
            {
                "symbol": f"sh60{index:04d}",
                "name": f"stock-{index}",
                "listing_date": "2020-01-01",
                "trade_status": True,
                "suspended": False,
                "is_st": False,
                "average_amount": 100_000_000,
                "industry": "bank" if index < 8 else f"industry-{index % 6}",
                "market_cap_bucket": "large" if index < 10 else "mid",
                "market_cap": 10_000_000_000,
                "score": 100 - index,
                "available_at": cutoff.isoformat(),
            }
        )
    correlations = {(records[0]["symbol"], records[1]["symbol"]): 0.95}
    snapshot = build_a_share_v4_candidates(
        snapshot_date=cutoff.date(),
        cutoff_at=cutoff,
        records=records,
        correlations=correlations,
        risk_on=True,
        source="pit_market",
        source_version="v1",
    )
    selected = [item for item in snapshot.members if item.representative]
    assert len(selected) <= 8
    assert sum(item.category == "bank" for item in selected) <= 2
    assert not ({records[0]["symbol"], records[1]["symbol"]} <= {item.symbol for item in selected})

    risk_off = build_a_share_v4_candidates(
        snapshot_date=cutoff.date(),
        cutoff_at=cutoff,
        records=records,
        risk_on=False,
        source="pit_market",
        source_version="v1",
    )
    assert not any(item.representative for item in risk_off.members)
    assert all(item.payload["target_total_exposure"] == 0 for item in risk_off.members)


def test_convertible_bond_missing_evidence_keeps_budget_zero():
    day = date(2026, 7, 17)
    snapshot = build_point_in_time_convertible_bond_pool(
        snapshot_date=day,
        cutoff_at=datetime(2026, 7, 17, 12, tzinfo=UTC),
        master_records=[
            _security(
                "sh110001",
                security_type="convertible_bond",
                category="convertible_bond",
                asset_class="convertible_bond",
            )
        ],
        trade_statuses=[_status("sh110001", trade_date=day)],
        source_version="v1",
    )
    member = snapshot.members[0]
    assert member.eligible is False
    assert {"remaining_balance", "redeem_status", "rating"} <= set(member.missing_fields)
    assert member.payload["production_budget"] == 0


def _predictions() -> list[VariantPrediction]:
    return [
        VariantPrediction(
            variant=variant,
            probabilities={"up": 0.6, "flat": 0.2, "down": 0.2},
            action="buy",
            target_weight=0.1,
            actually_triggered=True,
            data_completeness=1.0,
            role_completeness=1.0,
        )
        for variant in ABLATION_VARIANTS
    ]


def test_forward_ablation_cannot_backfill_and_needs_30_real_maturities(tmp_path):
    repository = StrategyEvidenceRepository(tmp_path / "evidence.db")
    frozen = datetime.now(UTC) - timedelta(days=1)
    cohort = repository.create_forward_cohort(
        protocol_version="v1",
        protocol_hash="a" * 64,
        frozen_at=frozen,
    )
    with pytest.raises(ValueError, match="cannot be backfilled"):
        repository.register_forward_sample(
            cohort_id=cohort["cohort_id"],
            sample_key="historical",
            symbol="sh510300",
            as_of=frozen.date() - timedelta(days=1),
            due_at=frozen + timedelta(days=5),
            horizon_days=5,
            predictions=_predictions(),
            context_fingerprint="b" * 64,
            start_price=4.0,
            quote_source="test",
            quote_provider="test",
            quote_version="v1",
            quote_fingerprint="c" * 64,
            strategy_version="s1",
            prompt_version="p1",
            governance_version="g1",
        )
    due = frozen + timedelta(days=5)
    repository.register_forward_sample(
        cohort_id=cohort["cohort_id"],
        sample_key="live-sample",
        symbol="sh510300",
        as_of=frozen.date(),
        due_at=due,
        horizon_days=5,
        predictions=_predictions(),
        context_fingerprint="d" * 64,
        start_price=4.0,
        quote_source="test",
        quote_provider="test",
        quote_version="v1",
        quote_fingerprint="e" * 64,
        strategy_version="s1",
        prompt_version="p1",
        governance_version="g1",
    )
    with pytest.raises(ValueError, match="real due time"):
        repository.settle_forward_sample(
            cohort_id=cohort["cohort_id"],
            sample_key="live-sample",
            horizon_days=5,
            realized_return_pct=2.0,
            outcome_source="licensed_close",
            observed_at=frozen,
        )
    scorecard = repository.forward_scorecard(
        cohort_id=cohort["cohort_id"], horizon_days=5
    )
    assert scorecard["variants"]["full_system"]["matured_samples"] == 0
    assert scorecard["variants"]["full_system"]["stage"] == "forward_shadow"


def test_forward_scorecard_uses_trade_days_for_claims_and_keeps_abstentions(
    tmp_path, monkeypatch
):
    repository = StrategyEvidenceRepository(tmp_path / "evidence.db")
    registered_at = datetime(2026, 7, 20, 9, tzinfo=UTC)

    class FrozenClock(datetime):
        current = registered_at

        @classmethod
        def now(cls, tz=None):
            value = cls.current
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(strategy_evidence_module, "datetime", FrozenClock)
    frozen = registered_at - timedelta(days=1)
    cohort = repository.create_forward_cohort(
        protocol_version="trade-day-v1",
        protocol_hash="f" * 64,
        frozen_at=frozen,
    )
    due = registered_at + timedelta(days=1)
    common = {
        "cohort_id": cohort["cohort_id"],
        "as_of": frozen.date(),
        "due_at": due,
        "horizon_days": 5,
        "context_fingerprint": "a" * 64,
        "start_price": 10.0,
        "quote_source": "test",
        "quote_provider": "test",
        "quote_version": "v1",
        "quote_fingerprint": "b" * 64,
        "strategy_version": "s1",
        "prompt_version": "p1",
        "governance_version": "g1",
    }
    repository.register_forward_sample(
        sample_key="same-day-1",
        symbol="sh510300",
        predictions=_predictions(),
        **common,
    )
    abstaining = [
        item.model_copy(
            update={"actually_triggered": item.variant.value != "full_system"}
        )
        for item in _predictions()
    ]
    repository.register_forward_sample(
        sample_key="same-day-2",
        symbol="sh510500",
        predictions=abstaining,
        **common,
    )
    FrozenClock.current = due + timedelta(days=1)
    repository.settle_forward_sample(
        cohort_id=cohort["cohort_id"],
        sample_key="same-day-1",
        horizon_days=5,
        realized_return_pct=10.0,
        outcome_source="licensed_close",
        observed_at=FrozenClock.current,
    )
    repository.settle_forward_sample(
        cohort_id=cohort["cohort_id"],
        sample_key="same-day-2",
        horizon_days=5,
        realized_return_pct=-10.0,
        outcome_source="licensed_close",
        observed_at=FrozenClock.current,
    )

    scorecard = repository.forward_scorecard(
        cohort_id=cohort["cohort_id"], horizon_days=5
    )
    full = scorecard["variants"]["full_system"]
    assert scorecard["coverage"]["registered_samples"] == 2
    assert scorecard["coverage"]["settled_samples"] == 2
    assert full["matured_samples"] == 2
    assert full["independent_trade_days"] == 1
    assert full["stage"] == "forward_shadow"
    assert full["trigger_rate"] == pytest.approx(0.5)
    assert full["abstain_rate"] == pytest.approx(0.5)
    assert full["paired_trade_day_comparisons"]["quant_only"]["status"] == "insufficient"


def test_dynamic_budget_smoothing_respects_cash_lots_and_degradation():
    plan = plan_smoothed_rebalance(
        nav=100_000,
        available_cash=80_000,
        current_quantities={"sh510300": 1_000},
        desired_weights={"sh510300": 0.0, "sh510500": 0.8},
        prices={"sh510300": 20.0, "sh510500": 10.0},
        sellable_quantities={"sh510300": 500},
        policy=BudgetSmoothingPolicy(lot_size=100),
        evidence_degraded=True,
    )
    assert plan["cash_reserve_satisfied"] is True
    assert all(order["quantity"] % 100 == 0 for order in plan["orders"])
    assert not any(order["side"] == "buy" for order in plan["orders"])
    assert plan["target_quantities"]["sh510300"] >= 500


def test_point_in_time_master_and_status_are_immutable_and_queryable(tmp_path):
    repository = StrategyEvidenceRepository(tmp_path / "pit.db")
    security = _security("sh510300")
    status = _status("sh510300", trade_date=date(2026, 7, 17))
    assert repository.save_security_master(master_version="v1", records=[security]) == 1
    assert repository.save_trade_status(security_type="etf", records=[status]) == 1
    master = repository.security_master(
        security_type="etf",
        master_version="v1",
        cutoff_at=datetime(2026, 7, 17, 12, tzinfo=UTC),
    )
    statuses = repository.trade_statuses(
        security_type="etf",
        trade_date=date(2026, 7, 17),
        cutoff_at=datetime(2026, 7, 17, 12, tzinfo=UTC),
    )
    assert [item.symbol for item in master] == ["sh510300"]
    assert [item.symbol for item in statuses] == ["sh510300"]
    changed = security.model_copy(update={"name": "changed"})
    with pytest.raises(ValueError, match="immutable"):
        repository.save_security_master(master_version="v1", records=[changed])


def test_point_in_time_strategy_replays_preserve_research_boundary(settings):
    cutoff = datetime(2026, 7, 17, 7, tzinfo=UTC)
    stock_records = [
        {
            "symbol": f"sh60{index:04d}",
            "listing_date": "2020-01-01",
            "trade_status": True,
            "average_amount": 100_000_000,
            "industry": f"industry-{index % 5}",
            "market_cap_bucket": ["large", "mid", "small"][index % 3],
            "market_cap": 10_000_000_000,
            "score": 20 - index,
            "future_return_pct": 5 - index * 0.2,
            "available_at": cutoff.isoformat(),
        }
        for index in range(20)
    ]
    stock = run_a_share_strategy_lab_v4(
        settings,
        episodes=[
            {
                "as_of": "2026-07-17",
                "cutoff_at": cutoff.isoformat(),
                "records": stock_records,
                "risk_on": True,
                "benchmark_return_pct": 1.0,
            }
        ],
        source="licensed_pit",
        source_version="v1",
        bootstrap_simulations=100,
    )
    assert stock["evidence_stage"] == "research_replay"
    assert stock["metrics"]["mean_rank_ic"] > 0
    assert stock["production_admitted"] is False

    bond_records = [
        {
            "symbol": f"sh11000{index}",
            "listing_date": "2024-01-01",
            "master_available_at": "2024-01-02T00:00:00+00:00",
            "status_available_at": cutoff.isoformat(),
            "trade_status": True,
            "amount": 50_000_000,
            "remaining_balance": 500_000_000,
            "redeem_status": "none",
            "rating": "AA",
            "price": 100 + index,
            "premium_pct": 10 + index,
            "future_return_pct": 2 - index * 0.2,
            "source": "licensed_pit",
            "source_version": "v1",
        }
        for index in range(3)
    ]
    settings.values.setdefault("costs", {})["convertible_bond"] = {
        "commission_rate": 0.001,
        "slippage_bps": 20.0,
        "trade_lot": 10,
    }
    bond = run_convertible_bond_point_in_time_evidence(
        settings,
        episodes=[
            {
                "as_of": "2026-07-17",
                "cutoff_at": cutoff.isoformat(),
                "records": bond_records,
            }
        ],
        source_version="v1",
    )
    assert bond["production_budget"] == 0
    assert bond["forward_shadow_required"] is True
    assert bond["cost_model"]["source"] == "costs.convertible_bond"
    assert bond["cost_model"]["round_trip_pct"] == pytest.approx(0.6)

    etf_snapshot = build_point_in_time_etf_pool(
        snapshot_date=date(2026, 7, 17),
        cutoff_at=cutoff,
        master_records=[
            _security("sh510300", category="large_equity"),
            _security("sh518880", category="gold", asset_class="gold"),
        ],
        trade_statuses=[
            _status("sh510300", trade_date=date(2026, 7, 17), available_at=cutoff),
            _status("sh518880", trade_date=date(2026, 7, 17), available_at=cutoff),
        ],
        source_version="v1",
    )
    etf = run_point_in_time_etf_replay(
        settings,
        episodes=[
            {
                "as_of": "2026-07-17",
                "snapshot": etf_snapshot.model_dump(mode="json"),
                "signal_scores": {"sh510300": 1.0, "sh518880": 0.5},
                "forward_returns": {"sh510300": 2.0, "sh518880": 1.0},
                "benchmark_return_pct": 1.0,
            }
        ],
        top_k=2,
    )
    assert etf["used_today_frozen_universe"] is False
    assert etf["episodes"][0]["snapshot_fingerprint"] == etf_snapshot.fingerprint


def test_a_share_v4_handles_large_daily_candidate_batch():
    cutoff = datetime(2026, 7, 17, 7, tzinfo=UTC)
    records = [
        {
            "symbol": f"sh{600000 + index:06d}",
            "listing_date": "2010-01-01",
            "trade_status": True,
            "average_amount": 100_000_000 + index,
            "industry": f"industry-{index % 40}",
            "market_cap_bucket": ["large", "mid", "small"][index % 3],
            "market_cap": 10_000_000_000,
            "score": float(5_000 - index),
            "available_at": cutoff.isoformat(),
        }
        for index in range(5_000)
    ]
    snapshot = build_a_share_v4_candidates(
        snapshot_date=date(2026, 7, 17),
        cutoff_at=cutoff,
        records=records,
        risk_on=True,
        source="large_pit_fixture",
        source_version="v1",
    )
    assert len(snapshot.members) == 5_000
    assert len([item for item in snapshot.members if item.representative]) == 8
