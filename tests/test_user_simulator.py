from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest

import quantlab.workflows.simulator as simulator_workflow
import quantlab.persistence.simulator as simulator_persistence
from quantlab.config import Settings
from quantlab.domain import AssetType, DataQuality, MarketQuote
from quantlab.market import QuoteService, TradingCalendarService
from quantlab.persistence import (
    PaperTradingRepository,
    TerminalRepository,
    UserPaperTradingRepository,
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


def _settings(tmp_path) -> Settings:
    return Settings(
        values={
            "system": {
                "database_path": "quantlab.db",
                "data_dir": "data",
                "initial_capital": 100_000.0,
                "test_mode": True,
            },
            "risk": {
                "max_total_exposure": 0.80,
                "max_single_position": 0.15,
                "max_industry_exposure": 0.30,
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
            "strategies": {"etf_rotation": {"universe": ["sh510300"]}},
        },
        root=tmp_path,
    )


def _quote(
    *,
    symbol: str = "sh600001",
    price: float = 10.0,
    as_of: date = date(2026, 7, 17),
    suspended: bool = False,
    limit_up: bool = False,
    limit_down: bool = False,
    session_status: str = "open",
    data_quality: DataQuality = DataQuality.AVAILABLE,
) -> MarketQuote:
    return MarketQuote(
        symbol=symbol,
        name="测试股票",
        asset_type=AssetType.STOCK,
        raw_price=price,
        as_of=as_of,
        available_at=datetime.combine(as_of, datetime.min.time(), tzinfo=UTC),
        source="fixture",
        data_quality=data_quality,
        suspended=suspended,
        limit_up=limit_up,
        limit_down=limit_down,
        industry="制造",
        trade_lot=100,
        t_plus_one=True,
        session_status=session_status,
        quote_kind="realtime" if session_status == "open" else "current_close",
        evidence_stage="test",
        risk_metadata={
            "risk_check_complete": True,
            "financial_check_complete": True,
            "financial_quality_score": 0.8,
            "listing_days": 1000,
        },
    )


class _AuthoritativeFixtureQuoteProvider:
    provider_name = "authoritative_fixture_quote_provider"
    provider_version = "test-v1"
    authoritative = True

    def __init__(self, quote: MarketQuote):
        self._quote = quote

    def quote(self, symbol: str, *, asset_type, as_of: date) -> MarketQuote:
        if symbol != self._quote.symbol or as_of < self._quote.as_of:
            raise ValueError("fixture quote is unavailable")
        return self._quote.model_copy(
            update={
                "asset_type": asset_type,
                "authoritative": True,
            }
        )


def _account(settings: Settings, key: str = "account-create-0001") -> dict:
    return create_user_paper_account(
        settings,
        name="我的模拟账户",
        idempotency_key=key,
    )


def test_pretrade_uses_configured_market_date_at_utc_midnight_boundary(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path).with_overrides(
        {"system": {"timezone": "Asia/Shanghai"}}
    )
    account = _account(settings, "market-midnight-account")
    captured = {}

    def quote_loader(_settings, symbol, *, asset_type=None, as_of=None, quote_service=None):
        captured["as_of"] = as_of
        return _quote(symbol=symbol, as_of=date(2026, 7, 20))

    monkeypatch.setattr(simulator_workflow, "load_latest_trade_quote", quote_loader)

    check = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol="sh600001",
        side="buy",
        quantity=100,
        requested_at=datetime(2026, 7, 19, 16, 30, tzinfo=UTC),
    )

    assert captured["as_of"] == date(2026, 7, 20)
    assert check["quote"]["as_of"] == "2026-07-20"


def _submit(
    settings: Settings,
    account_id: str,
    *,
    side: str,
    quantity: int,
    quote: MarketQuote,
    key: str,
) -> dict:
    quote_service = QuoteService(_AuthoritativeFixtureQuoteProvider(quote))
    simulation_mode = (
        "next_open_simulation"
        if quote.session_status == "closed"
        else "intraday_simulation"
    )
    check = run_pretrade_check(
        settings,
        account_id=account_id,
        symbol=quote.symbol,
        side=side,
        quantity=quantity,
        quote_service=quote_service,
        requested_at=datetime.combine(quote.as_of, datetime.min.time(), tzinfo=UTC),
    )
    return submit_user_paper_order(
        settings,
        check_id=check["check_id"],
        quantity=quantity,
        idempotency_key=key,
        requested_at=datetime.combine(quote.as_of, datetime.min.time(), tzinfo=UTC),
        quote_service=quote_service,
        user_confirmation=_confirmation(
            check,
            account_id=account_id,
            quantity=quantity,
            simulation_mode=simulation_mode,
            close_reference_acknowledged=(simulation_mode == "next_open_simulation"),
        ),
    )


def _confirmation(
    check: dict,
    *,
    account_id: str,
    quantity: int,
    source: str = "pytest_user_confirmation",
    simulation_mode: str = "intraday_simulation",
    close_reference_acknowledged: bool = False,
) -> dict:
    return {
        "confirmed": True,
        "check_id": check["check_id"],
        "account_id": account_id,
        "symbol": check["symbol"],
        "side": check["side"],
        "quantity": quantity,
        "source": source,
        "simulation_mode": simulation_mode,
        "close_reference_acknowledged": close_reference_acknowledged,
    }


def test_order_expiry_uses_open_sessions_instead_of_calendar_days(tmp_path):
    settings = _settings(tmp_path)
    calendar = TradingCalendarService.from_settings(settings)
    records = []
    cursor = date(2026, 9, 30)
    while cursor <= date(2026, 10, 20):
        records.append(
            {
                "trade_date": cursor.isoformat(),
                "is_open": cursor >= date(2026, 10, 8) and cursor.weekday() < 5
                or cursor == date(2026, 9, 30),
            }
        )
        cursor += timedelta(days=1)
    calendar.ingest(
        records,
        namespace="production",
        trust_level="server_observed",
        provider="calendar_fixture",
        source="calendar_fixture",
        endpoint="fixture",
        source_version="v1",
        available_at=datetime(2026, 7, 1, tzinfo=UTC),
        license_status="fixture",
        raw_fingerprint="golden-week-calendar",
    )
    account = _account(settings, "holiday-expiry-account")

    order = _submit(
        settings,
        account["account_id"],
        side="buy",
        quantity=100,
        quote=_quote(as_of=date(2026, 9, 30), session_status="closed"),
        key="holiday-expiry-order",
    )

    assert order["eligible_trade_date"] == "2026-10-08"
    assert datetime.fromisoformat(order["expires_at"]).date() >= date(2026, 10, 15)


def test_three_account_types_are_strictly_isolated(tmp_path):
    settings = _settings(tmp_path)
    user = _account(settings)
    system = PaperTradingRepository(tmp_path / "quantlab.db")
    system.ensure_account("strategy_shadow", "影子盘", "fixed")
    manual = TerminalRepository(tmp_path / "quantlab.db")

    assert user["account_type"] == "user_paper"
    assert user["evidence_eligible"] is False
    assert system.accounts()[0]["account_type"] == "system_shadow"
    assert system.accounts()[0]["evidence_eligible"] is True
    assert user["account_id"] not in {item["account_id"] for item in system.accounts()}
    manual_overview = manual.portfolio_overview()
    assert manual_overview["account_type"] == "manual_real_ledger"
    assert manual_overview["evidence_eligible"] is False
    assert user_simulator_repository(settings).performance(user["account_id"])[
        "evidence_eligible"
    ] is False


def test_buy_partial_fill_add_reduce_clear_t1_and_restart_consistency(
    tmp_path,
    monkeypatch,
):
    fixed_now = datetime(2026, 7, 17, 2, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    monkeypatch.setattr(simulator_workflow, "datetime", FrozenDateTime)
    monkeypatch.setattr(simulator_persistence, "datetime", FrozenDateTime)
    settings = _settings(tmp_path)
    account = _account(settings)
    quote = _quote()
    order = _submit(
        settings,
        account["account_id"],
        side="buy",
        quantity=1_000,
        quote=quote,
        key="buy-order-0001",
    )
    assert order["context_id"]
    assert order["context_version"] == "2.0"
    assert order["context_fingerprint"]
    first = settle_user_paper_order(
        settings,
        order_id=order["order_id"],
        quote=quote,
        fill_quantity=400,
        fill_key="fill-buy-0001",
    )
    assert first["order"]["status"] == "partially_filled"
    repeated = settle_user_paper_order(
        settings,
        order_id=order["order_id"],
        quote=quote,
        fill_quantity=400,
        fill_key="fill-buy-0001",
    )
    assert repeated["idempotent"] is True
    second = settle_user_paper_order(
        settings,
        order_id=order["order_id"],
        quote=quote,
        fill_quantity=600,
        fill_key="fill-buy-0002",
    )
    assert second["order"]["status"] == "filled"
    repository = UserPaperTradingRepository(tmp_path / "quantlab.db")
    position = repository.positions(account["account_id"])[0]
    assert position["quantity"] == 1_000
    assert position["frozen_quantity"] == 1_000
    assert len(repository.fills(account["account_id"])) == 2

    same_day_sell = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol=quote.symbol,
        side="sell",
        quantity=100,
        quote=quote,
        requested_at=datetime.combine(quote.as_of, datetime.min.time(), tzinfo=UTC),
    )
    assert same_day_sell["allowed_to_submit"] is False
    assert "t_plus_one_or_insufficient_position" in same_day_sell["hard_failures"]

    next_day = date(2026, 7, 20)
    mark_user_paper_account(
        settings,
        account_id=account["account_id"],
        snapshot_date=next_day,
        marks=[_quote(price=11.0, as_of=next_day)],
        benchmark_quote=MarketQuote(
            symbol="sh000300",
            asset_type=AssetType.INDEX,
            raw_price=4_000,
            as_of=next_day,
            source="fixture",
            trade_lot=1,
            t_plus_one=False,
        ),
    )
    position = repository.positions(account["account_id"])[0]
    assert position["frozen_quantity"] == 0
    reduce_quote = _quote(price=11.0, as_of=next_day)
    reduce_order = _submit(
        settings,
        account["account_id"],
        side="sell",
        quantity=200,
        quote=reduce_quote,
        key="sell-order-0001",
    )
    settle_user_paper_order(
        settings,
        order_id=reduce_order["order_id"],
        quote=reduce_quote,
        fill_key="fill-sell-0001",
    )
    overview = UserPaperTradingRepository(tmp_path / "quantlab.db").overview(
        account["account_id"]
    )
    assert overview["positions"][0]["quantity"] == 800
    assert "market_value" in overview["positions"][0]
    assert "unrealized_pnl" in overview["positions"][0]
    assert "today_pnl" in overview["positions"][0]
    assert overview["realized_pnl"] > 0

    clear_order = _submit(
        settings,
        account["account_id"],
        side="sell",
        quantity=800,
        quote=reduce_quote,
        key="sell-order-0002",
    )
    settle_user_paper_order(
        settings,
        order_id=clear_order["order_id"],
        quote=reduce_quote,
        fill_key="fill-sell-0002",
    )
    restarted = UserPaperTradingRepository(tmp_path / "quantlab.db")
    assert restarted.positions(account["account_id"]) == []
    assert len(restarted.fills(account["account_id"])) == 4
    assert restarted.overview(account["account_id"])["cash"] > 100_000


@pytest.mark.parametrize(
    ("quote", "reason"),
    [
        (_quote(suspended=True), "suspended_or_missing"),
        (_quote(limit_up=True), "limit_up"),
        (
            _quote(
                as_of=date(2026, 7, 10),
                data_quality=DataQuality.STALE,
            ),
            "market_data_stale",
        ),
    ],
)
def test_pretrade_rejects_suspension_limit_and_stale_data(tmp_path, quote, reason):
    settings = _settings(tmp_path)
    account = _account(settings)
    check = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol=quote.symbol,
        side="buy",
        quantity=100,
        quote=quote,
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    assert check["allowed_to_submit"] is False
    assert reason in check["hard_failures"]


def test_research_failure_degrades_but_cannot_relax_deterministic_risk(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    account = _account(settings)

    def failed_research(*_args, **_kwargs):
        raise RuntimeError("provider failed with sensitive internal detail")

    monkeypatch.setattr(simulator_workflow, "_research_context", failed_research)
    available = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol="sh600001",
        side="buy",
        quantity=100,
        quote=_quote(),
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    assert available["hard_risk_passed"] is True
    assert available["allowed_to_submit"] is True
    assert available["reviewer_status"] == "unavailable"
    assert "deterministic checks still completed" in available["data_gaps"][0]
    assert "sensitive internal detail" not in str(available)

    monkeypatch.setattr(
        simulator_workflow,
        "_research_context",
        lambda *_args, **_kwargs: {
            "run_id": "research-buy",
            "decision": {"action": "buy", "reasons": ["LLM strongly supports buying"]},
            "reviewer_status": "approved",
            "supporting_evidence": ["LLM strongly supports buying"],
            "opposing_evidence": [],
            "invalidation_conditions": [],
            "data_gaps": [],
        },
    )
    blocked = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol="sh600001",
        side="buy",
        quantity=100,
        quote=_quote(limit_up=True),
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    assert blocked["suggested_action"] == "buy"
    assert blocked["allowed_to_submit"] is False
    assert blocked["hard_risk_passed"] is False
    assert "limit_up" in blocked["hard_failures"]


def test_rejected_cancelled_expired_and_non_trading_orders_are_persisted(tmp_path):
    settings = _settings(tmp_path)
    account = _account(settings)
    invalid = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol="sh600001",
        side="buy",
        quantity=50,
        quote=_quote(),
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    rejected = submit_user_paper_order(
        settings,
        check_id=invalid["check_id"],
        quantity=50,
        idempotency_key="rejected-order-0001",
        user_confirmation=_confirmation(
            invalid,
            account_id=account["account_id"],
            quantity=50,
        ),
    )
    assert rejected["status"] == "rejected"
    assert "invalid_trade_lot" in rejected["rejection_reason"]

    closed_quote = _quote(session_status="closed")
    pending = _submit(
        settings,
        account["account_id"],
        side="buy",
        quantity=100,
        quote=closed_quote,
        key="pending-order-0001",
    )
    assert pending["eligible_trade_date"] == "2026-07-20"
    with pytest.raises(ValueError, match="eligible trade date"):
        settle_user_paper_order(
            settings,
            order_id=pending["order_id"],
            quote=closed_quote,
            fill_key="too-early-fill",
        )
    cancelled = cancel_user_paper_order(settings, pending["order_id"])
    assert cancelled["status"] == "cancelled"

    expiring = _submit(
        settings,
        account["account_id"],
        side="buy",
        quantity=100,
        quote=_quote(symbol="sh600002"),
        key="expiring-order-0001",
    )
    repository = user_simulator_repository(settings)
    with repository.connect() as db:
        db.execute(
            "UPDATE user_paper_orders SET expires_at=? WHERE order_id=?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), expiring["order_id"]),
        )
    expired = repository.expire_orders(datetime.now(UTC))
    assert expired[0]["status"] == "expired"
    assert repository.order(expiring["order_id"])["rejection_reason"] == "order_expired"


def test_cash_position_and_limit_down_checks(tmp_path):
    settings = _settings(tmp_path)
    account = _account(settings)
    too_large = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol="sh600001",
        side="buy",
        quantity=20_000,
        quote=_quote(price=10),
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    assert too_large["allowed_to_submit"] is False
    assert {
        "insufficient_cash",
        "maximum_single_weight_exceeded",
        "maximum_total_exposure_exceeded",
    } & set(too_large["hard_failures"])

    no_position = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol="sh600001",
        side="sell",
        quantity=100,
        quote=_quote(limit_down=True),
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    assert no_position["allowed_to_submit"] is False
    assert "limit_down" in no_position["hard_failures"]
    assert "t_plus_one_or_insufficient_position" in no_position["hard_failures"]


def test_mark_to_market_performance_drawdown_benchmark_and_new_season(
    tmp_path,
    monkeypatch,
):
    fixed_now = datetime(2026, 7, 17, 2, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    monkeypatch.setattr(simulator_workflow, "datetime", FrozenDateTime)
    monkeypatch.setattr(simulator_persistence, "datetime", FrozenDateTime)
    settings = _settings(tmp_path)
    account = _account(settings)
    order = _submit(
        settings,
        account["account_id"],
        side="buy",
        quantity=1_000,
        quote=_quote(),
        key="mark-buy-0001",
    )
    settle_user_paper_order(
        settings,
        order_id=order["order_id"],
        quote=_quote(),
        fill_key="mark-fill-0001",
    )
    first_date = date(2026, 7, 20)
    first = mark_user_paper_account(
        settings,
        account_id=account["account_id"],
        snapshot_date=first_date,
        marks=[_quote(price=11.0, as_of=first_date)],
        benchmark_quote=MarketQuote(
            symbol="sh000300",
            asset_type=AssetType.INDEX,
            raw_price=4_000,
            as_of=first_date,
            source="fixture",
            trade_lot=1,
            t_plus_one=False,
        ),
    )
    second_date = date(2026, 7, 21)
    second = mark_user_paper_account(
        settings,
        account_id=account["account_id"],
        snapshot_date=second_date,
        marks=[_quote(price=8.0, as_of=second_date)],
        benchmark_quote=MarketQuote(
            symbol="sh000300",
            asset_type=AssetType.INDEX,
            raw_price=3_800,
            as_of=second_date,
            source="fixture",
            trade_lot=1,
            t_plus_one=False,
        ),
    )
    assert first["unrealized_pnl"] > 0
    assert second["drawdown"] < 0
    assert second["benchmark_return"] == pytest.approx(-0.05)
    performance = user_simulator_repository(settings).performance(account["account_id"])
    assert performance["maximum_drawdown"] < 0
    assert performance["cumulative_fees"] > 0
    assert performance["turnover"] > 0

    next_season = user_simulator_repository(settings).start_new_season(
        account["account_id"],
        idempotency_key="new-season-0001",
    )
    assert next_season["season"] == 2
    assert next_season["supersedes_account_id"] == account["account_id"]
    assert user_simulator_repository(settings).account(account["account_id"])[
        "status"
    ] == "closed"
    assert len(user_simulator_repository(settings).snapshots(account["account_id"])) == 2


def test_concurrent_and_repeated_order_confirmation_is_idempotent(tmp_path):
    settings = _settings(tmp_path)
    account = _account(settings)
    check = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol="sh600001",
        side="buy",
        quantity=100,
        quote=_quote(),
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    def confirm():
        return submit_user_paper_order(
            settings,
            check_id=check["check_id"],
            quantity=100,
            idempotency_key="concurrent-order-0001",
            user_confirmation=_confirmation(
                check,
                account_id=account["account_id"],
                quantity=100,
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        orders = list(pool.map(lambda _index: confirm(), range(2)))
    assert orders[0]["order_id"] == orders[1]["order_id"]
    assert len(user_simulator_repository(settings).orders(account["account_id"])) == 1


def test_workflow_and_repository_both_reject_missing_or_forged_confirmation(tmp_path):
    settings = _settings(tmp_path)
    account = _account(settings)
    check = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol="sh600001",
        side="buy",
        quantity=100,
        quote=_quote(),
        requested_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="explicit user confirmation"):
        submit_user_paper_order(
            settings,
            check_id=check["check_id"],
            quantity=100,
            idempotency_key="missing-confirmation-order",
        )
    forged = _confirmation(
        check,
        account_id=account["account_id"],
        quantity=100,
    )
    forged["symbol"] = "sz000001"
    with pytest.raises(ValueError, match="confirmation symbol"):
        submit_user_paper_order(
            settings,
            check_id=check["check_id"],
            quantity=100,
            idempotency_key="forged-confirmation-order",
            user_confirmation=forged,
        )
    repository = user_simulator_repository(settings)
    with pytest.raises(ValueError, match="confirmation confirmed"):
        repository.create_order(
            check_id=check["check_id"],
            quantity=100,
            idempotency_key="repository-bypass-order",
            requested_at=datetime(2026, 7, 17, tzinfo=UTC),
            eligible_trade_date=date(2026, 7, 17),
            expires_at=datetime(2026, 7, 24, tzinfo=UTC),
        user_confirmation={
            "confirmed": False,
                "check_id": check["check_id"],
                "account_id": account["account_id"],
                "symbol": check["symbol"],
                "side": check["side"],
                "quantity": 100,
            "source": "repository_bypass_attempt",
        },
    )


def test_next_open_simulation_requires_acknowledgement_and_waits_for_next_open(tmp_path):
    settings = _settings(tmp_path)
    account = _account(settings, "next-open-mode-account")
    close_quote = _quote(session_status="closed")
    quote_service = QuoteService(_AuthoritativeFixtureQuoteProvider(close_quote))
    check = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol=close_quote.symbol,
        side="buy",
        quantity=100,
        quote_service=quote_service,
        requested_at=datetime.combine(close_quote.as_of, datetime.min.time(), tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="intraday_simulation requires"):
        submit_user_paper_order(
            settings,
            check_id=check["check_id"],
            quantity=100,
            idempotency_key="next-open-intraday-rejected",
            requested_at=datetime.combine(close_quote.as_of, datetime.min.time(), tzinfo=UTC),
            quote_service=quote_service,
            user_confirmation=_confirmation(
                check,
                account_id=account["account_id"],
                quantity=100,
                simulation_mode="intraday_simulation",
            ),
        )
    with pytest.raises(ValueError, match="close_reference_acknowledged"):
        submit_user_paper_order(
            settings,
            check_id=check["check_id"],
            quantity=100,
            idempotency_key="next-open-ack-required",
            requested_at=datetime.combine(close_quote.as_of, datetime.min.time(), tzinfo=UTC),
            quote_service=quote_service,
            user_confirmation=_confirmation(
                check,
                account_id=account["account_id"],
                quantity=100,
                simulation_mode="next_open_simulation",
            ),
        )

    order = submit_user_paper_order(
        settings,
        check_id=check["check_id"],
        quantity=100,
        idempotency_key="next-open-pending-order",
        requested_at=datetime.combine(close_quote.as_of, datetime.min.time(), tzinfo=UTC),
        quote_service=quote_service,
        user_confirmation=_confirmation(
            check,
            account_id=account["account_id"],
            quantity=100,
            simulation_mode="next_open_simulation",
            close_reference_acknowledged=True,
        ),
    )
    assert order["status"] == "pending"
    assert order["eligible_trade_date"] == "2026-07-20"
    assert order["user_confirmation"]["simulation_mode"] == "next_open_simulation"
    assert order["user_confirmation"]["close_reference_acknowledged"] is True
    assert order["payload"]["quote_submission_context"]["quote_kind"] == "current_close"
    with pytest.raises(ValueError, match="eligible trade date"):
        settle_user_paper_order(
            settings,
            order_id=order["order_id"],
            quote=close_quote,
            fill_key="next-open-same-day-fill",
        )
