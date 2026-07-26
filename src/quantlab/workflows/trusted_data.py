from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from quantlab.config import Settings
from quantlab.domain.data_governance import DataNamespace, DataTrustLevel
from quantlab.domain.strategy_evidence import (
    EvidenceStage,
    PointInTimePoolSnapshot,
    PointInTimeSecurity,
    PointInTimeTradeStatus,
)
from quantlab.market import TradingCalendarService
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.round6 import Round6Repository
from quantlab.persistence.round7 import Round7Repository
from quantlab.persistence.round8 import Round8Repository
from quantlab.persistence.round9 import Round9Repository
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
from quantlab.workflows.trusted_data_adapters import (
    FreeTrustedDataAdapter,
    TrustedDataAdapter,
)


def refresh_trusted_data(
    settings: Settings,
    *,
    as_of: date,
    adapter: TrustedDataAdapter | None = None,
) -> dict[str, Any]:
    """Ingest only server-configured sources; job payload cannot upload authoritative data."""

    path = settings.resolve(settings.get("system.database_path"))
    repository = Round5Repository(path)
    trust = DataTrustLevel(
        settings.get("runtime.trusted_data_default_level", DataTrustLevel.SERVER_OBSERVED.value)
    )
    license_status = str(settings.get("runtime.trusted_data_license_status", "unverified_no_sla"))
    results: dict[str, Any] = {}
    auto_enabled = bool(settings.get("runtime.trusted_data_auto_refresh_enabled", False))
    automatic_bundle: dict[str, Any] | None = None

    def load_automatic_bundle() -> dict[str, Any]:
        nonlocal automatic_bundle
        if automatic_bundle is None:
            automatic_bundle = (adapter or FreeTrustedDataAdapter(settings)).collect(as_of)
        return automatic_bundle

    calendar_path = _configured_file(settings, "runtime.trusted_calendar_path")
    if calendar_path:
        try:
            rows = _read_calendar(calendar_path)
            raw = _file_fingerprint(calendar_path)
            results["calendar"] = TradingCalendarService.from_settings(settings).ingest(
                rows,
                namespace=DataNamespace.PRODUCTION,
                trust_level=trust,
                provider="server_configured_file",
                source=str(calendar_path),
                endpoint="configured_calendar_csv",
                source_version=raw[:16],
                available_at=datetime.now(UTC),
                license_status=license_status,
                raw_fingerprint=raw,
            )
        except Exception as exc:
            results["calendar"] = _failed_source(
                repository,
                batch_type="trading_calendar",
                source_path=calendar_path,
                endpoint="configured_calendar_csv",
                trust=trust,
                license_status=license_status,
                as_of=as_of,
                exc=exc,
            )
    elif auto_enabled:
        try:
            results["calendar"] = _ingest_automatic_calendar(
                settings,
                load_automatic_bundle(),
                trust=trust,
                as_of=as_of,
            )
        except Exception as exc:
            results["calendar"] = _failed_automatic_source(
                repository,
                batch_type="trading_calendar",
                endpoint="automatic_free_calendar",
                trust=trust,
                as_of=as_of,
                exc=exc,
            )
    else:
        results["calendar"] = _unavailable_source(
            repository,
            batch_type="trading_calendar",
            endpoint="configured_calendar_csv",
            setting_key="runtime.trusted_calendar_path",
            trust=trust,
            license_status=license_status,
            as_of=as_of,
        )
    master_path = _configured_file(settings, "runtime.trusted_security_master_path")
    if master_path:
        try:
            records = list(csv.DictReader(master_path.open(encoding="utf-8-sig", newline="")))
            results["security_master"] = _ingest_security_master_records(
                settings,
                records,
                trust=trust,
                provider="server_configured_file",
                source=str(master_path),
                endpoint="configured_security_master_csv",
                source_version=_file_fingerprint(master_path)[:16],
                license_status=license_status,
                available_at=datetime.now(UTC),
                raw_fingerprint=_file_fingerprint(master_path),
                as_of=as_of,
            )
        except Exception as exc:
            results["security_master"] = _failed_source(
                repository,
                batch_type="security_master",
                source_path=master_path,
                endpoint="configured_security_master_csv",
                trust=trust,
                license_status=license_status,
                as_of=as_of,
                exc=exc,
            )
    elif auto_enabled:
        try:
            bundle = load_automatic_bundle()
            records = list(bundle.get("security_master") or [])
            if not records:
                raise ValueError(_automatic_missing_reason(bundle, "security_master"))
            raw = _bundle_component_fingerprint(bundle, "security_master")
            results["security_master"] = _ingest_security_master_records(
                settings,
                records,
                trust=trust,
                provider="baostock",
                source="server_automatic_free_source",
                endpoint="automatic_free_security_master",
                source_version=str(bundle.get("provider_version") or raw[:16]),
                license_status=str(bundle.get("license_status") or "unverified_no_sla"),
                available_at=_bundle_fetched_at(bundle),
                raw_fingerprint=raw,
                as_of=as_of,
            )
        except Exception as exc:
            results["security_master"] = _failed_automatic_source(
                repository,
                batch_type="security_master",
                endpoint="automatic_free_security_master",
                trust=trust,
                as_of=as_of,
                exc=exc,
            )
    else:
        results["security_master"] = _unavailable_source(
            repository,
            batch_type="security_master",
            endpoint="configured_security_master_csv",
            setting_key="runtime.trusted_security_master_path",
            trust=trust,
            license_status=license_status,
            as_of=as_of,
        )
    industry_path = _configured_file(settings, "runtime.trusted_industry_path")
    if industry_path:
        try:
            records = list(
                csv.DictReader(industry_path.open(encoding="utf-8-sig", newline=""))
            )
            raw = _file_fingerprint(industry_path)
            failures = [
                {"row_number": index, "reason": "missing symbol, industry or effective_date"}
                for index, item in enumerate(records, start=2)
                if not str(item.get("symbol") or "").strip()
                or not str(item.get("industry") or "").strip()
                or not str(item.get("effective_date") or item.get("date") or "").strip()
            ]
            manifest = repository.create_manifest(
                batch_type="industry_membership",
                namespace=DataNamespace.PRODUCTION,
                trust_level=trust,
                provider="server_configured_file",
                source=str(industry_path),
                endpoint="configured_industry_csv",
                source_version=raw[:16],
                available_at=datetime.now(UTC),
                license_status=license_status,
                payload=records,
                raw_fingerprint=raw,
                record_count=len(records),
                status="partial" if failures else "completed",
                missing_reason="some industry rows were invalid" if failures else None,
                failure_records=failures,
            )
            saved = repository.save_industry_memberships(manifest["manifest_id"], records)
            rich_saved = Round7Repository(repository.path).save_industry_records(
                records,
                namespace=DataNamespace.PRODUCTION.value,
                trust_level=trust.value,
                provider="server_configured_file",
                source_version=raw[:16],
                available_at=datetime.fromisoformat(str(manifest["available_at"])),
                manifest_id=manifest["manifest_id"],
            )
            valid_symbols = {
                str(item.get("symbol") or "")
                for item in records
                if str(item.get("symbol") or "").strip()
                and str(item.get("industry") or "").strip()
                and str(item.get("effective_date") or item.get("date") or "").strip()
            }
            results["industry"] = {
                "manifest": manifest,
                "records_saved": saved,
                "rich_records_saved": rich_saved,
                "field_coverage": len(valid_symbols) / max(1, len(records)),
                "field_coverage_by_field": {
                    "industry": len(valid_symbols) / max(1, len(records))
                },
                "provider_distribution": {"server_configured_file": saved},
            }
        except Exception as exc:
            results["industry"] = _failed_source(
                repository,
                batch_type="industry_membership",
                source_path=industry_path,
                endpoint="configured_industry_csv",
                trust=trust,
                license_status=license_status,
                as_of=as_of,
                exc=exc,
            )
    elif auto_enabled:
        try:
            results["industry"] = _ingest_automatic_industry(
                repository,
                load_automatic_bundle(),
                trust=trust,
                as_of=as_of,
            )
        except Exception as exc:
            results["industry"] = _failed_automatic_source(
                repository,
                batch_type="industry_membership",
                endpoint="automatic_free_industry",
                trust=trust,
                as_of=as_of,
                exc=exc,
            )
    else:
        results["industry"] = _unavailable_source(
            repository,
            batch_type="industry_membership",
            endpoint="configured_industry_csv",
            setting_key="runtime.trusted_industry_path",
            trust=trust,
            license_status=license_status,
            as_of=as_of,
        )
    pool_path = _configured_file(settings, "runtime.trusted_pit_pool_path")
    formal_pool_allowed = _formal_pool_allowed(
        settings,
        results.get("calendar"),
        as_of=as_of,
        minimum_trust=trust,
    )
    if formal_pool_allowed is False:
        results["point_in_time_pool"] = _skipped_non_trading_source(
            repository,
            batch_type="point_in_time_pool",
            endpoint="production_point_in_time_pool",
            trust=trust,
            license_status=license_status,
            as_of=as_of,
        )
    elif pool_path:
        manifest: dict[str, Any] | None = None
        try:
            raw_payload = json.loads(pool_path.read_text(encoding="utf-8"))
            raw = _file_fingerprint(pool_path)
            manifest = repository.create_manifest(
                batch_type="point_in_time_pool",
                namespace=DataNamespace.PRODUCTION,
                trust_level=trust,
                provider="server_configured_file",
                source=str(pool_path),
                endpoint="configured_pit_pool_json",
                source_version=raw[:16],
                available_at=datetime.now(UTC),
                license_status=license_status,
                payload=raw_payload,
                raw_fingerprint=raw,
                record_count=len(raw_payload.get("members", [])),
            )
            raw_payload.update(
                {
                    "namespace": DataNamespace.PRODUCTION.value,
                    "trust_level": trust.value,
                    "manifest_id": manifest["manifest_id"],
                    "fingerprint": "",
                }
            )
            snapshot = PointInTimePoolSnapshot.model_validate(raw_payload)
            results["point_in_time_pool"] = {
                "manifest": manifest,
                "snapshot": StrategyEvidenceRepository(path).save_pool_snapshot(snapshot),
            }
        except Exception as exc:
            if manifest:
                failed_manifest = repository.update_manifest_result(
                    manifest["manifest_id"],
                    status="failed",
                    record_count=0,
                    missing_reason=f"{type(exc).__name__}: {exc}",
                    failure_records=[{"reason": str(exc), "type": type(exc).__name__}],
                )
                results["point_in_time_pool"] = {
                    "manifest": failed_manifest,
                    "records_saved": 0,
                }
            else:
                results["point_in_time_pool"] = _failed_source(
                    repository,
                    batch_type="point_in_time_pool",
                    source_path=pool_path,
                    endpoint="configured_pit_pool_json",
                    trust=trust,
                    license_status=license_status,
                    as_of=as_of,
                    exc=exc,
                )
    elif auto_enabled:
        try:
            results["point_in_time_pool"] = _ingest_automatic_pool(
                settings,
                load_automatic_bundle(),
                trust=trust,
                as_of=as_of,
            )
        except Exception as exc:
            results["point_in_time_pool"] = _failed_automatic_source(
                repository,
                batch_type="point_in_time_pool",
                endpoint="automatic_free_point_in_time_pool",
                trust=trust,
                as_of=as_of,
                exc=exc,
            )
    else:
        results["point_in_time_pool"] = _unavailable_source(
            repository,
            batch_type="point_in_time_pool",
            endpoint="configured_pit_pool_json",
            setting_key="runtime.trusted_pit_pool_path",
            trust=trust,
            license_status=license_status,
            as_of=as_of,
        )
    coverage = _record_refresh_state(settings, results, as_of=as_of)
    raw_provider_selections = dict(
        (automatic_bundle or {}).get("selected_providers") or {}
    )
    for source_key, result in results.items():
        manifest = result.get("manifest") or {}
        provider = manifest.get("provider")
        if provider == "server_configured_file":
            raw_provider_selections[source_key] = {
                "selected_provider": provider,
                "reason": "server_configured_file_has_priority",
                "related_failures": [],
                "attempts": [],
            }
    refresh_id = str((automatic_bundle or {}).get("refresh_id") or hashlib.sha256(
        f"{as_of}:{datetime.now(UTC).isoformat()}".encode("utf-8")
    ).hexdigest())
    pool_result = results.get("point_in_time_pool") or {}
    pool_snapshot = pool_result.get("snapshot") or {}
    if pool_snapshot.get("snapshot_id"):
        linked = Round9Repository(path).link_pool_refresh(
            str(pool_snapshot["snapshot_id"]), refresh_id
        )
        pool_result["snapshot"] = {**pool_snapshot, "refresh_id": linked["refresh_id"]}
    provider_selections = _provider_acceptance_selections(
        results,
        raw_provider_selections,
        as_of=as_of,
    )
    observed_at = (
        _bundle_fetched_at(automatic_bundle) if automatic_bundle else datetime.now(UTC)
    )
    Round8Repository(path).record_provider_selections(
        refresh_id,
        provider_selections,
        observed_at=observed_at,
        market_date=as_of,
    )
    return {
        "as_of": as_of.isoformat(),
        "trust_level": trust.value,
        "automatic_refresh_enabled": auto_enabled,
        "sources": results,
        "coverage": coverage,
        "refresh_id": refresh_id,
        "selected_providers": raw_provider_selections,
        "provider_acceptance_selections": provider_selections,
    }


def _provider_acceptance_selections(
    results: dict[str, Any],
    raw: dict[str, dict[str, Any]],
    *,
    as_of: date,
) -> dict[str, dict[str, Any]]:
    aliases = {
        "calendar": "trading_calendar",
        "security_master": "security_master",
        "industry": "industry_membership",
        "point_in_time_universe": "point_in_time_universe",
        "current_spot": "market_spot",
    }
    result_keys = {
        "trading_calendar": "calendar",
        "security_master": "security_master",
        "industry_membership": "industry",
    }
    pool_result = results.get("point_in_time_pool") or {}
    pool_manifest = pool_result.get("manifest") or {}
    component_manifests = pool_result.get("component_manifests") or {}
    pool_snapshot = pool_result.get("snapshot") or {}
    output: dict[str, dict[str, Any]] = {}
    for raw_key, canonical in aliases.items():
        selection = dict(raw.get(raw_key) or {})
        result = results.get(result_keys.get(canonical, "")) or {}
        manifest = result.get("manifest") or {}
        if canonical in {"point_in_time_universe", "market_spot"}:
            manifest = component_manifests.get(canonical) or pool_manifest
            if (
                not selection
                and pool_snapshot
                and pool_manifest.get("provider") == "server_configured_file"
            ):
                selection = {
                    "selected_provider": "server_configured_file",
                    "reason": "configured_point_in_time_pool_supplies_component",
                    "related_failures": [],
                    "attempts": [],
                }
        output[canonical] = _provider_selection_metadata(
            selection,
            canonical=canonical,
            manifest=manifest,
            pool_snapshot=pool_snapshot,
            as_of=as_of,
        )
    pool_selection = {
        "selected_provider": pool_manifest.get("provider"),
        "reason": (
            "point_in_time_pool_materialized_from_selected_components"
            if pool_snapshot
            else str(pool_manifest.get("missing_reason") or "point_in_time_pool_unavailable")
        ),
        "related_failures": [],
        "attempts": _pool_provider_attempts(
            pool_result,
            selected_provider=pool_manifest.get("provider"),
        ),
        "status": pool_manifest.get("status"),
        "source_version": pool_manifest.get("source_version"),
    }
    output["point_in_time_pool"] = _provider_selection_metadata(
        pool_selection,
        canonical="point_in_time_pool",
        manifest=pool_manifest,
        pool_snapshot=pool_snapshot,
        as_of=as_of,
    )
    return output


def _pool_provider_attempts(
    pool_result: dict[str, Any],
    *,
    selected_provider: Any,
) -> list[dict[str, Any]]:
    provider = str(selected_provider or "").strip()
    if not provider or provider == "server_configured_file":
        return []
    return [
        {
            "provider": provider,
            "priority": 0,
            "component": "point_in_time_pool",
            "status": "completed",
            "attempt": 1,
            "materialized": True,
            "upstream_attempt_count": len(pool_result.get("provider_attempts") or []),
        }
    ]


def _provider_selection_metadata(
    selection: dict[str, Any],
    *,
    canonical: str,
    manifest: dict[str, Any],
    pool_snapshot: dict[str, Any],
    as_of: date,
) -> dict[str, Any]:
    attempts = _provider_attempts_with_priority(
        list(selection.get("attempts") or []),
    )
    successful_attempt = next(
        (
            item
            for item in reversed(attempts)
            if str(item.get("status") or "") in {"available", "completed"}
        ),
        None,
    )
    selected_provider = selection.get("selected_provider")
    related_failures = [
        item for item in (selection.get("related_failures") or []) if isinstance(item, dict)
    ]
    reason = str(selection.get("reason") or "unavailable")
    if selected_provider and related_failures and "fallback" not in reason.lower():
        reason = f"fallback_selected_after_primary_failure:{reason}"
    status = str(
        selection.get("status")
        or manifest.get("status")
        or (successful_attempt or {}).get("status")
        or ("available" if selected_provider else "unavailable")
    )
    capability = str(selection.get("capability") or canonical)
    pool_linked = canonical in {
        "point_in_time_universe",
        "market_spot",
        "point_in_time_pool",
    }
    return {
        **selection,
        "selected_provider": selected_provider,
        "reason": reason,
        "related_failures": related_failures,
        "attempts": [item for item in attempts if isinstance(item, dict)],
        "market_date": as_of.isoformat(),
        "status": status,
        "capability": capability,
        "source_version": str(
            selection.get("source_version")
            or manifest.get("source_version")
            or selection.get("provider_version")
            or ""
        ),
        "manifest_id": manifest.get("manifest_id"),
        "pool_snapshot_id": pool_snapshot.get("snapshot_id") if pool_linked else None,
        "pool_fingerprint": pool_snapshot.get("fingerprint") if pool_linked else None,
    }


def _provider_attempts_with_priority(
    attempts: list[Any],
) -> list[dict[str, Any]]:
    normalized = [dict(item) for item in attempts if isinstance(item, dict)]
    explicit_priorities = {
        str(item.get("provider") or "").strip(): int(item["priority"])
        for item in normalized
        if str(item.get("provider") or "").strip()
        and isinstance(item.get("priority"), int)
        and not isinstance(item.get("priority"), bool)
    }
    next_priority = max(explicit_priorities.values(), default=0) + 10
    inferred_priorities: dict[str, int] = {}
    output: list[dict[str, Any]] = []
    for item in normalized:
        provider = str(item.get("provider") or "").strip()
        if not provider:
            output.append(item)
            continue
        priority = explicit_priorities.get(provider)
        if priority is None:
            if provider not in inferred_priorities:
                inferred_priorities[provider] = next_priority
                next_priority += 10
            priority = inferred_priorities[provider]
        output.append({**item, "priority": priority})
    return output


def _configured_file(settings: Settings, key: str) -> Path | None:
    value = str(settings.get(key, "") or "").strip()
    if not value:
        return None
    path = settings.resolve(value).resolve()
    if not path.is_file():
        return None
    return path


def _read_calendar(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in csv.DictReader(path.open(encoding="utf-8-sig", newline="")):
        value = str(item.get("is_open") or "").strip().lower()
        rows.append(
            {
                "trade_date": date.fromisoformat(str(item["trade_date"])[:10]).isoformat(),
                "is_open": value in {"1", "true", "yes", "open"},
            }
        )
    return rows


def _file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unavailable_source(
    repository: Round5Repository,
    *,
    batch_type: str,
    endpoint: str,
    setting_key: str,
    trust: DataTrustLevel,
    license_status: str,
    as_of: date,
) -> dict[str, Any]:
    reason = f"{setting_key} is not configured or does not point to a file"
    manifest = repository.create_manifest(
        batch_type=batch_type,
        namespace=DataNamespace.PRODUCTION,
        trust_level=trust,
        provider="unconfigured",
        source="unavailable",
        endpoint=endpoint,
        source_version="none",
        available_at=datetime.now(UTC),
        license_status=license_status,
        payload={"as_of": as_of, "status": "unavailable", "setting_key": setting_key},
        raw_fingerprint="unavailable",
        record_count=0,
        status="unavailable",
        missing_reason=reason,
    )
    return {"manifest": manifest, "records_saved": 0, "status": "unavailable"}


def _failed_source(
    repository: Round5Repository,
    *,
    batch_type: str,
    source_path: Path,
    endpoint: str,
    trust: DataTrustLevel,
    license_status: str,
    as_of: date,
    exc: Exception,
) -> dict[str, Any]:
    raw = _file_fingerprint(source_path) if source_path.is_file() else "unavailable"
    reason = f"{type(exc).__name__}: {exc}"
    manifest = repository.create_manifest(
        batch_type=batch_type,
        namespace=DataNamespace.PRODUCTION,
        trust_level=trust,
        provider="server_configured_file",
        source=str(source_path),
        endpoint=endpoint,
        source_version=raw[:16],
        available_at=datetime.now(UTC),
        license_status=license_status,
        payload={"as_of": as_of, "status": "failed", "reason": reason},
        raw_fingerprint=raw,
        record_count=0,
        status="failed",
        missing_reason=reason,
        failure_records=[{"reason": str(exc), "type": type(exc).__name__}],
    )
    return {"manifest": manifest, "records_saved": 0, "status": "failed"}


def _ingest_security_master_records(
    settings: Settings,
    records: list[dict[str, Any]],
    *,
    trust: DataTrustLevel,
    provider: str,
    source: str,
    endpoint: str,
    source_version: str,
    license_status: str,
    available_at: datetime,
    raw_fingerprint: str,
    as_of: date,
) -> dict[str, Any]:
    normalized_records = _normalize_security_master_records(records)
    required = ("symbol", "name", "exchange", "listing_date", "status")
    coverage_by_field = {
        field: sum(item.get(field) not in {None, ""} for item in normalized_records)
        / max(1, len(normalized_records))
        for field in required
    }
    field_coverage = min(coverage_by_field.values(), default=0.0)
    failures = [
        {
            "row_number": index,
            "reason": "missing required security-master fields",
            "missing_fields": [field for field in required if item.get(field) in {None, ""}],
        }
        for index, item in enumerate(normalized_records, start=1)
        if any(item.get(field) in {None, ""} for field in required)
    ]
    valid = [
        item
        for item in normalized_records
        if not any(item.get(field) in {None, ""} for field in required)
    ]
    version = f"{source_version}:{raw_fingerprint[:16]}:{trust.value}:stable-v2"
    round5 = Round5Repository(settings.resolve(settings.get("system.database_path")))
    manifest = round5.create_manifest(
        batch_type="security_master",
        namespace=DataNamespace.PRODUCTION,
        trust_level=trust,
        provider=provider,
        source=source,
        endpoint=endpoint,
        source_version=version,
        available_at=available_at,
        license_status=license_status,
        payload={
            "as_of": as_of,
            "records": records,
            "normalized_record_count": len(normalized_records),
            "field_coverage": coverage_by_field,
        },
        raw_fingerprint=raw_fingerprint,
        record_count=len(valid),
        date_start=min(
            (date.fromisoformat(str(item["listing_date"])[:10]) for item in valid),
            default=None,
        ),
        date_end=as_of,
        status="partial" if failures else "completed",
        missing_reason="some security-master rows were incomplete" if failures else None,
        failure_records=failures,
    )
    models = [
        PointInTimeSecurity(
            symbol=item["symbol"],
            name=item.get("name") or item["symbol"],
            security_type="stock",
            exchange=item["exchange"],
            listing_date=item["listing_date"],
            delisting_date=item.get("delisting_date"),
            asset_class="equity",
            category=item.get("board") or "unclassified",
            status=item.get("status") or "unknown",
            source=provider,
            source_version=version,
            available_at=available_at,
            payload={
                "source_symbol": item.get("source_symbol"),
                "source_symbol_aliases": item.get("source_symbol_aliases", []),
                "lineage_records": item.get("lineage_records", []),
            },
            namespace=DataNamespace.PRODUCTION,
            trust_level=trust,
            manifest_id=manifest["manifest_id"],
        )
        for item in valid
    ]
    saved = StrategyEvidenceRepository(round5.path).save_security_master(
        master_version=version,
        records=models,
    )
    return {
        "manifest": manifest,
        "records_saved": saved,
        "field_coverage": field_coverage,
        "field_coverage_by_field": coverage_by_field,
        "provider_distribution": {provider: saved},
    }


def _normalize_security_master_records(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one current snapshot row per canonical symbol and retain code lineage."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in records:
        symbol = str(item.get("symbol") or "").strip()
        if symbol:
            grouped.setdefault(symbol, []).append(dict(item))
    output: list[dict[str, Any]] = []
    for symbol, variants in grouped.items():
        selected = max(
            variants,
            key=lambda item: (
                str(item.get("source_symbol") or "") == symbol,
                item.get("delisting_date") in {None, ""},
                str(item.get("status") or "").lower() in {"1", "listed", "active"},
                str(item.get("listing_date") or ""),
                str(item.get("source_symbol") or ""),
            ),
        )
        aliases = sorted(
            {
                str(item.get("source_symbol") or "").strip()
                for item in variants
                if str(item.get("source_symbol") or "").strip()
                and str(item.get("source_symbol") or "").strip() != selected.get("source_symbol")
            }
        )
        selected["source_symbol_aliases"] = aliases
        selected["lineage_records"] = [
            {
                "source_symbol": item.get("source_symbol"),
                "name": item.get("name"),
                "listing_date": item.get("listing_date"),
                "delisting_date": item.get("delisting_date"),
                "status": item.get("status"),
            }
            for item in sorted(
                variants,
                key=lambda item: (
                    str(item.get("listing_date") or ""),
                    str(item.get("source_symbol") or ""),
                ),
            )
        ]
        output.append(selected)
    return sorted(output, key=lambda item: str(item["symbol"]))


def _ingest_automatic_calendar(
    settings: Settings,
    bundle: dict[str, Any],
    *,
    trust: DataTrustLevel,
    as_of: date,
) -> dict[str, Any]:
    records = list(bundle.get("calendar") or [])
    if not records:
        raise ValueError(_automatic_missing_reason(bundle, "calendar"))
    raw = _bundle_component_fingerprint(bundle, "calendar")
    return TradingCalendarService.from_settings(settings).ingest(
        records,
        namespace=DataNamespace.PRODUCTION,
        trust_level=trust,
        provider=str(
            ((bundle.get("selected_providers") or {}).get("calendar") or {}).get(
                "selected_provider"
            )
            or bundle.get("provider")
            or "free_auto_adapter"
        ),
        source="server_automatic_free_source",
        endpoint="automatic_free_calendar",
        source_version=str(bundle.get("provider_version") or raw[:16]),
        available_at=_bundle_fetched_at(bundle),
        license_status=str(bundle.get("license_status") or "unverified_no_sla"),
        raw_fingerprint=raw,
    )


def _ingest_automatic_industry(
    repository: Round5Repository,
    bundle: dict[str, Any],
    *,
    trust: DataTrustLevel,
    as_of: date,
) -> dict[str, Any]:
    records = list(bundle.get("industry") or [])
    failures = [
        item for item in list(bundle.get("failures") or []) if item.get("component") == "industry"
    ]
    if not records:
        raise ValueError(_automatic_missing_reason(bundle, "industry"))
    raw = _bundle_component_fingerprint(bundle, "industry")
    provider_distribution = _provider_distribution(records)
    provider_name = str(
        ((bundle.get("selected_providers") or {}).get("industry") or {}).get(
            "selected_provider"
        )
        or "+".join(provider_distribution)
        or bundle.get("provider")
        or "free_auto_adapter"
    )
    manifest = repository.create_manifest(
        batch_type="industry_membership",
        namespace=DataNamespace.PRODUCTION,
        trust_level=trust,
        provider=provider_name,
        source="server_automatic_free_source",
        endpoint="automatic_free_industry",
        source_version=str(bundle.get("provider_version") or raw[:16]),
        available_at=_bundle_fetched_at(bundle),
        license_status=str(bundle.get("license_status") or "unverified_no_sla"),
        payload=records,
        raw_fingerprint=raw,
        record_count=len(records),
        date_start=as_of,
        date_end=as_of,
        status="partial" if failures else "completed",
        missing_reason="automatic industry source was partially degraded" if failures else None,
        failure_records=failures,
    )
    saved = repository.save_industry_memberships(manifest["manifest_id"], records)
    master_symbols = {
        str(item.get("symbol") or "")
        for item in list(bundle.get("security_master") or [])
        if str(item.get("status") or "listed") != "delisted"
    }
    industry_symbols = {str(item.get("symbol") or "") for item in records}
    coverage = len(industry_symbols & master_symbols) / max(1, len(master_symbols))
    rich_saved = Round7Repository(repository.path).save_industry_records(
        records,
        namespace=DataNamespace.PRODUCTION.value,
        trust_level=trust.value,
        provider=provider_name,
        source_version=str(bundle.get("provider_version") or raw[:16]),
        available_at=_bundle_fetched_at(bundle),
        manifest_id=manifest["manifest_id"],
    )
    return {
        "manifest": manifest,
        "records_saved": saved,
        "rich_records_saved": rich_saved,
        "field_coverage": coverage,
        "field_coverage_by_field": {"industry": coverage},
        "provider_distribution": provider_distribution,
    }


def _formal_pool_allowed(
    settings: Settings,
    calendar_result: dict[str, Any] | None,
    *,
    as_of: date,
    minimum_trust: DataTrustLevel,
) -> bool | None:
    """Return whether the just-ingested production calendar marks ``as_of`` open."""

    manifest = (calendar_result or {}).get("manifest") or {}
    available_at = manifest.get("available_at")
    if not manifest.get("manifest_id") or not available_at:
        return None
    cutoff = datetime.fromisoformat(str(available_at))
    try:
        return TradingCalendarService.from_settings(settings).is_open(
            as_of,
            cutoff_at=cutoff,
            formal=True,
            minimum_trust=minimum_trust,
        )
    except ValueError:
        return None


def _skipped_non_trading_source(
    repository: Round5Repository,
    *,
    batch_type: str,
    endpoint: str,
    trust: DataTrustLevel,
    license_status: str,
    as_of: date,
) -> dict[str, Any]:
    reason = "non-trading day: no formal point-in-time pool was created"
    manifest = repository.create_manifest(
        batch_type=batch_type,
        namespace=DataNamespace.PRODUCTION,
        trust_level=trust,
        provider="scheduler_calendar_gate",
        source="not_applicable",
        endpoint=endpoint,
        source_version="round7-non-trading-gate-v1",
        available_at=datetime.now(UTC),
        license_status=license_status,
        payload={"as_of": as_of, "status": "skipped_non_trading_day"},
        raw_fingerprint=hashlib.sha256(
            f"{batch_type}:{as_of}:non-trading".encode("utf-8")
        ).hexdigest(),
        record_count=0,
        date_start=as_of,
        date_end=as_of,
        status="skipped_non_trading_day",
        missing_reason=reason,
    )
    return {
        "manifest": manifest,
        "records_saved": 0,
        "status": "skipped_non_trading_day",
        "neutral_skip": True,
    }


def _ingest_automatic_pool(
    settings: Settings,
    bundle: dict[str, Any],
    *,
    trust: DataTrustLevel,
    as_of: date,
) -> dict[str, Any]:
    members = list(bundle.get("pool_members") or [])
    if not members:
        raise ValueError(_automatic_missing_reason(bundle, "pool_members"))
    path = settings.resolve(settings.get("system.database_path"))
    round5 = Round5Repository(path)
    strategy = StrategyEvidenceRepository(path)
    raw = _bundle_component_fingerprint(bundle, "pool_members")
    eligible = [item for item in members if item.get("eligible")]
    field_coverage_by_field = _pool_field_coverage(members)
    required_fields = tuple(
        settings.get(
            "runtime.trusted_pool_required_fields",
            [
                "symbol",
                "name",
                "exchange",
                "listing_date",
                "trade_date",
                "listed",
                "is_st",
                "suspended",
                "trade_status",
                "amount",
                "turnover_rate",
                "market_cap",
                "industry",
                "available_at",
                "source",
            ],
        )
    )
    required_coverage = {
        field: float(field_coverage_by_field.get(field, 0.0)) for field in required_fields
    }
    field_coverage = min(required_coverage.values(), default=0.0)
    average_field_coverage = sum(required_coverage.values()) / max(1, len(required_coverage))
    failures = list(bundle.get("failures") or [])
    refresh_started_at = _bundle_optional_timestamp(bundle, "refresh_started_at")
    refresh_finalized_at = _bundle_optional_timestamp(bundle, "refresh_finalized_at")
    snapshot_cutoff_at = _bundle_fetched_at(bundle)
    minimum_coverage = float(
        settings.get("runtime.trusted_data_minimum_field_coverage", 0.80)
    )
    status = (
        "completed"
        if eligible and field_coverage >= minimum_coverage and not failures
        else "partial"
    )
    provider_distribution = _provider_distribution(members)
    manifest = round5.create_manifest(
        batch_type="point_in_time_pool",
        namespace=DataNamespace.PRODUCTION,
        trust_level=trust,
        provider=str(bundle.get("provider") or "free_auto_adapter"),
        source="server_automatic_free_source",
        endpoint="automatic_free_point_in_time_pool",
        source_version=str(bundle.get("provider_version") or raw[:16]),
        available_at=snapshot_cutoff_at,
        refresh_started_at=refresh_started_at,
        refresh_finalized_at=refresh_finalized_at,
        snapshot_cutoff_at=snapshot_cutoff_at,
        license_status=str(bundle.get("license_status") or "unverified_no_sla"),
        payload={
            "as_of": as_of,
            "members": members,
            "refresh_started_at": bundle.get("refresh_started_at"),
            "refresh_finalized_at": bundle.get("refresh_finalized_at"),
            "snapshot_cutoff_at": bundle.get("snapshot_cutoff_at"),
            "timing": dict(bundle.get("timing") or {}),
            "field_coverage_by_field": field_coverage_by_field,
            "required_field_coverage": required_coverage,
            "provider_distribution": provider_distribution,
            "provider_attempts": list(bundle.get("provider_attempts") or []),
        },
        raw_fingerprint=raw,
        record_count=len(members),
        date_start=as_of,
        date_end=as_of,
        status=status,
        missing_reason=(
            None
            if status == "completed"
            else "free point-in-time pool has missing fields or degraded upstream components"
        ),
        failure_records=failures,
    )
    component_manifests: dict[str, dict[str, Any]] = {}
    for raw_key, canonical in (
        ("point_in_time_universe", "point_in_time_universe"),
        ("current_spot", "market_spot"),
    ):
        selection = dict((bundle.get("selected_providers") or {}).get(raw_key) or {})
        selected_provider = str(selection.get("selected_provider") or "").strip()
        if not selected_provider:
            continue
        component_payload = {
            "as_of": as_of,
            "component": canonical,
            "refresh_started_at": bundle.get("refresh_started_at"),
            "refresh_finalized_at": bundle.get("refresh_finalized_at"),
            "snapshot_cutoff_at": bundle.get("snapshot_cutoff_at"),
            "selected_provider": selected_provider,
            "selection_reason": selection.get("reason"),
            "related_failures": selection.get("related_failures") or [],
            "attempts": selection.get("attempts") or [],
            "pool_payload_fingerprint": manifest["payload_fingerprint"],
        }
        component_manifests[canonical] = round5.create_manifest(
            batch_type="point_in_time_pool",
            namespace=DataNamespace.PRODUCTION,
            trust_level=trust,
            provider=selected_provider,
            source="server_automatic_free_source",
            endpoint=f"automatic_free_{canonical}",
            source_version=str(bundle.get("provider_version") or raw[:16]),
            available_at=snapshot_cutoff_at,
            refresh_started_at=refresh_started_at,
            refresh_finalized_at=refresh_finalized_at,
            snapshot_cutoff_at=snapshot_cutoff_at,
            license_status=str(bundle.get("license_status") or "unverified_no_sla"),
            payload=component_payload,
            raw_fingerprint=hashlib.sha256(
                json.dumps(component_payload, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
            record_count=len(members),
            date_start=as_of,
            date_end=as_of,
            status="completed",
            failure_records=list(selection.get("related_failures") or []),
        )
    cutoff_at = snapshot_cutoff_at
    _assert_snapshot_field_times(members, cutoff_at=cutoff_at)
    source_version = str(bundle.get("provider_version") or raw[:16])
    refresh_id = str(bundle.get("refresh_id") or manifest["manifest_id"])
    status_records = [
        PointInTimeTradeStatus(
            **{
                **item,
                "methodology": (
                    f"{item.get('methodology') or 'server_observed_status'}:"
                    f"refresh:{refresh_id}"
                ),
                "payload": {
                    **dict(item.get("payload") or {}),
                    "refresh_id": refresh_id,
                    "base_methodology": item.get("methodology"),
                },
            },
            namespace=DataNamespace.PRODUCTION,
            trust_level=trust,
            manifest_id=manifest["manifest_id"],
        )
        for item in list(bundle.get("trade_status") or [])
    ]
    if status_records:
        strategy.save_trade_status(security_type="stock", records=status_records)
    snapshot = PointInTimePoolSnapshot(
        snapshot_type="a_share",
        snapshot_date=as_of,
        cutoff_at=cutoff_at,
        protocol_version=str(
            settings.get("strategies.a_share_v4.protocol_version", "a-share-v4-unknown")
        ),
        source=str(bundle.get("provider") or "free_auto_adapter"),
        source_version=source_version,
        stage=EvidenceStage.FORWARD_SHADOW,
        members=members,
        known_gaps=[
            "free sources have no service-level guarantee",
            "fields absent from the provider remain explicitly missing",
            *[
                f"{field}_coverage={coverage:.3f}"
                for field, coverage in required_coverage.items()
                if coverage < minimum_coverage
            ],
        ],
        created_at=cutoff_at,
        namespace=DataNamespace.PRODUCTION,
        trust_level=trust,
        manifest_id=manifest["manifest_id"],
    )
    saved_snapshot = strategy.save_pool_snapshot(snapshot)
    return {
        "manifest": manifest,
        "component_manifests": component_manifests,
        "snapshot": saved_snapshot,
        "security_master_records_saved": 0,
        "trade_status_records_saved": len(status_records),
        "eligible_members": len(eligible),
        "field_coverage": field_coverage,
        "average_field_coverage": average_field_coverage,
        "field_coverage_by_field": field_coverage_by_field,
        "required_field_coverage": required_coverage,
        "provider_distribution": provider_distribution,
        "provider_attempts": list(bundle.get("provider_attempts") or []),
    }


def _failed_automatic_source(
    repository: Round5Repository,
    *,
    batch_type: str,
    endpoint: str,
    trust: DataTrustLevel,
    as_of: date,
    exc: Exception,
) -> dict[str, Any]:
    reason = f"{type(exc).__name__}: {exc}"
    manifest = repository.create_manifest(
        batch_type=batch_type,
        namespace=DataNamespace.PRODUCTION,
        trust_level=trust,
        provider="automatic_free_source",
        source="unavailable",
        endpoint=endpoint,
        source_version="round9-capability-router-v1",
        available_at=datetime.now(UTC),
        license_status="unverified_no_sla",
        payload={"as_of": as_of, "status": "unavailable", "reason": reason},
        raw_fingerprint=hashlib.sha256(reason.encode("utf-8")).hexdigest(),
        record_count=0,
        status="unavailable",
        missing_reason=reason,
        failure_records=[{"reason": str(exc), "type": type(exc).__name__}],
    )
    return {"manifest": manifest, "records_saved": 0, "status": "unavailable"}


def _record_refresh_state(
    settings: Settings,
    results: dict[str, Any],
    *,
    as_of: date,
) -> dict[str, Any]:
    repository = Round6Repository(settings.resolve(settings.get("system.database_path")))
    output = {}
    for key, result in results.items():
        manifest = result.get("manifest") or {}
        count = int(manifest.get("record_count") or result.get("records_saved") or 0)
        field_coverage = float(result.get("field_coverage", 1.0 if count else 0.0))
        minimum_count = int(
            settings.get(
                f"runtime.trusted_{key}_minimum_records",
                1 if key == "calendar" else 3,
            )
        )
        minimum_ready = bool(
            manifest.get("status") in {"completed", "partial"}
            and count >= minimum_count
            and field_coverage >= float(
                settings.get("runtime.trusted_data_minimum_field_coverage", 0.80)
            )
        )
        output[key] = repository.update_data_source_state(
            str(manifest.get("batch_type") or key),
            status=str(manifest.get("status") or result.get("status") or "unavailable"),
            manifest_id=manifest.get("manifest_id"),
            date_start=manifest.get("date_start") or as_of.isoformat(),
            date_end=manifest.get("date_end") or as_of.isoformat(),
            symbol_count=count,
            field_coverage=field_coverage,
            minimum_ready=minimum_ready,
            detail={
                "source_key": key,
                "missing_reason": manifest.get("missing_reason"),
                "provider": manifest.get("provider"),
                "trust_level": manifest.get("trust_level"),
                "eligible_members": result.get("eligible_members", 0),
                "average_field_coverage": result.get("average_field_coverage"),
                "field_coverage_by_field": result.get("field_coverage_by_field", {}),
                "required_field_coverage": result.get("required_field_coverage", {}),
                "provider_distribution": result.get("provider_distribution", {}),
                "provider_attempts": result.get("provider_attempts", []),
            },
        )
    return output


def _pool_field_coverage(members: list[dict[str, Any]]) -> dict[str, float]:
    fields = (
        "symbol",
        "name",
        "exchange",
        "listing_date",
        "delisting_date",
        "trade_date",
        "listed",
        "is_st",
        "suspended",
        "trade_status",
        "amount",
        "turnover_rate",
        "market_cap",
        "industry",
        "available_at",
        "source",
        "eligible",
        "exclusion_reasons",
    )

    def available(item: dict[str, Any], field: str) -> bool:
        payload = item.get("payload") or {}
        if field in {"symbol", "name", "amount", "available_at", "source", "eligible"}:
            value = item.get(field)
        elif field == "industry":
            value = payload.get("industry") or item.get("category")
        elif field == "exclusion_reasons":
            return isinstance(item.get("exclusion_reasons"), list)
        elif field == "delisting_date":
            return "delisting_date" in payload
        else:
            value = payload.get(field)
        return value is not None and value != ""

    return {
        field: sum(available(item, field) for item in members) / max(1, len(members))
        for field in fields
    }


def _provider_distribution(records: list[dict[str, Any]]) -> dict[str, int]:
    output: dict[str, int] = {}
    for item in records:
        provider = str(item.get("provider") or item.get("source") or "unknown")
        for name in (part.strip() for part in provider.split("+") if part.strip()):
            output[name] = output.get(name, 0) + 1
    return dict(sorted(output.items()))


def _automatic_missing_reason(bundle: dict[str, Any], component: str) -> str:
    related = {
        "pool_members": {"pool_members", "point_in_time_universe", "current_spot"},
        "industry": {"industry"},
    }.get(component, {component})
    failures = [
        item
        for item in list(bundle.get("failures") or [])
        if item.get("component") in related
    ]
    if failures:
        return "; ".join(
            f"{item.get('provider') or 'unknown'}: {item.get('reason') or 'unavailable'}"
            for item in failures
        )
    if component == "pool_members" and bundle.get("snapshot_status") == (
        "non_trading_day_no_formal_pool"
    ):
        return "non-trading day: no formal point-in-time pool was created"
    return f"automatic free source returned no {component} records"


def _bundle_component_fingerprint(bundle: dict[str, Any], component: str) -> str:
    return hashlib.sha256(
        json.dumps(
            bundle.get(component) or [],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _bundle_fetched_at(bundle: dict[str, Any]) -> datetime:
    value = (
        bundle.get("snapshot_cutoff_at")
        or bundle.get("refresh_finalized_at")
        or bundle.get("fetched_at")
        or datetime.now(UTC)
    )
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _bundle_optional_timestamp(
    bundle: dict[str, Any],
    key: str,
) -> datetime | None:
    value = bundle.get(key)
    if value is None:
        return None
    return _aware_timestamp(value)


def snapshot_time_invariant(
    snapshot: dict[str, Any],
    *,
    registration_started_at: datetime | str | None = None,
) -> dict[str, Any]:
    cutoff = _aware_timestamp(snapshot["cutoff_at"])
    included: list[tuple[str, str, datetime]] = []
    spot_after_cutoff = 0
    for member in list(snapshot.get("members") or []):
        payload = dict(member.get("payload") or {})
        spot_observed = payload.get("spot_observed_at")
        if spot_observed and _aware_timestamp(spot_observed) > cutoff:
            spot_after_cutoff += 1
        for field, observation in dict(payload.get("field_observations") or {}).items():
            if observation.get("missing_reason") is not None:
                continue
            available_at = observation.get("available_at")
            if available_at:
                included.append(
                    (str(member.get("symbol") or ""), field, _aware_timestamp(available_at))
                )
    latest = max((item[2] for item in included), default=None)
    field_violations = [item for item in included if item[2] > cutoff]
    registration = (
        _aware_timestamp(registration_started_at)
        if registration_started_at is not None
        else None
    )
    cutoff_before_registration = registration is None or cutoff <= registration
    fields_before_cutoff = latest is None or latest <= cutoff
    return {
        "snapshot_id": snapshot.get("snapshot_id"),
        "cutoff_at": cutoff.isoformat(),
        "registration_started_at": registration.isoformat() if registration else None,
        "included_field_observations": len(included),
        "latest_field_available_at": latest.isoformat() if latest else None,
        "field_observations_after_cutoff": len(field_violations),
        "spot_members_after_cutoff": spot_after_cutoff,
        "fields_before_or_at_cutoff": fields_before_cutoff,
        "cutoff_before_or_at_registration": cutoff_before_registration,
        "invariant_holds": fields_before_cutoff and cutoff_before_registration,
        "violations": [
            {
                "symbol": symbol,
                "field": field,
                "available_at": available_at.isoformat(),
            }
            for symbol, field, available_at in field_violations[:100]
        ],
    }


def audit_formal_snapshot_timing(
    settings: Settings,
    *,
    snapshot_id: str,
    registration_id: str,
) -> dict[str, Any]:
    path = settings.resolve(settings.get("system.database_path"))
    strategy = StrategyEvidenceRepository(path)
    snapshot = strategy.pool_snapshot(snapshot_id)
    if snapshot is None:
        raise ValueError("formal snapshot not found")
    round5 = Round5Repository(path)
    with round5.connect() as db:
        registration = db.execute(
            "SELECT * FROM forward_registration_runs WHERE registration_id=?",
            (registration_id,),
        ).fetchone()
        if registration is None:
            raise ValueError("formal registration not found")
        samples = db.execute(
            "SELECT sample_key FROM forward_registration_samples WHERE registration_id=?",
            (registration_id,),
        ).fetchall()
    if registration["pool_snapshot_id"] != snapshot_id:
        raise ValueError("registration does not reference the audited snapshot")
    invariant = snapshot_time_invariant(
        snapshot,
        registration_started_at=registration["started_at"],
    )
    sample_keys = [row["sample_key"] for row in samples if row["sample_key"]]
    prediction_times: dict[str, Any] = {
        "prediction_count": 0,
        "earliest_registered_at": None,
        "earliest_created_at": None,
        "latest_created_at": None,
    }
    if sample_keys:
        placeholders = ",".join("?" for _ in sample_keys)
        with strategy.connect() as db:
            row = db.execute(
                f"""SELECT COUNT(*) AS prediction_count,
                           MIN(registered_at) AS earliest_registered_at,
                           MIN(created_at) AS earliest_created_at,
                           MAX(created_at) AS latest_created_at
                    FROM forward_ablation_predictions
                    WHERE sample_key IN ({placeholders})""",
                sample_keys,
            ).fetchone()
        prediction_times = dict(row)
    latest_observation = (
        _aware_timestamp(invariant["latest_field_available_at"])
        if invariant["latest_field_available_at"]
        else None
    )
    earliest_prediction = min(
        (
            _aware_timestamp(value)
            for value in (
                prediction_times.get("earliest_registered_at"),
                prediction_times.get("earliest_created_at"),
            )
            if value
        ),
        default=None,
    )
    return {
        **invariant,
        "experiment_id": registration["experiment_id"],
        "registration_id": registration_id,
        "registration_status": registration["status"],
        **prediction_times,
        "all_observations_before_registration": bool(
            latest_observation
            and latest_observation <= _aware_timestamp(registration["started_at"])
        ),
        "all_observations_before_predictions": bool(
            latest_observation
            and earliest_prediction
            and latest_observation <= earliest_prediction
        ),
    }


def record_formal_snapshot_timing_exception(
    settings: Settings,
    *,
    snapshot_id: str,
    registration_id: str,
) -> dict[str, Any]:
    audit = audit_formal_snapshot_timing(
        settings,
        snapshot_id=snapshot_id,
        registration_id=registration_id,
    )
    if audit["invariant_holds"]:
        raise ValueError("a conforming snapshot does not require an audit exception")
    if not audit["all_observations_before_registration"]:
        raise ValueError("snapshot observations reached or followed formal registration")
    if not audit["all_observations_before_predictions"]:
        raise ValueError("snapshot observations reached or followed formal prediction creation")
    snapshot = StrategyEvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    ).pool_snapshot(snapshot_id)
    assert snapshot is not None
    return StrategyEvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    ).record_formal_audit_exception(
        exception_type="snapshot_cutoff_precedes_included_field_observation",
        severity="audit_required_non_predictive",
        experiment_id=str(audit["experiment_id"]),
        registration_id=registration_id,
        snapshot_id=snapshot_id,
        market_date=date.fromisoformat(str(snapshot["snapshot_date"])[:10]),
        summary=(
            "Legacy snapshot cutoff metadata predates included provider observations; "
            "all observations preceded formal registration and prediction creation."
        ),
        payload={
            **audit,
            "disposition": "retain_immutable_samples_with_audit_marker",
            "prediction_after_data_violation": False,
            "snapshot_or_sample_rewrite_allowed": False,
        },
    )


def _assert_snapshot_field_times(
    members: list[dict[str, Any]],
    *,
    cutoff_at: datetime,
) -> None:
    audit = snapshot_time_invariant(
        {"snapshot_id": None, "cutoff_at": cutoff_at, "members": members}
    )
    if not audit["fields_before_or_at_cutoff"]:
        raise ValueError(
            "formal snapshot includes field observations later than its cutoff"
        )


def _aware_timestamp(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("formal evidence timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


__all__ = [
    "audit_formal_snapshot_timing",
    "record_formal_snapshot_timing_exception",
    "refresh_trusted_data",
    "snapshot_time_invariant",
]
