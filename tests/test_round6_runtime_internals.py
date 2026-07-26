from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from quantlab.config import Settings
from quantlab.persistence.round6 import Round6Repository
from quantlab.runtime import readiness as readiness_module
from quantlab.runtime import service as service_module
from quantlab.runtime.operations import backup_database
from quantlab.runtime.readiness import (
    primary_start_readiness,
    quality_source_fingerprint,
    runtime_health,
)
from quantlab.runtime.service import RuntimeServiceController, run_runtime_component
from quantlab.workflows import trusted_data_adapters as adapter_module
from quantlab.workflows.trusted_data import refresh_trusted_data
from quantlab.workflows.trusted_data_adapters import FreeTrustedDataAdapter


def _settings(tmp_path, *, test_mode: bool = False) -> Settings:
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
                "runtime_process_stale_after_seconds": 90,
                "runtime_health_maximum_age_seconds": 90,
                "quality_gate_maximum_age_hours": 168,
                "maximum_backup_age_hours": 200,
                "minimum_free_disk_gb": 0.0,
                "worker_idle_poll_seconds": 0.01,
                "scheduler_poll_seconds": 0.01,
                "notification_poll_seconds": 0.01,
            },
            "llm": {
                "provider": "local",
                "local_model": "round6-local-model",
                "local_base_url": "http://127.0.0.1:11434/v1",
            },
            "strategies": {
                "forward_primary": {
                    "candidate_count": 3,
                    "minimum_trust_level": "server_observed",
                },
                "a_share_v4": {"protocol_version": "round6-readiness-test"},
                "etf_rotation": {"universe": ["sh510300"]},
            },
            "costs": {"stock": {"trade_lot": 100}, "etf": {"trade_lot": 100}},
            "risk": {"max_single_position": 0.15},
        },
        root=tmp_path,
    )


class _ReadinessAdapter:
    provider_name = "readiness_fixture"
    provider_version = "v1"
    license_status = "unverified_no_sla"

    def __init__(self, observed: datetime):
        self.observed = observed

    def collect(self, as_of: date):
        symbols = ["sh600001", "sh600002", "sz000001"]
        return {
            "provider": self.provider_name,
            "provider_version": self.provider_version,
            "license_status": self.license_status,
            "fetched_at": self.observed - timedelta(minutes=5),
            "calendar": [
                {
                    "trade_date": (as_of + timedelta(days=offset)).isoformat(),
                    "is_open": (as_of + timedelta(days=offset)).weekday() < 5,
                }
                for offset in range(45)
            ],
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
                    "amount": 100_000_000.0,
                    "fund_size": 5_000_000_000.0,
                    "turnover_rate": 1.0,
                    "source": self.provider_name,
                    "methodology": "fixture",
                    "available_at": self.observed - timedelta(minutes=5),
                    "payload": {},
                }
                for symbol in symbols
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
                    "amount": 100_000_000.0 - index,
                    "fund_size": 5_000_000_000.0,
                    "liquidity_score": 100_000_000.0 - index,
                    "source": self.provider_name,
                    "available_at": self.observed - timedelta(minutes=5),
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


def test_readiness_success_and_runtime_health_cover_operational_components(tmp_path):
    observed = datetime(2026, 7, 20, 8, tzinfo=UTC)
    settings = _settings(tmp_path)
    report_path = tmp_path / "data" / "reports" / "quality-gate-latest.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps(
            {
                "status": "passed",
                "generated_at": (observed - timedelta(minutes=1)).isoformat(),
                "source_fingerprint": quality_source_fingerprint(settings),
                "quality_gates": {
                    "ruff": "passed",
                    "compileall": "passed",
                    "pytest": "passed",
                },
            }
        ),
        encoding="utf-8",
    )
    refresh_trusted_data(
        settings,
        as_of=date(2026, 7, 20),
        adapter=_ReadinessAdapter(observed),
    )
    processes = Round6Repository(settings.resolve("quantlab.db"))
    processes.claim_process("worker", instance_id="worker", now=observed)
    processes.claim_process("scheduler", instance_id="scheduler", now=observed)
    backup_database(settings, label="round6-health")

    readiness = primary_start_readiness(
        settings,
        trade_date=date(2026, 7, 20),
        now=observed,
    )
    health = runtime_health(settings, now=observed)

    assert readiness["start_allowed"] is True
    assert readiness["sample_registration_allowed"] is True
    assert health["database"]["healthy"] is True
    assert health["processes"]["worker"]["healthy"] is True
    assert health["backup"]["healthy"] is True
    assert health["formal_experiment"]["status"] == "not_started"


def test_primary_readiness_uses_pool_metadata_without_loading_pool_members(tmp_path, monkeypatch):
    """Readiness must remain a small metadata query for large PIT A-share pools."""

    settings = _settings(tmp_path, test_mode=True)
    observed = datetime(2026, 7, 20, 8, tzinfo=UTC)
    metadata_calls = []
    calendar_factory_calls = []

    class _MetadataOnlyStrategyRepository:
        def __init__(self, *_args, **_kwargs):
            pass

        def latest_pool_metadata(self, *args, **kwargs):
            metadata_calls.append((args, kwargs))
            return {
                "snapshot_id": "pit-metadata-only",
                "snapshot_date": "2026-07-20",
                "fingerprint": "metadata-fingerprint",
                "total_members": 5_537,
                "eligible_members": 4_991,
            }

        def latest_pool_snapshot(self, *_args, **_kwargs):
            raise AssertionError("readiness must not load a full PIT pool snapshot")

        def pool_snapshot(self, *_args, **_kwargs):
            raise AssertionError("readiness must not decode PIT pool member payloads")

    class _Calendar:
        def __init__(self):
            self.calls = []

        def day(self, value, **kwargs):
            self.calls.append(("day", value, kwargs))
            return {"is_open": True}

        def add_open_days(self, value, sessions, **kwargs):
            self.calls.append(("add_open_days", value, sessions, kwargs))
            return date(2026, 8, 17)

    calendar = _Calendar()

    class _CalendarService:
        @classmethod
        def from_settings(cls, configured_settings):
            calendar_factory_calls.append(configured_settings)
            return calendar

    monkeypatch.setattr(
        readiness_module,
        "StrategyEvidenceRepository",
        _MetadataOnlyStrategyRepository,
    )
    monkeypatch.setattr(readiness_module, "TradingCalendarService", _CalendarService)

    readiness = primary_start_readiness(
        settings,
        trade_date=date(2026, 7, 20),
        now=observed,
        require_runtime=False,
    )

    assert metadata_calls == [
        (
            ("a_share", date(2026, 7, 20)),
            {
                "namespace": readiness_module.DataNamespace.PRODUCTION,
                "minimum_trust": readiness_module.DataTrustLevel.SERVER_OBSERVED,
            },
        )
    ]
    assert len(calendar_factory_calls) == 1
    assert [call[0] for call in calendar.calls] == ["day", "add_open_days"]
    assert readiness["data"]["point_in_time_pool"] == {
        "ready": True,
        "snapshot_id": "pit-metadata-only",
        "fingerprint": "metadata-fingerprint",
        "total_members": 5_537,
        "eligible_members": 4_991,
    }
    assert readiness["data"]["calendar_horizon_end"] == "2026-08-17"
    assert readiness["is_trading_day"] is True
    assert readiness["start_allowed"] is True
    assert readiness["sample_registration_allowed"] is True


def test_readiness_and_scheduler_heartbeat_do_not_recursively_embed_prior_ticks(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path, test_mode=True)
    repository = Round6Repository(settings.resolve("quantlab.db"))
    repository.claim_process(
        "scheduler",
        instance_id="scheduler",
        detail={
            "last_result": {
                "readiness": {
                    "processes": {
                        "scheduler": {"detail": {"last_result": "recursive"}}
                    }
                }
            }
        },
    )
    readiness = primary_start_readiness(settings, require_runtime=False)
    assert "detail" not in readiness["processes"]["scheduler"]
    assert len(json.dumps(readiness, default=str)) < 30_000

    class _Scheduler:
        def __init__(self, *_args, **_kwargs):
            pass

        def tick(self, **_kwargs):
            return {
                "run_date": "2026-07-20",
                "calendar": {"is_open": True, "manifest_id": "calendar"},
                "jobs": [{"job_id": "job", "job_type": "refresh", "payload": {"large": "x" * 1000}}],
                "skipped": [],
                "readiness": readiness,
                "idempotent": True,
            }

    monkeypatch.setattr(service_module, "RuntimeScheduler", _Scheduler)
    loop_repository = _LoopRepository()
    service_module._run_scheduler(settings, loop_repository, "scheduler")
    detail = loop_repository.heartbeats[0][1]["detail"]
    encoded = json.dumps(detail)
    assert "recursive" not in encoded
    assert "large" not in encoded
    assert len(encoded) < 10_000


def test_runtime_health_reports_missing_process_backup_and_wrong_signal_date(tmp_path):
    settings = _settings(tmp_path)
    wrong_date = date(2026, 7, 19)
    observed = datetime(2026, 7, 20, 8, tzinfo=UTC)

    readiness = primary_start_readiness(
        settings,
        trade_date=wrong_date,
        now=observed,
    )
    health = runtime_health(settings, now=observed)

    assert "formal_signal_date_must_equal_server_market_date" in readiness["blockers"]
    assert any(item["code"] == "runtime_process_unhealthy" for item in health["alerts"])
    assert any(item["code"] == "database_backup_stale_or_missing" for item in health["alerts"])


def test_quality_gate_status_handles_invalid_payload(tmp_path):
    settings = _settings(tmp_path)
    path = tmp_path / "data" / "reports" / "quality-gate-latest.json"
    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")
    result = readiness_module._quality_gate_status(settings, datetime.now(UTC))
    assert result["ready"] is False
    assert result["status"] == "invalid"


def test_runtime_component_claim_success_duplicate_and_failure(tmp_path, monkeypatch):
    settings = _settings(tmp_path, test_mode=True)
    monkeypatch.setattr(
        service_module,
        "_run_job_worker",
        lambda *_args: {"status": "stopped", "handled_jobs": 0},
    )
    completed = run_runtime_component(settings, "worker", instance_id="worker-one")
    assert completed["status"] == "stopped"

    repository = Round6Repository(settings.resolve("quantlab.db"))
    repository.claim_process("worker", instance_id="active-worker")
    duplicate = run_runtime_component(settings, "worker", instance_id="worker-two")
    assert duplicate["claimed"] is False

    monkeypatch.setattr(
        service_module,
        "_run_scheduler",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError, match="boom"):
        run_runtime_component(settings, "scheduler", instance_id="scheduler-failed")
    failed = next(
        item for item in repository.processes() if item["process_type"] == "scheduler"
    )
    assert failed["status"] == "failed"
    with pytest.raises(ValueError, match="unsupported"):
        run_runtime_component(settings, "unknown")


def test_runtime_controller_start_stop_and_status(tmp_path, monkeypatch):
    settings = _settings(tmp_path, test_mode=True)
    controller = RuntimeServiceController(settings)
    launched_pids = iter([101, 102, 103, 104])

    class _Process:
        def __init__(self, *_args, **_kwargs):
            self.pid = next(launched_pids)

    monkeypatch.setattr(service_module.subprocess, "Popen", _Process)
    started = controller.start()
    assert [item["status"] for item in started["components"]] == ["launched"] * 4

    repository = controller.repository
    repository.claim_process("worker", instance_id="active-worker", pid=222)
    killed = []
    monkeypatch.setattr(service_module.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    stopped = controller.stop(grace_seconds=0)
    assert stopped["status"] == "stop_signalled"
    assert killed

    monkeypatch.setattr(service_module, "runtime_health", lambda _settings: {"status": "ok"})
    assert controller.status() == {"status": "ok"}


class _LoopRepository:
    def __init__(self):
        self.stop_checks = 0
        self.heartbeats = []

    def stop_requested(self, *_args):
        self.stop_checks += 1
        return self.stop_checks > 1

    def heartbeat_process(self, *args, **kwargs):
        self.heartbeats.append((args, kwargs))
        return True


def test_managed_worker_scheduler_and_notification_loops_stop_cooperatively(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path, test_mode=True)

    class _Worker:
        def __init__(self, *_args, **_kwargs):
            pass

        def run_once(self):
            return {"status": "completed"}

    monkeypatch.setattr(service_module, "JobWorker", _Worker)
    worker_repository = _LoopRepository()
    worker_result = service_module._run_job_worker(settings, worker_repository, "worker")
    assert worker_result["handled_jobs"] == 1
    assert worker_repository.heartbeats

    class _Scheduler:
        def __init__(self, *_args, **_kwargs):
            pass

        def tick(self, **_kwargs):
            return {"jobs": []}

    monkeypatch.setattr(service_module, "RuntimeScheduler", _Scheduler)
    scheduler_repository = _LoopRepository()
    scheduler_result = service_module._run_scheduler(
        settings, scheduler_repository, "scheduler"
    )
    assert scheduler_result["ticks"] == 1

    class _NotificationWorker:
        def __init__(self, *_args, **_kwargs):
            pass

        def run_once(self, **_kwargs):
            return {"external_delivered": 2}

    monkeypatch.setattr(service_module, "NotificationDeliveryWorker", _NotificationWorker)
    notification_repository = _LoopRepository()
    notification_result = service_module._run_notification_worker(
        settings, notification_repository, "notification_worker"
    )
    assert notification_result["deliveries"] == 2


def test_managed_scheduler_records_handler_failure(tmp_path, monkeypatch):
    settings = _settings(tmp_path, test_mode=True)

    class _Scheduler:
        def __init__(self, *_args, **_kwargs):
            pass

        def tick(self, **_kwargs):
            raise ValueError("calendar unavailable")

    monkeypatch.setattr(service_module, "RuntimeScheduler", _Scheduler)
    repository = _LoopRepository()
    result = service_module._run_scheduler(settings, repository, "scheduler")
    assert result["last_result"]["status"] == "failed"


def test_managed_api_uses_uvicorn_and_returns_cleanly(tmp_path, monkeypatch):
    settings = _settings(tmp_path, test_mode=True)

    class _Config:
        def __init__(self, _app, *, host, port, log_level):
            self.host = host
            self.port = port
            self.log_level = log_level

    class _Server:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        def run(self):
            return None

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(Config=_Config, Server=_Server))
    repository = _LoopRepository()
    repository.stop_checks = -10
    result = service_module._run_api(settings, repository, "api")
    assert result["port"] == 8000


def test_free_adapter_collects_and_preserves_partial_failures(tmp_path, monkeypatch):
    settings = _settings(tmp_path, test_mode=True)
    adapter = FreeTrustedDataAdapter(settings)

    class _BaoStock:
        def trading_calendar(self, start, end):
            return [{"trade_date": "2026-07-20", "is_open": True}]

        def security_master_records(self):
            return [
                {
                    "symbol": "sh600001",
                    "name": "Fixture Bank",
                    "exchange": "SH",
                    "board": "main",
                    "listing_date": "2010-01-01",
                    "delisting_date": None,
                }
            ]

        def industry_records(self, *, as_of):
            return [
                {
                    "symbol": "sh600001",
                    "name": "Fixture Bank",
                    "industry": "bank",
                    "classification": "fixture",
                    "effective_date": as_of.isoformat(),
                }
            ]

        def point_in_time_universe(self, _day):
            return [
                SimpleNamespace(
                    symbol="sh600001",
                    name="Fixture Bank",
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
        lambda _day: {
            "sh600001": {
                "name": "Fixture Bank",
                "price": 10.0,
                "amount": 100_000_000.0,
                "turnover_rate": 1.0,
                "market_cap": 5_000_000_000.0,
                "industry": "bank",
                "provider": "akshare_eastmoney",
                "available_at": datetime.now(UTC).isoformat(),
            }
        },
    )
    bundle = adapter.collect(date(2026, 7, 20))
    assert bundle["pool_members"][0]["eligible"] is True
    assert bundle["pool_members"][0]["representative_rank"] == 1
    assert bundle["industry"][0]["industry"] == "bank"
    assert bundle["raw_fingerprint"]

    class _BrokenBaoStock:
        def trading_calendar(self, *_args):
            return [{"trade_date": "2026-07-20", "is_open": True}]

        def security_master_records(self):
            raise RuntimeError("master")

        def industry_records(self, *, as_of):
            raise RuntimeError(f"industry {as_of}")

        def point_in_time_universe(self, _day):
            raise RuntimeError("universe")

    adapter.baostock = _BrokenBaoStock()
    monkeypatch.setattr(
        adapter_module,
        "_akshare_spot",
        lambda _day: (_ for _ in ()).throw(RuntimeError("spot")),
    )
    monkeypatch.setattr(
        adapter_module,
        "_tencent_spot",
        lambda _day, _symbols: (_ for _ in ()).throw(RuntimeError("tencent spot")),
    )
    monkeypatch.setattr(
        adapter_module,
        "_akshare_sina_spot",
        lambda _day: (_ for _ in ()).throw(RuntimeError("sina spot")),
    )
    degraded = adapter.collect(date(2026, 7, 20))
    assert len(degraded["failures"]) == 6
    assert degraded["pool_members"] == []


def test_akshare_spot_schema_and_validation(monkeypatch):
    today = datetime.now(UTC).astimezone(adapter_module.MARKET_TZ).date()
    frame = pd.DataFrame(
        [
            {
                "代码": "600001",
                "名称": "Fixture",
                "最新价": 10.0,
                "成交额": 100.0,
                "换手率": 1.0,
                "总市值": 1000.0,
                "所属行业": "bank",
            }
        ]
    )
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(stock_zh_a_spot_em=lambda: frame),
    )
    result = adapter_module._akshare_spot(today)
    assert result["sh600001"]["industry"] == "bank"
    with pytest.raises(ValueError, match="cannot be backfilled"):
        adapter_module._akshare_spot(today - timedelta(days=1))

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(stock_zh_a_spot_em=lambda: pd.DataFrame([{"名称": "bad"}])),
    )
    with pytest.raises(ValueError, match="no security code"):
        adapter_module._akshare_spot(today)


def test_adapter_numeric_helpers():
    assert adapter_module._optional_float("1.5") == 1.5
    assert adapter_module._optional_float("nan") is None
    assert adapter_module._optional_float("bad") is None
    assert adapter_module._optional_float(-1) is None
    failure = adapter_module._failure("calendar", ValueError("bad"))
    assert failure["component"] == "calendar"
