from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from quantlab.config import Settings
from quantlab.data.baostock import PointInTimeSecurity as BaoStockSecurity
from quantlab.domain import AssetType, DataQuality, MarketQuote
from quantlab.domain.data_governance import DataTrustLevel
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.round6 import Round6Repository
from quantlab.persistence.round7 import Round7Repository
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
from quantlab.runtime import autostart as autostart_module
from quantlab.runtime.autostart import RuntimeAutostartManager
from quantlab.runtime.scheduler import DEFAULT_SCHEDULES
from quantlab.runtime.soak import capture_soak_observation, soak_report
from quantlab.workflows import trusted_data_adapters as adapter_module
from quantlab.workflows.investor_portfolio import (
    investor_recommendation_detail,
    investor_recommendation_effects,
    record_recommendation_adoption,
)
from quantlab.workflows.product_demo import (
    confirm_historical_research_demo,
    live_demo_status,
    prepare_historical_research_demo,
    reset_historical_research_demo,
    run_historical_research_demo,
)
from quantlab.workflows.simulator import (
    create_user_paper_account,
    run_pretrade_check,
    settle_user_paper_order,
    submit_user_paper_order,
    user_simulator_repository,
)
from quantlab.workflows.trusted_data import refresh_trusted_data
from quantlab.workflows.trusted_data_adapters import FreeTrustedDataAdapter


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path: Path, *, test_mode: bool = True) -> Settings:
    return Settings(
        values={
            "system": {
                "database_path": str(tmp_path / "quantlab.db"),
                "data_dir": str(tmp_path / "data"),
                "test_mode": test_mode,
                "timezone": "Asia/Shanghai",
            },
            "runtime": {
                "trusted_data_auto_refresh_enabled": True,
                "trusted_data_default_level": "server_observed",
                "trusted_data_license_status": "unverified_no_sla",
                "trusted_data_minimum_field_coverage": 0.8,
                "trusted_calendar_minimum_records": 1,
                "trusted_security_master_minimum_records": 1,
                "trusted_industry_minimum_records": 1,
                "trusted_point_in_time_pool_minimum_records": 1,
                "trusted_provider_timeout_seconds": 0.2,
                "trusted_provider_max_attempts": 1,
                "trusted_provider_failure_threshold": 1,
                "trusted_provider_cooldown_seconds": 600,
                "runtime_health_maximum_age_seconds": 90,
                "backup_directory": str(tmp_path / "backups"),
                "demo_directory": str(tmp_path / "demo"),
                "autostart_directory": str(tmp_path / "runtime"),
            },
            "llm": {"provider": "mock", "allow_mock_fallback": True},
            "strategies": {
                "forward_primary": {
                    "candidate_count": 1,
                    "minimum_trust_level": "server_observed",
                },
                "a_share_v4": {"protocol_version": "round7-test"},
                "etf_rotation": {"universe": ["sh510300"]},
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
            "risk": {
                "max_single_position": 0.15,
                "max_industry_exposure": 0.30,
                "max_total_exposure": 0.80,
            },
        },
        root=PROJECT_ROOT,
    )


def _automatic_bundle(as_of: date, *, open_day: bool, complete: bool = True):
    fetched = datetime(2026, 7, 18, 7, tzinfo=UTC)

    class Adapter:
        provider_name = "round7_fixture"
        provider_version = "v1"
        license_status = "unverified_no_sla"

        def collect(self, _as_of: date):
            return {
                "provider": self.provider_name,
                "provider_version": self.provider_version,
                "license_status": self.license_status,
                "fetched_at": fetched,
                "calendar": [{"trade_date": as_of.isoformat(), "is_open": open_day}],
                "security_master": [
                    {
                        "symbol": "sh600001",
                        "name": "测试银行",
                        "exchange": "SH",
                        "board": "main",
                        "listing_date": "2010-01-01",
                        "delisting_date": None,
                        "status": "listed",
                    }
                ],
                "industry": [
                    {
                        "symbol": "sh600001",
                        "industry": "银行",
                        "classification": "baostock",
                        "effective_date": "2026-01-01",
                        "provider": "baostock",
                    }
                ],
                "trade_status": [
                    {
                        "symbol": "sh600001",
                        "trade_date": as_of,
                        "trade_status": True,
                        "suspended": False,
                        "is_st": False,
                        "amount": 100_000_000.0,
                        "fund_size": 5_000_000_000.0,
                        "turnover_rate": 1.2 if complete else None,
                        "source": "round7_fixture",
                        "methodology": "fixture",
                        "available_at": fetched,
                        "payload": {},
                    }
                ],
                "pool_members": [
                    {
                        "symbol": "sh600001",
                        "name": "测试银行",
                        "asset_class": "equity",
                        "category": "银行",
                        "eligible": complete,
                        "exclusion_reasons": []
                        if complete
                        else ["required_field_unavailable:turnover_rate"],
                        "amount": 100_000_000.0,
                        "fund_size": 5_000_000_000.0 if complete else None,
                        "liquidity_score": 100_000_000.0,
                        "source": "round7_fixture",
                        "available_at": fetched,
                        "data_quality": "available" if complete else "degraded",
                        "missing_fields": [] if complete else ["turnover_rate", "market_cap"],
                        "payload": {
                            "exchange": "SH",
                            "listing_date": "2010-01-01",
                            "delisting_date": None,
                            "trade_date": as_of.isoformat(),
                            "listed": True,
                            "is_st": False,
                            "suspended": False,
                            "trade_status": True,
                            "turnover_rate": 1.2 if complete else None,
                            "market_cap": 5_000_000_000.0 if complete else None,
                            "industry": "银行",
                        },
                    }
                ],
                "failures": [],
                "provider_attempts": [],
            }

    return Adapter()


def test_provider_fallback_records_attempts_and_keeps_partial_fields_explicit(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    as_of = date(2026, 7, 20)
    adapter = FreeTrustedDataAdapter(settings)
    monkeypatch.setattr(
        adapter.baostock,
        "trading_calendar",
        lambda *_args, **_kwargs: [{"trade_date": as_of.isoformat(), "is_open": True}],
    )
    monkeypatch.setattr(
        adapter.baostock,
        "security_master_records",
        lambda: [
            {
                "symbol": "sh600001",
                "name": "测试银行",
                "exchange": "SH",
                "board": "main",
                "listing_date": "2010-01-01",
                "delisting_date": None,
                "status": "listed",
            }
        ],
    )
    monkeypatch.setattr(
        adapter.baostock,
        "industry_records",
        lambda **_kwargs: [
            {
                "symbol": "sh600001",
                "industry": "银行",
                "classification": "baostock",
                "effective_date": "2026-01-01",
            }
        ],
    )
    monkeypatch.setattr(
        adapter.baostock,
        "point_in_time_universe",
        lambda _day: [
            BaoStockSecurity(
                symbol="sh600001",
                name="测试银行",
                exchange="SH",
                board="main",
                trade_status=False,
            )
        ],
    )
    monkeypatch.setattr(
        adapter_module,
        "_akshare_spot",
        lambda _day: (_ for _ in ()).throw(RuntimeError("EastMoney unavailable")),
    )
    monkeypatch.setattr(
        adapter_module,
        "_tencent_spot",
        lambda _day, _symbols: (_ for _ in ()).throw(RuntimeError("Tencent unavailable")),
    )
    monkeypatch.setattr(
        adapter_module,
        "_akshare_sina_spot",
        lambda _day: {
            "sh600001": {
                "name": "ST测试银行",
                "price": 10.0,
                "amount": 100_000_000.0,
                "turnover_rate": None,
                "market_cap": None,
                "industry": "",
                "provider": "akshare_sina",
                "available_at": datetime.now(UTC).isoformat(),
            }
        },
    )

    first = adapter.collect(as_of)
    second = adapter.collect(as_of)

    assert any(
        item["provider"] == "akshare_eastmoney" and item["status"] == "failed"
        for item in first["provider_attempts"]
    )
    assert any(
        item["provider"] == "akshare_sina" and item["status"] == "available"
        for item in first["provider_attempts"]
    )
    assert first["pool_members"][0]["eligible"] is False
    assert {"turnover_rate", "market_cap"} <= set(first["pool_members"][0]["missing_fields"])
    assert {
        "not_confirmed_tradeable_on_snapshot_date",
        "historical_st_name_flag",
    } <= set(first["pool_members"][0]["exclusion_reasons"])
    assert any(
        item["provider"] == "akshare_eastmoney" and item["status"] == "circuit_open"
        for item in second["provider_attempts"]
    )


def test_non_trading_refresh_updates_master_and_industry_without_formal_pool(tmp_path):
    settings = _settings(tmp_path)
    as_of = date(2026, 7, 18)
    result = refresh_trusted_data(
        settings,
        as_of=as_of,
        adapter=_automatic_bundle(as_of, open_day=False),
    )
    states = {
        item["batch_type"]: item
        for item in Round6Repository(settings.resolve(settings.get("system.database_path"))).data_source_states()
    }
    assert result["sources"]["security_master"]["records_saved"] == 1
    assert result["sources"]["industry"]["rich_records_saved"] == 1
    assert result["sources"]["point_in_time_pool"]["status"] == "skipped_non_trading_day"
    assert states["point_in_time_pool"]["consecutive_failures"] == 0
    assert states["security_master"]["minimum_ready"] is True
    assert Round7Repository(settings.resolve(settings.get("system.database_path"))).industry_records()
    trusted_schedule = next(item for item in DEFAULT_SCHEDULES if item["name"] == "trusted_data_refresh")
    assert trusted_schedule["trading_days_only"] is False


def test_strict_field_coverage_keeps_partial_pool_below_readiness(tmp_path):
    settings = _settings(tmp_path)
    as_of = date(2026, 7, 20)
    result = refresh_trusted_data(
        settings,
        as_of=as_of,
        adapter=_automatic_bundle(as_of, open_day=True, complete=False),
    )
    pool = result["sources"]["point_in_time_pool"]
    assert pool["manifest"]["status"] == "partial"
    assert pool["field_coverage"] == 0.0
    assert pool["required_field_coverage"]["turnover_rate"] == 0.0
    assert pool["required_field_coverage"]["market_cap"] == 0.0
    state = next(
        item
        for item in Round6Repository(settings.resolve(settings.get("system.database_path"))).data_source_states()
        if item["batch_type"] == "point_in_time_pool"
    )
    assert state["minimum_ready"] is False


def test_identical_security_master_refresh_is_content_idempotent(tmp_path):
    settings = _settings(tmp_path)
    as_of = date(2026, 7, 20)

    first = refresh_trusted_data(
        settings,
        as_of=as_of,
        adapter=_automatic_bundle(as_of, open_day=True),
    )
    second = refresh_trusted_data(
        settings,
        as_of=as_of,
        adapter=_automatic_bundle(as_of, open_day=True),
    )

    assert first["sources"]["security_master"]["records_saved"] == 1
    assert second["sources"]["security_master"]["records_saved"] == 1
    assert second["sources"]["security_master"]["manifest"]["status"] == "completed"


def test_security_code_change_keeps_lineage_without_duplicate_master_rows(tmp_path):
    settings = _settings(tmp_path)
    as_of = date(2026, 7, 20)
    adapter = _automatic_bundle(as_of, open_day=True)
    original_collect = adapter.collect

    def collect(day):
        bundle = original_collect(day)
        bundle["security_master"] = [
            {
                "symbol": "sz302132",
                "source_symbol": "sz300114",
                "name": "旧代码",
                "exchange": "SZ",
                "board": "gem",
                "listing_date": "2010-08-27",
                "delisting_date": "2025-02-17",
                "status": "0",
            },
            {
                "symbol": "sz302132",
                "source_symbol": "sz302132",
                "name": "当前代码",
                "exchange": "SZ",
                "board": "gem",
                "listing_date": "2010-08-27",
                "delisting_date": None,
                "status": "1",
            },
        ]
        return bundle

    adapter.collect = collect
    result = refresh_trusted_data(settings, as_of=as_of, adapter=adapter)
    master = result["sources"]["security_master"]
    records = StrategyEvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    ).security_master(
        security_type="stock",
        master_version=master["manifest"]["source_version"],
        cutoff_at=datetime(2026, 7, 21, tzinfo=UTC),
    )

    assert master["manifest"]["record_count"] == 1
    assert len(records) == 1
    assert records[0].payload["source_symbol"] == "sz302132"
    assert records[0].payload["source_symbol_aliases"] == ["sz300114"]


def test_investor_adoption_revisions_are_idempotent_and_auditable(tmp_path):
    settings = _settings(tmp_path)
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    portfolio = repository.create_investor_portfolio(name="真实组合", cash=100_000)
    recommendation_id = "recommendation-round7"
    with repository.connect() as db:
        db.execute(
            """INSERT INTO investor_recommendations(
                   recommendation_id,portfolio_id,symbol,as_of,action,quantity_min,
                   quantity_max,actionable,context_id,context_fingerprint,payload,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                recommendation_id,
                portfolio["portfolio_id"],
                "sh600001",
                "2026-07-18",
                "buy",
                100,
                300,
                1,
                "context",
                "fingerprint",
                json.dumps({"asset_type": "stock", "start_price": 10.0, "due_dates": {"5": "2026-07-25", "20": "2026-08-18"}}),
                datetime.now(UTC).isoformat(),
            ),
        )

    first = record_recommendation_adoption(
        settings,
        recommendation_id=recommendation_id,
        decision="adopted",
        trade_side="buy",
        actual_quantity=100,
        actual_price=10.0,
        actual_trade_date=date(2026, 7, 18),
        transaction_cost=5.0,
    )
    second = record_recommendation_adoption(
        settings,
        recommendation_id=recommendation_id,
        decision="partially_adopted",
        trade_side="buy",
        actual_quantity=200,
        actual_price=9.0,
        actual_trade_date=date(2026, 7, 18),
        transaction_cost=5.0,
        note="修正录入数量",
    )
    replay = record_recommendation_adoption(
        settings,
        recommendation_id=recommendation_id,
        decision="partially_adopted",
        trade_side="buy",
        actual_quantity=200,
        actual_price=9.0,
        actual_trade_date=date(2026, 7, 18),
        transaction_cost=5.0,
        note="修正录入数量",
    )
    detail = investor_recommendation_detail(settings, recommendation_id=recommendation_id)
    overview = repository.investor_overview(portfolio["portfolio_id"])

    assert first["revision_number"] == 1
    assert second["revision_number"] == 2
    assert replay["idempotent"] is True
    assert len(detail["revisions"]) == 2
    assert overview["positions"][0]["quantity"] == 200
    assert overview["portfolio"]["cash"] == pytest.approx(98_195.0)
    with repository.connect() as db:
        sources = [row[0] for row in db.execute("SELECT source FROM investor_trades ORDER BY created_at")]
    assert sources == ["superseded_user_reported_external_fill", "user_reported_external_fill"]
    effects = investor_recommendation_effects(
        settings, portfolio_id=portfolio["portfolio_id"]
    )
    assert effects["training_eligible"] is False
    assert effects["forward_scorecard_eligible"] is False


def test_historical_demo_prepare_stops_before_order_and_is_isolated(tmp_path):
    settings = _settings(tmp_path)
    main = Round5Repository(settings.resolve(settings.get("system.database_path")))
    before = len(main.experiments())

    prepared = prepare_historical_research_demo(settings)

    assert prepared["stage"] == "pretrade_ready"
    assert prepared["order"] is None
    assert prepared["fills"] == []
    assert prepared["historical_scorecard"] is None
    assert prepared["pretrade"]["allowed_to_submit"] is True
    assert prepared["pretrade"]["symbol"] == prepared["selected_candidate"]["symbol"]
    assert prepared["research"]["frozen"] is True
    assert prepared["research"]["evidence_boundary"] == "research_only"
    assert prepared["formal_experiments_in_demo_database"] == 0
    assert len(main.experiments()) == before
    assert user_simulator_repository(settings).accounts() == []
    assert Path(prepared["isolated_database"]).parent == tmp_path / "demo"


def test_historical_demo_confirm_requires_matching_explicit_confirmation(tmp_path):
    settings = _settings(tmp_path)
    prepared = prepare_historical_research_demo(settings)
    arguments = {
        "check_id": prepared["pretrade"]["check_id"],
        "dataset_fingerprint": prepared["dataset"]["fingerprint"],
    }

    with pytest.raises(ValueError, match="explicit user confirmation"):
        confirm_historical_research_demo(settings, **arguments, confirmed=False)
    with pytest.raises(ValueError, match="dataset fingerprint"):
        confirm_historical_research_demo(
            settings,
            check_id=arguments["check_id"],
            dataset_fingerprint="wrong-fingerprint",
            confirmed=True,
        )
    with pytest.raises(ValueError, match="pre-trade check not found"):
        confirm_historical_research_demo(
            settings,
            check_id="missing-check",
            dataset_fingerprint=arguments["dataset_fingerprint"],
            confirmed=True,
        )
    assert prepare_historical_research_demo(settings)["order"] is None

    completed = confirm_historical_research_demo(settings, **arguments, confirmed=True)

    assert completed["stage"] == "completed"
    assert completed["order"]["status"] == "filled"
    assert completed["order"]["user_confirmation"]["confirmed"] is True
    assert completed["order"]["user_confirmation"]["check_id"] == arguments["check_id"]
    assert len(completed["fills"]) == 1
    assert completed["dataset"]["research_only"] is True
    assert completed["dataset"]["training_eligible"] is False
    assert completed["dataset"]["forward_scorecard_eligible"] is False
    assert completed["historical_scorecard"]["evidence_boundary"] == "research_only"
    assert completed["formal_experiments_in_demo_database"] == 0
    assert user_simulator_repository(settings).accounts() == []


def test_historical_demo_reset_removes_only_the_isolated_ledger(tmp_path):
    settings = _settings(tmp_path)
    prepared = prepare_historical_research_demo(settings)
    isolated_database = Path(prepared["isolated_database"])
    normal_database = settings.resolve(settings.get("system.database_path"))
    normal_database_existed = normal_database.exists()

    reset = reset_historical_research_demo(settings)

    assert reset["database_path"] == str(isolated_database)
    assert reset["dataset_fingerprint"] == prepared["dataset"]["fingerprint"]
    assert prepared["dataset"]["fingerprint"][:16] in isolated_database.name
    assert str(isolated_database) in reset["removed"]
    assert not isolated_database.exists()
    assert normal_database.exists() is normal_database_existed
    assert user_simulator_repository(settings).accounts() == []
    replay = prepare_historical_research_demo(settings)
    assert replay["stage"] == "pretrade_ready"
    assert replay["order"] is None


def test_historical_demo_is_isolated_idempotent_and_never_creates_formal_evidence(tmp_path):
    settings = _settings(tmp_path)
    main = Round5Repository(settings.resolve(settings.get("system.database_path")))
    before = len(main.experiments())

    first = run_historical_research_demo(settings)
    second = run_historical_research_demo(settings)

    assert first["order"]["status"] == "filled"
    assert len(first["fills"]) == 1
    assert second["order"]["order_id"] == first["order"]["order_id"]
    assert len(second["fills"]) == 1
    assert first["dataset"]["research_only"] is True
    assert first["formal_experiments_in_demo_database"] == 0
    assert len(main.experiments()) == before
    assert Path(first["isolated_database"]).parent == tmp_path / "demo"
    live = live_demo_status(settings)
    assert "selected_candidate" not in live
    assert "claim_boundary" in live


def test_windows_autostart_manager_install_status_disable_remove_without_secrets(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    calls: list[list[str]] = []

    def fake_schtasks(arguments, *, check=True):
        calls.append(arguments)
        return {"returncode": 0, "stdout": "Ready", "stderr": ""}

    monkeypatch.setattr(autostart_module, "_run_schtasks", fake_schtasks)
    manager = RuntimeAutostartManager(settings)
    installed = manager.install()
    launcher = Path(installed["launcher"])
    launcher_text = launcher.read_text(encoding="utf-8")
    status = manager.status()
    disabled = manager.disable()
    removed = manager.remove()

    assert installed["secrets_in_command"] is False
    assert "API_KEY" not in launcher_text and "sk-" not in launcher_text
    assert status["status"] == "installed"
    assert disabled["status"] == "disabled"
    assert removed["status"] == "removed"
    assert [call[0] for call in calls] == ["/Create", "/Query", "/Change", "/Delete"]


def test_windows_autostart_falls_back_to_user_startup_without_admin(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    def denied_schtasks(_arguments, *, check=True):
        return {"returncode": 1, "stdout": "", "stderr": "access denied"}

    monkeypatch.setattr(autostart_module, "_run_schtasks", denied_schtasks)
    manager = RuntimeAutostartManager(settings)
    installed = manager.install()
    status = manager.status()
    assert installed["status"] == "installed"
    assert installed["mechanism"] == "user_startup_folder"
    assert installed["fallback_used"] is True
    assert status["enabled"] is True
    assert manager.startup_path.is_file()
    disabled = manager.disable()
    assert disabled["status"] == "disabled"
    assert manager.startup_disabled_path.is_file()
    removed = manager.remove()
    assert removed["status"] == "removed"
    assert not manager.launcher_path.exists()
    assert not manager.startup_disabled_path.exists()


def test_soak_report_uses_only_actual_observation_interval(tmp_path):
    settings = _settings(tmp_path)
    repository = Round6Repository(settings.resolve(settings.get("system.database_path")))
    first = datetime(2026, 7, 18, 8, tzinfo=UTC)
    repository.claim_process("worker", instance_id="worker-1", now=first)
    repository.claim_process("scheduler", instance_id="scheduler-1", now=first)
    capture_soak_observation(settings, observed_at=first, source="test")
    repository.heartbeat_process("worker", "worker-1", now=first + timedelta(minutes=5))
    repository.heartbeat_process("scheduler", "scheduler-1", now=first + timedelta(minutes=5))
    capture_soak_observation(
        settings, observed_at=first + timedelta(minutes=5), source="test"
    )

    report = soak_report(settings)

    assert report["observation_count"] == 2
    assert report["actual_duration_seconds"] == 300.0
    assert report["restart_counts"] == {}
    assert "accelerated" in report["claim_boundary"]


def _test_quote(price: float = 10.0) -> MarketQuote:
    observed = datetime(2026, 7, 20, 2, tzinfo=UTC)
    return MarketQuote(
        symbol="sh510300",
        name="沪深300ETF",
        asset_type=AssetType.ETF,
        raw_price=price,
        as_of=date(2026, 7, 20),
        available_at=observed,
        observed_at=observed,
        source="round7_test_quote",
        provider="round7_test",
        source_version="v1",
        data_quality=DataQuality.AVAILABLE,
        session_status="open",
        quote_kind="realtime",
        authoritative=False,
        evidence_stage="test",
        trust_level=DataTrustLevel.TEST,
        license_status="test_only",
        actionable=True,
    )


def test_five_entry_simulator_shows_partial_fill_and_can_cancel_from_ui(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    account = create_user_paper_account(
        settings,
        name="UI生命周期",
        idempotency_key="round7-ui-account",
    )
    check = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol="sh510300",
        side="buy",
        quantity=200,
        quote=_test_quote(),
        requested_at=datetime(2026, 7, 20, 2, tzinfo=UTC),
    )
    order = submit_user_paper_order(
        settings,
        check_id=check["check_id"],
        quantity=200,
        idempotency_key="round7-ui-order",
        requested_at=datetime(2026, 7, 20, 2, 1, tzinfo=UTC),
        user_confirmation={
            "confirmed": True,
            "check_id": check["check_id"],
            "account_id": check["account_id"],
            "symbol": check["symbol"],
            "side": check["side"],
            "quantity": 200,
            "source": "round7_ui_test",
            "simulation_mode": "intraday_simulation",
            "close_reference_acknowledged": False,
        },
    )
    settle_user_paper_order(
        settings,
        order_id=order["order_id"],
        quote=_test_quote(10.1),
        fill_quantity=100,
        fill_key="round7-ui-partial-fill",
    )
    monkeypatch.setenv("QUANTLAB_DATABASE_PATH", str(tmp_path / "quantlab.db"))

    app = AppTest.from_file("dashboard/app.py").run(timeout=60)
    app.radio[0].set_value("组合与交易").run(timeout=60)

    assert not app.exception
    frames = "\n".join(str(item.value) for item in app.dataframe)
    assert "部分成交" in frames
    cancel = next(button for button in app.button if button.label == "撤销未完成委托")
    cancel.click()
    app.run(timeout=60)
    assert not app.exception
    assert user_simulator_repository(settings).order(order["order_id"])["status"] == "cancelled"


def test_normal_product_module_has_no_raw_json_and_advanced_mode_still_exists(tmp_path, monkeypatch):
    source = (PROJECT_ROOT / "dashboard" / "product_ui.py").read_text(encoding="utf-8")
    assert "st.json(" not in source
    monkeypatch.setenv("QUANTLAB_DATABASE_PATH", str(tmp_path / "ui.db"))
    app = AppTest.from_file("dashboard/app.py").run(timeout=60)
    rendered = "\n".join(str(item.value) for item in app.text)
    assert not app.exception
    assert "Brier" not in rendered
    assert "Prompt" not in rendered
    assert "Worker与Job" not in rendered
    next(
        button for button in app.button if button.key == "open_product_account_workspace"
    ).click().run(timeout=60)
    mine_view = next(item for item in app.button_group if item.key == "product_mine_view")
    mine_view.set_value("高级与审计").run(timeout=60)
    next(button for button in app.button if button.label == "进入工程审计").click().run(
        timeout=60
    )
    assert not app.exception
    assert len(app.tabs) >= 15


def test_narrow_layout_rules_keep_a_critical_simulator_action_working(tmp_path, monkeypatch):
    source = (PROJECT_ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    assert "@media (max-width: 900px)" in source
    assert '[data-testid="column"]' in source
    assert 'overflow-x: auto' in source
    monkeypatch.setenv("QUANTLAB_DATABASE_PATH", str(tmp_path / "narrow-ui.db"))

    app = AppTest.from_file("dashboard/app.py").run(timeout=60)
    app.radio[0].set_value("组合与交易").run(timeout=60)
    create = next(button for button in app.button if button.label == "创建模拟账户")
    create.click()
    app.run(timeout=60)

    assert not app.exception
    assert user_simulator_repository(Settings.load()).accounts()
