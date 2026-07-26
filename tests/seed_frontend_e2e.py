from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from quantlab.config import Settings
from quantlab.domain import AssetType, DataQuality, MarketQuote
from quantlab.domain.data_governance import DataTrustLevel
from quantlab.market.quotes import StoredTestQuoteProvider
from quantlab.persistence.migrations import ensure_database_initialized
from quantlab.workflows.simulator import (
    cancel_user_paper_order,
    create_user_paper_account,
    run_pretrade_check,
    settle_user_paper_order,
    submit_user_paper_order,
    user_simulator_repository,
)


def settings_for(database_path: Path) -> Settings:
    return Settings.load().with_overrides(
        {
            "system": {
                "database_path": str(database_path),
                "data_dir": str(database_path.parent / "data"),
                "test_mode": True,
                "initial_capital": 123_456_789.12,
            },
            "llm": {"provider": "mock", "allow_mock_fallback": True},
        }
    )


def _fixture_market_time() -> datetime:
    market_date = date.today()
    while market_date.weekday() >= 5:
        market_date -= timedelta(days=1)
    return datetime.combine(market_date, time(2), tzinfo=UTC)


FIXTURE_MARKET_TIME = _fixture_market_time()


def quote(symbol: str, price: float, name: str, *, asset_type: AssetType) -> MarketQuote:
    return MarketQuote(
        symbol=symbol,
        name=name,
        asset_type=asset_type,
        raw_price=price,
        as_of=FIXTURE_MARKET_TIME.date(),
        available_at=FIXTURE_MARKET_TIME,
        observed_at=FIXTURE_MARKET_TIME,
        source="frontend_e2e_fixture",
        provider="frontend_e2e_fixture",
        source_version="v1",
        data_quality=DataQuality.AVAILABLE,
        session_status="open",
        quote_kind="realtime",
        authoritative=False,
        evidence_stage="test",
        trust_level=DataTrustLevel.TEST,
        license_status="test_only",
        actionable=True,
        industry="浏览器验收行业",
        trade_lot=100,
        t_plus_one=asset_type == AssetType.STOCK,
        risk_metadata={
            "risk_check_complete": True,
            "financial_check_complete": True,
            "financial_quality_score": 0.82,
            "listing_days": 2000,
        },
    )


def confirmation(check: dict, quantity: int) -> dict:
    return {
        "confirmed": True,
        "check_id": check["check_id"],
        "account_id": check["account_id"],
        "symbol": check["symbol"],
        "side": check["side"],
        "quantity": quantity,
        "source": "frontend_browser_e2e_seed",
        "simulation_mode": "intraday_simulation",
        "close_reference_acknowledged": False,
    }


def submit(
    settings: Settings,
    account_id: str,
    market_quote: MarketQuote,
    *,
    quantity: int = 100,
    key: str,
) -> dict:
    check = run_pretrade_check(
        settings,
        account_id=account_id,
        symbol=market_quote.symbol,
        side="buy",
        quantity=quantity,
        quote=market_quote,
        requested_at=FIXTURE_MARKET_TIME + timedelta(minutes=1),
    )
    return submit_user_paper_order(
        settings,
        check_id=check["check_id"],
        quantity=quantity,
        idempotency_key=key,
        requested_at=FIXTURE_MARKET_TIME + timedelta(minutes=2),
        user_confirmation=confirmation(check, quantity),
    )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: seed_frontend_e2e.py <isolated-database-path>")
    database_path = Path(sys.argv[1]).resolve()
    production = (Path(__file__).resolve().parents[1] / "data" / "quantlab.db").resolve()
    if database_path == production or production in database_path.parents:
        raise SystemExit("frontend e2e seed must not target the production database")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.exists():
        database_path.unlink()
    os.environ["QUANTLAB_ENABLE_TEST_QUOTES"] = "1"
    settings = settings_for(database_path)
    ensure_database_initialized(database_path)
    account = create_user_paper_account(
        settings,
        name="跨市场长期模拟组合 · 浏览器验收",
        initial_capital=123_456_789.12,
        idempotency_key="frontend-e2e-account",
    )
    stored_quotes = StoredTestQuoteProvider(database_path)
    fixtures = [
        ("sh600000", 10.31, "浦发银行", AssetType.STOCK),
        ("sh600036", 42.88, "招商银行", AssetType.STOCK),
        ("sh600519", 1488.76, "贵州茅台", AssetType.STOCK),
        ("sh601318", 61.25, "中国平安", AssetType.STOCK),
        ("sh601899", 19.42, "紫金矿业", AssetType.STOCK),
        ("sz000333", 78.66, "美的集团", AssetType.STOCK),
        ("sz000651", 46.08, "格力电器", AssetType.STOCK),
        ("sz300750", 286.55, "宁德时代", AssetType.STOCK),
        ("sh510050", 3.102, "上证50ETF", AssetType.ETF),
        ("sh510300", 4.238, "沪深300ETF", AssetType.ETF),
        ("sh510500", 6.414, "中证500ETF", AssetType.ETF),
        ("sh588000", 1.184, "科创50ETF", AssetType.ETF),
    ]
    quotes = [quote(*item[:3], asset_type=item[3]) for item in fixtures]
    for item in quotes:
        stored_quotes.save(item)
    for index, item in enumerate(quotes[:9]):
        order = submit(
            settings,
            account["account_id"],
            item,
            key=f"frontend-e2e-filled-{index}",
        )
        settle_user_paper_order(
            settings,
            order_id=order["order_id"],
            quote=item,
            fill_key=f"frontend-e2e-fill-{index}",
        )
    partial = submit(
        settings,
        account["account_id"],
        quotes[10],
        quantity=200,
        key="frontend-e2e-partial",
    )
    settle_user_paper_order(
        settings,
        order_id=partial["order_id"],
        quote=quotes[10],
        fill_quantity=100,
        fill_key="frontend-e2e-partial-fill",
    )
    cancelled = submit(
        settings,
        account["account_id"],
        quotes[11],
        key="frontend-e2e-cancelled",
    )
    cancel_user_paper_order(settings, cancelled["order_id"])
    pending = submit(
        settings,
        account["account_id"],
        quotes[9],
        key="frontend-e2e-existing-pending",
    )
    repository = user_simulator_repository(settings)
    repository.record_review(
        account["account_id"],
        order_id=pending["order_id"],
        symbol="sh510300",
        review_type="user_trade_review",
        payload={
            "note": "这是一段用于验证真实长文本排版的复盘记录：在证据不完整、资金口径降级且组合已有同类风险暴露时，继续观察比立即增加仓位更符合当时的约束；该记录只属于隔离浏览器测试账本。",
            "source": "frontend_browser_e2e_seed",
        },
    )
    print(f"seeded={database_path} account={account['account_id']}")


if __name__ == "__main__":
    main()
