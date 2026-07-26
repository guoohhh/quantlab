from __future__ import annotations

import json
import sqlite3
import importlib
from datetime import UTC, date, datetime, timedelta

import httpx

from quantlab.api.app import app
from quantlab.config import Settings
from quantlab.domain import AnalysisContextPack, AssetType
from quantlab.domain.data_governance import DataNamespace, DataTrustLevel
from quantlab.domain.strategy_evidence import (
    ABLATION_VARIANTS,
    EvidenceStage,
    PointInTimePoolMember,
    PointInTimePoolSnapshot,
    VariantPrediction,
)
from quantlab.market import TradingCalendarService
from quantlab.persistence.migrations import initialize_or_upgrade_database
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
from quantlab.persistence.wide_research import EVIDENCE_BOUNDARY, WideResearchRepository
from quantlab.runtime.scheduler import DEFAULT_SCHEDULES, RuntimeScheduler
from quantlab.runtime.worker import default_job_handlers
from quantlab.workflows import wide_forward as wide_forward_module
from quantlab.workflows.wide_forward import (
    WIDE_PROTOCOL_VERSION,
    mark_wide_research_portfolios,
    preregister_late_start_wide_experiment,
    preregister_wide_forward_experiment,
    register_wide_forward_batch,
    select_wide_forward_sample,
)


api_module = importlib.import_module("quantlab.api.app")


def _settings(tmp_path) -> Settings:
    return Settings(
        values={
            "system": {
                "database_path": "quantlab.db",
                "data_dir": "data",
                "timezone": "Asia/Shanghai",
                "test_mode": True,
            },
            "llm": {"maximum_committee_roles": 6},
            "risk": {"max_single_position": 0.15},
            "costs": {
                "stock": {
                    "commission_rate": 0.00025,
                    "transfer_fee_rate": 0.00001,
                    "stamp_duty_rate": 0.0005,
                    "slippage_bps": 10.0,
                }
            },
            "strategies": {
                "wide_forward": {
                    "target_sample_size": 24,
                    "minimum_sample_size": 20,
                    "minimum_industries": 6,
                    "initial_research_nav": 100.0,
                    "maximum_llm_calls_per_batch": 240,
                    "maximum_llm_tokens_per_batch": 3_000_000,
                    "maximum_llm_cost_usd_per_batch": 40.0,
                    "reserved_tokens_per_call": 8_000,
                }
            },
        },
        root=tmp_path,
    )


def _members(day: date, count: int = 60) -> list[dict]:
    available = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=8)
    output = []
    for index in range(count):
        trend_value = (2.5, 0.0, -2.5)[index % 3]
        cap = (8e9, 8e10, 8e11)[index % 3] + index * 1e6
        symbol = f"sh6{index:05d}"
        raw_fingerprint = f"raw-{index // 10}"
        observations = {
            field: {
                "source": "trusted_fixture",
                "available_at": available.isoformat(),
                "market_date": day.isoformat(),
                "raw_response_fingerprint": raw_fingerprint,
                "missing_reason": None,
            }
            for field in (
                "price",
                "previous_close",
                "daily_return_pct",
                "amount",
                "turnover_rate",
                "market_cap",
            )
        }
        output.append(
            PointInTimePoolMember(
                symbol=symbol,
                name=f"样本{index}",
                asset_class="equity",
                category=f"行业{index % 10}",
                eligible=True,
                amount=80_000_000 + index * 1_000_000,
                fund_size=cap,
                liquidity_score=80_000_000 + index * 1_000_000,
                source="trusted_fixture",
                available_at=available,
                data_quality="available",
                payload={
                    "industry": f"行业{index % 10}",
                    "turnover_rate": 0.5 + index % 8,
                    "latest_price": 10.0 + index,
                    "previous_close": (10.0 + index) / (1 + trend_value / 100),
                    "daily_return_pct": trend_value,
                    "market_cap": cap,
                    "field_observations": observations,
                },
            ).model_dump(mode="json")
        )
    return output


def _snapshot(day: date) -> PointInTimePoolSnapshot:
    members = [PointInTimePoolMember.model_validate(item) for item in _members(day)]
    return PointInTimePoolSnapshot(
        snapshot_type="a_share",
        snapshot_date=day,
        cutoff_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=9),
        protocol_version="trusted-wide-fixture-v1",
        source="trusted_fixture",
        source_version="v1",
        stage=EvidenceStage.FORWARD_SHADOW,
        members=members,
        created_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=9),
        namespace=DataNamespace.PRODUCTION,
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        manifest_id="manifest-wide-fixture",
    )


def _fake_context(*_args, symbol: str, as_of: date, **_kwargs):
    return AnalysisContextPack(
        symbol=symbol,
        asset_type=AssetType.STOCK,
        as_of=as_of,
        cutoff_at=datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=9),
        blocks=[],
        deterministic_summary={"market_regime": "range"},
    ).model_dump(mode="json")


def _fake_predictions(*_args, cohort_id: str, symbol: str, horizon_days: int, **_kwargs):
    return [
        {
            "prediction_id": f"{symbol}-{horizon_days}-{variant.value}",
            "cohort_id": cohort_id,
            "sample_key": f"sample-{symbol}-{horizon_days}",
            "symbol": symbol,
            "horizon_days": horizon_days,
            "variant": variant.value,
            "probabilities": {"up": 0.5, "flat": 0.3, "down": 0.2},
            "action": "buy",
            "target_weight": 0.1,
            "actually_triggered": True,
            "data_completeness": 1.0,
            "role_completeness": 1.0,
            "context_fingerprint": f"context-{symbol}",
            "quote_fingerprint": f"quote-{symbol}",
            "prompt_version": "prompt-v1",
            "governance_version": "governance-v1",
            "prediction_fingerprint": f"prediction-{symbol}-{horizon_days}-{variant.value}",
            "payload": {
                "raw_llm_provider": "fixture",
                "raw_llm_model": "fixture-model",
                "statistical_model_id": "stat-v1",
            },
        }
        for variant in ABLATION_VARIANTS
    ]


def _request(method: str, path: str) -> httpx.Response:
    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path)

    import asyncio

    return asyncio.run(call())


def test_wide_selection_is_deterministic_and_multi_axis():
    selected = select_wide_forward_sample(
        _members(date(2026, 7, 22)),
        target_sample_size=24,
        minimum_sample_size=20,
        minimum_industries=6,
        seed="frozen-seed",
    )
    repeated = select_wide_forward_sample(
        list(reversed(_members(date(2026, 7, 22)))),
        target_sample_size=24,
        minimum_sample_size=20,
        minimum_industries=6,
        seed="frozen-seed",
    )
    assert selected["selected_stocks"] == 24
    assert [item["symbol"] for item in selected["members"]] == [
        item["symbol"] for item in repeated["members"]
    ]
    assert set(selected["strata"]["trend"]) == {"strong", "neutral", "weak"}
    assert set(selected["strata"]["market_cap"]) == {"large", "mid", "small"}
    assert len(selected["strata"]["industries"]) >= 6
    assert selected["manual_selection"] is False


def test_preregistered_wide_experiment_isolated_from_primary_tables(tmp_path):
    settings = _settings(tmp_path)
    initialize_or_upgrade_database(settings.resolve("quantlab.db"))
    result = preregister_wide_forward_experiment(
        settings,
        frozen_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
        signal_start_date=date(2026, 7, 22),
    )
    assert result["experiment"]["evidence_boundary"] == EVIDENCE_BOUNDARY
    assert result["experiment"]["status"] == "preregistered"
    assert len(result["research_portfolios"]) == 7
    repeated = preregister_wide_forward_experiment(
        settings,
        frozen_at=datetime(2026, 7, 21, 13, tzinfo=UTC),
        signal_start_date=date(2026, 7, 23),
    )
    assert repeated["experiment"]["experiment_id"] == result["experiment"]["experiment_id"]
    assert repeated["activation"]["idempotent"] is True
    with sqlite3.connect(settings.resolve("quantlab.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM shadow_accounts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM forward_registration_samples").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM research_portfolios").fetchone()[0] == 7


def test_late_start_experiment_is_distinct_and_does_not_hijack_strict_defaults(tmp_path):
    settings = _settings(tmp_path)
    initialize_or_upgrade_database(settings.resolve("quantlab.db"))
    strict = preregister_wide_forward_experiment(
        settings,
        frozen_at=datetime(2026, 7, 21, 7, tzinfo=UTC),
        signal_start_date=date(2026, 7, 22),
    )["experiment"]
    observed = datetime(2026, 7, 21, 13, tzinfo=UTC)
    TradingCalendarService.from_settings(settings).ingest(
        [{"trade_date": "2026-07-21", "is_open": True}],
        namespace=DataNamespace.PRODUCTION,
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        provider="fixture",
        source="fixture",
        endpoint="fixture",
        source_version="fixture-v1",
        available_at=observed - timedelta(minutes=1),
        license_status="fixture",
        raw_fingerprint="a" * 64,
    )
    late = preregister_late_start_wide_experiment(
        settings,
        trade_date=date(2026, 7, 21),
        frozen_at=observed,
    )["experiment"]
    assert late["experiment_id"] != strict["experiment_id"]
    assert late["payload"]["evidence_grade"] == "research_only_late_start_forward"
    assert late["payload"]["formal_primary_scorecard_eligible"] is False

    strategy = StrategyEvidenceRepository(settings.resolve("quantlab.db"))
    strategy.save_pool_snapshot(_snapshot(date(2026, 7, 21)))
    freezer_calls = 0

    def late_freezer(*args, **kwargs):
        nonlocal freezer_calls
        freezer_calls += 1
        return _fake_predictions(*args, **kwargs)

    batch = register_wide_forward_batch(
        settings,
        trade_date=date(2026, 7, 21),
        schedule_run_id="operator-late-start-20260721",
        experiment_id=late["experiment_id"],
        registration_origin="wide_forward_late_start_research",
        registration_started_at=datetime(2026, 7, 21, 14, tzinfo=UTC),
        prediction_freezer=late_freezer,
        context_builder=_fake_context,
        committee_runner=lambda *_args, **_kwargs: {
            "action": "buy",
            "confidence": 0.7,
            "suggested_weight_max": 0.1,
        },
    )
    repeated = register_wide_forward_batch(
        settings,
        trade_date=date(2026, 7, 21),
        schedule_run_id="operator-late-start-20260721",
        experiment_id=late["experiment_id"],
        registration_origin="wide_forward_late_start_research",
        registration_started_at=datetime(2026, 7, 21, 14, tzinfo=UTC),
        prediction_freezer=late_freezer,
        context_builder=_fake_context,
        committee_runner=lambda *_args, **_kwargs: {
            "action": "buy",
            "confidence": 0.7,
            "suggested_weight_max": 0.1,
        },
    )
    repository = WideResearchRepository(settings.resolve("quantlab.db"))
    assert batch["status"] == "completed"
    assert repeated["batch_id"] == batch["batch_id"]
    assert freezer_calls == 24 * 2
    assert batch["experiment_id"] == late["experiment_id"]
    assert repository.experiment_by_protocol_version(WIDE_PROTOCOL_VERSION)[
        "experiment_id"
    ] == strict["experiment_id"]
    with sqlite3.connect(settings.resolve("quantlab.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM shadow_accounts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM forward_registration_samples").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM research_portfolios").fetchone()[0] == 14
    scorecard = repository.scorecard(late["experiment_id"], 5)
    assert scorecard["coverage"]["registered_samples"] == 24
    assert scorecard["coverage"]["settled_samples"] == 0
    assert scorecard["batches"]["total"] == 1
    assert scorecard["variants"]["full_system"]["claim_status"] == (
        "research_only_collecting_evidence"
    )


def test_wide_registration_is_noop_while_an_existing_batch_is_running(tmp_path):
    settings = _settings(tmp_path)
    initialize_or_upgrade_database(settings.resolve("quantlab.db"))
    experiment = preregister_wide_forward_experiment(
        settings,
        frozen_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
        signal_start_date=date(2026, 7, 22),
    )["experiment"]
    strategy = StrategyEvidenceRepository(settings.resolve("quantlab.db"))
    strategy.save_pool_snapshot(_snapshot(date(2026, 7, 22)))
    snapshot = strategy.latest_pool_snapshot(
        "a_share",
        date(2026, 7, 22),
        namespace=DataNamespace.PRODUCTION,
        minimum_trust=DataTrustLevel.SERVER_OBSERVED,
    )
    assert snapshot is not None
    started = datetime(2026, 7, 22, 10, tzinfo=UTC)
    repository = WideResearchRepository(settings.resolve("quantlab.db"))
    existing = repository.begin_batch(
        experiment=experiment,
        trade_date=date(2026, 7, 22),
        snapshot=snapshot,
        schedule_run_id="wide-running-1",
        started_at=started,
    )
    assert existing["_newly_created"] is True

    result = register_wide_forward_batch(
        settings,
        trade_date=date(2026, 7, 22),
        schedule_run_id="wide-running-2",
        registration_started_at=started,
    )

    assert result["batch_id"] == existing["batch_id"]
    assert result["status"] == "running"
    assert result["idempotent"] is True
    assert result["in_progress"] is True
    assert result["reason"] == "wide_forward_batch_already_running"


def test_default_wide_marking_includes_late_start_experiments(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    initialize_or_upgrade_database(settings.resolve("quantlab.db"))
    repository = WideResearchRepository(settings.resolve("quantlab.db"))
    strict = repository.create_experiment(
        protocol_version=WIDE_PROTOCOL_VERSION,
        cohort_id="strict-cohort",
        target_sample_size=24,
        minimum_sample_size=20,
        signal_start_date=date(2026, 7, 22),
        frozen_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
        payload={"protocol": "strict"},
    )
    late = repository.create_experiment(
        protocol_version="wide-forward-late-start-2026-07-22-v2",
        cohort_id="late-cohort",
        target_sample_size=24,
        minimum_sample_size=20,
        signal_start_date=date(2026, 7, 22),
        frozen_at=datetime(2026, 7, 22, 10, tzinfo=UTC),
        payload={"protocol": "late"},
    )
    calls: list[str] = []

    def mark_one(_settings, *, repository, experiment, bar_service):
        assert repository.path == settings.resolve("quantlab.db")
        assert bar_service is marker_service
        calls.append(experiment["experiment_id"])
        return {
            "experiment_id": experiment["experiment_id"],
            "inserted_positions": 2,
            "portfolios": 7,
        }

    marker_service = object()
    monkeypatch.setattr(
        "quantlab.workflows.wide_forward._mark_wide_research_portfolio", mark_one
    )
    result = mark_wide_research_portfolios(settings, bar_service=marker_service)

    assert calls == [late["experiment_id"], strict["experiment_id"]]
    assert result["inserted_positions"] == 4
    assert result["portfolios"] == 14
    assert {item["experiment_id"] for item in result["experiments"]} == {
        strict["experiment_id"],
        late["experiment_id"],
    }


def test_wide_usage_counts_context_committee_roles(tmp_path):
    settings = _settings(tmp_path)
    initialize_or_upgrade_database(settings.resolve("quantlab.db"))
    started = datetime(2026, 7, 22, 10, tzinfo=UTC)
    cohort_id = "wide-cohort"
    rows = [
        ("forward:wide-cohort:sample", 10, 1, 0.10),
        ("raw-forward:wide-cohort:sample", 20, 2, 0.20),
        ("context-committee:wide-committee:wide-cohort:symbol:2026-07-22", 30, 3, 0.30),
        ("context-committee:wide-committee:other-cohort:symbol:2026-07-22", 40, 4, 0.40),
    ]
    with sqlite3.connect(settings.resolve("quantlab.db")) as db:
        for index, (task_id, input_tokens, output_tokens, cost) in enumerate(rows):
            db.execute(
                """INSERT INTO llm_governed_calls(
                       call_id,task_id,idempotency_key,cache_key,role,schema_name,
                       status,input_tokens,output_tokens,estimated_cost_usd,latency_ms,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"call-{index}",
                    task_id,
                    f"idempotency-{index}",
                    f"cache-{index}",
                    "fixture",
                    "Fixture",
                    "ok",
                    input_tokens,
                    output_tokens,
                    cost,
                    1.0,
                    (started + timedelta(seconds=index)).isoformat(),
                ),
            )

    usage = wide_forward_module._llm_usage(
        settings.resolve("quantlab.db"), cohort_id, started
    )

    assert usage == {
        "calls": 3,
        "input_tokens": 60,
        "output_tokens": 6,
        "cost_usd": 0.6,
        "latency_ms": 3.0,
    }


def test_wide_usage_reconciliation_preserves_terminal_batch_identity(tmp_path):
    settings = _settings(tmp_path)
    initialize_or_upgrade_database(settings.resolve("quantlab.db"))
    repository = WideResearchRepository(settings.resolve("quantlab.db"))
    experiment = repository.create_experiment(
        protocol_version="wide-usage-reconciliation-v1",
        cohort_id="usage-cohort",
        target_sample_size=24,
        minimum_sample_size=20,
        signal_start_date=date(2026, 7, 22),
        frozen_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
        payload={"protocol": "usage"},
    )
    batch = repository.begin_batch(
        experiment=experiment,
        trade_date=date(2026, 7, 22),
        snapshot={
            "snapshot_id": "usage-snapshot",
            "snapshot_date": "2026-07-22",
            "cutoff_at": "2026-07-22T09:00:00+00:00",
            "fingerprint": "f" * 64,
            "manifest_id": "usage-manifest",
        },
        schedule_run_id="usage-schedule",
        started_at=datetime(2026, 7, 22, 10, tzinfo=UTC),
    )
    completed = repository.finish_batch(
        batch["batch_id"],
        status="completed",
        member_count=24,
        prediction_count=336,
        llm_usage={},
        role_completeness=0.7,
        payload={"selection": {"manual_selection": False}},
    )

    reconciled = repository.reconcile_batch_usage(
        batch["batch_id"],
        llm_usage={
            "calls": 196,
            "input_tokens": 1_000,
            "output_tokens": 200,
            "cost_usd": 2.05,
            "latency_ms": 321.0,
        },
    )

    assert reconciled["status"] == "completed"
    assert reconciled["registration_completed_at"] == completed["registration_completed_at"]
    assert reconciled["member_count"] == 24
    assert reconciled["prediction_count"] == 336
    assert reconciled["llm_calls"] == 196
    assert reconciled["llm_cost_usd"] == 2.05
    reconciliation = reconciled["payload"]["llm_usage_reconciliation"]
    assert reconciliation["previous"]["calls"] == 0
    assert reconciliation["reason"] == "include_all_wide_committee_governed_calls"


def test_wide_portfolio_mark_is_not_blocked_by_same_day_registration():
    schedule = next(
        item
        for item in DEFAULT_SCHEDULES
        if item["name"] == "wide_research_portfolio_mark"
    )

    assert schedule["dependencies"] == ["prediction_settlement"]


def test_wide_batch_creates_paired_predictions_once_and_is_idempotent(tmp_path):
    settings = _settings(tmp_path)
    initialize_or_upgrade_database(settings.resolve("quantlab.db"))
    preregister_wide_forward_experiment(
        settings,
        frozen_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
        signal_start_date=date(2026, 7, 22),
    )
    strategy = StrategyEvidenceRepository(settings.resolve("quantlab.db"))
    strategy.save_pool_snapshot(_snapshot(date(2026, 7, 22)))
    counters = {"committee": 0, "freeze": 0}
    sample_identities = set()

    def committee(*_args, **_kwargs):
        counters["committee"] += 1
        return {"action": "buy", "confidence": 0.7, "suggested_weight_max": 0.1}

    def freezer(*args, **kwargs):
        counters["freeze"] += 1
        sample_identities.add(kwargs["sample_key_context_fingerprint"])
        return _fake_predictions(*args, **kwargs)

    first = register_wide_forward_batch(
        settings,
        trade_date=date(2026, 7, 22),
        schedule_run_id="schedule-wide-1",
        registration_started_at=datetime(2026, 7, 22, 10, tzinfo=UTC),
        prediction_freezer=freezer,
        context_builder=_fake_context,
        committee_runner=committee,
    )
    second = register_wide_forward_batch(
        settings,
        trade_date=date(2026, 7, 22),
        schedule_run_id="schedule-wide-1",
        registration_started_at=datetime(2026, 7, 22, 10, tzinfo=UTC),
        prediction_freezer=freezer,
        context_builder=_fake_context,
        committee_runner=committee,
    )
    assert first["status"] == "completed"
    assert first["member_count"] == 24
    assert first["prediction_count"] == 24 * 2 * 7
    assert len(first["prediction_links"]) == 24 * 2 * 7
    assert counters == {"committee": 24, "freeze": 48}
    assert len(sample_identities) == 1
    assert second["batch_id"] == first["batch_id"]
    assert counters == {"committee": 24, "freeze": 48}


def test_due_settlement_ignores_unlinked_wide_attempts(tmp_path):
    settings = _settings(tmp_path)
    initialize_or_upgrade_database(settings.resolve("quantlab.db"))
    strategy = StrategyEvidenceRepository(settings.resolve("quantlab.db"))
    wide = WideResearchRepository(settings.resolve("quantlab.db"))
    now = datetime.now(UTC)
    frozen_at = now - timedelta(minutes=2)
    cohort = strategy.create_forward_cohort(
        protocol_version="wide-link-boundary-v1",
        protocol_hash="f" * 64,
        frozen_at=frozen_at,
    )
    experiment = wide.create_experiment(
        protocol_version="wide-link-boundary-v1",
        cohort_id=cohort["cohort_id"],
        target_sample_size=24,
        minimum_sample_size=20,
        signal_start_date=now.date(),
        frozen_at=frozen_at,
        payload={"test": "wide-link-boundary"},
    )
    batch = wide.begin_batch(
        experiment=experiment,
        trade_date=now.date(),
        snapshot={
            "snapshot_date": now.date().isoformat(),
            "cutoff_at": (now - timedelta(minutes=1)).isoformat(),
            "snapshot_id": "snapshot-linked-boundary",
            "fingerprint": "a" * 64,
            "manifest_id": "manifest-linked-boundary",
        },
        schedule_run_id="schedule-linked-boundary",
        started_at=now,
    )
    wide.save_members(
        batch["batch_id"],
        [
            {
                "symbol": "sh600000",
                "selection_rank": 1,
                "industry": "银行",
                "market_cap_bucket": "large",
                "trend_bucket": "neutral",
                "price_change_state": "flat",
                "style_bucket": "large_defensive",
                "quant_score": 0.0,
                "quant_direction": "neutral",
                "observed_at": now.isoformat(),
                "source": "fixture",
                "source_fingerprint": "c" * 64,
                "missing_reasons": [],
                "payload": {},
            }
        ],
    )
    variants = [
        VariantPrediction(
            variant=variant,
            probabilities={"up": 0.5, "flat": 0.3, "down": 0.2},
            action="buy",
            target_weight=0.1,
            actually_triggered=True,
            data_completeness=1.0,
            role_completeness=1.0,
        )
        for variant in ABLATION_VARIANTS
    ]

    def register(sample_key: str):
        return strategy.register_forward_sample(
            cohort_id=cohort["cohort_id"],
            sample_key=sample_key,
            symbol="sh600000",
            as_of=now.date(),
            due_at=now + timedelta(days=1),
            horizon_days=5,
            predictions=variants,
            context_fingerprint=sample_key.ljust(64, "0")[:64],
            start_price=10.0,
            quote_source="fixture",
            quote_provider="fixture",
            quote_version="v1",
            quote_fingerprint="b" * 64,
            strategy_version="s1",
            prompt_version="p1",
            governance_version="g1",
            registration_origin="wide_forward_late_start_research",
        )

    linked = register("linked")
    register("unlinked")
    wide.link_predictions(
        batch_id=batch["batch_id"],
        symbol="sh600000",
        horizon_days=5,
        predictions=linked,
    )
    due = strategy.due_forward_samples(as_of=now + timedelta(days=2))
    assert [item["sample_key"] for item in due] == ["linked"]


def test_wide_registration_rejects_historical_date_and_future_snapshot(tmp_path):
    settings = _settings(tmp_path)
    initialize_or_upgrade_database(settings.resolve("quantlab.db"))
    preregister_wide_forward_experiment(
        settings,
        frozen_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
        signal_start_date=date(2026, 7, 22),
    )
    repository = WideResearchRepository(settings.resolve("quantlab.db"))
    experiment = repository.latest_experiment()
    assert experiment is not None
    snapshot = {
        "snapshot_id": "snapshot-1",
        "snapshot_date": "2026-07-21",
        "cutoff_at": "2026-07-21T10:00:00+00:00",
        "fingerprint": "snapshot-fingerprint",
        "manifest_id": "manifest-1",
    }
    import pytest

    with pytest.raises(ValueError, match="cannot predate"):
        repository.begin_batch(
            experiment=experiment,
            trade_date=date(2026, 7, 21),
            snapshot=snapshot,
            schedule_run_id="schedule-1",
            started_at=datetime(2026, 7, 21, 11, tzinfo=UTC),
        )
    snapshot["snapshot_date"] = "2026-07-22"
    snapshot["cutoff_at"] = "2026-07-22T12:00:00+00:00"
    with pytest.raises(ValueError, match="not knowable"):
        repository.begin_batch(
            experiment=experiment,
            trade_date=date(2026, 7, 22),
            snapshot=snapshot,
            schedule_run_id="schedule-2",
            started_at=datetime(2026, 7, 22, 11, tzinfo=UTC),
        )


def test_wide_registration_rejects_field_observed_after_snapshot_cutoff(tmp_path):
    settings = _settings(tmp_path)
    initialize_or_upgrade_database(settings.resolve("quantlab.db"))
    preregister_wide_forward_experiment(
        settings,
        frozen_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
        signal_start_date=date(2026, 7, 22),
    )
    snapshot = _snapshot(date(2026, 7, 22))
    snapshot.members[0].payload["field_observations"]["price"]["available_at"] = (
        "2026-07-22T09:30:00+00:00"
    )
    strategy = StrategyEvidenceRepository(settings.resolve("quantlab.db"))
    strategy.save_pool_snapshot(snapshot)
    import pytest

    with pytest.raises(ValueError, match="point-in-time field identity"):
        register_wide_forward_batch(
            settings,
            trade_date=date(2026, 7, 22),
            schedule_run_id="schedule-future-field",
            registration_started_at=datetime(2026, 7, 22, 10, tzinfo=UTC),
            prediction_freezer=_fake_predictions,
            context_builder=_fake_context,
            committee_runner=lambda *_args, **_kwargs: {
                "action": "buy",
                "confidence": 0.7,
                "suggested_weight_max": 0.1,
            },
        )


def test_fractional_portfolio_uses_t_plus_one_return_and_never_short(tmp_path):
    settings = _settings(tmp_path)
    initialize_or_upgrade_database(settings.resolve("quantlab.db"))
    result = preregister_wide_forward_experiment(
        settings,
        frozen_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
        signal_start_date=date(2026, 7, 22),
    )
    experiment = result["experiment"]
    repository = WideResearchRepository(settings.resolve("quantlab.db"))
    batch = repository.begin_batch(
        experiment=experiment,
        trade_date=date(2026, 7, 22),
        snapshot={
            "snapshot_id": "snapshot-wide",
            "snapshot_date": "2026-07-22",
            "cutoff_at": "2026-07-22T09:00:00+00:00",
            "fingerprint": "snapshot-wide-fingerprint",
            "manifest_id": "manifest-wide",
        },
        schedule_run_id="schedule-wide",
        started_at=datetime(2026, 7, 22, 10, tzinfo=UTC),
    )
    member = _members(date(2026, 7, 22), 1)[0]
    repository.save_members(
        batch["batch_id"],
        [
            {
                "symbol": member["symbol"],
                "selection_rank": 1,
                "industry": "行业0",
                "market_cap_bucket": "small",
                "trend_bucket": "weak",
                "price_change_state": "down",
                "style_bucket": "contrarian",
                "quant_score": -0.5,
                "quant_direction": "down",
                "observed_at": member["available_at"],
                "source": "trusted_fixture",
                "source_fingerprint": "source-fingerprint",
                "missing_reasons": [],
                "payload": member["payload"],
            }
        ],
    )
    predictions = _fake_predictions(
        cohort_id=experiment["cohort_id"], symbol=member["symbol"], horizon_days=5
    )
    with repository.connect() as db:
        for prediction in predictions:
            triggered = prediction["variant"] != "full_system"
            db.execute(
                """INSERT INTO forward_ablation_predictions(
                       prediction_id,cohort_id,sample_key,symbol,as_of,registered_at,due_at,
                       horizon_days,variant,probabilities,action,target_weight,
                       actually_triggered,data_completeness,role_completeness,
                       context_fingerprint,start_price,quote_source,quote_provider,
                       quote_version,quote_fingerprint,strategy_version,prompt_version,
                       governance_version,prediction_fingerprint,payload,evidence_stage,
                       created_at,registration_origin
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                            'forward_shadow',?,?)""",
                (
                    prediction["prediction_id"],
                    experiment["cohort_id"],
                    prediction["sample_key"],
                    member["symbol"],
                    "2026-07-22",
                    "2026-07-22T10:00:00+00:00",
                    "2026-07-29T10:00:00+00:00",
                    5,
                    prediction["variant"],
                    json.dumps(prediction["probabilities"]),
                    "avoid" if not triggered else "buy",
                    0.1 if triggered else 0.0,
                    int(triggered),
                    1.0,
                    1.0,
                    prediction["context_fingerprint"],
                    10.0,
                    "fixture",
                    "fixture",
                    "v1",
                    prediction["quote_fingerprint"],
                    "strategy-v1",
                    prediction["prompt_version"],
                    prediction["governance_version"],
                    prediction["prediction_fingerprint"],
                    json.dumps(prediction["payload"]),
                    "2026-07-22T10:00:00+00:00",
                    "wide_forward_research",
                ),
            )
            db.execute(
                """INSERT INTO forward_ablation_outcomes(
                       prediction_id,realized_direction,realized_return_pct,
                       portfolio_return_pct,turnover,drawdown,transaction_cost_pct,
                       outcome_source,observed_at,payload,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    prediction["prediction_id"],
                    "down",
                    -3.0,
                    -0.3 if triggered else 0.0,
                    0.1 if triggered else 0.0,
                    -0.3 if triggered else 0.0,
                    0.0,
                    "fixture",
                    "2026-07-29T11:00:00+00:00",
                    "{}",
                    "2026-07-29T11:00:00+00:00",
                ),
            )
    repository.link_predictions(
        batch_id=batch["batch_id"],
        symbol=member["symbol"],
        horizon_days=5,
        predictions=predictions,
    )
    repository.finish_batch(
        batch["batch_id"],
        status="completed",
        member_count=1,
        prediction_count=7,
        llm_usage={},
        role_completeness=1.0,
        payload={},
    )
    marked = repository.mark_settled_positions(
        experiment_id=experiment["experiment_id"],
        benchmark_returns={("2026-07-22", 5): 1.0},
        standardized_returns={
            (member["symbol"], "2026-07-22", 5): {
                "execution_date": "2026-07-23",
                "entry_price": 11.0,
                "exit_price": 9.9,
                "realized_return_pct": -10.0,
                "entry_fingerprint": "entry",
                "outcome_fingerprint": "outcome",
            }
        },
    )
    assert marked["inserted_positions"] == 7
    portfolios = repository.portfolios(experiment["experiment_id"])
    full = next(item for item in portfolios if item["variant"] == "full_system")
    detail = repository.portfolio(full["portfolio_id"])
    assert detail is not None
    position = detail["positions"][0]
    assert position["execution_date"] == "2026-07-23"
    assert position["weight"] == 1.0
    assert position["triggered"] == 0
    assert position["gross_return_pct"] == 0.0
    assert detail["fractional_units"] == 1
    with repository.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM shadow_accounts").fetchone()[0] == 0


def test_user_adoption_records_fill_evidence_but_is_never_formal(tmp_path):
    settings = _settings(tmp_path)
    initialize_or_upgrade_database(settings.resolve("quantlab.db"))
    repository = WideResearchRepository(settings.resolve("quantlab.db"))
    order = {
        "order_id": "order-1",
        "check_id": "check-1",
        "account_id": "account-1",
        "symbol": "sh600000",
        "research_run_id": "research-1",
        "context_id": "context-1",
        "context_fingerprint": "context-fingerprint",
        "side": "buy",
        "requested_quantity": 100,
    }
    check = {
        "suggested_action": "buy",
        "suggested_quantity": 100,
        "llm_suggested_action": "buy",
        "research_link_status": "linked",
        "reference_price": 10.0,
        "reference_time": "2026-07-22T07:00:00+00:00",
        "quote": {"quote_fingerprint": "quote-fingerprint"},
    }
    record = repository.record_user_adoption(order=order, check=check)
    assert record["adoption_status"] == "adopted"
    with repository.connect() as db:
        db.execute(
            """INSERT INTO user_paper_accounts(
                   account_id,name,initial_capital,current_cash,created_at,updated_at
               ) VALUES('account-1','用户账户',100000,99000,?,?)""",
            (datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )
        db.execute(
            """INSERT INTO user_paper_orders(
                   order_id,account_id,idempotency_key,check_id,symbol,asset_type,side,
                   requested_quantity,filled_quantity,status,requested_at,
                   eligible_trade_date,reference_price,research_run_id,context_id,
                   context_fingerprint,created_at,updated_at
               ) VALUES('order-1','account-1','key-1','check-1','sh600000','stock','buy',
                        100,100,'filled',?,'2026-07-23',10,'research-1','context-1',
                        'context-fingerprint',?,?)""",
            (
                "2026-07-22T07:00:00+00:00",
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )
        db.execute(
            """INSERT INTO user_paper_fills(
                   fill_id,order_id,fill_key,account_id,symbol,research_run_id,
                   context_id,side,quantity,raw_price,fill_price,gross_value,
                   commission,stamp_duty,transfer_fee,slippage,transaction_fees,
                   trade_date,created_at
               ) VALUES('fill-1','order-1','fill-key-1','account-1','sh600000',
                        'research-1','context-1','buy',100,10,10.01,1001,5,0,0.01,1,5.01,
                        '2026-07-23',?)""",
            (datetime.now(UTC).isoformat(),),
        )
        db.execute(
            """INSERT INTO user_paper_positions(
                   account_id,symbol,asset_type,quantity,average_cost,latest_price,
                   mark_source,updated_at
               ) VALUES('account-1','sh600000','stock',100,10.0601,11.0,'fixture',?)""",
            (datetime.now(UTC).isoformat(),),
        )
    outcomes = repository.user_adoption_outcomes(account_id="account-1")
    assert outcomes[0]["fill_quantity"] == 100
    assert outcomes[0]["formal_forward_scorecard_eligible"] is False
    assert outcomes[0]["outcome_status"] == "filled_pending_horizon_evaluation"
    assert outcomes[0]["average_fill_price"] == 10.01
    assert outcomes[0]["marked_return_pct"] > 9.0
    assert outcomes[0]["return_scope"] == "filled_buy_marked_to_latest_server_account_price"


def test_wide_api_is_read_only_and_scheduler_rejects_backfill(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    initialize_or_upgrade_database(settings.resolve("quantlab.db"))
    result = preregister_wide_forward_experiment(
        settings,
        frozen_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
        signal_start_date=date(2026, 7, 22),
    )
    monkeypatch.setattr(api_module, "_settings", lambda: settings)
    response = _request("GET", "/api/wide-forward/experiments")
    assert response.status_code == 200
    assert response.json()["experiments"][0]["experiment_id"] == result["experiment"][
        "experiment_id"
    ]
    forbidden = _request("POST", "/api/wide-forward/registration-jobs")
    assert forbidden.status_code == 403
    scheduler = RuntimeScheduler(settings)
    tick = scheduler.tick(
        now=datetime(2026, 7, 22, 17, 0, tzinfo=UTC),
        run_date=date(2026, 7, 21),
        backfill=True,
    )
    skipped = {item["name"]: item["reason"] for item in tick["skipped"]}
    assert skipped["wide_forward_registration"] == (
        "forward_backfill_is_not_preregistered_evidence"
    )


def test_wide_worker_handlers_enforce_scheduler_identity(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    initialize_or_upgrade_database(settings.resolve("quantlab.db"))
    handlers = default_job_handlers(settings)

    class Repository:
        schedule_run = None

        def schedule_run_for_job(self, _job_id):
            return self.schedule_run

    class Context:
        def __init__(self):
            self.repository = Repository()
            self.job = {"job_id": "job-wide"}
            self.progress_events = []

        def progress(self, value, message):
            self.progress_events.append((value, message))

    context = Context()
    missing = handlers["wide_forward_registration"](
        context, {"as_of": "2026-07-22"}
    )
    assert missing["status"] == "skipped"
    context.repository.schedule_run = {
        "schedule_name": "wrong",
        "schedule_job_type": "wide_forward_registration",
        "is_backfill": False,
        "run_date": "2026-07-22",
        "schedule_run_id": "schedule-wide",
    }
    wrong = handlers["wide_forward_registration"](
        context, {"as_of": "2026-07-22"}
    )
    assert wrong["reason"] == "job is not linked to the wide forward schedule"
    context.repository.schedule_run["schedule_name"] = "wide_forward_registration"
    backfill = handlers["wide_forward_registration"](
        context, {"as_of": "2026-07-22", "is_backfill": True}
    )
    assert backfill["reason"] == "backfills cannot create wide forward evidence"
    mismatch = handlers["wide_forward_registration"](
        context, {"as_of": "2026-07-23"}
    )
    assert "immutable schedule run date" in mismatch["reason"]

    preregister_wide_forward_experiment(
        settings,
        frozen_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
        signal_start_date=date(2026, 7, 22),
    )
    context.repository.schedule_run["run_date"] = "2026-07-21"
    before_start = handlers["wide_forward_registration"](
        context, {"as_of": "2026-07-21"}
    )
    assert before_start["status"] == "skipped"
    assert before_start["signal_start_date"] == "2026-07-22"
    context.repository.schedule_run["run_date"] = "2026-07-22"

    monkeypatch.setattr(
        "quantlab.runtime.readiness.primary_start_readiness",
        lambda *_args, **_kwargs: {
            "sample_registration_allowed": False,
            "blockers": ["point_in_time_pool_field_coverage_below_minimum"],
            "quality_gate": {"ready": False},
            "data": {"point_in_time_pool": {"eligible_members": 0}},
        },
    )
    gated = handlers["wide_forward_registration"](
        context,
        {"as_of": "2026-07-22", "require_forward_readiness": True},
    )
    assert gated["status"] == "skipped"
    assert gated["reason"] == "wide_forward_readiness_failed"
    assert "point_in_time_pool_has_fewer_than_wide_sample_target" in gated["blockers"]

    monkeypatch.setattr(
        "quantlab.runtime.readiness.primary_start_readiness",
        lambda *_args, **_kwargs: {
            "sample_registration_allowed": True,
            "blockers": [],
            "quality_gate": {"ready": True},
            "data": {"point_in_time_pool": {"eligible_members": 24}},
        },
    )
    monkeypatch.setattr(
        "quantlab.workflows.wide_forward.register_wide_forward_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("wide sample does not meet the frozen industry diversity requirement")
        ),
    )
    selection_skipped = handlers["wide_forward_registration"](
        context,
        {"as_of": "2026-07-22", "require_forward_readiness": True},
    )
    assert selection_skipped["status"] == "skipped"
    assert selection_skipped["reason"] == "wide_forward_selection_not_ready"

    calls = []

    def registration(*_args, **kwargs):
        calls.append(kwargs)
        kwargs["progress_callback"](0.5, "half")
        return {"status": "completed", "batch_id": "batch-wide"}

    monkeypatch.setattr(
        "quantlab.workflows.wide_forward.register_wide_forward_batch", registration
    )
    success = handlers["wide_forward_registration"](
        context, {"as_of": "2026-07-22"}
    )
    assert success["batch_id"] == "batch-wide"
    assert calls[0]["schedule_run_id"] == "schedule-wide"
    assert context.progress_events[-1] == (0.5, "half")

    context.repository.schedule_run = None
    missing_mark = handlers["wide_research_portfolio_mark"](context, {})
    assert missing_mark["status"] == "skipped"
    context.repository.schedule_run = {"schedule_name": "wide_research_portfolio_mark"}
    monkeypatch.setattr(
        "quantlab.workflows.wide_forward.mark_wide_research_portfolios",
        lambda _settings: {"inserted_positions": 7},
    )
    marked = handlers["wide_research_portfolio_mark"](context, {})
    assert marked == {"inserted_positions": 7}
    assert context.progress_events[-1][0] == 0.20
