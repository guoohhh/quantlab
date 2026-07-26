from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from quantlab.config import Settings
from quantlab.domain.data_governance import DataNamespace, DataTrustLevel
from quantlab.market import TradingCalendarService
from quantlab.persistence.jobs import JobRepository
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
from quantlab.runtime.scheduler import RuntimeScheduler
from quantlab.runtime.scheduler import DEFAULT_SCHEDULES
from quantlab.workflows import trusted_data_adapters as adapter_module
from quantlab.workflows.forward_preflight import morning_forward_preflight
from quantlab.workflows.trusted_data_adapters import FreeTrustedDataAdapter
from quantlab.workflows.trusted_data import (
    _ingest_automatic_pool,
    snapshot_time_invariant,
)


def _settings(tmp_path) -> Settings:
    return Settings(
        values={
            "system": {
                "database_path": str(tmp_path / "quantlab.db"),
                "data_dir": str(tmp_path / "data"),
                "test_mode": True,
                "timezone": "Asia/Shanghai",
            },
            "runtime": {
                "trusted_provider_timeout_seconds": 1.0,
                "trusted_provider_max_attempts": 1,
                "trusted_provider_failure_threshold": 3,
                "trusted_provider_cooldown_seconds": 1.0,
                "trusted_data_minimum_field_coverage": 0.8,
                "runtime_health_maximum_age_seconds": 90,
            },
            "llm": {"provider": "mock", "allow_mock_fallback": True},
            "strategies": {
                "forward_primary": {
                    "candidate_count": 3,
                    "minimum_trust_level": "server_observed",
                }
            },
        },
        root=tmp_path,
    )


def _tencent_body(market_date: date) -> bytes:
    fields = [""] * 50
    fields[0] = "1"
    fields[1] = "测试银行"
    fields[2] = "600001"
    fields[3] = "10.25"
    fields[30] = f"{market_date:%Y%m%d}151001"
    fields[35] = "10.25/12345/987654321"
    fields[38] = "1.23"
    fields[44] = "40.00"
    fields[45] = "50.50"
    return f'v_sh600001="{"~".join(fields)}";\n'.encode("gb18030")


class _Response:
    def __init__(self, body: bytes):
        self.content = body

    def raise_for_status(self) -> None:
        return None


def test_tencent_quote_preserves_units_timestamp_fingerprint_and_rejects_stale(
    monkeypatch,
):
    today = datetime.now(UTC).astimezone(adapter_module.MARKET_TZ).date()
    monkeypatch.setattr(
        adapter_module.httpx,
        "get",
        lambda *_args, **_kwargs: _Response(_tencent_body(today)),
    )

    result = adapter_module._tencent_spot(today, ["sh600001"])
    record = result["sh600001"]
    assert record["amount"] == 987_654_321.0
    assert record["turnover_rate"] == 1.23
    assert record["market_cap"] == 5_050_000_000.0
    assert record["provider_market_date"] == today.isoformat()
    assert record["provider_timestamp"].startswith(today.isoformat())
    assert len(record["provider_response_fingerprint"]) == 64
    assert record["market_cap_unit"] == "CNY"

    yesterday = today - timedelta(days=1)
    monkeypatch.setattr(
        adapter_module.httpx,
        "get",
        lambda *_args, **_kwargs: _Response(_tencent_body(yesterday)),
    )
    with pytest.raises(ValueError, match="observed_market_dates"):
        adapter_module._tencent_spot(today, ["sh600001"])
    stale_probe = adapter_module._tencent_spot(
        today,
        ["sh600001"],
        require_market_date=False,
    )
    assert stale_probe["sh600001"]["provider_market_date"] == yesterday.isoformat()


def test_eastmoney_failure_selects_tencent_and_keeps_failure_audit(tmp_path, monkeypatch):
    today = datetime.now(UTC).astimezone(adapter_module.MARKET_TZ).date()
    adapter = FreeTrustedDataAdapter(_settings(tmp_path))

    class _BaoStock:
        def trading_calendar(self, *_args):
            return [{"trade_date": today.isoformat(), "is_open": True}]

        def security_master_records(self):
            return [
                {
                    "symbol": "sh600001",
                    "name": "测试银行",
                    "exchange": "SH",
                    "board": "main",
                    "listing_date": "2010-01-01",
                    "delisting_date": None,
                    "status": "listed",
                }
            ]

        def industry_records(self, *, as_of):
            return [
                {
                    "symbol": "sh600001",
                    "industry": "银行",
                    "classification": "fixture",
                    "effective_date": as_of.isoformat(),
                }
            ]

        def point_in_time_universe(self, _day):
            return [
                SimpleNamespace(
                    symbol="sh600001",
                    name="测试银行",
                    exchange="SH",
                    board="main",
                    trade_status=True,
                    source_symbol="sh600001",
                )
            ]

    adapter.baostock = _BaoStock()
    monkeypatch.setattr(
        adapter_module,
        "_akshare_spot",
        lambda _day: (_ for _ in ()).throw(RuntimeError("Eastmoney blocked")),
    )
    monkeypatch.setattr(
        adapter_module,
        "_tencent_spot",
        lambda _day, _symbols: {
            "sh600001": {
                "name": "测试银行",
                "price": 10.25,
                "amount": 987_654_321.0,
                "turnover_rate": 1.23,
                "market_cap": 5_050_000_000.0,
                "industry": "",
                "provider": "tencent_quote",
                "available_at": datetime.now(UTC).isoformat(),
                "provider_timestamp": datetime.now(adapter_module.MARKET_TZ).isoformat(),
                "provider_market_date": today.isoformat(),
                "provider_response_fingerprint": "a" * 64,
                "provider_batch_index": 1,
            }
        },
    )
    monkeypatch.setattr(
        adapter_module,
        "_akshare_sina_spot",
        lambda _day: (_ for _ in ()).throw(AssertionError("Sina should not be selected")),
    )

    bundle = adapter.collect(today)
    selection = bundle["selected_providers"]["current_spot"]
    assert selection["selected_provider"] == "tencent_quote"
    assert selection["reason"] == "fallback_selected_after_higher_priority_failure"
    assert any(item["provider"] == "akshare_eastmoney" for item in bundle["failures"])
    assert bundle["pool_members"][0]["eligible"] is True
    assert bundle["pool_members"][0]["payload"]["spot_provider_market_date"] == (
        today.isoformat()
    )


def test_empty_baostock_universe_routes_tencent_from_trusted_security_master(
    tmp_path,
    monkeypatch,
):
    today = datetime.now(UTC).astimezone(adapter_module.MARKET_TZ).date()
    adapter = FreeTrustedDataAdapter(_settings(tmp_path))

    class _BaoStock:
        def trading_calendar(self, *_args):
            return [{"trade_date": today.isoformat(), "is_open": True}]

        def security_master_records(self):
            return [
                {
                    "symbol": "sh600001",
                    "name": "测试银行",
                    "exchange": "SH",
                    "board": "main",
                    "listing_date": "2010-01-01",
                    "delisting_date": None,
                    "status": "listed",
                }
            ]

        def industry_records(self, *, as_of):
            return [
                {
                    "symbol": "sh600001",
                    "industry": "银行",
                    "classification": "fixture",
                    "effective_date": as_of.isoformat(),
                }
            ]

        def point_in_time_universe(self, _day):
            return []

    adapter.baostock = _BaoStock()
    monkeypatch.setattr(
        adapter_module,
        "_akshare_spot",
        lambda _day: (_ for _ in ()).throw(RuntimeError("Eastmoney blocked")),
    )
    observed_symbols = []

    def tencent(_day, symbols):
        observed_symbols.extend(symbols)
        return {
            "sh600001": {
                "name": "测试银行",
                "price": 10.25,
                "amount": 987_654_321.0,
                "turnover_rate": 1.23,
                "market_cap": 5_050_000_000.0,
                "industry": "",
                "provider": "tencent_quote",
                "available_at": datetime.now(UTC).isoformat(),
                "provider_timestamp": datetime.now(adapter_module.MARKET_TZ).isoformat(),
                "provider_market_date": today.isoformat(),
                "provider_response_fingerprint": "c" * 64,
                "provider_batch_index": 1,
            }
        }

    monkeypatch.setattr(adapter_module, "_tencent_spot", tencent)
    bundle = adapter.collect(today)

    assert observed_symbols == ["sh600001"]
    assert bundle["selected_providers"]["current_spot"]["selected_provider"] == (
        "tencent_quote"
    )
    member = bundle["pool_members"][0]
    assert member["eligible"] is True
    assert member["payload"]["trade_status"] is True
    assert member["payload"]["field_observations"]["market_cap"]["source"] == (
        "tencent_quote"
    )
    assert any(
        item["provider"] == "baostock" and item["component"] == "point_in_time_universe"
        for item in bundle["failures"]
    )


def test_live_calendar_failure_uses_trusted_cache_and_still_routes_current_quotes(
    tmp_path,
    monkeypatch,
):
    today = datetime.now(UTC).astimezone(adapter_module.MARKET_TZ).date()
    settings = _settings(tmp_path)
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    observed_at = datetime.now(UTC) - timedelta(minutes=5)
    calendar_manifest = repository.create_manifest(
        batch_type="trading_calendar",
        namespace=DataNamespace.PRODUCTION,
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        provider="fixture",
        source="fixture",
        endpoint="fixture",
        source_version="fixture-v1",
        available_at=observed_at,
        license_status="fixture",
        payload=[{"trade_date": today.isoformat(), "is_open": True}],
        raw_fingerprint="calendar-fixture",
        record_count=1,
        date_start=today,
        date_end=today,
    )
    repository.save_calendar_days(
        calendar_manifest["manifest_id"],
        [{"trade_date": today.isoformat(), "is_open": True}],
    )
    industry_manifest = repository.create_manifest(
        batch_type="industry_membership",
        namespace=DataNamespace.PRODUCTION,
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        provider="fixture",
        source="fixture",
        endpoint="fixture",
        source_version="fixture-v1",
        available_at=observed_at,
        license_status="fixture",
        payload=[
            {
                "symbol": "sh600001",
                "industry": "bank",
                "effective_date": today.isoformat(),
            }
        ],
        raw_fingerprint="industry-fixture",
        record_count=1,
        date_start=today,
        date_end=today,
    )
    repository.save_industry_memberships(
        industry_manifest["manifest_id"],
        [
            {
                "symbol": "sh600001",
                "industry": "bank",
                "effective_date": today.isoformat(),
            }
        ],
    )

    class _BaoStock:
        def trading_calendar(self, *_args):
            return []

        def security_master_records(self):
            return [
                {
                    "symbol": "sh600001",
                    "name": "Fixture Bank",
                    "exchange": "SH",
                    "board": "main",
                    "listing_date": "2010-01-01",
                    "delisting_date": None,
                    "status": "listed",
                }
            ]

        def industry_records(self, *, as_of):
            return []

        def point_in_time_universe(self, _day):
            return []

    adapter = FreeTrustedDataAdapter(settings)
    adapter.baostock = _BaoStock()
    monkeypatch.setattr(
        adapter_module,
        "_akshare_spot",
        lambda _day: (_ for _ in ()).throw(RuntimeError("Eastmoney blocked")),
    )
    monkeypatch.setattr(
        adapter_module,
        "_tencent_spot",
        lambda _day, symbols: {
            symbol: {
                "name": "Fixture Bank",
                "price": 10.25,
                "amount": 987_654_321.0,
                "turnover_rate": 1.23,
                "market_cap": 5_050_000_000.0,
                "industry": "",
                "provider": "tencent_quote",
                "available_at": datetime.now(UTC).isoformat(),
                "provider_timestamp": datetime.now(
                    adapter_module.MARKET_TZ
                ).isoformat(),
                "provider_market_date": today.isoformat(),
                "provider_response_fingerprint": "d" * 64,
                "provider_batch_index": 1,
            }
            for symbol in symbols
        },
    )

    bundle = adapter.collect(today)

    assert bundle["selected_providers"]["calendar"]["selected_provider"] == (
        "cached_trusted_calendar"
    )
    assert bundle["selected_providers"]["industry"]["selected_provider"] == (
        "cached_trusted_industry"
    )
    assert bundle["selected_providers"]["current_spot"]["selected_provider"] == (
        "tencent_quote"
    )
    assert bundle["snapshot_status"] == "trading_day_collection"
    assert bundle["pool_members"][0]["eligible"] is True
    assert bundle["pool_members"][0]["payload"]["industry"] == "bank"


def test_same_day_pool_refresh_appends_trade_status_and_snapshot_versions(tmp_path):
    settings = _settings(tmp_path)
    today = datetime.now(UTC).astimezone(adapter_module.MARKET_TZ).date()

    def bundle(refresh_id: str, price: float, observed: datetime):
        member = {
            "symbol": "sh600001",
            "name": "测试银行",
            "asset_class": "equity",
            "category": "银行",
            "eligible": True,
            "exclusion_reasons": [],
            "representative": True,
            "representative_rank": 1,
            "amount": 100_000_000.0,
            "fund_size": 5_000_000_000.0,
            "liquidity_score": 100_000_000.0,
            "source": "baostock+tencent_quote",
            "available_at": observed,
            "data_quality": "available",
            "missing_fields": [],
            "payload": {
                "exchange": "SH",
                "listing_date": "2010-01-01",
                "delisting_date": None,
                "trade_date": today.isoformat(),
                "listed": True,
                "trade_status": True,
                "is_st": False,
                "suspended": False,
                "turnover_rate": 1.2,
                "latest_price": price,
                "market_cap": 5_000_000_000.0,
                "industry": "银行",
            },
        }
        return {
            "refresh_id": refresh_id,
            "provider": "baostock+tencent_quote",
            "provider_version": "fixture-v1",
            "license_status": "fixture",
            "fetched_at": observed,
            "failures": [],
            "provider_attempts": [],
            "selected_providers": {},
            "pool_members": [member],
            "trade_status": [
                {
                    "symbol": "sh600001",
                    "trade_date": today,
                    "trade_status": True,
                    "suspended": False,
                    "is_st": False,
                    "amount": 100_000_000.0,
                    "fund_size": 5_000_000_000.0,
                    "turnover_rate": 1.2,
                    "source": "baostock+tencent_quote",
                    "methodology": "master_and_server_observed_quote_status_v2",
                    "available_at": observed,
                    "payload": {"latest_price": price},
                }
            ],
        }

    first = _ingest_automatic_pool(
        settings,
        bundle("refresh-1", 10.0, datetime(2026, 7, 21, 7, 20, tzinfo=UTC)),
        trust=DataTrustLevel.SERVER_OBSERVED,
        as_of=today,
    )
    second = _ingest_automatic_pool(
        settings,
        bundle("refresh-2", 10.2, datetime(2026, 7, 21, 8, 20, tzinfo=UTC)),
        trust=DataTrustLevel.SERVER_OBSERVED,
        as_of=today,
    )
    repository = StrategyEvidenceRepository(settings.resolve("quantlab.db"))
    with repository.connect() as db:
        rows = db.execute(
            """SELECT methodology,record_fingerprint FROM pit_trade_status
               WHERE symbol='sh600001' ORDER BY available_at"""
        ).fetchall()

    assert first["snapshot"]["snapshot_id"] != second["snapshot"]["snapshot_id"]
    assert len(rows) == 2
    assert rows[0]["methodology"].endswith("refresh:refresh-1")
    assert rows[1]["methodology"].endswith("refresh:refresh-2")
    assert rows[0]["record_fingerprint"] != rows[1]["record_fingerprint"]


def test_scheduler_same_day_recovery_creates_new_audited_attempt(tmp_path):
    settings = _settings(tmp_path)
    today = date(2026, 7, 21)
    observed = datetime.combine(today, time(17, 30), tzinfo=adapter_module.MARKET_TZ)
    TradingCalendarService.from_settings(settings).ingest(
        [{"trade_date": today.isoformat(), "is_open": True}],
        namespace=DataNamespace.PRODUCTION,
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        provider="fixture",
        source="fixture",
        endpoint="fixture",
        source_version="fixture-v1",
        available_at=observed.astimezone(UTC) - timedelta(minutes=1),
        license_status="fixture",
        raw_fingerprint="d" * 64,
    )
    scheduler = RuntimeScheduler(settings)
    scheduler.register_defaults()
    repository = JobRepository(settings.resolve("quantlab.db"))
    schedules = {item["name"]: item for item in repository.schedules()}

    def completed_schedule(name: str, result: dict):
        schedule = schedules[name]
        run = repository.create_schedule_run(
            schedule_id=schedule["schedule_id"],
            run_date=today,
            is_backfill=False,
            payload={"fixture": True},
        )
        job = repository.submit(
            job_type=schedule["job_type"],
            payload={"as_of": today.isoformat()},
            idempotency_key=f"fixture:{name}:{today}",
            available_at=observed.astimezone(UTC) - timedelta(hours=2),
            schedule_run_id=run["schedule_run_id"],
        )
        repository.link_schedule_job(run["schedule_run_id"], job["job_id"])
        claimed = repository.claim(
            worker_id="fixture-worker",
            now=observed.astimezone(UTC) + timedelta(hours=1),
        )
        assert claimed and claimed["job_id"] == job["job_id"]
        repository.complete(job["job_id"], "fixture-worker", result=result)
        return run, job

    completed_schedule("prediction_settlement", {"status": "completed"})
    original_run, original_job = completed_schedule(
        "forward_sample_registration",
        {"status": "skipped", "reason": "primary_readiness_failed"},
    )
    recovered = scheduler.recover_same_day(
        "forward_sample_registration",
        reason="trusted pool repaired after append-only post-close refresh",
        now=observed,
    )
    runs = [
        item
        for item in repository.schedule_runs(today, 100)
        if item["schedule_id"] == original_run["schedule_id"]
    ]

    assert len(runs) == 2
    assert recovered["schedule_run"]["attempt_number"] == 2
    assert recovered["schedule_run"]["recovery_of_schedule_run_id"] == (
        original_run["schedule_run_id"]
    )
    assert recovered["job"]["idempotency_key"].endswith(":attempt:2")
    assert repository.job(original_job["job_id"])["status"] == "completed"


def test_new_formal_snapshot_finalizes_after_all_included_observations(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    market_date = date(2026, 7, 21)
    instants = iter(
        datetime(2026, 7, 21, 7, 0, second, tzinfo=UTC)
        for second in range(7)
    )
    adapter = FreeTrustedDataAdapter(settings, clock=lambda: next(instants))

    class _BaoStock:
        def trading_calendar(self, *_args):
            return [{"trade_date": market_date.isoformat(), "is_open": True}]

        def security_master_records(self):
            return [
                {
                    "symbol": "sh600001",
                    "name": "fixture",
                    "exchange": "SH",
                    "listing_date": "2010-01-01",
                    "delisting_date": None,
                    "status": "listed",
                }
            ]

        def industry_records(self, *, as_of):
            assert as_of == market_date
            return [
                {
                    "symbol": "sh600001",
                    "industry": "bank",
                    "classification": "fixture",
                    "effective_date": market_date.isoformat(),
                }
            ]

        def point_in_time_universe(self, _day):
            return [
                SimpleNamespace(
                    symbol="sh600001",
                    name="fixture",
                    exchange="SH",
                    board="main",
                    trade_status=True,
                    source_symbol="sh.600001",
                )
            ]

    adapter.baostock = _BaoStock()
    monkeypatch.setattr(
        adapter_module,
        "_akshare_spot",
        lambda _day: (_ for _ in ()).throw(ConnectionError("fixture")),
    )
    spot_observed = datetime(2026, 7, 21, 7, 0, 5, tzinfo=UTC)
    monkeypatch.setattr(
        adapter_module,
        "_tencent_spot",
        lambda _day, _symbols: {
            "sh600001": {
                "name": "fixture",
                "price": 10.0,
                "amount": 100_000_000.0,
                "turnover_rate": 1.2,
                "market_cap": 5_000_000_000.0,
                "industry": "bank",
                "provider": "tencent_quote",
                "available_at": spot_observed.isoformat(),
                "provider_timestamp": "2026-07-21T15:00:00+08:00",
                "provider_market_date": market_date.isoformat(),
                "provider_response_fingerprint": "a" * 64,
                "provider_batch_index": 1,
            }
        },
    )
    bundle = adapter.collect(market_date)
    finalized = datetime(2026, 7, 21, 7, 0, 6, tzinfo=UTC)

    assert bundle["refresh_started_at"] < bundle["refresh_finalized_at"]
    assert bundle["refresh_finalized_at"] == finalized
    assert bundle["snapshot_cutoff_at"] == finalized
    assert bundle["pool_members"][0]["available_at"] == spot_observed

    ingested = _ingest_automatic_pool(
        settings,
        bundle,
        trust=DataTrustLevel.SERVER_OBSERVED,
        as_of=market_date,
    )
    snapshot = StrategyEvidenceRepository(
        settings.resolve("quantlab.db")
    ).pool_snapshot(ingested["snapshot"]["snapshot_id"])
    audit = snapshot_time_invariant(
        snapshot,
        registration_started_at=finalized + timedelta(seconds=1),
    )

    assert audit["invariant_holds"] is True
    assert audit["field_observations_after_cutoff"] == 0
    assert audit["latest_field_available_at"] == spot_observed.isoformat()
    assert ingested["manifest"]["available_at"] == finalized.isoformat()
    assert ingested["manifest"]["refresh_started_at"] == (
        bundle["refresh_started_at"].isoformat()
    )
    assert ingested["manifest"]["refresh_finalized_at"] == finalized.isoformat()
    assert ingested["manifest"]["snapshot_cutoff_at"] == finalized.isoformat()


def test_formal_audit_exception_is_idempotent_and_immutable(tmp_path):
    repository = StrategyEvidenceRepository(tmp_path / "quantlab.db")
    payload = {
        "cutoff_at": "2026-07-21T09:57:02+00:00",
        "latest_field_available_at": "2026-07-21T09:58:17+00:00",
        "all_observations_before_registration": True,
        "all_observations_before_predictions": True,
    }
    first = repository.record_formal_audit_exception(
        exception_type="snapshot_cutoff_precedes_included_field_observation",
        severity="audit_required_non_predictive",
        experiment_id="experiment-1",
        registration_id="registration-1",
        snapshot_id="snapshot-1",
        market_date=date(2026, 7, 21),
        summary="fixture",
        payload=payload,
    )
    second = repository.record_formal_audit_exception(
        exception_type="snapshot_cutoff_precedes_included_field_observation",
        severity="audit_required_non_predictive",
        experiment_id="experiment-1",
        registration_id="registration-1",
        snapshot_id="snapshot-1",
        market_date=date(2026, 7, 21),
        summary="fixture",
        payload=payload,
    )

    assert second["exception_id"] == first["exception_id"]
    with repository.connect() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE formal_evidence_audit_exceptions SET summary='changed' WHERE exception_id=?",
            (first["exception_id"],),
        )
    with repository.connect() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "DELETE FROM formal_evidence_audit_exceptions WHERE exception_id=?",
            (first["exception_id"],),
        )


def test_scheduler_recovery_rejects_incomplete_dependency_and_is_active_idempotent(
    tmp_path,
):
    settings = _settings(tmp_path)
    run_date = date(2026, 7, 21)
    observed = datetime(2026, 7, 21, 17, 30, tzinfo=adapter_module.MARKET_TZ)
    TradingCalendarService.from_settings(settings).ingest(
        [{"trade_date": run_date.isoformat(), "is_open": True}],
        namespace=DataNamespace.PRODUCTION,
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        provider="fixture",
        source="fixture",
        endpoint="fixture",
        source_version="fixture-v1",
        available_at=observed.astimezone(UTC) - timedelta(minutes=1),
        license_status="fixture",
        raw_fingerprint="e" * 64,
    )
    scheduler = RuntimeScheduler(settings)
    scheduler.register_defaults()
    repository = JobRepository(settings.resolve("quantlab.db"))
    schedules = {item["name"]: item for item in repository.schedules()}

    target = schedules["forward_sample_registration"]
    target_run = repository.create_schedule_run(
        schedule_id=target["schedule_id"],
        run_date=run_date,
        is_backfill=False,
        payload={"fixture": True},
    )
    target_job = repository.submit(
        job_type=target["job_type"],
        payload={"as_of": run_date.isoformat()},
        idempotency_key="fixed-target",
        available_at=observed.astimezone(UTC) - timedelta(hours=2),
        schedule_run_id=target_run["schedule_run_id"],
    )
    repository.link_schedule_job(target_run["schedule_run_id"], target_job["job_id"])
    claimed = repository.claim(worker_id="fixture", now=observed.astimezone(UTC))
    repository.complete(claimed["job_id"], "fixture", result={"status": "skipped"})

    dependency = schedules["prediction_settlement"]
    dependency_run = repository.create_schedule_run(
        schedule_id=dependency["schedule_id"],
        run_date=run_date,
        is_backfill=False,
        payload={"fixture": True},
    )
    dependency_job = repository.submit(
        job_type=dependency["job_type"],
        payload={"as_of": run_date.isoformat()},
        idempotency_key="fixed-dependency",
        available_at=observed.astimezone(UTC) - timedelta(hours=2),
        schedule_run_id=dependency_run["schedule_run_id"],
    )
    repository.link_schedule_job(
        dependency_run["schedule_run_id"], dependency_job["job_id"]
    )

    with pytest.raises(ValueError, match="dependencies are not completed"):
        scheduler.recover_same_day(
            "forward_sample_registration",
            reason="fixture dependency incomplete",
            now=observed,
        )

    claimed = repository.claim(worker_id="fixture", now=observed.astimezone(UTC))
    repository.complete(claimed["job_id"], "fixture", result={"status": "completed"})
    recovered = scheduler.recover_same_day(
        "forward_sample_registration",
        reason="fixture dependency completed",
        now=observed,
    )
    repeated = scheduler.recover_same_day(
        "forward_sample_registration",
        reason="duplicate active recovery",
        now=observed,
    )

    assert repeated["idempotent"] is True
    assert repeated["job"]["job_id"] == recovered["job"]["job_id"]
    assert repeated["schedule_run"]["attempt_number"] == 2


def test_scheduler_recovery_rejects_backfill_and_cross_midnight_previous_day(tmp_path):
    settings = _settings(tmp_path)
    first_day = date(2026, 7, 21)
    next_day = date(2026, 7, 22)
    observed = datetime(2026, 7, 21, 17, 30, tzinfo=adapter_module.MARKET_TZ)
    TradingCalendarService.from_settings(settings).ingest(
        [
            {"trade_date": first_day.isoformat(), "is_open": True},
            {"trade_date": next_day.isoformat(), "is_open": True},
        ],
        namespace=DataNamespace.PRODUCTION,
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        provider="fixture",
        source="fixture",
        endpoint="fixture",
        source_version="fixture-v1",
        available_at=observed.astimezone(UTC) - timedelta(minutes=1),
        license_status="fixture",
        raw_fingerprint="f" * 64,
    )
    scheduler = RuntimeScheduler(settings)
    scheduler.register_defaults()
    repository = JobRepository(settings.resolve("quantlab.db"))
    schedule = next(
        item
        for item in repository.schedules()
        if item["name"] == "forward_sample_registration"
    )
    backfill = repository.create_schedule_run(
        schedule_id=schedule["schedule_id"],
        run_date=first_day,
        is_backfill=True,
        payload={"fixture": True},
    )
    job = repository.submit(
        job_type=schedule["job_type"],
        payload={"as_of": first_day.isoformat(), "is_backfill": True},
        idempotency_key="fixed-backfill",
        available_at=observed.astimezone(UTC) - timedelta(hours=2),
        schedule_run_id=backfill["schedule_run_id"],
    )
    repository.link_schedule_job(backfill["schedule_run_id"], job["job_id"])

    with pytest.raises(ValueError, match="non-backfill original"):
        scheduler.recover_same_day(
            "forward_sample_registration",
            reason="backfill cannot become formal",
            now=observed,
        )
    with pytest.raises(ValueError, match="only allowed after the original schedule time"):
        scheduler.recover_same_day(
            "forward_sample_registration",
            reason="previous trading day cannot cross midnight",
            now=datetime(2026, 7, 22, 0, 5, tzinfo=adapter_module.MARKET_TZ),
        )


def test_real_adapter_preflight_reports_provider_schema_without_persisting_pool(
    tmp_path,
    monkeypatch,
):
    today = datetime.now(UTC).astimezone(adapter_module.MARKET_TZ).date()
    adapter = FreeTrustedDataAdapter(_settings(tmp_path))

    class _BaoStock:
        def trading_calendar(self, *_args):
            return [{"trade_date": today.isoformat(), "is_open": True}]

        def security_master_records(self):
            return [{"symbol": "sh600001"}]

        def point_in_time_universe(self, _day):
            return [SimpleNamespace(symbol="sh600001")]

    spot_record = {
        "sh600001": {
            "price": 10.0,
            "amount": 100_000_000.0,
            "turnover_rate": 1.0,
            "market_cap": 5_000_000_000.0,
            "provider_market_date": today.isoformat(),
            "provider_response_fingerprint": "b" * 64,
        }
    }
    adapter.baostock = _BaoStock()
    monkeypatch.setattr(adapter_module, "_akshare_spot", lambda _day: spot_record)
    monkeypatch.setattr(
        adapter_module,
        "_tencent_spot",
        lambda _day, _symbols, require_market_date=False: spot_record,
    )
    monkeypatch.setattr(adapter_module, "_akshare_sina_spot", lambda _day: spot_record)

    result = adapter.preflight(today, sample_limit=1)
    assert result["read_only"] is True
    assert result["formal_signal_snapshot_created"] is False
    assert result["calendar_open"] is True
    assert result["tencent_market_date_matches_request"] is True
    assert result["tencent_expected_formal_coverage_ready"] is True
    assert result["tencent_schema_coverage"] == {
        "price": 1.0,
        "amount": 1.0,
        "turnover_rate": 1.0,
        "market_cap": 1.0,
    }
    assert len(result["checks"]) == 6


def test_negative_daily_return_remains_a_signed_observation():
    assert adapter_module._optional_float(-2.08) is None
    assert adapter_module._optional_signed_float(-2.08) == pytest.approx(-2.08)


def test_morning_preflight_is_read_only_and_scheduled_before_refresh(tmp_path):
    today = datetime.now(UTC).astimezone(adapter_module.MARKET_TZ).date()

    class _Adapter:
        def preflight(self, as_of):
            return {
                "read_only": True,
                "formal_signal_snapshot_created": False,
                "calendar_open": True,
                "security_master_records": 5_000,
                "point_in_time_universe_records": 5_000,
                "tencent_expected_formal_coverage_ready": True,
                "tencent_market_date_matches_request": False,
                "checks": [
                    {
                        "provider": "tencent_quote",
                        "component": "market_spot",
                        "status": "available",
                    }
                ],
            }

    result = morning_forward_preflight(
        _settings(tmp_path),
        as_of=today,
        now=datetime.now(UTC),
        adapter=_Adapter(),
    )
    assert result["read_only"] is True
    assert result["formal_signal_snapshot_created"] is False
    assert result["formal_evidence_counts_before"] == result["formal_evidence_counts_after"]
    assert result["estimated_registration_conditions"]["tencent_market_date_is_today"] is False
    assert result["expected_registration_after_1510_refresh"] is False
    preflight = next(item for item in DEFAULT_SCHEDULES if item["name"] == "forward_preflight")
    refresh = next(item for item in DEFAULT_SCHEDULES if item["name"] == "trusted_data_refresh")
    assert preflight["local_time"] == "08:30"
    assert refresh["local_time"] == "15:10"


def test_morning_preflight_accepts_master_plus_current_tencent_universe_fallback(
    tmp_path, monkeypatch
):
    today = datetime.now(UTC).astimezone(adapter_module.MARKET_TZ).date()

    class _Adapter:
        def preflight(self, as_of):
            return {
                "calendar_open": True,
                "security_master_records": 5_000,
                "point_in_time_universe_records": 0,
                "tencent_expected_formal_coverage_ready": True,
                "tencent_market_date_matches_request": True,
                "checks": [
                    {
                        "provider": "tencent_quote",
                        "component": "market_spot",
                        "status": "available",
                    }
                ],
            }

    monkeypatch.setattr(
        "quantlab.workflows.forward_preflight.primary_start_readiness",
        lambda *_args, **_kwargs: {
            "sample_registration_allowed": False,
            "blockers": ["pool_not_refreshed_yet"],
            "quality_gate": {"ready": True},
            "processes": {
                "worker": {"healthy": True},
                "scheduler": {"healthy": True},
            },
        },
    )
    result = morning_forward_preflight(
        _settings(tmp_path),
        as_of=today,
        now=datetime.now(UTC),
        adapter=_Adapter(),
    )
    assert result["status"] == "ready_for_natural_refresh"
    assert result["expected_registration_after_1510_refresh"] is True
    assert result["estimated_registration_conditions"][
        "point_in_time_universe_or_master_quote_fallback_ready"
    ] is True
