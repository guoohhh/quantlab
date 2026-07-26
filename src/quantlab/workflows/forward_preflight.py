from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from quantlab.config import Settings
from quantlab.persistence.migrations import ensure_database_initialized
from quantlab.runtime.readiness import primary_start_readiness
from quantlab.workflows.trusted_data_adapters import FreeTrustedDataAdapter


MARKET_TZ = ZoneInfo("Asia/Shanghai")
FORMAL_EVIDENCE_TABLES = (
    "pit_pool_snapshots",
    "forward_experiment_protocols",
    "forward_registration_runs",
    "forward_registration_samples",
    "shadow_accounts",
)


def morning_forward_preflight(
    settings: Settings,
    *,
    as_of: date | None = None,
    now: datetime | None = None,
    adapter: FreeTrustedDataAdapter | None = None,
) -> dict[str, Any]:
    """Read-only source and runtime inspection before the natural close-window jobs."""

    observed = (now or datetime.now(UTC)).astimezone(UTC)
    target = as_of or observed.astimezone(MARKET_TZ).date()
    if target != observed.astimezone(MARKET_TZ).date():
        raise ValueError("forward preflight is restricted to the server's current market date")
    path = settings.resolve(settings.get("system.database_path"))
    ensure_database_initialized(path)
    before = _formal_row_counts(path)
    provider_probe = (adapter or FreeTrustedDataAdapter(settings)).preflight(target)
    readiness = primary_start_readiness(
        settings,
        trade_date=target,
        now=observed,
        require_runtime=True,
    )
    after = _formal_row_counts(path)
    if before != after:
        raise RuntimeError("read-only forward preflight changed formal evidence tables")

    checks = list(provider_probe.get("checks") or [])
    available_components = {
        (str(item.get("provider")), str(item.get("component")))
        for item in checks
        if item.get("status") == "available"
    }
    runtime_healthy = all(
        bool(readiness.get("processes", {}).get(name, {}).get("healthy"))
        for name in ("worker", "scheduler")
    )
    conditions = {
        "trusted_calendar_open": bool(provider_probe.get("calendar_open")),
        "security_master_available": int(
            provider_probe.get("security_master_records") or 0
        )
        > 0,
        "point_in_time_universe_available": int(
            provider_probe.get("point_in_time_universe_records") or 0
        )
        > 0,
        "tencent_quote_reachable": ("tencent_quote", "market_spot")
        in available_components,
        "tencent_required_schema_ready": bool(
            provider_probe.get("tencent_expected_formal_coverage_ready")
        ),
        "tencent_market_date_is_today": bool(
            provider_probe.get("tencent_market_date_matches_request")
        ),
        "quality_gate_current": bool(readiness.get("quality_gate", {}).get("ready")),
        "runtime_healthy": runtime_healthy,
    }
    conditions["point_in_time_universe_or_master_quote_fallback_ready"] = bool(
        conditions["point_in_time_universe_available"]
        or (
            conditions["security_master_available"]
            and conditions["tencent_quote_reachable"]
            and conditions["tencent_required_schema_ready"]
            and conditions["tencent_market_date_is_today"]
        )
    )
    expected_after_refresh = all(
        value
        for key, value in conditions.items()
        if key
        not in {
            "point_in_time_universe_available",
            "tencent_market_date_is_today",
        }
    )
    return {
        "status": "ready_for_natural_refresh" if expected_after_refresh else "blocked",
        "read_only": True,
        "formal_signal_snapshot_created": False,
        "formal_evidence_counts_before": before,
        "formal_evidence_counts_after": after,
        "as_of": target.isoformat(),
        "checked_at": observed.isoformat(),
        "provider_probe": provider_probe,
        "current_readiness": readiness,
        "estimated_registration_conditions": conditions,
        "expected_registration_after_1510_refresh": expected_after_refresh,
        "current_registration_allowed": bool(
            readiness.get("sample_registration_allowed")
        ),
        "claim_boundary": (
            "Preflight does not create or repair a production pool, primary experiment, "
            "registration run, sample, signal, order, fill, or shadow account. A stale "
            "provider market date remains blocking until the natural 15:10 refresh."
        ),
    }


def _formal_row_counts(path) -> dict[str, int]:
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10) as db:
        tables = {
            str(row[0])
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        return {
            table: int(db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            if table in tables
            else 0
            for table in FORMAL_EVIDENCE_TABLES
        }


__all__ = ["morning_forward_preflight"]
