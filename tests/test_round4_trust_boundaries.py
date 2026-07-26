from __future__ import annotations

import asyncio
import importlib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from quantlab.api.app import app
from quantlab.config import Settings
from quantlab.domain import (
    AnalysisContextPack,
    AssetType,
    CommitteeDecision,
    CommitteeRoleOpinion,
    ChatEvidenceAnswer,
    EvidenceBlock,
    EvidenceDomain,
    EvidenceQuality,
    MarketQuote,
)
from quantlab.llm.governance import GovernedLLMProvider
from quantlab.llm.providers import LLMProvider
from quantlab.market import InMemoryQuoteProvider, QuoteService, TradingCalendarService
from quantlab.persistence.evidence import EvidenceRepository
from quantlab.persistence.chat import ChatRepository
from quantlab.persistence.jobs import JobRepository
from quantlab.persistence.migrations import (
    COMPONENT_ORDER,
    initialize_or_upgrade_database,
)
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
from quantlab.persistence.terminal import TerminalRepository
from quantlab.runtime.worker import JobWorker
from quantlab.workflows.forward_ablation import (
    create_round3_forward_cohort,
    freeze_forward_ablation_sample,
)
from quantlab.workflows.llm_committee import run_context_committee_with_provider
from quantlab.workflows.role_governance import (
    decide_role_challenge,
    freeze_role_challenge,
    record_role_outcome,
)
from quantlab.workflows.simulator import (
    cancel_user_paper_order,
    create_user_paper_account,
    mark_user_paper_account,
    run_pretrade_check,
    settle_user_paper_order,
    submit_user_paper_order,
    user_simulator_repository,
)


api_module = importlib.import_module("quantlab.api.app")
forward_module = importlib.import_module("quantlab.workflows.forward_ablation")
chat_module = importlib.import_module("quantlab.workflows.chat")
context_module = importlib.import_module("quantlab.workflows.context")
simulator_module = importlib.import_module("quantlab.workflows.simulator")
simulator_persistence_module = importlib.import_module("quantlab.persistence.simulator")
committee_module = importlib.import_module("quantlab.workflows.llm_committee")


def _settings(tmp_path, *, broad_limits: bool = False) -> Settings:
    limit = 1.0 if broad_limits else 0.15
    return Settings(
        values={
            "system": {
                "database_path": "quantlab.db",
                "data_dir": "data",
                "test_mode": True,
            },
            "risk": {
                "max_total_exposure": 1.0 if broad_limits else 0.80,
                "max_single_position": limit,
                "max_industry_exposure": 1.0 if broad_limits else 0.30,
            },
            "costs": {
                "stock": {
                    "commission_rate": 0.00025,
                    "minimum_commission": 5.0,
                    "stamp_duty_rate": 0.0005,
                    "transfer_fee_rate": 0.00001,
                    "slippage_bps": 10.0,
                    "stop_slippage_bps": 25.0,
                    "trade_lot": 100,
                },
                "etf": {
                    "commission_rate": 0.0001,
                    "minimum_commission": 5.0,
                    "stamp_duty_rate": 0.0,
                    "transfer_fee_rate": 0.0,
                    "slippage_bps": 5.0,
                    "stop_slippage_bps": 15.0,
                    "trade_lot": 100,
                },
            },
            "calibration": {"flat_threshold_pct": 1.0},
            "llm": {
                "maximum_committee_roles": 3,
                "maximum_committee_rounds": 1,
                "role_minimum_matured_samples": 30,
            },
            "strategies": {
                "etf_rotation": {"universe": ["sh510300"]},
                "a_share_v4": {"protocol_version": "a-share-v4-test"},
            },
        },
        root=tmp_path,
    )


def _quote(
    *,
    symbol: str = "sh600001",
    price: float = 10.0,
    as_of: date = date(2026, 7, 17),
    session_status: str = "open",
) -> MarketQuote:
    return MarketQuote(
        symbol=symbol,
        asset_type=AssetType.ETF if symbol == "sh510300" else AssetType.STOCK,
        raw_price=price,
        as_of=as_of,
        available_at=datetime.combine(as_of, datetime.min.time(), tzinfo=UTC),
        source="round4_test_fixture",
        source_version="fixture-v1",
        provider="round4_test_fixture",
        industry="test-industry",
        trade_lot=100,
        t_plus_one=symbol != "sh510300",
        session_status=session_status,
        quote_kind="realtime",
        evidence_stage="test",
        risk_metadata={
            "risk_check_complete": True,
            "financial_check_complete": True,
            "financial_quality_score": 1.0,
            "listing_days": 1000,
        },
    )


def _service(*quotes: MarketQuote) -> QuoteService:
    return QuoteService(InMemoryQuoteProvider(list(quotes)))


def _confirmation(check: dict, *, quantity: int, source: str = "round4_test") -> dict:
    return {
        "confirmed": True,
        "check_id": check["check_id"],
        "account_id": check["account_id"],
        "symbol": check["symbol"],
        "side": check["side"],
        "quantity": quantity,
        "source": source,
        "simulation_mode": "intraday_simulation",
        "close_reference_acknowledged": False,
    }


def _request(
    method: str,
    path: str,
    *,
    payload=None,
    client=("127.0.0.1", 12345),
    headers=None,
):
    async def request():
        transport = httpx.ASGITransport(app=app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as session:
            return await session.request(method, path, json=payload, headers=headers)

    return asyncio.run(request())


def _quote_payload(price: float = 10.0) -> dict:
    return {
        "symbol": "sh600001",
        "asset_type": "stock",
        "raw_price": price,
        "as_of": "2026-07-17",
        "source": "attacker",
        "trade_lot": 1,
        "t_plus_one": False,
        "limit_up": False,
        "session_status": "open",
    }


def test_public_simulator_and_chat_reject_client_authoritative_quotes(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)
    monkeypatch.setenv("QUANTLAB_ENABLE_TEST_QUOTES", "1")
    imported = _request(
        "POST",
        "/internal/test/quotes",
        payload={**_quote_payload(), "source": "test_import"},
    )
    assert imported.status_code == 200
    account = _request(
        "POST",
        "/api/simulator/accounts",
        payload={"name": "round4", "idempotency_key": "round4-account"},
    ).json()
    forged = _request(
        "POST",
        "/api/simulator/pretrade-check",
        payload={
            "account_id": account["account_id"],
            "symbol": "sh600001",
            "side": "buy",
            "quantity": 100,
            "quote": {**_quote_payload(price=0.01), "limit_up": False},
        },
    )
    assert forged.status_code == 422
    valid = _request(
        "POST",
        "/api/simulator/pretrade-check",
        payload={
            "account_id": account["account_id"],
            "symbol": "sh600001",
            "side": "buy",
            "quantity": 100,
        },
    )
    assert valid.status_code == 200
    assert valid.json()["reference_price"] == 10.0
    forged_confirm = _request(
        "POST",
        "/api/chat/actions/not-real/confirm",
        payload={"quantity": 100, "quote": _quote_payload(price=0.01)},
    )
    assert forged_confirm.status_code == 422


def test_remote_sensitive_get_is_forbidden_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv("QUANTLAB_API_TOKEN", raising=False)
    monkeypatch.setattr(api_module, "_settings", lambda: _settings(tmp_path))
    remote = ("203.0.113.88", 5555)
    assert _request("GET", "/api/health", client=remote).status_code == 200
    assert _request("GET", "/api/simulator/accounts", client=remote).status_code == 403
    assert _request("GET", "/api/chat/conversations", client=remote).status_code == 403
    assert _request("GET", "/api/notifications", client=remote).status_code == 403


def test_pending_orders_reserve_cash_and_invalidate_concurrent_checks(tmp_path):
    settings = _settings(tmp_path, broad_limits=True)
    account = create_user_paper_account(
        settings,
        name="reservation",
        idempotency_key="reservation-account",
    )
    quote = _quote()
    service = _service(quote)
    first_check = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol=quote.symbol,
        side="buy",
        quantity=6_000,
        quote_service=service,
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    competing_check = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol=quote.symbol,
        side="buy",
        quantity=6_000,
        quote_service=service,
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    def submit(check_id: str, key: str):
        check = first_check if check_id == first_check["check_id"] else competing_check
        return submit_user_paper_order(
            settings,
            check_id=check_id,
            quantity=6_000,
            idempotency_key=key,
            user_confirmation=_confirmation(check, quantity=6_000),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        orders = list(
            pool.map(
                lambda item: submit(*item),
                [
                    (first_check["check_id"], "reservation-order-one"),
                    (competing_check["check_id"], "reservation-order-two"),
                ],
            )
        )
    assert sum(item["status"] == "pending" for item in orders) == 1
    overview = user_simulator_repository(settings).overview(account["account_id"])
    assert 0 < overview["frozen_cash"] <= overview["cash"]
    assert overview["available_cash"] == pytest.approx(
        overview["cash"] - overview["frozen_cash"]
    )
    after_reservation = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol=quote.symbol,
        side="buy",
        quantity=6_000,
        quote_service=service,
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    assert after_reservation["allowed_to_submit"] is False
    assert "insufficient_cash" in after_reservation["hard_failures"]


def test_order_confirmation_refetches_quote_and_idempotent_replay_keeps_original_order(
    tmp_path,
):
    settings = _settings(tmp_path, broad_limits=True)
    account = create_user_paper_account(
        settings,
        name="confirmation refresh",
        idempotency_key="confirmation-refresh-account",
    )
    checked_quote = _quote(price=10.0)
    changed_quote = _quote(price=11.0)
    check = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol=checked_quote.symbol,
        side="buy",
        quantity=1_000,
        quote_service=_service(checked_quote),
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="market quote changed"):
        submit_user_paper_order(
            settings,
            check_id=check["check_id"],
            quantity=1_000,
            idempotency_key="confirmation-refresh-order",
            quote_service=_service(changed_quote),
            requested_at=datetime(2026, 7, 17, tzinfo=UTC),
            user_confirmation=_confirmation(check, quantity=1_000),
        )

    order = submit_user_paper_order(
        settings,
        check_id=check["check_id"],
        quantity=1_000,
        idempotency_key="confirmation-refresh-order",
        quote_service=_service(checked_quote),
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
        user_confirmation=_confirmation(check, quantity=1_000),
    )
    replay = submit_user_paper_order(
        settings,
        check_id=check["check_id"],
        quantity=1_000,
        idempotency_key="confirmation-refresh-order",
        quote_service=_service(changed_quote),
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
        user_confirmation=_confirmation(check, quantity=1_000),
    )

    assert replay["order_id"] == order["order_id"]
    forged_replay = _confirmation(check, quantity=1_000)
    forged_replay["source"] = "different_confirmation_source"
    with pytest.raises(ValueError, match="different confirmed order"):
        submit_user_paper_order(
            settings,
            check_id=check["check_id"],
            quantity=1_000,
            idempotency_key="confirmation-refresh-order",
            quote_service=_service(checked_quote),
            requested_at=datetime(2026, 7, 17, tzinfo=UTC),
            user_confirmation=forged_replay,
        )
    assert len(user_simulator_repository(settings).orders(account["account_id"])) == 1


def test_pending_sell_orders_reserve_sellable_quantity_and_release_on_cancel(
    tmp_path,
    monkeypatch,
):
    fixed_now = datetime(2026, 7, 20, 2, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    monkeypatch.setattr(simulator_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(simulator_persistence_module, "datetime", FrozenDateTime)
    settings = _settings(tmp_path, broad_limits=True)
    account = create_user_paper_account(
        settings,
        name="sell reservation",
        idempotency_key="sell-reservation-account",
    )
    trade_date = date(2026, 7, 17)
    quote = _quote(as_of=trade_date)
    buy = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol=quote.symbol,
        side="buy",
        quantity=1_000,
        quote_service=_service(quote),
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    buy_order = submit_user_paper_order(
        settings,
        check_id=buy["check_id"],
        quantity=1_000,
        idempotency_key="sell-reservation-buy",
        user_confirmation=_confirmation(buy, quantity=1_000),
    )
    settle_user_paper_order(
        settings,
        order_id=buy_order["order_id"],
        quote=quote,
        fill_key="sell-reservation-fill",
    )
    next_quote = _quote(price=10.5, as_of=date(2026, 7, 20))
    mark_user_paper_account(
        settings,
        account_id=account["account_id"],
        snapshot_date=next_quote.as_of,
        marks=[next_quote],
        benchmark_quote=MarketQuote(
            symbol="sh000300",
            asset_type=AssetType.INDEX,
            raw_price=4_000,
            as_of=next_quote.as_of,
            source="fixture",
            trade_lot=1,
            t_plus_one=False,
        ),
    )
    service = _service(next_quote)
    checks = [
        run_pretrade_check(
            settings,
            account_id=account["account_id"],
            symbol=next_quote.symbol,
            side="sell",
            quantity=800,
            quote_service=service,
            requested_at=datetime(2026, 7, 20, tzinfo=UTC),
        )
        for _ in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        orders = list(
            pool.map(
                lambda item: submit_user_paper_order(
                    settings,
                    check_id=item[1]["check_id"],
                    quantity=800,
                    idempotency_key=f"sell-reservation-order-{item[0]}",
                    user_confirmation=_confirmation(item[1], quantity=800),
                ),
                enumerate(checks),
            )
        )
    pending = next(item for item in orders if item["status"] == "pending")
    position = user_simulator_repository(settings).positions(account["account_id"])[0]
    assert position["reserved_sell_quantity"] == 800
    assert position["sellable_quantity"] == 200
    cancel_user_paper_order(settings, pending["order_id"])
    released = user_simulator_repository(settings).positions(account["account_id"])[0]
    assert released["reserved_sell_quantity"] == 0
    assert released["sellable_quantity"] == 1_000


def test_simulator_amount_research_and_committee_soft_governance_branches(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path, broad_limits=True)
    account = create_user_paper_account(
        settings,
        name="committee branches",
        idempotency_key="committee-branch-account",
    )
    quote = _quote()
    monkeypatch.setattr(
        simulator_module,
        "_research_context",
        lambda *_args, **_kwargs: {
            "run_id": "research-1",
            "decision": {"action": "buy", "reasons": ["quant"]},
            "reviewer_status": "approved",
            "supporting_evidence": ["quant"],
            "opposing_evidence": [],
            "invalidation_conditions": [],
            "data_gaps": [],
            "committee_decision": {
                "action": "buy",
                "suggested_weight_min": 0.05,
                "suggested_weight_max": 0.10,
            },
        },
    )
    supported = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol=quote.symbol,
        side="buy",
        amount=1_200,
        quote_service=_service(quote),
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    assert supported["requested_quantity"] == 100
    assert supported["llm_suggested_action"] == "buy"
    assert supported["llm_suggested_weight_range"] == [0.05, 0.1]

    monkeypatch.setattr(
        simulator_module,
        "_research_context",
        lambda *_args, **_kwargs: {
            "run_id": "research-2",
            "decision": {"action": "watch"},
            "reviewer_status": "approved",
            "supporting_evidence": [],
            "opposing_evidence": [],
            "invalidation_conditions": [],
            "data_gaps": [],
            "committee_decision": {
                "action": "observe",
                "suggested_weight_min": 0.0,
                "suggested_weight_max": 0.0,
            },
        },
    )
    vetoed = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol=quote.symbol,
        side="buy",
        quantity=100,
        quote_service=_service(quote),
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    assert vetoed["suggested_quantity"] == 0
    assert vetoed["suggested_action"] == "observe"


def _flow_block(scope: str, key: str, available_at: datetime) -> EvidenceBlock:
    return EvidenceBlock(
        domain=EvidenceDomain.CAPITAL_FLOW,
        title=f"{scope}:{key}",
        source="flow-fixture",
        methodology="point-in-time fixture",
        as_of=available_at,
        available_at=available_at,
        fetched_at=available_at,
        freshness="fresh",
        quality=EvidenceQuality.AVAILABLE,
        payload={"scope": scope, "scope_key": key, "flow_trend": {"5": 1.0}},
    )


def test_context_pack_rich_and_future_data_branches():
    as_of = date(2026, 7, 17)
    observed = datetime(2026, 7, 17, 8, tzinfo=UTC)
    cutoff = datetime(2026, 7, 17, 15, tzinfo=UTC)
    rich = context_module.assemble_analysis_context_pack(
        symbol="sh600001",
        asset_type="stock",
        as_of=as_of,
        cutoff_at=cutoff,
        market={
            "raw_price": 10.0,
            "source": "market",
            "as_of": observed,
            "available_at": observed,
            "raw_ohlcv": [{"date": "2026-07-17", "close": 10.0}],
            "session_status": "closed",
        },
        technical={
            "source": "technical",
            "as_of": observed,
            "available_at": observed,
            **{
                f"normalized_adjusted_close_path_{window}": [1.0, 1.1]
                for window in (20, 60, 120, 250)
            },
            "returns_adjusted_pct": {"20": 5.0},
            "risk_adjusted_pct": {"volatility": 20.0},
            "moving_averages_adjusted": {"20": 9.5},
        },
        market_flow=_flow_block("market", "cn_market", observed),
        industry_flow=_flow_block("industry", "technology", observed),
        stock_flow=_flow_block("stock", "sh600001", observed),
        financial={
            "source": "financial",
            "report_date": "2026-06-30",
            "disclosure_date": observed,
            "revenue_yoy": 0.1,
        },
        valuation={"source": "valuation", "as_of": observed, "pe_ttm": 15.0},
        events=[
            {
                "event_id": "event-1",
                "event_type": "announcement",
                "title": "result",
                "source": "exchange",
                "event_date": "2026-07-16",
                "available_at": observed,
            }
        ],
        macro={
            "source": "macro",
            "as_of": observed,
            "available_at": observed,
            "rate": 1.5,
            "missing_fields": ["credit"],
            "degraded": True,
        },
        portfolio={
            "cash": 50_000,
            "equity": 100_000,
            "positions": [{"symbol": "sh600001", "weight": 0.1}],
        },
        strategy={
            "symbol": "sh600001",
            "source": "strategy",
            "as_of": observed,
            "available_at": observed,
            "decision": {"action": "buy"},
        },
    )
    assert rich.quality_score >= 0.7
    assert rich.block(EvidenceDomain.FINANCIAL).quality.value == "available"
    assert rich.block(EvidenceDomain.MACRO).quality.value == "degraded"
    assert rich.deterministic_summary["current_weight"] == 0.1

    future = observed + timedelta(days=1)
    future_pack = context_module.assemble_analysis_context_pack(
        symbol="sh600001",
        asset_type="stock",
        as_of=as_of,
        cutoff_at=cutoff,
        market={"raw_price": 10, "source": "m", "available_at": future},
        technical={"source": "t", "available_at": future},
        market_flow=_flow_block("market", "cn_market", future),
        financial={"source": "f", "disclosure_date": future},
        macro={"source": "macro", "available_at": future},
    )
    assert future_pack.review_required is True
    assert any("after cutoff" in gap for gap in future_pack.critical_gaps)
    assert context_module.market_flow_block_from_radar(None, as_of) is None
    assert context_module.macro_evidence_from_radar(None, as_of) is None
    assert context_module._valuation_from_financial({}) is None


def test_build_analysis_context_pack_orchestration_and_persistence(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)

    class _Dumpable:
        def __init__(self, payload):
            self.payload = payload

        def model_dump(self, mode="python"):
            return self.payload

    quant = {
        "as_of": date(2026, 7, 17),
        "price": 10.0,
        "source": "quant-fixture",
        "available_at": datetime(2026, 7, 17, 8, tzinfo=UTC),
        "degraded_sources": [],
        "price_history": {
            "recent_raw_and_adjusted_bars_30": [{"date": "2026-07-17", "close": 10}],
            "normalized_adjusted_close_path_20": [1.0, 1.1],
            "returns_adjusted_pct": {"20": 5.0},
            "risk_adjusted_pct": {},
            "moving_averages_adjusted": {},
            "latest_signal_close_vs_moving_averages": {},
            "raw_market_ranges": {},
            "average_trading_amount": {},
        },
        "report": _Dumpable({"momentum_score": 1.0}),
    }
    monkeypatch.setattr(
        "quantlab.workflows.research.load_quant_report",
        lambda *_args, **_kwargs: quant,
    )
    monkeypatch.setattr(
        "quantlab.fundamentals.load_a_share_financial_report",
        lambda *_args, **_kwargs: _Dumpable(
            {
                "source": "financial-fixture",
                "as_of": "2026-06-30",
                "disclosure_date": "2026-07-10",
                "pe_ttm": 12.0,
                "market_cap": 1_000_000,
            }
        ),
    )
    monkeypatch.setattr(
        "quantlab.workflows.events.collect_all_events",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "quantlab.learning.LearningRepository.events_between",
        lambda *_args, **_kwargs: [
            {
                "event_id": "event",
                "event_type": "announcement",
                "title": "event",
                "source": "exchange",
                "event_date": "2026-07-16",
                "available_at": "2026-07-16T08:00:00+00:00",
            }
        ],
    )
    radar = {
        "as_of": "2026-07-17",
        "generated_at": "2026-07-17T08:00:00+00:00",
        "source": "radar-fixture",
        "instruments": [],
        "market_regime": "balanced",
        "risk_appetite": "neutral",
    }
    monkeypatch.setattr(
        "quantlab.workflows.radar.build_market_radar",
        lambda *_args, **_kwargs: radar,
    )
    monkeypatch.setattr(
        context_module,
        "build_live_stock_flow",
        lambda *_args, **_kwargs: _flow_block(
            "stock",
            "sh600001",
            datetime(2026, 7, 17, 8, tzinfo=UTC),
        ),
    )
    monkeypatch.setattr(
        "quantlab.workflows.notification_rules.evaluate_flow_notification_rules",
        lambda *_args, **_kwargs: [],
    )
    payload = context_module.build_analysis_context_pack(
        settings,
        symbol="sh600001",
        as_of=date(2026, 7, 17),
        asset_type="stock",
        include_events=True,
        save=True,
    )
    assert payload["symbol"] == "sh600001"
    assert EvidenceRepository(settings.resolve("quantlab.db")).context(
        payload["context_id"]
    )


def test_chat_registry_error_and_context_helper_branches(tmp_path):
    settings = _settings(tmp_path)
    first = create_user_paper_account(
        settings,
        name="chat-one",
        idempotency_key="chat-helper-account-one",
    )
    second = create_user_paper_account(
        settings,
        name="chat-two",
        idempotency_key="chat-helper-account-two",
    )
    conversation = chat_module.create_chat_conversation(
        settings,
        title="helpers",
        account_id=first["account_id"],
        symbol="sh600001",
        idempotency_key="chat-helper-conversation",
    )
    registry = chat_module.ChatToolRegistry(settings, conversation)
    with pytest.raises(ValueError, match="not registered"):
        registry.execute("unknown", {})
    with pytest.raises(ValueError, match="not registered"):
        registry.permission("unknown")
    with pytest.raises(PermissionError, match="cross-account"):
        registry.execute("query_account", {"account_id": second["account_id"]})
    unbound = chat_module.ChatToolRegistry(settings, {"account_id": None})
    with pytest.raises(ValueError, match="not bound"):
        unbound.execute("query_account", {})
    with pytest.raises(ValueError, match="not found"):
        chat_module.create_chat_conversation(
            settings,
            title="invalid",
            account_id="missing",
        )
    with pytest.raises(ValueError, match="not found"):
        chat_module.handle_chat_message(
            settings,
            conversation_id="missing",
            content="hello",
        )
    with pytest.raises(ValueError, match="4000"):
        chat_module.handle_chat_message(
            settings,
            conversation_id=conversation["conversation_id"],
            content="x" * 4_001,
        )
    with pytest.raises(ValueError, match="queued"):
        chat_module.handle_chat_message(
            settings,
            conversation_id=conversation["conversation_id"],
            content="hello",
            existing_user_message_id="missing",
        )
    with pytest.raises(ValueError, match="not found"):
        chat_module.confirm_chat_action(settings, action_id="missing", quantity=1)
    with pytest.raises(ValueError, match="not found"):
        chat_module.cancel_chat_action(settings, "missing")
    with pytest.raises(ValueError, match="unavailable"):
        chat_module._load_or_run_research(
            settings,
            "sh600001",
            None,
            False,
            "stock",
        )
    reviewer = chat_module._reviewer_summary(
        {
            "run_id": "r1",
            "payload": {
                "reports": {"reviewer": {"approved": False}},
                "decision": {"action": "avoid", "requires_human_review": True},
            },
        }
    )
    assert reviewer["action"] == "avoid"
    assert chat_module._resolve_symbol(settings, "600001", "") == "sh600001"
    assert chat_module._symbols_from_text("sh600001 sz000001") == [
        "sh600001",
        "sz000001",
    ]
    pack = _context_pack("sh510300")
    facts = chat_module._deterministic_context_facts([pack])
    assert facts
    with pytest.raises(ValueError, match="unavailable"):
        chat_module._require_context(None)
    context_payload = pack.model_dump(mode="json")
    domain = chat_module._context_domain(context_payload, "market")
    assert domain["status"] == "available"
    assert chat_module._frozen_conversation_context(settings, conversation, "sh600001") is None

    chat_repository = ChatRepository(settings.resolve("quantlab.db"))
    user_message = chat_repository.add_message(
        conversation_id=conversation["conversation_id"],
        role="user",
        content="tool error",
    )
    with pytest.raises(PermissionError):
        chat_module._execute_tool(
            chat_repository,
            registry,
            conversation["conversation_id"],
            user_message["message_id"],
            "query_account",
            {"account_id": second["account_id"]},
        )
    action = chat_repository.create_action(
        conversation_id=conversation["conversation_id"],
        message_id=user_message["message_id"],
        action_type="unsupported",
        account_id=first["account_id"],
        symbol="sh600001",
        research_run_id=None,
        check_id="none",
        draft_payload={},
        idempotency_key="chat-unsupported-action",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    with pytest.raises(ValueError, match="unsupported"):
        chat_module.confirm_chat_action(
            settings,
            action_id=action["action_id"],
            quantity=1,
        )
    chat_repository.update_action(action["action_id"], status="confirmed")
    with pytest.raises(ValueError, match="confirmed"):
        chat_module.cancel_chat_action(settings, action["action_id"])


def test_worker_training_daily_and_historical_handlers(settings, monkeypatch):
    monkeypatch.setattr(
        "quantlab.workflows.learning.train_learning_models",
        lambda *_args, **_kwargs: [{"model_id": "m1"}],
    )
    monkeypatch.setattr(
        "quantlab.workflows.daily.run_daily_cycle",
        lambda *_args, **_kwargs: {"daily": True},
    )

    def replay(*_args, **kwargs):
        kwargs["progress_callback"](
            {"completed": 1, "total": 2, "message": "half"}
        )
        return {"replay": True}

    monkeypatch.setattr(
        "quantlab.workflows.replay.run_historical_blind_replay",
        replay,
    )
    worker = JobWorker(settings, worker_id="coverage-worker")
    jobs = [
        (
            "training",
            {"horizon_days": 5, "asset_scope": "stock", "force": True},
        ),
        ("daily_cycle", {"as_of": "2026-07-17", "run_research": True}),
        (
            "historical_replay",
            {
                "start": "2026-01-01",
                "end": "2026-06-30",
                "horizon_days": 5,
                "episodes": 2,
                "save": False,
                "confirm_large_run": True,
            },
        ),
    ]
    for index, (job_type, payload) in enumerate(jobs):
        worker.repository.submit(
            job_type=job_type,
            payload=payload,
            idempotency_key=f"coverage-handler-{index}",
        )
    results = worker.run_until_empty(10)
    assert [item["status"] for item in results] == ["completed"] * 3


def test_mark_to_market_worker_isolates_one_account_failure(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    first = create_user_paper_account(
        settings,
        name="healthy",
        initial_capital=100_000.0,
        idempotency_key="mark-healthy-round4",
    )
    second = create_user_paper_account(
        settings,
        name="broken",
        initial_capital=100_000.0,
        idempotency_key="mark-broken-round4",
    )

    def fake_mark(_settings, *, account_id, snapshot_date=None, quote_service=None):
        if account_id == second["account_id"]:
            raise ValueError("account quote unavailable")
        return {
            "account_id": account_id,
            "snapshot_date": snapshot_date.isoformat(),
            "status": "marked",
        }

    monkeypatch.setattr(simulator_module, "mark_user_paper_account", fake_mark)
    worker = JobWorker(settings, worker_id="mark-isolation-round4")
    worker.repository.submit(
        job_type="mark_to_market",
        payload={"as_of": "2026-07-17"},
        idempotency_key="mark-isolation-round4",
    )

    result = worker.run_once()

    assert result["status"] == "completed"
    assert [item["account_id"] for item in result["result_payload"]["accounts"]] == [
        first["account_id"]
    ]
    assert result["result_payload"]["failures"] == [
        {
            "account_id": second["account_id"],
            "status": "failed",
            "error_type": "ValueError",
        }
    ]


def test_worker_research_handler_persists_bounded_result(settings, monkeypatch):
    output = {"decision_run": SimpleNamespace(run_id="research-handler-run")}
    monkeypatch.setattr(
        "quantlab.workflows.research.analyze_symbol",
        lambda *_args, **_kwargs: output,
    )
    monkeypatch.setattr(
        "quantlab.reporting.build_research_audit_package",
        lambda _output: {
            "decision": {"action": "observe"},
            "analysis_context_pack": {"context_id": "ctx-handler"},
        },
    )
    worker = JobWorker(settings, worker_id="research-handler-worker")
    worker.repository.submit(
        job_type="research",
        payload={
            "symbol": "sh600001",
            "as_of": "2026-07-17",
            "asset_type": "stock",
            "include_events": False,
            "save": False,
        },
        idempotency_key="research-handler-job",
    )
    result = worker.run_once()
    assert result["status"] == "completed"
    assert result["result_payload"]["action"] == "observe"


def test_trading_calendar_uses_imported_holiday_not_weekday_guess(tmp_path):
    settings = _settings(tmp_path)
    repository = JobRepository(settings.resolve(settings.get("system.database_path")))
    available_at = datetime.now(UTC) - timedelta(minutes=1)
    for day in range(1, 8):
        repository.upsert_trading_day(
            trade_date=date(2026, 10, day),
            is_open=False,
            source="exchange_calendar_fixture",
            available_at=available_at,
        )
    repository.upsert_trading_day(
        trade_date=date(2026, 10, 8),
        is_open=True,
        source="exchange_calendar_fixture",
        available_at=available_at,
    )
    calendar = TradingCalendarService(repository)
    assert calendar.next_open_day(date(2026, 9, 30)) == date(2026, 10, 8)
    assert calendar.add_open_days(date(2026, 9, 30), 1) == date(2026, 10, 8)


def test_public_forward_api_rejects_server_owned_times_predictions_and_results(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(api_module, "_settings", lambda: _settings(tmp_path))
    cohort = _request(
        "POST",
        "/api/forward-ablation/cohorts",
        payload={"frozen_at": "2020-01-01T00:00:00Z"},
    )
    assert cohort.status_code == 403
    sample = _request(
        "POST",
        "/api/forward-ablation/samples",
        payload={
            "cohort_id": "x",
            "symbol": "sh510300",
            "horizon_days": 5,
            "registered_at": "2020-01-01T00:00:00Z",
            "due_at": "2020-01-02T00:00:00Z",
            "predictions": [],
        },
    )
    assert sample.status_code == 422
    settlement = _request(
        "POST",
        "/api/forward-ablation/settlements",
        payload={
            "cohort_id": "x",
            "sample_key": "y",
            "horizon_days": 5,
            "realized_return_pct": 999,
            "observed_at": "2099-01-01T00:00:00Z",
        },
    )
    assert settlement.status_code == 422


class _AuthoritativeProvider:
    provider_name = "authoritative_fixture"
    provider_version = "v1"
    authoritative = True

    def __init__(self, quote: MarketQuote):
        self.value = quote

    def quote(self, symbol: str, *, asset_type: AssetType, as_of: date) -> MarketQuote:
        result = self.value.model_copy(deep=True)
        result.asset_type = asset_type
        result.authoritative = True
        result.source = self.provider_name
        result.provider = self.provider_name
        result.source_version = self.provider_version
        return result


class _UnavailableAuthoritativeProvider:
    provider_name = "unavailable_authoritative_fixture"
    provider_version = "v1"
    authoritative = True

    def quote(self, symbol: str, *, asset_type: AssetType, as_of: date) -> MarketQuote:
        raise ValueError(f"no authoritative quote for {symbol} on {as_of.isoformat()}")


def _context_pack(
    symbol: str = "sh510300",
    *,
    as_of: date = date(2026, 7, 17),
) -> AnalysisContextPack:
    observed = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)
    blocks = [
        EvidenceBlock(
            domain=domain,
            title=domain.value,
            source="round4_fixture",
            methodology="deterministic test evidence",
            as_of=observed,
            available_at=observed,
            fetched_at=observed,
            freshness="fresh",
            quality=EvidenceQuality.AVAILABLE,
            payload={"momentum_score": 2.0, "return_20_pct": 5.0},
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
        asset_type=AssetType.ETF,
        as_of=as_of,
        cutoff_at=observed,
        blocks=blocks,
        critical_gaps=[],
        deterministic_summary={"market_regime": "balanced"},
    )


def test_forward_freeze_persists_server_quote_context_and_prediction_fingerprints(tmp_path):
    settings = _settings(tmp_path)
    signal_date = datetime.now(UTC).date()
    cohort = create_round3_forward_cohort(settings)
    service = QuoteService(
        _AuthoritativeProvider(_quote(symbol="sh510300", price=4.0, as_of=signal_date))
    )

    def committee(*_args, **_kwargs):
        return {
            "action": "buy",
            "confidence": 0.7,
            "suggested_weight_max": 0.10,
            "degraded_roles": [],
        }

    rows = freeze_forward_ablation_sample(
        settings,
        cohort_id=cohort["cohort_id"],
        symbol="sh510300",
        horizon_days=5,
        quote_service=service,
        context_pack=_context_pack(as_of=signal_date),
        committee_runner=committee,
    )
    assert len(rows) == 7
    assert {row["quote_provider"] for row in rows} == {"authoritative_fixture"}
    assert all(row["start_price"] == 4.0 for row in rows)
    assert all(row["prediction_fingerprint"] for row in rows)
    assert all(row["governance_version"] for row in rows)
    assert all(datetime.fromisoformat(row["registered_at"]).tzinfo is not None for row in rows)
    repository = StrategyEvidenceRepository(settings.resolve("quantlab.db"))
    with pytest.raises(ValueError, match="cannot be in the future"):
        repository.settle_forward_sample(
            cohort_id=cohort["cohort_id"],
            sample_key=rows[0]["sample_key"],
            horizon_days=5,
            realized_return_pct=2.0,
            outcome_source="forged",
            observed_at=datetime.now(UTC) + timedelta(days=30),
        )
    repository.record_research_run(
        protocol_version="research-v1",
        strategy_type="historical",
        requested_range={"start": "2020-01-01", "end": "2020-12-31"},
        status="completed",
        passed=True,
        payload={"realized_return_pct": 999},
    )
    score = repository.forward_scorecard(
        cohort_id=cohort["cohort_id"],
        horizon_days=5,
    )
    assert score["variants"]["full_system"]["matured_samples"] == 0


def test_forward_settlement_worker_fetches_quote_and_calculates_outcome(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    signal_date = datetime.now(UTC).date()
    cohort = create_round3_forward_cohort(settings)
    start_service = QuoteService(
        _AuthoritativeProvider(_quote(symbol="sh510300", price=4.0, as_of=signal_date))
    )
    rows = freeze_forward_ablation_sample(
        settings,
        cohort_id=cohort["cohort_id"],
        symbol="sh510300",
        horizon_days=5,
        quote_service=start_service,
        context_pack=_context_pack(as_of=signal_date),
        committee_runner=lambda *_args, **_kwargs: {
            "action": "buy",
            "confidence": 0.7,
            "suggested_weight_max": 0.10,
            "degraded_roles": [],
        },
    )
    repository = StrategyEvidenceRepository(settings.resolve("quantlab.db"))
    with repository.connect() as db:
        db.execute(
            """
            UPDATE forward_ablation_predictions SET due_at=?
            WHERE cohort_id=? AND sample_key=? AND horizon_days=5
            """,
            (
                (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                cohort["cohort_id"],
                rows[0]["sample_key"],
            ),
        )
    end_quote = _quote(symbol="sh510300", price=4.4, as_of=datetime.now(UTC).date())
    end_quote.available_at = datetime.now(UTC) - timedelta(minutes=5)
    end_service = QuoteService(_AuthoritativeProvider(end_quote))
    monkeypatch.setattr(
        forward_module.QuoteService,
        "from_settings",
        classmethod(lambda _cls, _settings: end_service),
    )
    worker = JobWorker(settings, worker_id="forward-settlement-round4")
    worker.repository.submit(
        job_type="forward_settlement_scan",
        payload={"limit": 10},
        idempotency_key="forward-settlement-round4",
    )
    result = worker.run_once()
    assert result["status"] == "completed", result.get("error_detail")
    assert result["result_payload"]["settled"] == 1
    score = repository.forward_scorecard(
        cohort_id=cohort["cohort_id"],
        horizon_days=5,
    )
    assert score["variants"]["full_system"]["matured_samples"] == 1
    with repository.connect() as db:
        outcome = db.execute(
            """
            SELECT outcome_source,realized_return_pct,observed_at
            FROM forward_ablation_outcomes LIMIT 1
            """
        ).fetchone()
    assert "authoritative_fixture:v1" in outcome["outcome_source"]
    assert outcome["realized_return_pct"] == pytest.approx(10.0)
    assert datetime.fromisoformat(outcome["observed_at"]) >= datetime.now(UTC) - timedelta(minutes=1)


def test_forward_settlement_keeps_sample_pending_when_due_quote_is_unavailable(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    signal_date = datetime.now(UTC).date()
    cohort = create_round3_forward_cohort(settings)
    rows = freeze_forward_ablation_sample(
        settings,
        cohort_id=cohort["cohort_id"],
        symbol="sh510300",
        horizon_days=5,
        quote_service=QuoteService(
            _AuthoritativeProvider(
                _quote(symbol="sh510300", price=4.0, as_of=signal_date)
            )
        ),
        context_pack=_context_pack(as_of=signal_date),
        committee_runner=lambda *_args, **_kwargs: {
            "action": "buy",
            "confidence": 0.7,
            "suggested_weight_max": 0.10,
            "degraded_roles": [],
        },
    )
    repository = StrategyEvidenceRepository(settings.resolve("quantlab.db"))
    due_at = datetime.now(UTC) - timedelta(minutes=1)
    with repository.connect() as db:
        db.execute(
            """
            UPDATE forward_ablation_predictions SET due_at=?
            WHERE cohort_id=? AND sample_key=? AND horizon_days=5
            """,
            (due_at.isoformat(), cohort["cohort_id"], rows[0]["sample_key"]),
        )
    unavailable_service = QuoteService(_UnavailableAuthoritativeProvider())
    monkeypatch.setattr(
        forward_module.QuoteService,
        "from_settings",
        classmethod(lambda _cls, _settings: unavailable_service),
    )
    worker = JobWorker(settings, worker_id="forward-unavailable-round4")
    worker.repository.submit(
        job_type="forward_settlement_scan",
        payload={"limit": 10},
        idempotency_key="forward-unavailable-round4",
    )

    result = worker.run_once()

    assert result["status"] == "completed"
    assert result["result_payload"]["settled"] == 0
    assert result["result_payload"]["pending"] == 1
    pending = result["result_payload"]["results"][0]
    assert pending["status"] == "pending"
    assert pending["reason"].startswith("authoritative_quote_unavailable")
    score = repository.forward_scorecard(
        cohort_id=cohort["cohort_id"],
        horizon_days=5,
    )
    assert score["variants"]["full_system"]["matured_samples"] == 0


def test_capital_flow_worker_persists_market_and_industry_snapshots(
    settings,
    monkeypatch,
):
    generated = datetime.now(UTC).isoformat()
    radar = {
        "as_of": "2026-07-17",
        "generated_at": generated,
        "source": "radar_fixture",
        "risk_appetite": "neutral",
        "breadth": {"positive_share": 0.6},
        "leaders": ["sh510300"],
        "laggards": [],
        "instruments": [],
        "sectors": [
            {
                "name": "technology",
                "change_pct": 1.2,
                "turnover_pct": 3.4,
                "up_count": 20,
                "down_count": 10,
                "leader": "sh600001",
                "heat_score": 88.0,
            }
        ],
    }
    monkeypatch.setattr(
        "quantlab.workflows.radar.build_market_radar",
        lambda *_args, **_kwargs: radar,
    )
    worker = JobWorker(settings, worker_id="capital-round4")
    worker.repository.submit(
        job_type="capital_flow_refresh",
        payload={"as_of": "2026-07-17", "include_sectors": True},
        idempotency_key="capital-flow-round4",
    )
    result = worker.run_once()
    assert result["status"] == "completed", result.get("error_detail")
    evidence = EvidenceRepository(settings.resolve("quantlab.db"))
    assert evidence.flows("market", as_of="2026-07-17")
    industries = evidence.flows("industry", as_of="2026-07-17")
    assert industries[0]["payload"]["claim_boundary"].startswith("This snapshot")
    assert industries[0]["payload"]["flow_trend"]["5"]["status"] == "unavailable"


def test_capital_flow_get_is_query_only_and_refresh_requires_worker_job(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    monkeypatch.setattr(api_module, "_settings", lambda: settings)

    market = _request("GET", "/api/capital-flow/market?as_of=2026-07-17")
    stock = _request("GET", "/api/capital-flow/stocks/sh600001?as_of=2026-07-17")
    market_refresh = _request(
        "GET", "/api/capital-flow/market?as_of=2026-07-17&refresh=true"
    )
    stock_refresh = _request(
        "GET", "/api/capital-flow/stocks/sh600001?as_of=2026-07-17&refresh=true"
    )
    submitted = _request(
        "POST",
        "/api/capital-flow/refresh-jobs?as_of=2026-07-17&symbols=sh600001",
    )

    assert market.status_code == 200
    assert market.json()["quality"] == "unavailable"
    assert stock.status_code == 200
    assert stock.json()["quality"] == "unavailable"
    assert market_refresh.status_code == 409
    assert stock_refresh.status_code == 409
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "queued"
    assert submitted.json()["payload"]["symbols"] == ["sh600001"]


def test_capital_flow_refresh_persists_unavailable_and_last_success(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    evidence = EvidenceRepository(settings.resolve("quantlab.db"))
    previous_at = datetime(2026, 7, 16, 7, tzinfo=UTC)
    for scope, key in (("market", "cn_market"), ("stock", "sh600001")):
        evidence.save_flow(
            EvidenceBlock(
                domain=EvidenceDomain.CAPITAL_FLOW,
                title=f"previous {scope}",
                source="licensed_previous",
                methodology="confirmed previous snapshot",
                as_of=previous_at,
                available_at=previous_at,
                quality=EvidenceQuality.AVAILABLE,
                payload={"scope": scope, "scope_key": key, "status": "available"},
            )
        )
    TerminalRepository(settings.resolve("quantlab.db")).upsert_watchlist(
        "sh600001", "fixture", "audit", ""
    )

    def fail_radar(*_args, **_kwargs):
        raise RuntimeError("radar unavailable")

    def fail_stock(*_args, **_kwargs):
        raise RuntimeError("stock flow unavailable")

    monkeypatch.setattr("quantlab.workflows.radar.build_market_radar", fail_radar)
    monkeypatch.setattr("quantlab.workflows.capital_flow.build_live_stock_flow", fail_stock)
    worker = JobWorker(settings, worker_id="capital-unavailable-audit")
    worker.repository.submit(
        job_type="capital_flow_refresh",
        payload={"as_of": "2026-07-17", "include_sectors": True},
        idempotency_key="capital-unavailable-audit",
    )

    result = worker.run_once()

    assert result["status"] == "completed"
    assert result["result_payload"]["degraded"] is True
    market = evidence.flows("market", scope_key="cn_market", as_of="2026-07-17")[0]
    stock = evidence.flows("stock", scope_key="sh600001", as_of="2026-07-17")[0]
    industry = evidence.flows("industry", scope_key="all", as_of="2026-07-17")[0]
    assert market["quality"] == "unavailable"
    assert stock["quality"] == "unavailable"
    assert industry["quality"] == "unavailable"
    assert datetime.fromisoformat(
        market["payload"]["last_success_at"].replace("Z", "+00:00")
    ) == previous_at
    assert datetime.fromisoformat(
        stock["payload"]["last_success_at"].replace("Z", "+00:00")
    ) == previous_at


class _CommitteeProvider(LLMProvider):
    provider_name = "committee-fixture"
    model = "committee-model"

    async def structured(self, system: str, prompt: str, schema):
        if schema is CommitteeRoleOpinion:
            role = next(
                item
                for item in ("technical", "capital_flow", "portfolio_risk")
                if item in system
            )
            stance = (
                "bullish"
                if role == "technical"
                else "bearish"
                if role == "capital_flow"
                else "neutral"
            )
            return CommitteeRoleOpinion(
                role=role,
                stance=stance,
                confidence=1.0,
                importance=1.0,
                summary=role,
            )
        return CommitteeDecision(
            action="observe",
            confidence=0.5,
            deterministic_max_weight=0.15,
            context_id="placeholder",
            context_version="2.0",
            context_fingerprint="placeholder",
        )


class _SlowCountingCommitteeProvider(_CommitteeProvider):
    def __init__(self):
        self.calls = 0

    async def structured(self, system: str, prompt: str, schema):
        self.calls += 1
        await asyncio.sleep(0.05)
        return await super().structured(system, prompt, schema)


def test_concurrent_committee_idempotency_does_not_duplicate_paid_calls(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    base = _SlowCountingCommitteeProvider()
    pack = _context_pack()
    monkeypatch.setattr(committee_module, "build_provider", lambda _config: base)

    def run_once():
        return committee_module.run_context_committee(
            settings,
            pack=pack,
            deterministic_max_weight=0.15,
            idempotency_key="concurrent-committee-audit",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: run_once(), range(2)))

    assert results[0]["governance_version"] == results[1]["governance_version"]
    assert base.calls == 4


def test_promoted_role_policy_changes_real_committee_aggregation(tmp_path):
    settings = _settings(tmp_path)
    pack = _context_pack()
    before = asyncio.run(
        run_context_committee_with_provider(
            settings,
            pack=pack,
            deterministic_max_weight=0.15,
            provider=_CommitteeProvider(),
        )
    )
    for index in range(30):
        record_role_outcome(
            settings,
            role="technical",
            run_id=f"role-{index}",
            symbol="sh510300",
            as_of="2026-07-17",
            horizon_days=5,
            probabilities={"up": 0.7, "flat": 0.2, "down": 0.1},
            realized_direction="up",
            realized_return_pct=2.0,
        )
    challenge = freeze_role_challenge(settings, "technical")
    decide_role_challenge(
        settings,
        challenge["challenge_id"],
        passed=True,
        decision="promote",
        reason="frozen challenge passed",
    )
    after = asyncio.run(
        run_context_committee_with_provider(
            settings,
            pack=pack,
            deterministic_max_weight=0.15,
            provider=_CommitteeProvider(),
        )
    )
    assert before.role_weights["technical"] == 1.0
    assert after.role_weights["technical"] == 1.25
    assert after.governance_version != before.governance_version
    assert after.governance_aggregate_score > before.governance_aggregate_score


def test_promoted_role_policy_is_scoped_to_mature_market_regimes(tmp_path):
    settings = _settings(tmp_path)
    for index in range(30):
        record_role_outcome(
            settings,
            role="macro",
            run_id=f"macro-bull-{index}",
            symbol="sh510300",
            as_of="2026-07-17",
            horizon_days=5,
            probabilities={"up": 0.7, "flat": 0.2, "down": 0.1},
            realized_direction="up",
            realized_return_pct=2.0,
            market_regime="bull",
        )
    challenge = freeze_role_challenge(settings, "macro")
    with pytest.raises(ValueError, match="insufficient matured samples"):
        decide_role_challenge(
            settings,
            challenge["challenge_id"],
            passed=True,
            decision="promote",
            reason="wrong regime",
            applicable_regimes=["bear"],
        )
    decide_role_challenge(
        settings,
        challenge["challenge_id"],
        passed=True,
        decision="promote",
        reason="bull regime challenge passed",
        applicable_regimes=["bull"],
    )
    repository = EvidenceRepository(settings.resolve("quantlab.db"))
    bull = repository.active_role_policy(["macro"], market_regime="bull")
    bear = repository.active_role_policy(["macro"], market_regime="bear")
    assert bull["roles"]["macro"]["weight"] == 1.25
    assert bull["roles"]["macro"]["minimum_samples"] == 30
    assert bull["roles"]["macro"]["applicable_regimes"] == ["bull"]
    assert bear["roles"]["macro"]["weight"] == 1.0


class _FailingChatProvider(LLMProvider):
    provider_name = "chat-failure"
    model = "chat-failure"

    async def structured(self, system: str, prompt: str, schema):
        raise RuntimeError("provider unavailable")


class _CapitalOnlyChatProvider(LLMProvider):
    provider_name = "chat-capital-only"
    model = "chat-capital-only"

    def __init__(self, capital_block_id: str):
        self.capital_block_id = capital_block_id

    async def structured(self, system: str, prompt: str, schema):
        return ChatEvidenceAnswer(
            answer="capital only",
            facts=[],
            evidence_refs=[self.capital_block_id, "invalid-block"],
            suggested_action="buy",
            suggested_weight_min=0.2,
            suggested_weight_max=0.8,
            requires_user_review=False,
        )


def test_chat_context_answer_provider_failure_and_capital_only_guard(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    pack = _context_pack()
    monkeypatch.setattr(
        chat_module,
        "build_provider",
        lambda _settings: _FailingChatProvider(),
    )
    fallback, _ = chat_module._answer_context_question(
        settings,
        packs=[pack],
        question="why",
        task_id="chat-fallback",
    )
    assert fallback.suggested_action == "review_required"
    assert fallback.facts
    capital = pack.block(EvidenceDomain.CAPITAL_FLOW)
    monkeypatch.setattr(
        chat_module,
        "build_provider",
        lambda _settings: _CapitalOnlyChatProvider(capital.block_id),
    )
    guarded, _ = chat_module._answer_context_question(
        settings,
        packs=[pack],
        question="buy?",
        task_id="chat-capital-only",
    )
    assert guarded.suggested_action == "review_required"
    assert guarded.suggested_weight_max == 0.0
    assert "invalid_evidence_references_removed" in guarded.missing_data
    assert "capital_flow_alone_cannot_generate_buy" in guarded.missing_data


def test_chat_research_loader_and_symbol_search_branches(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    class _FakeDecisionRepository:
        @staticmethod
        def _record(run_id, symbol):
            as_of = date.today().isoformat()
            identity = {
                "run_id": run_id,
                "symbol": symbol,
                "requested_as_of": as_of,
                "effective_as_of": as_of,
                "origin": "user_interactive_research",
                "evidence_stage": "research_only",
            }
            return {
                **identity,
                "as_of": as_of,
                "payload": {
                    "decision": {"symbol": symbol, "as_of": as_of},
                    "research_context": {
                        "analysis_context_pack": {
                            "symbol": symbol,
                            "as_of": as_of,
                            "asset_type": "stock",
                        }
                    },
                    "research_identity": identity,
                },
            }

        records = {"saved": _record.__func__("saved", "sh600001")}

        def __init__(self, _path):
            pass

        def get(self, run_id):
            return self.records.get(run_id)

        def latest_for_symbol(self, symbol):
            return next(
                (item for item in self.records.values() if item.get("symbol") == symbol),
                None,
            )

        def save(self, run, _payload, **_kwargs):
            self.records[run.run_id] = self._record(run.run_id, "sh600002")

    monkeypatch.setattr(chat_module, "DecisionRepository", _FakeDecisionRepository)
    existing = chat_module._load_or_run_research(
        settings,
        "sh600001",
        "saved",
        False,
        "stock",
    )
    assert existing["run_id"] == "saved"
    monkeypatch.setattr(
        chat_module,
        "analyze_symbol",
        lambda *_args, **_kwargs: {
            "decision_run": SimpleNamespace(run_id="generated")
        },
    )
    monkeypatch.setattr(chat_module, "research_persistence_context", lambda _output: {})
    generated = chat_module._load_or_run_research(
        settings,
        "sh600002",
        None,
        True,
        "stock",
    )
    assert generated["run_id"] == "generated"
    monkeypatch.setattr(
        chat_module,
        "search_stocks",
        lambda *_args, **_kwargs: {"results": [{"symbol": "sh600001", "name": "alpha"}]},
    )
    assert chat_module._resolve_symbol(settings, None, "alpha") == "sh600001"
    monkeypatch.setattr(
        chat_module,
        "search_stocks",
        lambda *_args, **_kwargs: {
            "results": [
                {"symbol": "sh600001", "name": "alpha"},
                {"symbol": "sh600002", "name": "beta"},
            ]
        },
    )
    assert chat_module._resolve_symbol(settings, None, "please inspect beta") == "sh600002"


class _CountingProvider(LLMProvider):
    provider_name = "counting"

    def __init__(self, model: str):
        self.model = model
        self.calls = 0

    async def structured(self, system: str, prompt: str, schema):
        self.calls += 1
        return CommitteeRoleOpinion(
            role="technical",
            stance="neutral",
            confidence=0.5,
            summary=self.model,
        )


def test_llm_cache_isolated_by_provider_model_and_governance(tmp_path):
    repository = EvidenceRepository(tmp_path / "cache.db")
    first_base = _CountingProvider("model-a")
    second_base = _CountingProvider("model-b")
    first = GovernedLLMProvider(
        first_base,
        repository,
        context_id="ctx",
        context_fingerprint="fingerprint",
        task_id="task-a",
        prompt_version="p1",
        schema_version="s1",
        governance_version="g1",
    )
    second = GovernedLLMProvider(
        second_base,
        repository,
        context_id="ctx",
        context_fingerprint="fingerprint",
        task_id="task-b",
        prompt_version="p1",
        schema_version="s1",
        governance_version="g1",
    )
    asyncio.run(first.structured("You are the technical member", "same", CommitteeRoleOpinion))
    asyncio.run(second.structured("You are the technical member", "same", CommitteeRoleOpinion))
    assert first_base.calls == 1
    assert second_base.calls == 1


def test_failed_dependency_propagates_to_equivalent_blocked_terminal(tmp_path):
    repository = JobRepository(tmp_path / "jobs.db")
    upstream = repository.submit(
        job_type="upstream",
        payload={},
        idempotency_key="upstream",
        max_attempts=1,
    )
    downstream = repository.submit(
        job_type="downstream",
        payload={},
        idempotency_key="downstream",
        dependency_job_ids=[upstream["job_id"]],
    )
    repository.claim(worker_id="worker")
    repository.fail(upstream["job_id"], "worker", "boom", retryable=False)
    blocked = repository.job(downstream["job_id"])
    assert blocked["status"] == "failed"
    assert blocked["error_code"] == "dependency_blocked"
    assert "blocked" in {event["event_type"] for event in repository.events(downstream["job_id"])}


def test_worker_heartbeat_prevents_duplicate_claim_and_cancel_is_cooperative(settings):
    started = __import__("threading").Event()
    release = __import__("threading").Event()

    def long_handler(_context, _payload):
        started.set()
        release.wait(timeout=3)
        return {"side_effect": "done"}

    worker = JobWorker(
        settings,
        worker_id="lease-owner",
        handlers={"long": long_handler},
    )
    job = worker.repository.submit(
        job_type="long",
        payload={},
        idempotency_key="long-running",
        timeout_seconds=1,
        max_attempts=2,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.run_once)
        assert started.wait(timeout=2)
        time.sleep(1.2)
        assert worker.repository.recover_stale()["recovered"] == 0
        assert worker.repository.claim(worker_id="second-worker") is None
        requested = worker.repository.cancel(job["job_id"], "user requested")
        assert requested["status"] == "running"
        assert requested["cancel_requested"] is True
        release.set()
        result = future.result(timeout=5)
    assert result["status"] == "cancelled"
    assert result["error_code"] == "cancelled_after_side_effect"


def test_worker_crash_after_side_effect_start_is_not_reexecuted(settings):
    repository = JobRepository(settings.resolve("quantlab.db"))
    job = repository.submit(
        job_type="unsafe",
        payload={},
        idempotency_key="unsafe-side-effect",
        timeout_seconds=1,
        max_attempts=2,
        available_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    claimed = repository.claim(
        worker_id="crashed-worker",
        now=datetime.now(UTC) - timedelta(seconds=5),
    )
    assert claimed["job_id"] == job["job_id"]
    repository.mark_side_effect_state(job["job_id"], "crashed-worker", "started")
    assert repository.recover_stale()["recovered"] == 1
    calls = []
    worker = JobWorker(
        settings,
        worker_id="replacement-worker",
        handlers={"unsafe": lambda _context, _payload: calls.append(1) or {}},
    )
    result = worker.run_once()
    assert result["status"] == "failed"
    assert result["error_code"] == "side_effect_outcome_unknown"
    assert calls == []


def test_unified_migrations_initialize_repeat_and_upgrade_old_job_schema(tmp_path):
    path = tmp_path / "fresh.db"
    first = initialize_or_upgrade_database(path)
    second = initialize_or_upgrade_database(path)
    assert list(dict.fromkeys(item["component"] for item in first["migrations"])) == list(
        COMPONENT_ORDER
    )
    assert any(
        item["component"] == "evidence" and item["version"] == 7
        for item in first["migrations"]
    )
    assert second["migrations"] == first["migrations"]

    old_path = tmp_path / "old.db"
    JobRepository(old_path)
    with sqlite3.connect(old_path) as db:
        db.execute("ALTER TABLE background_jobs DROP COLUMN side_effect_state")
        db.execute("DELETE FROM schema_migrations WHERE version=5")
        db.execute(
            "DELETE FROM quantlab_migration_registry WHERE component='jobs' AND version=5"
        )
    upgraded = JobRepository(old_path)
    with upgraded.connect() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(background_jobs)")}
    assert "side_effect_state" in columns
    assert upgraded.schema_status()["current_version"] == 6

    tampered = tmp_path / "tampered.db"
    EvidenceRepository(tampered)
    with sqlite3.connect(tampered) as db:
        db.execute(
            """
            UPDATE quantlab_migration_registry SET checksum='bad'
                WHERE component='evidence' AND version=6
            """
        )
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        EvidenceRepository(tampered)


def test_new_component_migration_backs_up_database_after_round5_is_installed(tmp_path):
    path = tmp_path / "incremental.db"
    initialize_or_upgrade_database(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "DELETE FROM quantlab_migration_registry WHERE component='evidence' AND version=7"
        )
        db.execute("CREATE TABLE IF NOT EXISTS user_preserved_data(value TEXT)")
        db.execute("INSERT INTO user_preserved_data(value) VALUES('keep-me')")

    upgraded = initialize_or_upgrade_database(path)

    assert upgraded["pre_upgrade_backup"] is not None
    assert "evidence:7" in upgraded["pre_upgrade_backup"]["pending_migrations"]
    with sqlite3.connect(upgraded["pre_upgrade_backup"]["database"]) as backup:
        assert backup.execute("SELECT value FROM user_preserved_data").fetchone()[0] == "keep-me"
