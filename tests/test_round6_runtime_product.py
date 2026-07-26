from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from streamlit.testing.v1 import AppTest

from quantlab.config import Settings
from quantlab.domain import AssetType, MarketQuote
from quantlab.market import ExecutionQuoteService, TradingCalendarService
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.round6 import Round6Repository
from quantlab.runtime.readiness import formal_experiment_status, primary_start_readiness
from quantlab.runtime.scheduler import RuntimeScheduler
from quantlab.workflows.forward_experiment import register_primary_forward_samples
from quantlab.workflows.product import build_product_home, record_product_usage
from quantlab.workflows.simulator import create_user_paper_account
from quantlab.workflows.trusted_data import refresh_trusted_data


MARKET_TZ = ZoneInfo("Asia/Shanghai")


def _settings(tmp_path, *, test_mode: bool = True) -> Settings:
    return Settings(
        values={
            "system": {
                "database_path": "quantlab.db",
                "data_dir": "data",
                "test_mode": test_mode,
                "timezone": "Asia/Shanghai",
            },
            "runtime": {
                "trusted_data_auto_refresh_enabled": True,
                "trusted_data_default_level": "server_observed",
                "trusted_data_license_status": "unverified_no_sla",
                "trusted_data_minimum_field_coverage": 0.8,
            },
            "llm": {"provider": "mock", "allow_mock_fallback": True},
            "strategies": {
                "forward_primary": {
                    "candidate_count": 3,
                    "minimum_trust_level": "server_observed",
                },
                "a_share_v4": {"protocol_version": "a-share-v4-round6-test"},
                "etf_rotation": {"universe": ["sh510300"]},
            },
            "costs": {
                "stock": {"trade_lot": 100},
                "etf": {"trade_lot": 100},
            },
            "risk": {"max_single_position": 0.15},
        },
        root=tmp_path,
    )


class _AutomaticFixtureAdapter:
    provider_name = "automatic_fixture"
    provider_version = "v1"
    license_status = "unverified_no_sla"

    def collect(self, as_of: date):
        fetched = datetime.now(UTC) - timedelta(minutes=1)
        calendar = [
            {
                "trade_date": (as_of + timedelta(days=offset)).isoformat(),
                "is_open": (as_of + timedelta(days=offset)).weekday() < 5,
            }
            for offset in range(45)
        ]
        symbols = ["sh600001", "sh600002", "sz000001"]
        return {
            "provider": self.provider_name,
            "provider_version": self.provider_version,
            "license_status": self.license_status,
            "fetched_at": fetched,
            "calendar": calendar,
            "industry": [
                {"symbol": symbol, "industry": "bank", "effective_date": as_of.isoformat()}
                for symbol in symbols
            ],
            "security_master": [
                {
                    "symbol": symbol,
                    "name": symbol,
                    "exchange": "SH" if symbol.startswith("sh") else "SZ",
                    "board": "main",
                    "listing_date": "2010-01-01",
                    "delisting_date": None,
                    "status": "listed",
                }
                for symbol in symbols
            ],
            "trade_status": [
                {
                    "symbol": symbol,
                    "trade_date": as_of,
                    "trade_status": True,
                    "suspended": False,
                    "is_st": False,
                    "amount": 100_000_000.0 - index * 1_000_000,
                    "fund_size": 5_000_000_000.0,
                    "turnover_rate": 1.0,
                    "source": self.provider_name,
                    "methodology": "fixture_server_observation",
                    "available_at": fetched,
                    "payload": {},
                }
                for index, symbol in enumerate(symbols)
            ],
            "pool_members": [
                {
                    "symbol": symbol,
                    "name": symbol,
                    "asset_class": "equity",
                    "category": "bank",
                    "eligible": True,
                    "representative": True,
                    "representative_rank": index + 1,
                    "amount": 100_000_000.0 - index * 1_000_000,
                    "fund_size": 5_000_000_000.0,
                    "liquidity_score": 100_000_000.0 - index * 1_000_000,
                    "source": self.provider_name,
                    "available_at": fetched,
                    "data_quality": "available",
                    "missing_fields": [],
                    "payload": {
                        "exchange": "SH" if symbol.startswith("sh") else "SZ",
                        "listing_date": "2010-01-01",
                        "delisting_date": None,
                        "trade_date": as_of.isoformat(),
                        "listed": True,
                        "is_st": False,
                        "suspended": False,
                        "trade_status": True,
                        "turnover_rate": 1.0,
                        "market_cap": 5_000_000_000.0,
                        "industry": "bank",
                    },
                }
                for index, symbol in enumerate(symbols)
            ],
            "failures": [],
        }


class _QuoteProvider:
    provider_name = "round6_quote_fixture"
    provider_version = "v1"
    authoritative = True

    def __init__(self, quote: MarketQuote):
        self.value = quote

    def quote(self, symbol: str, *, asset_type: AssetType, as_of: date) -> MarketQuote:
        result = self.value.model_copy(deep=True)
        result.symbol = symbol
        result.asset_type = asset_type
        return result


def test_automatic_trusted_refresh_runs_without_manual_files_and_records_coverage(tmp_path):
    settings = _settings(tmp_path)
    today = datetime.now(UTC).astimezone(MARKET_TZ).date()
    while today.weekday() >= 5:
        today += timedelta(days=1)

    result = refresh_trusted_data(
        settings,
        as_of=today,
        adapter=_AutomaticFixtureAdapter(),
    )

    assert result["automatic_refresh_enabled"] is True
    assert result["sources"]["calendar"]["records_saved"] >= 30
    assert result["sources"]["industry"]["records_saved"] == 3
    assert result["sources"]["point_in_time_pool"]["eligible_members"] == 3
    states = {item["batch_type"]: item for item in Round6Repository(
        settings.resolve("quantlab.db")
    ).data_source_states()}
    assert states["trading_calendar"]["minimum_ready"] is True
    assert states["industry_membership"]["minimum_ready"] is True
    assert states["point_in_time_pool"]["minimum_ready"] is True


def test_production_readiness_failure_does_not_create_primary_cohort(tmp_path):
    settings = _settings(tmp_path, test_mode=False)
    today = datetime.now(UTC).astimezone(MARKET_TZ).date()

    result = register_primary_forward_samples(
        settings,
        trade_date=today,
        server_now=datetime.now(UTC),
        activation_origin="scheduler",
        activation_reference={"run_date": today.isoformat()},
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "primary_readiness_failed"
    assert result["formal_evidence_created"] is False
    assert Round5Repository(settings.resolve("quantlab.db")).primary_experiment() is None


def test_closed_market_day_scheduler_never_creates_forward_registration(tmp_path):
    settings = _settings(tmp_path, test_mode=False)
    today = datetime.now(UTC).astimezone(MARKET_TZ).date()
    TradingCalendarService.from_settings(settings).ingest(
        [{"trade_date": today.isoformat(), "is_open": False}],
        namespace="production",
        trust_level="server_observed",
        provider="fixture",
        source="fixture",
        endpoint="fixture",
        source_version="v1",
        available_at=datetime.now(UTC) - timedelta(minutes=1),
        license_status="fixture",
        raw_fingerprint="closed-round6",
    )
    observed = datetime.combine(today, datetime.min.time(), tzinfo=MARKET_TZ).replace(
        hour=17
    ).astimezone(UTC)

    result = RuntimeScheduler(settings).tick(now=observed)

    assert not any(job["job_type"] == "forward_sample_registration" for job in result["jobs"])
    assert Round5Repository(settings.resolve("quantlab.db")).primary_experiment() is None


def test_runtime_process_claim_is_singleton_and_restartable(tmp_path):
    repository = Round6Repository(tmp_path / "quantlab.db")
    observed = datetime.now(UTC)
    first = repository.claim_process("scheduler", instance_id="one", now=observed)
    duplicate = repository.claim_process("scheduler", instance_id="two", now=observed)
    assert first["claimed"] is True
    assert duplicate["claimed"] is False
    repository.request_stop("scheduler")
    assert repository.stop_requested("scheduler", "one") is True
    assert repository.finish_process("scheduler", "one") is True
    restarted = repository.claim_process(
        "scheduler", instance_id="two", now=observed + timedelta(seconds=1)
    )
    assert restarted["claimed"] is True


def test_execution_quote_distinguishes_realtime_and_previous_close():
    now = datetime.now(UTC)
    today = now.astimezone(MARKET_TZ).date()
    realtime = MarketQuote(
        symbol="sh600001",
        asset_type=AssetType.STOCK,
        raw_price=10.0,
        as_of=today,
        available_at=now,
        source="fixture",
        session_status="open",
    )
    live_service = ExecutionQuoteService(_QuoteProvider(realtime))
    live = live_service.get("sh600001", asset_type="stock", as_of=today)
    assert live.quote_kind == "realtime"
    assert live.actionable is True
    health = live_service.health_snapshot()
    assert health["status"] == "available"
    assert health["quote_kind"] == "realtime"

    prior = realtime.model_copy(deep=True)
    prior.as_of = today - timedelta(days=1)
    prior.available_at = now - timedelta(days=1)
    prior.session_status = "closed"
    closed = ExecutionQuoteService(_QuoteProvider(prior)).get(
        "sh600001", asset_type="stock", as_of=today
    )
    assert closed.quote_kind == "previous_close"
    assert closed.actionable is False
    assert "quote_kind_previous_close_is_not_intraday_actionable" in closed.actionability_reasons


def test_prediction_and_shadow_scorecards_are_separate_products(tmp_path):
    status = formal_experiment_status(_settings(tmp_path))
    assert "prediction_scorecard" in status
    assert "shadow_trading_scorecard" in status
    assert status["scorecard_boundary"]["prediction"] != status["scorecard_boundary"]["trading"]


def test_product_usage_is_never_training_or_forward_evidence(tmp_path):
    settings = _settings(tmp_path)
    event = record_product_usage(
        settings,
        event_type="ai_recommendation_viewed",
        entrypoint="AI研究",
        symbol="sh600001",
    )
    assert event["training_eligible"] is False
    assert event["forward_scorecard_eligible"] is False
    assert event["payload"]["usage_only"] is True


def test_product_home_handles_no_account_and_no_data(tmp_path):
    settings = _settings(tmp_path)
    assert build_product_home(settings)["state"] == "no_account"
    create_user_paper_account(
        settings,
        name="round6",
        idempotency_key="round6-home-account",
    )
    assert build_product_home(settings)["state"] == "no_data"


def test_streamlit_has_vnext_primary_entries_and_preserves_advanced_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTLAB_DATABASE_PATH", str(tmp_path / "streamlit.db"))
    app = AppTest.from_file("dashboard/app.py").run(timeout=60)
    assert not app.segmented_control
    assert app.radio[0].options == [
        "今日",
        "市场与发现",
        "研究台",
        "组合与交易",
        "决策复盘",
    ]
    assert app.radio[0].value == "今日"
    assert not app.tabs
    assert not app.exception
    rendered = "\n".join(item.value for item in app.text)
    assert "Worker与Job" not in rendered

    next(
        button for button in app.button if button.key == "open_product_account_workspace"
    ).click().run(timeout=60)
    mine_view = next(item for item in app.button_group if item.key == "product_mine_view")
    mine_view.set_value("高级与审计").run(timeout=60)
    next(button for button in app.button if button.label == "进入工程审计").click().run(
        timeout=60
    )
    advanced_labels = {tab.label for tab in app.tabs}
    assert len(app.tabs) >= 15
    assert {"证据中心", "学习状态", "LLM 配置", "审计报告"} <= advanced_labels
    assert not app.exception


def test_readiness_reports_real_blockers_instead_of_starting_empty_primary(tmp_path):
    readiness = primary_start_readiness(_settings(tmp_path, test_mode=False))
    assert readiness["start_allowed"] is False
    assert readiness["blockers"]
    assert readiness["current_primary_experiment"] is None
