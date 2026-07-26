from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from quantlab.config import Settings
from quantlab.domain import (
    AnalysisContextPack,
    AssetType,
    Bar,
    EvidenceBlock,
    EvidenceDomain,
    EvidenceQuality,
    MarketQuote,
)
from quantlab.domain.data_governance import DataNamespace, DataTrustLevel
from quantlab.domain.strategy_evidence import (
    PointInTimePoolMember,
    PointInTimePoolSnapshot,
)
from quantlab.market import (
    ExecutionQuoteService,
    PriceDisagreementError,
    ResearchBarService,
    TradingCalendarService,
)
from quantlab.persistence.evidence import EvidenceRepository
from quantlab.persistence.migrations import COMPONENT_ORDER, initialize_or_upgrade_database
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
from quantlab.runtime.scheduler import DEFAULT_SCHEDULES, RuntimeScheduler
from quantlab.runtime.worker import JobWorker, default_job_handlers
from quantlab.workflows import forward_ablation as ablation_module
from quantlab.workflows import context as context_module
from quantlab.workflows import forward_experiment as experiment_module
from quantlab.workflows.forward_experiment import (
    ensure_primary_forward_experiment,
    formal_forward_scorecard,
    register_primary_forward_samples,
    save_manual_forward_exploration,
)
from quantlab.workflows.context import build_analysis_context_pack
from quantlab.workflows.capital_flow import unavailable_flow_block
from quantlab.workflows.investor_portfolio import (
    build_investor_recommendation,
    confirm_investor_import,
    create_investor_portfolio,
    investor_csv_template,
    mark_investor_portfolios,
    preview_investor_csv,
    record_investor_trade,
    record_recommendation_adoption,
    settle_investor_recommendation_outcomes,
)
from quantlab.workflows.shadow_trading import (
    execute_pending_shadow_orders,
    mark_shadow_accounts,
    shadow_scorecard,
)
from quantlab.workflows.trusted_data import refresh_trusted_data


def _open_trading_fixture_clock() -> tuple[date, datetime]:
    observed_now = datetime.now(UTC)
    trading_date = observed_now.date()
    while trading_date.weekday() >= 5:
        trading_date += timedelta(days=1)
    scheduled_at = datetime.combine(trading_date, time(8), tzinfo=UTC)
    return trading_date, max(scheduled_at, observed_now + timedelta(minutes=5))


FIXTURE_OPEN_TRADING_DATE, FIXTURE_SERVER_NOW = _open_trading_fixture_clock()


def _settings(tmp_path, *, candidates: int = 1) -> Settings:
    return Settings(
        values={
            "system": {
                "database_path": "quantlab.db",
                "data_dir": "data",
                "test_mode": True,
            },
            "calibration": {"flat_threshold_pct": 1.0},
            "risk": {"max_single_position": 0.15},
            "llm": {"allow_mock_fallback": True, "task_cost_budget_usd": 1.0},
            "costs": {
                "stock": {
                    "commission_rate": 0.00025,
                    "minimum_commission": 5.0,
                    "stamp_duty_rate": 0.0005,
                    "transfer_fee_rate": 0.00001,
                    "slippage_bps": 10.0,
                    "stop_slippage_bps": 25.0,
                    "trade_lot": 100,
                }
            },
            "strategies": {
                "a_share_v4": {"protocol_version": "a-share-v4-test"},
                "forward_primary": {
                    "candidate_count": candidates,
                    "initial_capital": 100_000.0,
                    "minimum_trust_level": "server_observed",
                    "simple_baseline_weight": 0.05,
                    "asset_scope": ["a_share"],
                },
                "etf_rotation": {"universe": []},
            },
        },
        root=tmp_path,
    )


def _pack(symbol: str, as_of: date, momentum: float = 2.0) -> AnalysisContextPack:
    observed = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)
    blocks = [
        EvidenceBlock(
            domain=domain,
            title=domain.value,
            source="round5_fixture",
            methodology="deterministic fixture",
            as_of=observed,
            available_at=observed,
            fetched_at=observed,
            freshness="fresh",
            quality=EvidenceQuality.AVAILABLE,
            payload={"momentum_score": momentum, "return_20_pct": momentum},
        )
        for domain in (
            EvidenceDomain.MARKET,
            EvidenceDomain.TECHNICAL,
            EvidenceDomain.CAPITAL_FLOW,
            EvidenceDomain.PORTFOLIO,
            EvidenceDomain.STRATEGY,
        )
    ]
    return AnalysisContextPack(
        symbol=symbol,
        asset_type=AssetType.STOCK,
        as_of=as_of,
        cutoff_at=observed + timedelta(hours=15),
        blocks=blocks,
        critical_gaps=[],
        deterministic_summary={"market_regime": "range"},
    )


class _QuoteProvider:
    provider_name = "round5_execution_fixture"
    provider_version = "v1"
    authoritative = True

    def __init__(
        self,
        prices: dict[date, float],
        *,
        session: str = "closed",
        age_minutes: int = 1,
    ):
        self.prices = prices
        self.session = session
        self.age_minutes = age_minutes

    def quote(self, symbol: str, *, asset_type: AssetType, as_of: date) -> MarketQuote:
        eligible = [day for day in self.prices if day <= as_of]
        if not eligible:
            raise ValueError("quote unavailable")
        day = max(eligible)
        return MarketQuote(
            symbol=symbol,
            asset_type=asset_type,
            raw_price=self.prices[day],
            as_of=day,
            available_at=datetime.now(UTC) - timedelta(minutes=self.age_minutes),
            source=self.provider_name,
            provider=self.provider_name,
            source_version=self.provider_version,
            trust_level=DataTrustLevel.SERVER_OBSERVED,
            license_status="fixture",
            endpoint="fixture/quote",
            session_status=self.session,
            industry="trusted-industry",
            risk_metadata={"risk_check_complete": True, "financial_check_complete": True},
        )


class _BarProvider:
    name = "round5_bar_fixture"

    def __init__(self, values: dict[date, tuple[float, float]]):
        self.values = values

    def bars(self, symbols: list[str], start: date, end: date) -> list[Bar]:
        return [
            Bar(
                symbol=symbols[0],
                date=day,
                open=value[0],
                high=max(value),
                low=min(value),
                close=value[1],
                available_at=datetime.now(UTC) - timedelta(minutes=1),
                source=self.name,
            )
            for day, value in self.values.items()
            if start <= day <= end
        ]


def _bar_service(values: dict[date, tuple[float, float]]) -> ResearchBarService:
    return ResearchBarService(
        _BarProvider(values),
        provider_name="round5_bar_fixture",
        provider_version="v1",
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        license_status="fixture",
    )


def _calendar_and_pool(
    settings: Settings,
    signal_date: date,
    symbol: str | list[str] = "sh600001",
):
    symbols = [symbol] if isinstance(symbol, str) else symbol
    fixture_available_at = min(
        datetime.combine(signal_date, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=7),
        datetime.now(UTC) - timedelta(minutes=2),
    )
    calendar = TradingCalendarService.from_settings(settings)
    days = []
    cursor = signal_date
    open_days = 0
    while open_days < 35:
        is_open = cursor.weekday() < 5
        days.append({"trade_date": cursor.isoformat(), "is_open": is_open})
        open_days += int(is_open)
        cursor += timedelta(days=1)
    calendar.ingest(
        days,
        namespace=DataNamespace.PRODUCTION,
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        provider="calendar_fixture",
        source="exchange_schedule_fixture",
        endpoint="fixture/calendar",
        source_version="v1",
        available_at=fixture_available_at,
        license_status="fixture",
        raw_fingerprint="calendar-fixture",
    )
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    manifest = repository.create_manifest(
        batch_type="point_in_time_pool",
        namespace=DataNamespace.PRODUCTION,
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        provider="pool_fixture",
        source="pool_fixture",
        endpoint="fixture/pool",
        source_version="v1",
        available_at=fixture_available_at,
        license_status="fixture",
        payload={"symbols": symbols, "date": signal_date},
        raw_fingerprint="pool-fixture",
        record_count=len(symbols),
    )
    snapshot = PointInTimePoolSnapshot(
        snapshot_type="a_share",
        snapshot_date=signal_date,
        cutoff_at=fixture_available_at,
        protocol_version="a-share-v4-test",
        source="pool_fixture",
        source_version="v1",
        stage="forward_shadow",
        members=[
            PointInTimePoolMember(
                symbol=item,
                name="fixture",
                asset_class="equity",
                category="trusted-industry",
                eligible=True,
                representative=True,
                representative_rank=index,
                source="pool_fixture",
                available_at=fixture_available_at,
                payload={
                    "trade_date": signal_date.isoformat(),
                    "latest_price": 10.0 + index,
                    "suspended": False,
                    "is_st": False,
                    "industry": "trusted-industry",
                    "spot_provider_market_date": signal_date.isoformat(),
                    "field_sources": {"current_spot": "pool_fixture_quote"},
                    "field_observations": {
                        "price": {
                            "source": "pool_fixture_quote",
                            "available_at": fixture_available_at.isoformat(),
                            "market_date": signal_date.isoformat(),
                            "raw_response_fingerprint": f"quote-{item}",
                            "missing_reason": None,
                        }
                    },
                },
            )
            for index, item in enumerate(symbols, start=1)
        ],
        created_at=fixture_available_at,
        namespace=DataNamespace.PRODUCTION,
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        manifest_id=manifest["manifest_id"],
    )
    StrategyEvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    ).save_pool_snapshot(snapshot)


def _committee(*_args, **_kwargs) -> dict:
    return {
        "action": "buy",
        "confidence": 0.8,
        "suggested_weight_max": 0.15,
        "degraded_roles": [],
        "supporting_evidence": ["technical"],
        "opposing_evidence": ["valuation"],
        "invalidation_conditions": ["trend break"],
    }


def _raw(*_args, **_kwargs) -> dict:
    return {
        "probabilities": {"up": 0.70, "flat": 0.20, "down": 0.10},
        "action": "buy",
        "confidence": 0.7,
        "complete": True,
    }


def test_trust_levels_calendar_namespaces_and_execution_fail_closed(tmp_path):
    settings = _settings(tmp_path)
    calendar = TradingCalendarService.from_settings(settings)
    holiday = date(2026, 10, 1)
    calendar.ingest(
        [{"trade_date": holiday.isoformat(), "is_open": False}],
        namespace="research",
        trust_level="user_imported",
        provider="user",
        source="upload",
        endpoint="public",
        source_version="v1",
        available_at=datetime.now(UTC) - timedelta(minutes=1),
        license_status="unverified",
        raw_fingerprint="user-calendar",
    )
    with pytest.raises(ValueError, match="trusted production"):
        calendar.day(holiday, formal=True)
    calendar.ingest(
        [
            {"trade_date": "2026-09-30", "is_open": True},
            {"trade_date": "2026-10-01", "is_open": False},
            {"trade_date": "2026-10-02", "is_open": False},
            {"trade_date": "2026-10-03", "is_open": False},
            {"trade_date": "2026-10-04", "is_open": False},
            {"trade_date": "2026-10-05", "is_open": False},
            {"trade_date": "2026-10-06", "is_open": False},
            {"trade_date": "2026-10-07", "is_open": False},
            {"trade_date": "2026-10-08", "is_open": True},
        ],
        namespace="production",
        trust_level="server_observed",
        provider="server",
        source="exchange_schedule",
        endpoint="internal",
        source_version="v2",
        available_at=datetime.now(UTC) - timedelta(minutes=1),
        license_status="fixture",
        raw_fingerprint="trusted-calendar",
    )
    assert calendar.next_open_day(date(2026, 9, 30), formal=True) == date(2026, 10, 8)
    assert calendar.business_day_age(
        date(2026, 9, 30), date(2026, 10, 8), formal=True
    ) == 1

    unknown = ExecutionQuoteService(
        _QuoteProvider({date(2026, 7, 20): 10.0}, session="unknown")
    ).get("sh600001", asset_type="stock", as_of=date(2026, 7, 20))
    assert unknown.actionable is False
    assert "session_status_is_unknown" in unknown.actionability_reasons
    with pytest.raises(PriceDisagreementError):
        ExecutionQuoteService(
            [
                _QuoteProvider({date(2026, 7, 20): 10.0}, session="open"),
                _QuoteProvider({date(2026, 7, 20): 11.0}, session="open"),
            ],
            maximum_price_deviation_bps=50,
        ).get("sh600001", asset_type="stock", as_of=date(2026, 7, 20))


def test_user_industry_and_public_pit_data_cannot_change_production_risk(tmp_path):
    settings = _settings(tmp_path)
    evidence = EvidenceRepository(settings.resolve(settings.get("system.database_path")))
    evidence.save_industry_memberships(
        [
            {
                "symbol": "sh600001",
                "industry": "user-industry",
                "date": "2026-07-20",
                "available_at": "2026-07-20T00:00:00+00:00",
            }
        ],
        source="public_calculation",
        source_version="v1",
        namespace="research",
        trust_level="user_imported",
    )
    from quantlab.market.quotes import _point_in_time_industry

    assert _point_in_time_industry(settings, "sh600001", date(2026, 7, 20)) == (None, None)
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    manifest = repository.create_manifest(
        batch_type="industry_membership",
        namespace="production",
        trust_level="server_observed",
        provider="server",
        source="trusted-industry",
        endpoint="internal",
        source_version="v2",
        available_at=datetime.now(UTC) - timedelta(minutes=1),
        license_status="fixture",
        payload={"symbol": "sh600001", "industry": "trusted-industry"},
        raw_fingerprint="trusted-industry",
        record_count=1,
    )
    repository.save_industry_memberships(
        manifest["manifest_id"],
        [{"symbol": "sh600001", "industry": "trusted-industry", "date": "2026-07-20"}],
    )
    assert _point_in_time_industry(settings, "sh600001", date(2026, 7, 20))[0] == (
        "trusted-industry"
    )


def test_primary_registration_is_automatic_and_manual_exploration_is_excluded(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    signal_date = FIXTURE_OPEN_TRADING_DATE
    _calendar_and_pool(settings, signal_date)
    pack = _pack("sh600001", signal_date)
    monkeypatch.setattr(
        ablation_module,
        "build_analysis_context_pack",
        lambda *_args, **_kwargs: pack.model_dump(mode="json"),
    )
    service = ExecutionQuoteService(
        _QuoteProvider({signal_date: 10.0}, session="closed")
    )
    result = register_primary_forward_samples(
        settings,
        trade_date=signal_date,
        quote_service=service,
        committee_runner=_committee,
        raw_llm_runner=_raw,
        server_now=FIXTURE_SERVER_NOW,
    )
    assert result["registered_samples"] == 2, [item["reason"] for item in result["samples"]]
    assert {item["status"] for item in result["samples"]} == {"registered"}
    experiment = ensure_primary_forward_experiment(settings)
    repository = StrategyEvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    with repository.connect() as db:
        origins = {
            row[0]
            for row in db.execute(
                "SELECT DISTINCT registration_origin FROM forward_ablation_predictions"
            ).fetchall()
        }
    assert origins == {"automatic_primary"}

    monkeypatch.setattr(
        experiment_module,
        "build_analysis_context_pack",
        lambda *_args, **_kwargs: pack.model_dump(mode="json"),
    )
    manual = save_manual_forward_exploration(
        settings,
        symbol="sh600001",
        horizon_days=5,
        account_id=None,
        quote_service=service,
        committee_runner=_committee,
        raw_llm_runner=_raw,
        server_now=FIXTURE_SERVER_NOW,
    )
    assert manual["evidence_stage"] == "manual_exploration"
    scorecard = formal_forward_scorecard(settings)
    assert scorecard["formal_samples"] == 2
    assert scorecard["primary"]["experiment_id"] == experiment["experiment_id"]
    assert len(scorecard["experiments"]) == 1


def test_scheduler_recovery_appends_registration_attempt_and_uses_frozen_pool_quote(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    signal_date = FIXTURE_OPEN_TRADING_DATE
    _calendar_and_pool(settings, signal_date)
    pack = _pack("sh600001", signal_date)
    built_for: list[date] = []

    def build_exact_context(*_args, **kwargs):
        built_for.append(kwargs["as_of"])
        return pack.model_dump(mode="json")

    monkeypatch.setattr(
        ablation_module,
        "build_analysis_context_pack",
        build_exact_context,
    )
    first = register_primary_forward_samples(
        settings,
        trade_date=signal_date,
        quote_service=ExecutionQuoteService(
            _QuoteProvider({signal_date - timedelta(days=1): 10.0}, session="closed")
        ),
        committee_runner=_committee,
        raw_llm_runner=_raw,
        server_now=FIXTURE_SERVER_NOW,
    )
    assert first["status"] == "failed"
    assert first["failed_samples"] == 2
    EvidenceRepository(settings.resolve(settings.get("system.database_path"))).save_context(
        _pack("sh600001", signal_date - timedelta(days=1))
    )

    recovered = register_primary_forward_samples(
        settings,
        trade_date=signal_date,
        committee_runner=_committee,
        raw_llm_runner=_raw,
        server_now=FIXTURE_SERVER_NOW,
        activation_origin="scheduler",
        activation_reference={
            "attempt_number": 2,
            "recovery_of_schedule_run_id": "original-schedule-run",
            "recovery_reason": "fixture recovery after exact quote became available",
        },
    )

    assert recovered["status"] == "completed"
    assert recovered["registered_samples"] == 2
    assert recovered["attempt_number"] == 2
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    runs = repository.registration_runs(recovered["experiment_id"])
    assert [(item["attempt_number"], item["status"]) for item in runs] == [
        (1, "failed"),
        (2, "completed"),
    ]
    assert runs[1]["recovery_of_registration_id"] == runs[0]["registration_id"]
    assert runs[0]["failed_samples"] == 2
    assert {item["status"] for item in recovered["samples"]} == {"registered"}
    assert built_for == [signal_date, signal_date]


def test_context_decision_date_stays_exact_when_technical_history_lags(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    signal_date = FIXTURE_OPEN_TRADING_DATE
    technical_date = signal_date - timedelta(days=1)
    technical_available_at = datetime.combine(
        technical_date,
        time(16),
        tzinfo=UTC,
    )
    quote_available_at = FIXTURE_SERVER_NOW
    quant = {
        "as_of": technical_date,
        "price": 9.0,
        "source": "historical_fixture",
        "available_at": technical_available_at,
        "degraded_sources": [],
        "price_history": {
            "recent_raw_and_adjusted_bars_30": [],
            "returns_adjusted_pct": {},
            "risk_adjusted_pct": {},
            "moving_averages_adjusted": {},
            "latest_signal_close_vs_moving_averages": {},
            "raw_market_ranges": {},
            "average_trading_amount": {},
        },
        "report": SimpleNamespace(model_dump=lambda **_kwargs: {}),
    }
    monkeypatch.setattr("quantlab.workflows.research.load_quant_report", lambda *_a: quant)
    monkeypatch.setattr(
        "quantlab.fundamentals.load_a_share_financial_report",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    monkeypatch.setattr(
        "quantlab.workflows.radar.build_market_radar",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    monkeypatch.setattr(
        context_module,
        "build_live_stock_flow",
        lambda *_a, **_k: unavailable_flow_block(
            scope="stock",
            key="sh600001",
            as_of=technical_date,
            source="fixture",
            reason="fixture",
        ),
    )
    monkeypatch.setattr(
        "quantlab.workflows.reflection.controlled_research_memory",
        lambda *_a, **_k: {"lessons": []},
    )
    quote = MarketQuote(
        symbol="sh600001",
        asset_type=AssetType.STOCK,
        raw_price=10.0,
        as_of=signal_date,
        available_at=quote_available_at,
        source="trusted_point_in_time_pool",
        session_status="closed",
        quote_kind="current_close",
        trust_level=DataTrustLevel.SERVER_OBSERVED,
    )

    payload = build_analysis_context_pack(
        settings,
        symbol="sh600001",
        as_of=signal_date,
        asset_type="stock",
        market_quote=quote,
        include_events=False,
        save=False,
    )

    assert payload["as_of"] == signal_date.isoformat()
    market = next(item for item in payload["blocks"] if item["domain"] == "market")
    technical = next(
        item for item in payload["blocks"] if item["domain"] == "technical"
    )
    assert market["payload"]["current_raw_price"] == 10.0
    assert datetime.fromisoformat(market["as_of"]).date() == signal_date
    assert datetime.fromisoformat(technical["as_of"]).date() == technical_date


def test_production_registration_persists_event_time_readiness_snapshot(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.values["system"]["test_mode"] = False
    signal_date = FIXTURE_OPEN_TRADING_DATE
    _calendar_and_pool(settings, signal_date)
    readiness = {
        "as_of": signal_date.isoformat(),
        "checked_at": FIXTURE_SERVER_NOW.isoformat(),
        "start_allowed": True,
        "sample_registration_allowed": True,
        "blockers": [],
        "data": {"point_in_time_pool": {"ready": True}},
    }
    monkeypatch.setattr(
        "quantlab.runtime.readiness.primary_start_readiness",
        lambda *_args, **_kwargs: readiness,
    )
    pack = _pack("sh600001", signal_date)
    monkeypatch.setattr(
        ablation_module,
        "build_analysis_context_pack",
        lambda *_args, **_kwargs: pack.model_dump(mode="json"),
    )
    result = register_primary_forward_samples(
        settings,
        trade_date=signal_date,
        activation_origin="scheduler",
        activation_reference={"run_date": signal_date.isoformat()},
        quote_service=ExecutionQuoteService(
            _QuoteProvider({signal_date: 10.0}, session="closed")
        ),
        committee_runner=_committee,
        raw_llm_runner=_raw,
        server_now=FIXTURE_SERVER_NOW,
    )
    assert result["registered_samples"] == 2
    assert result["payload"]["readiness"]["start_allowed"] is True
    stored = Round5Repository(settings.resolve(settings.get("system.database_path"))).registration_runs(
        result["experiment_id"]
    )[0]
    assert stored["payload"]["readiness"]["as_of"] == signal_date.isoformat()
    from quantlab.workflows.experiment_recorder import _persisted_registration_readiness

    persisted = _persisted_registration_readiness(
        settings.resolve(settings.get("system.database_path")), signal_date
    )
    assert persisted is not None
    assert persisted["sample_registration_allowed"] is True


def test_shadow_accounts_exit_symbols_that_leave_the_daily_candidate_set(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    signal_date = FIXTURE_OPEN_TRADING_DATE
    _calendar_and_pool(settings, signal_date, symbol=["sh600001"])
    experiment = ensure_primary_forward_experiment(settings)
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    now = FIXTURE_SERVER_NOW.isoformat()
    with repository.transaction() as db:
        for account in repository.shadow_accounts(experiment["cohort_id"]):
            db.execute(
                "UPDATE shadow_accounts SET cash=cash-1000 WHERE account_id=?",
                (account["account_id"],),
            )
            db.execute(
                """INSERT INTO shadow_positions(
                    account_id,symbol,quantity,average_cost,latest_price,
                    latest_price_at,realized_pnl,updated_at
                ) VALUES(?,?,?,?,?,?,0,?)""",
                (
                    account["account_id"],
                    "sh600999",
                    100,
                    10.0,
                    10.0,
                    now,
                    now,
                ),
            )
    pack = _pack("sh600001", signal_date)
    monkeypatch.setattr(
        ablation_module,
        "build_analysis_context_pack",
        lambda *_args, **_kwargs: pack.model_dump(mode="json"),
    )

    result = register_primary_forward_samples(
        settings,
        trade_date=signal_date,
        quote_service=ExecutionQuoteService(
            _QuoteProvider({signal_date: 10.0}, session="closed")
        ),
        committee_runner=_committee,
        raw_llm_runner=_raw,
        server_now=FIXTURE_SERVER_NOW,
    )

    assert result["registered_samples"] == 2
    with repository.connect() as db:
        exits = db.execute(
            """SELECT account_id,symbol,side,requested_quantity,target_weight,reason
               FROM shadow_orders
               WHERE sample_key LIKE ? ORDER BY account_id""",
            (f"{result['registration_id']}:%candidate-exit%",),
        ).fetchall()
    assert len(exits) == 7
    assert {row["symbol"] for row in exits} == {"sh600999"}
    assert {row["side"] for row in exits} == {"sell"}
    assert {row["requested_quantity"] for row in exits} == {100}
    assert {row["target_weight"] for row in exits} == {0.0}


def test_failed_registration_is_preserved_and_not_selectively_hidden(tmp_path):
    settings = _settings(tmp_path, candidates=2)
    experiment = ensure_primary_forward_experiment(settings)
    result = register_primary_forward_samples(
        settings,
        trade_date=date(2026, 7, 20),
        server_now=datetime(2026, 7, 20, 16, tzinfo=UTC),
    )
    assert result["status"] == "failed"
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    assert repository.registration_runs(experiment["experiment_id"])[0]["failure_reason"]
    assert repository.consecutive_registration_failures(experiment["experiment_id"]) == 1
    path = settings.resolve(settings.get("system.database_path"))
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM runtime_failures WHERE source_type='forward_sample_registration'"
        ).fetchone()[0] == 1


def test_variant_definitions_are_independent_of_forbidden_inputs(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    low = _pack("sh600001", date(2026, 7, 20), momentum=-8.0)
    high = _pack("sh600001", date(2026, 7, 20), momentum=8.0)
    monkeypatch.setattr(ablation_module, "predict_active_model", lambda *_args, **_kwargs: None)
    low_predictions = ablation_module._execute_variants(
        settings,
        pack=low,
        committee={"action": "avoid", "confidence": 1.0, "suggested_weight_max": 0},
        horizon_days=5,
        deterministic_max_weight=0.15,
        raw_llm=_raw(),
    )
    high_predictions = ablation_module._execute_variants(
        settings,
        pack=high,
        committee={"action": "avoid", "confidence": 1.0, "suggested_weight_max": 0},
        horizon_days=5,
        deterministic_max_weight=0.15,
        raw_llm=_raw(),
    )
    by_low = {item.variant.value: item for item in low_predictions}
    by_high = {item.variant.value: item for item in high_predictions}
    assert by_low["simple_baseline"].probabilities == by_high["simple_baseline"].probabilities
    assert by_low["raw_llm"].probabilities == _raw()["probabilities"]
    assert by_low["raw_llm"].probabilities == by_high["raw_llm"].probabilities
    assert by_low["statistical_model"].target_weight == 0
    assert by_low["llm_stat_fusion"].target_weight == 0


def test_seven_shadow_accounts_keep_missing_open_pending_and_use_real_nav_path(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    signal_date = FIXTURE_OPEN_TRADING_DATE
    _calendar_and_pool(settings, signal_date)
    pack = _pack("sh600001", signal_date, momentum=8.0)
    monkeypatch.setattr(
        ablation_module,
        "build_analysis_context_pack",
        lambda *_args, **_kwargs: pack.model_dump(mode="json"),
    )
    result = register_primary_forward_samples(
        settings,
        trade_date=signal_date,
        quote_service=ExecutionQuoteService(
            _QuoteProvider({signal_date: 10.0}, session="closed")
        ),
        committee_runner=_committee,
        raw_llm_runner=_raw,
        server_now=FIXTURE_SERVER_NOW,
    )
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    accounts = repository.shadow_accounts(result["cohort_id"])
    assert len(accounts) == 7
    assert len({item["account_id"] for item in accounts}) == 7
    calendar = TradingCalendarService.from_settings(settings)
    next_day = calendar.next_open_day(signal_date)
    missing = execute_pending_shadow_orders(
        settings,
        as_of=next_day,
        bar_service=_bar_service({}),
    )
    assert missing["pending"] > 0
    assert any(
        item["status"] == "pending"
        for account in accounts
        for item in repository.shadow_overview(account["account_id"])["orders"]
    )
    execution = execute_pending_shadow_orders(
        settings,
        as_of=next_day,
        bar_service=_bar_service({next_day: (10.0, 10.0)}),
    )
    assert execution["filled"] > 0
    mark_shadow_accounts(
        settings,
        as_of=next_day,
        bar_service=_bar_service({next_day: (10.0, 10.0)}),
    )
    baseline_account = next(item for item in accounts if item["variant"] == "simple_baseline")
    first_nav = repository.shadow_overview(baseline_account["account_id"])["nav"][-1]
    mark_shadow_accounts(
        settings,
        as_of=next_day,
        bar_service=_bar_service({next_day: (10.0, 10.0)}),
    )
    repeated_nav = repository.shadow_overview(baseline_account["account_id"])["nav"][-1]
    assert repeated_nav["daily_pnl"] == pytest.approx(first_nav["daily_pnl"])
    second = calendar.next_open_day(next_day)
    mark_shadow_accounts(
        settings,
        as_of=second,
        bar_service=_bar_service({second: (8.0, 8.0)}),
    )
    third = calendar.next_open_day(second)
    mark_shadow_accounts(
        settings,
        as_of=third,
        bar_service=_bar_service({third: (9.0, 9.0)}),
    )
    score = shadow_scorecard(settings)
    baseline = score["variants"]["simple_baseline"]
    assert baseline["maximum_drawdown"] < 0
    assert baseline["turnover"] > 0
    assert baseline["transaction_cost"] > 0
    assert baseline["evidence_scope"] == "executable_simulated_account_nav"
    assert baseline["execution"]["fill_rate"] == pytest.approx(1.0)
    assert baseline["execution"]["nav_days"] >= 2


def test_investor_csv_dedup_mark_recommendation_adoption_and_outcome(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    start = FIXTURE_OPEN_TRADING_DATE
    _calendar_and_pool(settings, start)
    portfolio = create_investor_portfolio(settings, name="real holdings", cash=100_000)
    content = (
        "symbol,name,asset_type,quantity,average_cost,industry\n"
        "sh600001,fixture,stock,1000,10.0,trusted-industry\n"
    )
    preview = preview_investor_csv(
        settings,
        portfolio_id=portfolio["portfolio_id"],
        import_type="positions",
        csv_content=content,
        idempotency_key="positions-import-0001",
    )
    confirmed = confirm_investor_import(
        settings, import_id=preview["import_id"], confirm=True
    )
    assert confirmed["rows_applied"] == 1
    duplicate = preview_investor_csv(
        settings,
        portfolio_id=portfolio["portfolio_id"],
        import_type="positions",
        csv_content=content,
        idempotency_key="positions-import-0002",
    )
    assert duplicate["import_id"] == preview["import_id"]

    quote_service = ExecutionQuoteService(
        _QuoteProvider({start: 11.0}, session="open")
    )
    marked = mark_investor_portfolios(
        settings,
        as_of=start,
        portfolio_id=portfolio["portfolio_id"],
        quote_service=quote_service,
    )
    assert marked["portfolios"][0]["equity"] == pytest.approx(111_000)
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    overview = repository.investor_overview(portfolio["portfolio_id"])
    assert overview["portfolio"]["initial_equity"] == pytest.approx(110_000)
    assert overview["nav"][-1]["today_pnl"] == pytest.approx(1_000)
    mark_investor_portfolios(
        settings,
        as_of=start,
        portfolio_id=portfolio["portfolio_id"],
        quote_service=quote_service,
    )
    repeated = repository.investor_overview(portfolio["portfolio_id"])["nav"][-1]
    assert repeated["today_pnl"] == pytest.approx(1_000)
    pack = _pack("sh600001", start)
    monkeypatch.setattr(
        "quantlab.workflows.investor_portfolio.build_analysis_context_pack",
        lambda *_args, **_kwargs: pack.model_dump(mode="json"),
    )
    recommendation = build_investor_recommendation(
        settings,
        portfolio_id=portfolio["portfolio_id"],
        symbol="sh600001",
        quote_service=quote_service,
        committee_runner=_committee,
        server_now=datetime.combine(start, time(16), tzinfo=UTC),
    )
    assert recommendation["payload"]["broker_order_sent"] is False
    assert recommendation["payload"]["actionable"] is True
    adoption = record_recommendation_adoption(
        settings,
        recommendation_id=recommendation["recommendation_id"],
        decision="partially_adopted",
        trade_side="buy",
        actual_quantity=100,
        actual_price=11.0,
        actual_trade_date=start,
    )
    assert adoption["decision"] == "partially_adopted"
    due_5 = date.fromisoformat(recommendation["payload"]["due_dates"]["5"])
    outcome = settle_investor_recommendation_outcomes(
        settings,
        as_of=due_5,
        bar_service=_bar_service({due_5: (12.0, 12.0)}),
    )
    assert outcome["settled"] == 1
    formal = formal_forward_scorecard(settings)
    assert formal["formal_samples"] == 0
    with sqlite3.connect(settings.resolve(settings.get("system.database_path"))) as db:
        assert db.execute("SELECT COUNT(*) FROM investor_recommendation_outcomes").fetchone()[0] == 1
        assert db.execute(
            "SELECT evidence_eligible FROM investor_portfolios WHERE portfolio_id=?",
            (portfolio["portfolio_id"],),
        ).fetchone()[0] == 0


def test_unknown_session_recommendation_is_review_only(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    start = date(2026, 7, 20)
    _calendar_and_pool(settings, start)
    portfolio = create_investor_portfolio(settings, name="review", cash=100_000)
    pack = _pack("sh600001", start)
    monkeypatch.setattr(
        "quantlab.workflows.investor_portfolio.build_analysis_context_pack",
        lambda *_args, **_kwargs: pack.model_dump(mode="json"),
    )
    recommendation = build_investor_recommendation(
        settings,
        portfolio_id=portfolio["portfolio_id"],
        symbol="sh600001",
        quote_service=ExecutionQuoteService(
            _QuoteProvider({start: 10.0}, session="unknown")
        ),
        committee_runner=_committee,
        server_now=datetime(2026, 7, 20, 16, tzinfo=UTC),
    )
    assert recommendation["action"] == "review_required"
    assert recommendation["actionable"] == 0


def test_formal_registration_rejects_closed_day_and_preserves_expected_failures(tmp_path):
    settings = _settings(tmp_path, candidates=2)
    closed_day = date(2026, 10, 1)
    TradingCalendarService.from_settings(settings).ingest(
        [{"trade_date": closed_day.isoformat(), "is_open": False}],
        namespace=DataNamespace.PRODUCTION,
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        provider="calendar_fixture",
        source="exchange_schedule_fixture",
        endpoint="fixture/calendar",
        source_version="v1",
        available_at=datetime.now(UTC) - timedelta(minutes=1),
        license_status="fixture",
        raw_fingerprint="closed-day-calendar",
    )

    result = register_primary_forward_samples(
        settings,
        trade_date=closed_day,
        server_now=datetime(2026, 10, 1, 16, tzinfo=UTC),
    )

    assert result["status"] == "failed"
    assert result["expected_samples"] == 4
    assert result["failed_samples"] == 4
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    samples = repository.registration_samples(result["registration_id"])
    assert len(samples) == 4
    assert {item["status"] for item in samples} == {"failed"}
    assert {item["reason"] for item in samples} == {
        "formal_registration_requires_an_open_trading_day"
    }


def test_primary_cohort_freezes_models_role_policy_and_variant_rules(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    signal_date = FIXTURE_OPEN_TRADING_DATE
    _calendar_and_pool(settings, signal_date)
    experiment = ensure_primary_forward_experiment(settings)
    frozen = experiment["frozen_payload"]
    assert frozen["model_configuration"]
    assert set(frozen["statistical_models"]) == {"5", "20"}
    assert frozen["role_policies"]["range"]["roles"]
    assert frozen["variant_policy"]["llm_stat_statistical_weight"] == 0.5
    pack = _pack("sh600001", signal_date)
    monkeypatch.setattr(
        ablation_module,
        "build_analysis_context_pack",
        lambda *_args, **_kwargs: pack.model_dump(mode="json"),
    )
    monkeypatch.setattr(
        ablation_module,
        "predict_active_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("formal cohort must not read the current active model")
        ),
    )
    used_policies: list[dict] = []

    def committee(*_args, **kwargs):
        used_policies.append(kwargs["role_policy_override"])
        return _committee()

    result = register_primary_forward_samples(
        settings,
        trade_date=signal_date,
        quote_service=ExecutionQuoteService(
            _QuoteProvider({signal_date: 10.0}, session="closed")
        ),
        committee_runner=committee,
        raw_llm_runner=_raw,
        server_now=FIXTURE_SERVER_NOW,
    )
    assert result["registered_samples"] == 2
    assert used_policies
    assert {
        item["governance_version"] for item in used_policies
    } == {frozen["role_policies"]["range"]["governance_version"]}

    changed = _settings(tmp_path)
    changed.values["llm"]["model"] = "different-model"
    replayed = register_primary_forward_samples(
        changed,
        trade_date=signal_date,
        server_now=datetime(2026, 7, 21, 16, tzinfo=UTC),
    )
    assert replayed["registration_id"] == result["registration_id"]
    assert replayed["status"] == "completed"
    assert replayed["registered_samples"] == 2
    assert len(Round5Repository(
        settings.resolve(settings.get("system.database_path"))
    ).registration_runs(experiment["experiment_id"])) == 1


def test_operator_backfill_cannot_create_primary_forward_registration(tmp_path):
    settings = _settings(tmp_path)
    run_date = date(2026, 7, 20)
    _calendar_and_pool(settings, run_date)

    result = RuntimeScheduler(settings).backfill(run_date)

    assert any(
        item["name"] == "forward_sample_registration"
        and item["reason"] == "primary_forward_backfill_is_not_formal_evidence"
        for item in result["skipped"]
    )
    assert not Round5Repository(
        settings.resolve(settings.get("system.database_path"))
    ).registration_runs(ensure_primary_forward_experiment(settings)["experiment_id"])


def test_execution_quote_age_blocks_actionability():
    today = FIXTURE_OPEN_TRADING_DATE
    quote = ExecutionQuoteService(
        _QuoteProvider({today: 10.0}, session="open", age_minutes=10),
        maximum_quote_age_seconds=120,
    ).get("sh600001", asset_type="stock", as_of=today)

    assert quote.actionable is False
    assert "execution_quote_exceeds_freshness_limit" in quote.actionability_reasons


def test_shadow_accounts_reserve_cash_across_multiple_candidates(tmp_path, monkeypatch):
    settings = _settings(tmp_path, candidates=2)
    settings.values["risk"]["max_single_position"] = 0.8
    signal_date = FIXTURE_OPEN_TRADING_DATE
    symbols = ["sh600001", "sh600002"]
    _calendar_and_pool(settings, signal_date, symbols)
    monkeypatch.setattr(
        ablation_module,
        "build_analysis_context_pack",
        lambda *_args, **kwargs: _pack(
            kwargs["symbol"], signal_date, momentum=8.0
        ).model_dump(mode="json"),
    )

    result = register_primary_forward_samples(
        settings,
        trade_date=signal_date,
        quote_service=ExecutionQuoteService(
            _QuoteProvider({signal_date: 10.0}, session="closed")
        ),
        committee_runner=_committee,
        raw_llm_runner=_raw,
        server_now=FIXTURE_SERVER_NOW,
    )

    assert result["registered_samples"] == 4
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    accounts = repository.shadow_accounts(result["cohort_id"])
    assert all(float(item["frozen_cash"]) <= float(item["cash"]) + 1e-9 for item in accounts)
    raw_account = next(item for item in accounts if item["variant"] == "raw_llm")
    raw_orders = repository.shadow_overview(raw_account["account_id"])["orders"]
    assert len(raw_orders) == 2
    assert sum(float(item["reserved_cash"]) for item in raw_orders) <= float(
        raw_account["cash"]
    )

    next_day = TradingCalendarService.from_settings(settings).next_open_day(signal_date)
    execute_pending_shadow_orders(
        settings,
        as_of=next_day,
        bar_service=_bar_service({next_day: (10.0, 10.0)}),
    )
    assert all(
        float(item["cash"]) >= -1e-9
        for item in repository.shadow_accounts(result["cohort_id"])
    )


def test_shadow_order_expires_and_releases_reserved_cash(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    signal_date = FIXTURE_OPEN_TRADING_DATE
    _calendar_and_pool(settings, signal_date)
    pack = _pack("sh600001", signal_date, momentum=8.0)
    monkeypatch.setattr(
        ablation_module,
        "build_analysis_context_pack",
        lambda *_args, **_kwargs: pack.model_dump(mode="json"),
    )
    result = register_primary_forward_samples(
        settings,
        trade_date=signal_date,
        quote_service=ExecutionQuoteService(
            _QuoteProvider({signal_date: 10.0}, session="closed")
        ),
        committee_runner=_committee,
        raw_llm_runner=_raw,
        server_now=FIXTURE_SERVER_NOW,
    )
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    raw_account = next(
        item
        for item in repository.shadow_accounts(result["cohort_id"])
        if item["variant"] == "raw_llm"
    )
    assert float(raw_account["frozen_cash"]) > 0

    pending_order = repository.shadow_overview(raw_account["account_id"])["orders"][0]
    execution = execute_pending_shadow_orders(
        settings,
        as_of=date.fromisoformat(pending_order["expires_at"]) + timedelta(days=1),
        bar_service=_bar_service({}),
    )

    assert execution["expired"] > 0
    updated = next(
        item
        for item in repository.shadow_accounts(result["cohort_id"])
        if item["variant"] == "raw_llm"
    )
    assert updated["frozen_cash"] == pytest.approx(0.0)
    assert repository.shadow_overview(raw_account["account_id"])["orders"][0][
        "status"
    ] == "expired"


def test_trusted_data_refresh_records_success_unavailable_partial_and_failure(tmp_path):
    unavailable_settings = _settings(tmp_path / "unavailable")
    unavailable = refresh_trusted_data(unavailable_settings, as_of=date(2026, 7, 20))
    assert {
        unavailable["sources"][key]["manifest"]["status"]
        for key in ("calendar", "industry", "point_in_time_pool")
    } == {"unavailable"}

    root = tmp_path / "configured"
    root.mkdir()
    day = date(2026, 7, 20)
    calendar_path = root / "calendar.csv"
    calendar_path.write_text(
        "trade_date,is_open\n2026-07-20,true\n2026-07-21,true\n",
        encoding="utf-8",
    )
    industry_path = root / "industry.csv"
    industry_path.write_text(
        "symbol,industry,effective_date\n"
        "sh600001,bank,2026-07-20\n"
        "sh600002,,2026-07-20\n",
        encoding="utf-8",
    )
    pool_path = root / "pool.json"
    pool = PointInTimePoolSnapshot(
        snapshot_type="a_share",
        snapshot_date=day,
        cutoff_at=datetime(2026, 7, 20, 15, tzinfo=UTC),
        protocol_version="a-share-v4-test",
        source="configured_pool_fixture",
        source_version="v1",
        stage="forward_shadow",
        members=[
            PointInTimePoolMember(
                symbol="sh600001",
                name="fixture",
                asset_class="equity",
                category="bank",
                eligible=True,
                representative=True,
                representative_rank=1,
                source="configured_pool_fixture",
                available_at=datetime(2026, 7, 20, 15, tzinfo=UTC),
            )
        ],
        created_at=datetime(2026, 7, 20, 15, tzinfo=UTC),
        namespace=DataNamespace.PRODUCTION,
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        manifest_id="replaced-by-ingestion",
    )
    pool_path.write_text(
        json.dumps(pool.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    settings = _settings(root)
    settings.values["runtime"] = {
        "trusted_calendar_path": str(calendar_path),
        "trusted_industry_path": str(industry_path),
        "trusted_pit_pool_path": str(pool_path),
        "trusted_data_default_level": "server_observed",
        "trusted_data_license_status": "fixture_license",
    }

    refreshed = refresh_trusted_data(settings, as_of=day)

    assert refreshed["sources"]["calendar"]["records_saved"] == 2
    assert refreshed["sources"]["industry"]["records_saved"] == 1
    assert refreshed["sources"]["industry"]["manifest"]["status"] == "partial"
    assert refreshed["sources"]["industry"]["manifest"]["failure_records"]
    assert TradingCalendarService.from_settings(settings).is_open(day, formal=True)
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    assert repository.industry_as_of("sh600001", as_of=day)["industry"] == "bank"
    assert StrategyEvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    ).latest_pool_snapshot(
        "a_share",
        day,
        namespace=DataNamespace.PRODUCTION,
        minimum_trust=DataTrustLevel.SERVER_OBSERVED,
    )

    broken_pool = root / "broken-pool.json"
    broken_pool.write_text("{not-json", encoding="utf-8")
    broken_settings = _settings(tmp_path / "broken")
    broken_settings.values["runtime"] = {
        "trusted_pit_pool_path": str(broken_pool),
        "trusted_data_default_level": "server_observed",
        "trusted_data_license_status": "fixture_license",
    }
    broken = refresh_trusted_data(broken_settings, as_of=day)
    assert broken["sources"]["point_in_time_pool"]["manifest"]["status"] == "failed"
    assert broken["sources"]["point_in_time_pool"]["manifest"]["failure_records"]


def test_investor_import_validation_and_manual_trade_ledger(tmp_path):
    settings = _settings(tmp_path)
    portfolio = create_investor_portfolio(settings, name="manual", cash=1_000)
    assert investor_csv_template("positions").startswith("symbol,name")
    assert investor_csv_template("trades").startswith("symbol,asset_type")
    with pytest.raises(ValueError, match="positions or trades"):
        investor_csv_template("unknown")
    with pytest.raises(ValueError, match="positions or trades"):
        preview_investor_csv(
            settings,
            portfolio_id=portfolio["portfolio_id"],
            import_type="unknown",
            csv_content="symbol\nsh600001\n",
            idempotency_key="invalid-type-import",
        )
    with pytest.raises(ValueError, match="columns"):
        preview_investor_csv(
            settings,
            portfolio_id=portfolio["portfolio_id"],
            import_type="positions",
            csv_content="symbol\nsh600001\n",
            idempotency_key="invalid-columns-import",
        )
    invalid = preview_investor_csv(
        settings,
        portfolio_id=portfolio["portfolio_id"],
        import_type="positions",
        csv_content=(
            "symbol,name,asset_type,quantity,average_cost,industry\n"
            "sh600001,bad,stock,0,10.0,bank\n"
        ),
        idempotency_key="invalid-row-import",
    )
    assert invalid["error_count"] == 1
    with pytest.raises(ValueError, match="explicit confirmation"):
        confirm_investor_import(settings, import_id=invalid["import_id"], confirm=False)
    with pytest.raises(ValueError, match="invalid rows"):
        confirm_investor_import(settings, import_id=invalid["import_id"], confirm=True)
    with pytest.raises(ValueError, match="not found"):
        confirm_investor_import(settings, import_id="missing-import", confirm=True)

    bought = record_investor_trade(
        settings,
        portfolio_id=portfolio["portfolio_id"],
        symbol="sh600001",
        asset_type="stock",
        side="buy",
        quantity=10,
        price=10.0,
        transaction_cost=1.0,
        trade_date=date(2026, 7, 20),
        idempotency_key="manual-buy-0001",
    )
    repeated = record_investor_trade(
        settings,
        portfolio_id=portfolio["portfolio_id"],
        symbol="sh600001",
        asset_type="stock",
        side="buy",
        quantity=10,
        price=10.0,
        transaction_cost=1.0,
        trade_date=date(2026, 7, 20),
        idempotency_key="manual-buy-0001",
    )
    assert repeated["trade_id"] == bought["trade_id"]
    sold = record_investor_trade(
        settings,
        portfolio_id=portfolio["portfolio_id"],
        symbol="sh600001",
        asset_type="stock",
        side="sell",
        quantity=5,
        price=12.0,
        transaction_cost=1.0,
        trade_date=date(2026, 7, 21),
        idempotency_key="manual-sell-0001",
    )
    assert sold["side"] == "sell"
    overview = Round5Repository(
        settings.resolve(settings.get("system.database_path"))
    ).investor_overview(portfolio["portfolio_id"])
    assert overview["positions"][0]["quantity"] == 5
    assert overview["positions"][0]["realized_pnl"] > 0
    with pytest.raises(ValueError, match="exceeds portfolio cash"):
        record_investor_trade(
            settings,
            portfolio_id=portfolio["portfolio_id"],
            symbol="sh600002",
            asset_type="stock",
            side="buy",
            quantity=10_000,
            price=10.0,
            transaction_cost=0.0,
            trade_date=date(2026, 7, 21),
            idempotency_key="too-large-buy",
        )
    with pytest.raises(ValueError, match="exceeds position"):
        record_investor_trade(
            settings,
            portfolio_id=portfolio["portfolio_id"],
            symbol="sh600001",
            asset_type="stock",
            side="sell",
            quantity=100,
            price=12.0,
            transaction_cost=0.0,
            trade_date=date(2026, 7, 21),
            idempotency_key="too-large-sell",
        )


def test_round5_migration_backup_tables_worker_and_scheduler_contract(tmp_path):
    path = tmp_path / "quantlab.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE legacy_data(id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO legacy_data(value) VALUES('preserved')")
    status = initialize_or_upgrade_database(path)
    assert status["pre_upgrade_backup"] is not None
    assert list(dict.fromkeys(item["component"] for item in status["migrations"])) == list(
        COMPONENT_ORDER
    )
    assert any(
        item["component"] == "evidence" and item["version"] == 7
        for item in status["migrations"]
    )
    second = initialize_or_upgrade_database(path)
    assert second["pre_upgrade_backup"] is None
    with sqlite3.connect(path) as db:
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert db.execute("SELECT value FROM legacy_data").fetchone()[0] == "preserved"
    assert {
        "forward_experiment_protocols",
        "forward_registration_runs",
        "shadow_accounts",
        "shadow_orders",
        "shadow_positions",
        "shadow_nav",
        "trusted_data_manifests",
        "trusted_calendar_days",
        "trusted_industry_membership",
        "investor_portfolios",
        "investor_imports",
        "investor_positions",
        "investor_trades",
        "investor_nav",
        "investor_recommendations",
        "investor_recommendation_outcomes",
    } <= tables
    handlers = default_job_handlers(_settings(tmp_path / "handlers"))
    assert {
        "forward_preflight",
        "trusted_data_refresh",
        "forward_sample_registration",
        "shadow_account_cycle",
        "investor_mark_to_market",
        "investor_outcome_settlement",
    } <= set(handlers)
    assert [item["name"] for item in DEFAULT_SCHEDULES] == [
        "premarket_digest",
        "forward_preflight",
        "trusted_data_refresh",
        "capital_flow_refresh",
        "prediction_settlement",
            "forward_sample_registration",
            "shadow_account_cycle",
            "wide_forward_registration",
            "account_mark_to_market",
            "investor_mark_to_market",
            "investor_outcome_settlement",
            "wide_research_portfolio_mark",
        "thesis_due_scan",
        "thesis_event_check",
        "thesis_price_invalidation_check",
        "authoritative_reflection_settlement",
        "controlled_memory_refresh",
        "decision_task_refresh",
        "account_daily_report",
        "notification_dispatch",
        "retention_cleanup",
        "database_backup",
    ]


def test_worker_automatic_registration_failure_is_idempotent(tmp_path):
    settings = _settings(tmp_path)
    worker = JobWorker(settings, worker_id="round5-worker")
    day = date(2026, 7, 20)
    job = worker.repository.submit(
        job_type="forward_sample_registration",
        payload={"as_of": day.isoformat()},
        idempotency_key="round5-registration-once",
    )
    result = worker.run_once()
    assert result["job_id"] == job["job_id"]
    assert result["status"] == "completed"
    assert result["result_payload"]["status"] == "skipped"
    assert "scheduler-owned" in result["result_payload"]["reason"]
    assert worker.run_once() is None
