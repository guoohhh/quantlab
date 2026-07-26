from __future__ import annotations

import hashlib
import json
import sqlite3
import time as time_module
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

import httpx

from quantlab.config import Settings
from quantlab.data import BaoStockProvider
from quantlab.data.a_share_symbols import canonical_a_share_symbol
from quantlab.data.provider_router import (
    DataCapability,
    ProviderCallInFlight,
    ProviderCallTimeout,
    ProviderRegistration,
    ProviderRouter,
    call_single_flight,
)
from quantlab.persistence.round7 import Round7Repository


MARKET_TZ = ZoneInfo("Asia/Shanghai")


class TrustedDataAdapter(Protocol):
    provider_name: str
    provider_version: str
    license_status: str

    def collect(self, as_of: date) -> dict[str, Any]: ...


class FreeTrustedDataAdapter:
    """Best-effort free-source adapter. Failures stay explicit and never become fake records."""

    provider_name = "baostock+akshare+tencent+sina"
    provider_version = "round11-master-routed-append-only-v1"
    license_status = "unverified_no_sla"

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self.baostock = BaoStockProvider(
            cache_dir=settings.resolve(settings.get("system.data_dir")) / "cache" / "baostock"
        )
        self.health = Round7Repository(
            settings.resolve(settings.get("system.database_path"))
        )
        self.timeout_seconds = float(
            settings.get("runtime.trusted_provider_timeout_seconds", 35.0)
        )
        self.maximum_attempts = max(
            1, int(settings.get("runtime.trusted_provider_max_attempts", 2))
        )
        self.failure_threshold = max(
            1, int(settings.get("runtime.trusted_provider_failure_threshold", 3))
        )
        self.cooldown_seconds = float(
            settings.get("runtime.trusted_provider_cooldown_seconds", 300.0)
        )
        self._timed_out_providers: set[str] = set()
        self.router = ProviderRouter(
            [
                ProviderRegistration(
                    key="baostock",
                    capabilities=frozenset(
                        {
                            DataCapability.TRADING_CALENDAR,
                            DataCapability.SECURITY_MASTER,
                            DataCapability.INDUSTRY_MEMBERSHIP,
                            DataCapability.TRADE_STATUS,
                            DataCapability.DAILY_BARS,
                        }
                    ),
                    priority=10,
                    trust_level="server_observed",
                    license_status=self.license_status,
                    version=self.provider_version,
                ),
                ProviderRegistration(
                    key="akshare_eastmoney",
                    capabilities=frozenset({DataCapability.MARKET_SPOT}),
                    priority=20,
                    trust_level="server_observed",
                    license_status=self.license_status,
                    version=self.provider_version,
                ),
                ProviderRegistration(
                    key="tencent_quote",
                    capabilities=frozenset({DataCapability.MARKET_SPOT}),
                    priority=25,
                    trust_level="server_observed",
                    license_status=self.license_status,
                    version=self.provider_version,
                ),
                ProviderRegistration(
                    key="akshare_sina",
                    capabilities=frozenset({DataCapability.MARKET_SPOT}),
                    priority=30,
                    trust_level="server_observed",
                    license_status=self.license_status,
                    version=self.provider_version,
                ),
            ]
        )

    def _now(self) -> datetime:
        observed = self._clock()
        if observed.tzinfo is None:
            raise ValueError("trusted data adapter clock must be timezone-aware")
        return observed.astimezone(UTC)

    def collect(self, as_of: date) -> dict[str, Any]:
        self._timed_out_providers.clear()
        refresh_started_at = self._now()
        output: dict[str, Any] = {
            "refresh_id": str(uuid.uuid4()),
            "provider": self.provider_name,
            "provider_version": self.provider_version,
            "license_status": self.license_status,
            "refresh_started_at": refresh_started_at,
            "calendar": [],
            "security_master": [],
            "trade_status": [],
            "industry": [],
            "pool_members": [],
            "daily_bars_probe": [],
            "failures": [],
            "provider_attempts": [],
            "selected_providers": {},
            "provider_capabilities": self.router.capability_manifest(),
            "snapshot_status": "unknown",
        }
        output["calendar"] = self._collect_routed_component(
            output,
            capability=DataCapability.TRADING_CALENDAR,
            component="calendar",
            callbacks={
                "baostock": lambda: self.baostock.trading_calendar(
                    as_of - timedelta(days=370),
                    as_of + timedelta(days=120),
                )
            },
            default=[],
        )
        if not any(
            str(item.get("trade_date"))[:10] == as_of.isoformat()
            for item in output["calendar"]
        ):
            cached_calendar = self._cached_calendar_records(as_of)
            if cached_calendar:
                output["calendar"] = cached_calendar
                cached_attempt = {
                    "provider": "cached_trusted_calendar",
                    "component": "calendar",
                    "status": "available",
                    "record_count": len(cached_calendar),
                    "fallback_reason": "live_calendar_unavailable",
                }
                output["provider_attempts"].append(cached_attempt)
                output["selected_providers"]["calendar"] = {
                    "selected_provider": "cached_trusted_calendar",
                    "reason": "latest previously verified production calendar day",
                    "related_failures": list(output["failures"]),
                    "attempts": [cached_attempt],
                    "capability": DataCapability.TRADING_CALENDAR.value,
                }
        calendar_observed_at = self._now()
        security_master_records = self._collect_routed_component(
            output,
            capability=DataCapability.SECURITY_MASTER,
            component="security_master",
            callbacks={"baostock": self.baostock.security_master_records},
            default=[],
        )
        if not security_master_records:
            security_master_records = self._cached_security_master_records(as_of)
            if security_master_records:
                cached_attempt = {
                    "provider": "cached_pit_security_master",
                    "component": "security_master",
                    "status": "available",
                    "record_count": len(security_master_records),
                    "fallback_reason": "live_security_master_unavailable",
                }
                output["provider_attempts"].append(cached_attempt)
                output["selected_providers"]["security_master"] = {
                    "selected_provider": "cached_pit_security_master",
                    "reason": "latest previously verified production master",
                    "related_failures": [],
                    "attempts": [cached_attempt],
                    "capability": DataCapability.SECURITY_MASTER.value,
                }
        security_master_observed_at = self._now()
        output["security_master"] = [
            {
                **item,
                "available_at": item.get("available_at") or security_master_observed_at,
                "provider": item.get("provider") or "baostock",
                "source_version": item.get("source_version") or self.provider_version,
            }
            for item in security_master_records
        ]
        industry_records = self._collect_routed_component(
            output,
            capability=DataCapability.INDUSTRY_MEMBERSHIP,
            component="industry",
            callbacks={
                "baostock": lambda: self.baostock.industry_records(as_of=as_of)
            },
            default=[],
        )
        if not industry_records:
            industry_records = self._cached_industry_records(as_of)
            if industry_records:
                cached_attempt = {
                    "provider": "cached_trusted_industry",
                    "component": "industry",
                    "status": "available",
                    "record_count": len(industry_records),
                    "fallback_reason": "live_industry_unavailable",
                }
                output["provider_attempts"].append(cached_attempt)
                output["selected_providers"]["industry"] = {
                    "selected_provider": "cached_trusted_industry",
                    "reason": "latest previously verified production industry membership",
                    "related_failures": [
                        item
                        for item in output["failures"]
                        if item.get("component") == "industry"
                    ],
                    "attempts": [cached_attempt],
                    "capability": DataCapability.INDUSTRY_MEMBERSHIP.value,
                }
        industry_observed_at = self._now()
        output["industry"] = [
            {
                **item,
                "available_at": item.get("available_at") or industry_observed_at,
                "provider": item.get("provider") or "baostock",
                "source_version": item.get("source_version") or self.provider_version,
            }
            for item in industry_records
        ]
        calendar_day = next(
            (
                item
                for item in output["calendar"]
                if str(item.get("trade_date"))[:10] == as_of.isoformat()
            ),
            None,
        )
        is_trading_day = bool(calendar_day and calendar_day.get("is_open"))
        universe = []
        universe_observed_at = refresh_started_at
        spot: dict[str, dict[str, Any]] = {}
        if is_trading_day:
            output["snapshot_status"] = "trading_day_collection"
            universe = self._collect_routed_component(
                output,
                capability=DataCapability.TRADE_STATUS,
                component="point_in_time_universe",
                callbacks={
                    "baostock": lambda: self.baostock.point_in_time_universe(as_of)
                },
                default=[],
            )
            universe_observed_at = self._now()
            trusted_master_symbols = _security_master_candidate_symbols(
                output["security_master"],
                as_of,
            )
            quote_symbols = sorted(
                set(trusted_master_symbols) | {item.symbol for item in universe}
            )
            spot_callbacks = {
                "akshare_eastmoney": lambda: _akshare_spot(as_of),
                "tencent_quote": lambda: _tencent_spot(
                    as_of,
                    quote_symbols,
                ),
                "akshare_sina": lambda: _akshare_sina_spot(as_of),
            }
            spot = self._collect_routed_component(
                output,
                capability=DataCapability.MARKET_SPOT,
                component="current_spot",
                callbacks=spot_callbacks,
                default={},
            )
            spot_collection_completed_at = self._now()
        else:
            output["snapshot_status"] = "non_trading_day_no_formal_pool"
            spot_collection_completed_at = self._now()

        daily_bars_probe_enabled = bool(
            self.settings.get("runtime.trusted_daily_bars_probe_enabled", False)
        )
        bars_callback = getattr(self.baostock, "bars", None)
        output["daily_bars_probe"] = self._collect_routed_component(
            output,
            capability=DataCapability.DAILY_BARS,
            component="daily_bars",
            callbacks=(
                {
                    "baostock": lambda: [
                        item.model_dump(mode="json")
                        for item in bars_callback(
                            ["sh600000"], as_of - timedelta(days=10), as_of
                        )
                    ]
                }
                if daily_bars_probe_enabled and callable(bars_callback)
                else {}
            ),
            default=[],
        )
        if not daily_bars_probe_enabled:
            output["selected_providers"]["daily_bars"] = {
                "selected_provider": None,
                "reason": "probe_disabled_by_configuration",
                "related_failures": [],
                "attempts": [],
                "capability": DataCapability.DAILY_BARS.value,
            }

        master_by_symbol = {item["symbol"]: item for item in output["security_master"]}
        industry_by_symbol = {item["symbol"]: item for item in industry_records}
        universe_by_symbol = {item.symbol: item for item in universe}
        symbols = sorted(set(master_by_symbol) | set(universe_by_symbol) | set(spot))
        master_raw_fingerprint = _records_fingerprint(output["security_master"])
        industry_raw_fingerprint = _records_fingerprint(industry_records)
        pool_members = []
        trade_status = []
        industry = []
        for symbol in symbols:
            universe_item = universe_by_symbol.get(symbol)
            market_item = spot.get(symbol, {})
            master = master_by_symbol.get(symbol, {})
            name = str(
                market_item.get("name")
                or getattr(universe_item, "name", "")
                or master.get("name")
                or symbol
            )
            industry_record = industry_by_symbol.get(symbol, {})
            industry_name = str(
                industry_record.get("industry") or market_item.get("industry") or ""
            ).strip()
            industry_classification = str(
                industry_record.get("classification")
                or ("eastmoney_spot" if market_item.get("industry") else "")
            ).strip()
            amount = _optional_float(market_item.get("amount"))
            market_cap = _optional_float(market_item.get("market_cap"))
            turnover = _optional_float(market_item.get("turnover_rate"))
            price = _optional_float(market_item.get("price"))
            previous_close = _optional_float(market_item.get("previous_close"))
            daily_return_pct = _optional_signed_float(
                market_item.get("daily_return_pct")
            )
            listed = _master_record_listed(master, as_of)
            quote_date_matches = (
                str(market_item.get("provider_market_date") or "") == as_of.isoformat()
            )
            status_confirmed = (
                bool(getattr(universe_item, "trade_status", False))
                if universe_item is not None
                else bool(quote_date_matches and price is not None and price > 0)
            )
            tradeable = bool(
                listed
                and status_confirmed
                and price is not None
                and price > 0
            )
            is_st = "ST" in name.upper()
            suspended = bool(listed and not status_confirmed)
            missing = [
                field
                for field, value in (
                    ("industry", industry_name),
                    ("amount", amount),
                    ("turnover_rate", turnover),
                    ("market_cap", market_cap),
                    ("listing_date", master.get("listing_date")),
                )
                if value in {None, ""}
            ]
            reasons = []
            if not tradeable:
                reasons.append("not_confirmed_tradeable_on_snapshot_date")
            if is_st:
                reasons.append("historical_st_name_flag")
            if missing:
                reasons.extend(f"required_field_unavailable:{field}" for field in missing)
            trade_status_provider = (
                "baostock"
                if universe_item is not None
                else market_item.get("provider")
            )
            field_sources = {
                "security_master": "baostock",
                "industry": industry_record.get("provider") or (
                    market_item.get("provider") if market_item.get("industry") else None
                ),
                "current_spot": market_item.get("provider"),
                "trade_status": trade_status_provider,
            }
            field_observations = _pool_field_observations(
                as_of=as_of,
                security_master_observed_at=security_master_observed_at,
                industry_observed_at=industry_observed_at,
                universe_observed_at=universe_observed_at,
                master=master,
                industry_record=industry_record,
                market_item=market_item,
                trade_status_provider=trade_status_provider,
                master_raw_fingerprint=master_raw_fingerprint,
                industry_raw_fingerprint=industry_raw_fingerprint,
            )
            member_available_at = _latest_included_observation(field_observations)
            source_chain = "+".join(
                dict.fromkeys(
                    item
                    for item in ("baostock", trade_status_provider, market_item.get("provider"))
                    if item
                )
            )
            pool_members.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "asset_class": "equity",
                    "category": industry_name or str(master.get("board") or "unclassified"),
                    "eligible": not reasons,
                    "exclusion_reasons": list(dict.fromkeys(reasons)),
                    "amount": amount,
                    "fund_size": market_cap,
                    "liquidity_score": amount,
                    "source": source_chain,
                    "available_at": member_available_at,
                    "data_quality": "available" if not missing else "degraded",
                    "missing_fields": missing,
                    "payload": {
                        "exchange": getattr(universe_item, "exchange", None)
                        or master.get("exchange"),
                        "board": getattr(universe_item, "board", None) or master.get("board"),
                        "listing_date": master.get("listing_date"),
                        "delisting_date": master.get("delisting_date"),
                        "trade_date": as_of.isoformat(),
                        "listed": listed,
                        "trade_status": tradeable,
                        "is_st": is_st,
                        "suspended": suspended,
                        "turnover_rate": turnover,
                        "latest_price": price,
                        "previous_close": previous_close,
                        "daily_return_pct": daily_return_pct,
                        "market_cap": market_cap,
                        "industry": industry_name or None,
                        "industry_classification": industry_classification or None,
                        "field_sources": field_sources,
                        "field_observations": field_observations,
                        "spot_observed_at": market_item.get("available_at"),
                        "spot_provider_timestamp": market_item.get("provider_timestamp"),
                        "spot_provider_market_date": market_item.get(
                            "provider_market_date"
                        ),
                        "spot_provider_response_fingerprint": market_item.get(
                            "provider_response_fingerprint"
                        ),
                        "spot_provider_batch_index": market_item.get(
                            "provider_batch_index"
                        ),
                        "source_symbol": getattr(universe_item, "source_symbol", None),
                    },
                }
            )
            trade_status.append(
                {
                    "symbol": symbol,
                    "trade_date": as_of,
                    "trade_status": tradeable,
                    "suspended": suspended,
                    "is_st": is_st,
                    "amount": amount,
                    "fund_size": market_cap,
                    "turnover_rate": turnover,
                    "source": source_chain,
                    "methodology": "master_and_server_observed_quote_status_v2",
                    "available_at": member_available_at,
                    "payload": {
                        "missing_fields": missing,
                        "field_sources": field_sources,
                        "field_observations": field_observations,
                        "market_cap": market_cap,
                        "previous_close": previous_close,
                        "daily_return_pct": daily_return_pct,
                        "spot_provider_timestamp": market_item.get("provider_timestamp"),
                        "spot_provider_market_date": market_item.get(
                            "provider_market_date"
                        ),
                        "spot_provider_response_fingerprint": market_item.get(
                            "provider_response_fingerprint"
                        ),
                    },
                }
            )
            if industry_name and symbol not in industry_by_symbol:
                industry.append(
                    {
                        "symbol": symbol,
                        "industry": industry_name,
                        "classification": industry_classification or "unknown",
                        "effective_date": str(
                            industry_record.get("effective_date") or as_of.isoformat()
                        )[:10],
                        "available_at": str(
                            industry_record.get("available_at")
                            or market_item.get("available_at")
                            or industry_observed_at
                        ),
                        "provider": industry_record.get("provider")
                        or market_item.get("provider")
                        or "unknown",
                        "source_version": industry_record.get("source_version")
                        or self.provider_version,
                    }
                )

        ranked = sorted(
            (item for item in pool_members if item["eligible"]),
            key=lambda item: (-(item.get("amount") or 0.0), item["symbol"]),
        )
        for rank, item in enumerate(ranked, start=1):
            item["representative"] = True
            item["representative_rank"] = rank
        output["pool_members"] = pool_members
        output["trade_status"] = trade_status
        output["industry"].extend(industry)
        refresh_finalized_at = self._now()
        latest_field_observation = _latest_pool_observation(pool_members)
        if latest_field_observation > refresh_finalized_at:
            raise ValueError(
                "included provider observation is later than refresh finalization"
            )
        output["refresh_finalized_at"] = refresh_finalized_at
        output["snapshot_cutoff_at"] = refresh_finalized_at
        # Legacy consumers still read fetched_at; it now means the finalized cutoff,
        # never the request-start timestamp.
        output["fetched_at"] = refresh_finalized_at
        output["timing"] = {
            "refresh_started_at": refresh_started_at,
            "calendar_observed_at": calendar_observed_at,
            "security_master_observed_at": security_master_observed_at,
            "industry_observed_at": industry_observed_at,
            "universe_observed_at": universe_observed_at,
            "spot_collection_completed_at": spot_collection_completed_at,
            "latest_included_field_observation": latest_field_observation,
            "refresh_finalized_at": refresh_finalized_at,
            "snapshot_cutoff_at": refresh_finalized_at,
        }
        output["raw_fingerprint"] = hashlib.sha256(
            json.dumps(
                {
                    "as_of": as_of.isoformat(),
                    "calendar": output["calendar"],
                    "security_master": output["security_master"],
                    "trade_status": trade_status,
                    "industry": industry,
                    "pool_members": pool_members,
                    "provider_attempts": output["provider_attempts"],
                    "daily_bars_probe": output["daily_bars_probe"],
                    "timing": output["timing"],
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return output

    def preflight(self, as_of: date, *, sample_limit: int = 24) -> dict[str, Any]:
        """Probe source reachability and schemas without materializing formal data."""

        observed_at = datetime.now(UTC)
        checks: list[dict[str, Any]] = []

        def probe(provider: str, component: str, callback) -> Any:
            started = time_module.monotonic()
            try:
                value = _call_with_timeout(provider, callback, self.timeout_seconds)
                summary = _result_observability(value)
                checks.append(
                    {
                        "provider": provider,
                        "component": component,
                        "status": "available" if summary["record_count"] else "unavailable",
                        "latency_ms": round((time_module.monotonic() - started) * 1000, 2),
                        **summary,
                    }
                )
                return value
            except Exception as exc:
                checks.append(
                    {
                        "provider": provider,
                        "component": component,
                        "status": "unavailable",
                        "latency_ms": round((time_module.monotonic() - started) * 1000, 2),
                        "record_count": 0,
                        "error_type": type(exc).__name__,
                        "failure_reason": str(exc),
                    }
                )
                return None

        calendar = probe(
            "baostock",
            "trading_calendar",
            lambda: self.baostock.trading_calendar(as_of, as_of + timedelta(days=30)),
        ) or []
        master = probe(
            "baostock",
            "security_master",
            self.baostock.security_master_records,
        ) or []
        if not master:
            master = self._cached_security_master_records(as_of)
            if master:
                checks.append(
                    {
                        "provider": "cached_pit_security_master",
                        "component": "security_master",
                        "status": "available",
                        "record_count": len(master),
                        "fallback_reason": "live_security_master_unavailable",
                    }
                )
        universe = probe(
            "baostock",
            "point_in_time_universe",
            lambda: self.baostock.point_in_time_universe(as_of),
        ) or []
        sample_symbols = sorted(
            {
                str(getattr(item, "symbol", "") or item.get("symbol", ""))
                if isinstance(item, dict)
                else str(getattr(item, "symbol", ""))
                for item in universe
            }
            - {""}
        )
        sample_symbols = sorted(
            set(sample_symbols) | set(_security_master_candidate_symbols(master, as_of))
        )[: max(1, int(sample_limit))]
        if not sample_symbols:
            sample_symbols = ["sh600000", "sh600519", "sz000001"]
        probe("akshare_eastmoney", "market_spot", lambda: _akshare_spot(as_of))
        tencent = probe(
            "tencent_quote",
            "market_spot",
            lambda: _tencent_spot(
                as_of,
                sample_symbols,
                require_market_date=False,
            ),
        ) or {}
        probe("akshare_sina", "market_spot", lambda: _akshare_sina_spot(as_of))
        tencent_summary = _result_observability(tencent)
        minimum_coverage = float(
            self.settings.get("runtime.trusted_data_minimum_field_coverage", 0.80)
        )
        provider_dates = list(tencent_summary.get("provider_market_dates") or [])
        return {
            "read_only": True,
            "formal_signal_snapshot_created": False,
            "as_of": as_of.isoformat(),
            "observed_at": observed_at.isoformat(),
            "provider_capabilities": self.router.capability_manifest(),
            "checks": checks,
            "calendar_open": any(
                str(item.get("trade_date"))[:10] == as_of.isoformat()
                and bool(item.get("is_open"))
                for item in calendar
            ),
            "security_master_records": len(master),
            "point_in_time_universe_records": len(universe),
            "tencent_sample_symbols": sample_symbols,
            "tencent_schema_coverage": tencent_summary.get("field_coverage_by_field", {}),
            "tencent_provider_market_dates": provider_dates,
            "tencent_market_date_matches_request": provider_dates == [as_of.isoformat()],
            "tencent_expected_formal_coverage_ready": all(
                float(tencent_summary.get("field_coverage_by_field", {}).get(field, 0.0))
                >= minimum_coverage
                for field in ("price", "amount", "turnover_rate", "market_cap")
            ),
            "claim_boundary": (
                "This probe checks source reachability and field schemas only. It does not "
                "create a production point-in-time pool, a primary experiment, or formal samples."
            ),
        }

    def _cached_security_master_records(self, as_of: date) -> list[dict[str, Any]]:
        path = self.settings.resolve(self.settings.get("system.database_path"))
        if not path.is_file():
            return []
        try:
            with sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10
            ) as db:
                db.row_factory = sqlite3.Row
                table = db.execute(
                    """SELECT 1 FROM sqlite_master
                       WHERE type='table' AND name='pit_security_master'"""
                ).fetchone()
                if table is None:
                    return []
                version = db.execute(
                    """SELECT master_version FROM pit_security_master
                       WHERE namespace='production' AND trust_rank>=3
                         AND available_at<=? AND listing_date<=?
                       GROUP BY master_version
                       ORDER BY MAX(available_at) DESC LIMIT 1""",
                    (self._now().isoformat(), as_of.isoformat()),
                ).fetchone()
                if version is None:
                    return []
                rows = db.execute(
                    """SELECT * FROM pit_security_master
                       WHERE master_version=? AND security_type='stock'
                         AND available_at<=? AND listing_date<=?
                         AND (delisting_date IS NULL OR delisting_date>=?)
                       ORDER BY symbol""",
                    (
                        version["master_version"],
                        self._now().isoformat(),
                        as_of.isoformat(),
                        as_of.isoformat(),
                    ),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {
                "symbol": row["symbol"],
                "name": row["name"],
                "exchange": row["exchange"],
                "listing_date": row["listing_date"],
                "delisting_date": row["delisting_date"],
                "status": row["status"],
                "board": row["category"],
                "source": row["source"],
                "provider": "cached_pit_security_master",
                "source_version": row["source_version"],
                "available_at": row["available_at"],
                "raw_fingerprint": row["record_fingerprint"],
                "source_symbol": row["symbol"],
            }
            for row in rows
        ]

    def _cached_calendar_records(self, as_of: date) -> list[dict[str, Any]]:
        path = self.settings.resolve(self.settings.get("system.database_path"))
        if not path.is_file():
            return []
        window_start = as_of - timedelta(days=370)
        window_end = as_of + timedelta(days=120)
        try:
            with sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10
            ) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    """SELECT * FROM (
                           SELECT *,ROW_NUMBER() OVER(
                               PARTITION BY trade_date
                               ORDER BY trust_rank DESC,available_at DESC
                           ) AS calendar_rank
                           FROM trusted_calendar_days
                           WHERE namespace='production' AND trade_date BETWEEN ? AND ?
                             AND trust_rank>=3 AND available_at<=?
                       ) WHERE calendar_rank=1 ORDER BY trade_date""",
                    (
                        window_start.isoformat(),
                        window_end.isoformat(),
                        self._now().isoformat(),
                    ),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {
                "trade_date": row["trade_date"],
                "is_open": bool(row["is_open"]),
                "provider": "cached_trusted_calendar",
                "source": row["source"],
                "source_version": row["source_version"],
                "available_at": row["available_at"],
                "raw_fingerprint": row["record_fingerprint"],
                "manifest_id": row["manifest_id"],
            }
            for row in rows
        ]

    def _cached_industry_records(self, as_of: date) -> list[dict[str, Any]]:
        path = self.settings.resolve(self.settings.get("system.database_path"))
        if not path.is_file():
            return []
        try:
            with sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10
            ) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    """SELECT * FROM (
                           SELECT *,ROW_NUMBER() OVER(
                               PARTITION BY symbol
                               ORDER BY effective_date DESC,trust_rank DESC,available_at DESC
                           ) AS membership_rank
                           FROM trusted_industry_membership
                           WHERE namespace='production' AND effective_date<=?
                             AND trust_rank>=3 AND available_at<=?
                       ) WHERE membership_rank=1 ORDER BY symbol""",
                    (as_of.isoformat(), self._now().isoformat()),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {
                "symbol": row["symbol"],
                "industry": row["industry"],
                "classification": "cached_trusted_industry",
                "effective_date": row["effective_date"],
                "provider": "cached_trusted_industry",
                "source": row["source"],
                "source_version": row["source_version"],
                "available_at": row["available_at"],
                "raw_fingerprint": row["record_fingerprint"],
                "manifest_id": row["manifest_id"],
            }
            for row in rows
        ]

    def _collect_routed_component(
        self,
        output: dict[str, Any],
        *,
        capability: DataCapability,
        component: str,
        callbacks: dict[str, Any],
        default: Any,
    ) -> Any:
        registrations = self.router.providers_for(capability)
        if not registrations:
            output["selected_providers"][component] = {
                "selected_provider": None,
                "reason": "no_provider_declares_required_capability",
                "related_failures": [],
                "attempts": [],
                "capability": capability.value,
            }
            return default
        value = default
        for registration in registrations:
            callback = callbacks.get(registration.key)
            if callback is None:
                if component == "daily_bars":
                    output["selected_providers"][component] = {
                        "selected_provider": None,
                        "reason": "provider_declares_capability_but_adapter_method_unavailable",
                        "related_failures": [],
                        "attempts": [],
                        "capability": capability.value,
                    }
                    return default
                failure = {
                    "component": component,
                    "provider": registration.key,
                    "priority": registration.priority,
                    "status": "failed",
                    "type": "CapabilityCallbackUnavailable",
                    "reason": f"no callback registered for {capability.value}",
                }
                output["provider_attempts"].append(dict(failure))
                output["failures"].append(failure)
                continue
            value = self._collect_component(
                output,
                provider_key=registration.key,
                provider_priority=registration.priority,
                component=component,
                callback=callback,
                default=default,
            )
            if value:
                selected = output["selected_providers"].get(component) or {}
                related_failures = list(selected.get("related_failures") or [])
                if related_failures:
                    selected["reason"] = "fallback_selected_after_higher_priority_failure"
                selected["capability"] = capability.value
                selected["provider_version"] = registration.version
                selected["trust_level"] = registration.trust_level
                selected["license_status"] = registration.license_status
                output["selected_providers"][component] = selected
                return value
        selected = output["selected_providers"].get(component) or {}
        selected["capability"] = capability.value
        output["selected_providers"][component] = selected
        return default

    def _collect_component(
        self,
        output: dict[str, Any],
        *,
        provider_key: str,
        provider_priority: int | None = None,
        component: str,
        callback,
        default,
    ):
        now = datetime.now(UTC)
        if provider_key in self._timed_out_providers:
            output["provider_attempts"].append(
                {
                    "provider": provider_key,
                    "priority": provider_priority,
                    "component": component,
                    "status": "blocked_after_provider_timeout",
                }
            )
            output["failures"].append(
                {
                    "component": component,
                    "provider": provider_key,
                    "priority": provider_priority,
                    "status": "blocked_after_provider_timeout",
                    "type": "ProviderTimeoutCascadeBlocked",
                    "reason": "an earlier component timed out; no overlapping provider call was started",
                }
            )
            output["selected_providers"].setdefault(
                component,
                {
                    "selected_provider": None,
                    "reason": "unfinished_provider_call_blocked_overlap",
                    "related_failures": [output["failures"][-1]],
                    "attempts": [output["provider_attempts"][-1]],
                },
            )
            return default
        if not self.health.provider_available(provider_key, component, now=now):
            state = self.health.provider_state(provider_key, component) or {}
            output["provider_attempts"].append(
                {
                    "provider": provider_key,
                    "priority": provider_priority,
                    "component": component,
                    "status": "circuit_open",
                    "circuit_open_until": state.get("circuit_open_until"),
                }
            )
            output["failures"].append(
                {
                    "component": component,
                    "provider": provider_key,
                    "priority": provider_priority,
                    "status": "circuit_open",
                    "type": "CircuitOpen",
                    "reason": "provider circuit is open",
                }
            )
            output["selected_providers"].setdefault(
                component,
                {
                    "selected_provider": None,
                    "reason": "provider_circuit_open",
                    "related_failures": [output["failures"][-1]],
                    "attempts": [output["provider_attempts"][-1]],
                },
            )
            return default
        last_error: Exception | None = None
        total_started = time_module.monotonic()
        timeout_seconds = float(
            self.settings.get(
                f"runtime.trusted_provider_{component}_timeout_seconds",
                self.timeout_seconds,
            )
        )
        for attempt in range(1, self.maximum_attempts + 1):
            started = time_module.monotonic()
            try:
                value = _call_with_timeout(provider_key, callback, timeout_seconds)
                if value is None or hasattr(value, "__len__") and len(value) == 0:
                    raise ValueError("provider returned no records")
                latency_ms = (time_module.monotonic() - started) * 1000
                observability = _result_observability(value)
                self.health.record_provider_attempt(
                    provider_key=provider_key,
                    component=component,
                    status="available",
                    latency_ms=latency_ms,
                    source_version=self.provider_version,
                    detail={"attempt": attempt, **observability},
                    failure_threshold=self.failure_threshold,
                    cooldown_seconds=self.cooldown_seconds,
                )
                output["provider_attempts"].append(
                    {
                        "provider": provider_key,
                    "priority": provider_priority,
                    "component": component,
                    "status": "available",
                    "attempt": attempt,
                        "latency_ms": round(latency_ms, 2),
                        **observability,
                    }
                )
                related_failures = [
                    item
                    for item in output["failures"]
                    if item.get("component") == component
                ]
                output["selected_providers"][component] = {
                    "selected_provider": provider_key,
                    "reason": "highest_priority_available_capability_provider",
                    "related_failures": related_failures,
                    "attempts": [
                        item
                        for item in output["provider_attempts"]
                        if item.get("component") == component
                    ],
                }
                return value
            except Exception as exc:
                last_error = exc
                timed_call = isinstance(exc, (ProviderCallTimeout, ProviderCallInFlight))
                if timed_call:
                    self._timed_out_providers.add(provider_key)
                output["provider_attempts"].append(
                    {
                        "provider": provider_key,
                        "priority": provider_priority,
                        "component": component,
                        "status": "timeout" if timed_call else "failed",
                        "attempt": attempt,
                        "latency_ms": round((time_module.monotonic() - started) * 1000, 2),
                        "error_type": type(exc).__name__,
                    }
                )
                if timed_call:
                    break
                if attempt < self.maximum_attempts:
                    time_module.sleep(min(2.0, 0.25 * 2 ** (attempt - 1)))
        assert last_error is not None
        latency_ms = (time_module.monotonic() - total_started) * 1000
        self.health.record_provider_attempt(
            provider_key=provider_key,
            component=component,
            status="timeout"
            if isinstance(last_error, (ProviderCallTimeout, ProviderCallInFlight))
            else "failed",
            latency_ms=latency_ms,
            source_version=self.provider_version,
            error_type=type(last_error).__name__,
            error_detail=str(last_error),
            detail={"attempts": self.maximum_attempts},
            failure_threshold=self.failure_threshold,
            cooldown_seconds=self.cooldown_seconds,
        )
        output["failures"].append(
            {
                "component": component,
                "provider": provider_key,
                "priority": provider_priority,
                "status": (
                    "timeout"
                    if isinstance(last_error, (ProviderCallTimeout, ProviderCallInFlight))
                    else "failed"
                ),
                "type": type(last_error).__name__,
                "reason": str(last_error),
            }
        )
        output["selected_providers"].setdefault(
            component,
            {
                "selected_provider": None,
                "reason": "all_capability_providers_failed_or_blocked",
                "related_failures": [
                    item
                    for item in output["failures"]
                    if item.get("component") == component
                ],
                "attempts": [
                    item
                    for item in output["provider_attempts"]
                    if item.get("component") == component
                ],
            },
        )
        return default


def _akshare_spot(as_of: date) -> dict[str, dict[str, Any]]:
    market_today = datetime.now(UTC).astimezone(MARKET_TZ).date()
    if as_of != market_today:
        raise ValueError("free current-market snapshot cannot be backfilled into another date")
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover - optional data dependency
        raise RuntimeError("akshare is not installed") from exc
    frame = ak.stock_zh_a_spot_em()
    if frame is None or frame.empty:
        raise ValueError("akshare current A-share snapshot returned no rows")
    columns = {str(column).strip(): column for column in frame.columns}

    def column(*names: str):
        return next((columns[name] for name in names if name in columns), None)

    code_column = column("代码", "证券代码", "code")
    if code_column is None:
        raise ValueError("akshare current A-share schema has no security code")
    name_column = column("名称", "证券简称", "name")
    price_column = column("最新价", "现价", "price")
    amount_column = column("成交额", "amount")
    turnover_column = column("换手率", "turnover_rate")
    market_cap_column = column("总市值", "market_cap")
    industry_column = column("所属行业", "行业", "industry")
    output = {}
    observed_at = datetime.now(UTC)
    for _, row in frame.iterrows():
        try:
            raw_code = str(row[code_column]).strip().split(".")[0].zfill(6)
            if not raw_code.isdigit() or len(raw_code) != 6:
                continue
            prefixed = ("sh" if raw_code.startswith(("5", "6", "9")) else "sz") + raw_code
            symbol = canonical_a_share_symbol(prefixed)
        except ValueError:
            continue
        output[symbol] = {
            "name": str(row[name_column]).strip() if name_column is not None else symbol,
            "price": row[price_column] if price_column is not None else None,
            "amount": row[amount_column] if amount_column is not None else None,
            "turnover_rate": row[turnover_column] if turnover_column is not None else None,
            "market_cap": row[market_cap_column] if market_cap_column is not None else None,
            "industry": str(row[industry_column]).strip()
            if industry_column is not None
            else "",
            "provider": "akshare_eastmoney",
            "available_at": observed_at.isoformat(),
        }
    return output


def _tencent_spot(
    as_of: date,
    symbols: list[str],
    *,
    require_market_date: bool = True,
    batch_size: int = 250,
    request_attempts: int = 3,
    batch_pause_seconds: float = 0.02,
) -> dict[str, dict[str, Any]]:
    """Fetch audited Tencent quotes; stale quote dates never enter a formal pool."""

    market_today = datetime.now(UTC).astimezone(MARKET_TZ).date()
    if as_of != market_today:
        raise ValueError("free current-market snapshot cannot be backfilled into another date")
    normalized = sorted(
        {
            canonical_a_share_symbol(symbol)
            for symbol in symbols
            if str(symbol).lower().startswith(("sh", "sz", "bj"))
        }
    )
    if not normalized:
        raise ValueError("Tencent quote adapter received no A-share symbols")
    output: dict[str, dict[str, Any]] = {}
    observed_dates: set[str] = set()
    stale_records = 0
    for batch_index, start in enumerate(range(0, len(normalized), max(1, batch_size)), start=1):
        batch = normalized[start : start + max(1, batch_size)]
        response = None
        last_error: Exception | None = None
        for request_attempt in range(1, max(1, int(request_attempts)) + 1):
            try:
                response = httpx.get(
                    f"https://qt.gtimg.cn/q={','.join(batch)}",
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (compatible; QuantLab/1.0; audited-market-data)"
                        ),
                        "Referer": "https://finance.qq.com/",
                    },
                    timeout=15.0,
                )
                response.raise_for_status()
                break
            except Exception as exc:
                last_error = exc
                if request_attempt < max(1, int(request_attempts)):
                    time_module.sleep(min(1.0, 0.15 * 2 ** (request_attempt - 1)))
        if response is None:
            assert last_error is not None
            raise RuntimeError(
                f"Tencent quote batch {batch_index} failed after {request_attempts} attempts"
            ) from last_error
        body = response.content.decode("gb18030", errors="replace")
        response_fingerprint = hashlib.sha256(response.content).hexdigest()
        available_at = datetime.now(UTC).isoformat()
        for line in body.splitlines():
            record = _parse_tencent_quote_line(
                line,
                available_at=available_at,
                response_fingerprint=response_fingerprint,
                batch_index=batch_index,
            )
            if record is None:
                continue
            provider_date = str(record["provider_market_date"])
            observed_dates.add(provider_date)
            if require_market_date and provider_date != as_of.isoformat():
                stale_records += 1
                continue
            output[str(record.pop("symbol"))] = record
        if start + len(batch) < len(normalized) and batch_pause_seconds > 0:
            time_module.sleep(float(batch_pause_seconds))
    if not output:
        suffix = (
            f"; rejected_stale_records={stale_records}"
            if require_market_date and stale_records
            else ""
        )
        raise ValueError(
            "Tencent quote provider returned no records valid for "
            f"{as_of.isoformat()}; observed_market_dates={sorted(observed_dates)}{suffix}"
        )
    return output


def _security_master_candidate_symbols(
    records: list[dict[str, Any]],
    as_of: date,
) -> list[str]:
    output = []
    for item in records:
        symbol = str(item.get("symbol") or "").strip().lower()
        if not symbol.startswith(("sh", "sz", "bj")):
            continue
        if not _master_record_listed(item, as_of):
            continue
        try:
            output.append(canonical_a_share_symbol(symbol))
        except ValueError:
            continue
    return sorted(set(output))


def _master_record_listed(record: dict[str, Any], as_of: date) -> bool:
    if not record:
        return False
    listing = str(record.get("listing_date") or "")[:10]
    delisting = str(record.get("delisting_date") or "")[:10]
    status = str(record.get("status") or "").strip().lower()
    if not listing or listing > as_of.isoformat():
        return False
    if delisting and delisting < as_of.isoformat():
        return False
    return status not in {"0", "delisted", "inactive", "terminated"}


def _records_fingerprint(records: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _pool_field_observations(
    *,
    as_of: date,
    security_master_observed_at: datetime,
    industry_observed_at: datetime,
    universe_observed_at: datetime,
    master: dict[str, Any],
    industry_record: dict[str, Any],
    market_item: dict[str, Any],
    trade_status_provider: str | None,
    master_raw_fingerprint: str,
    industry_raw_fingerprint: str,
) -> dict[str, dict[str, Any]]:
    master_observed = str(
        master.get("available_at")
        or security_master_observed_at.astimezone(UTC).isoformat()
    )
    industry_observed = str(
        industry_record.get("available_at")
        or industry_observed_at.astimezone(UTC).isoformat()
    )
    universe_observed = universe_observed_at.astimezone(UTC).isoformat()
    spot_observed = str(market_item.get("available_at") or universe_observed)
    spot_market_date = market_item.get("provider_market_date")
    spot_fingerprint = market_item.get("provider_response_fingerprint")

    def audit(
        value: Any,
        *,
        source: str | None,
        available_at: str,
        market_date: str | None,
        raw_fingerprint: str | None,
    ) -> dict[str, Any]:
        available = value not in {None, ""}
        return {
            "source": source if available else None,
            "available_at": available_at,
            "market_date": market_date,
            "raw_response_fingerprint": raw_fingerprint,
            "missing_reason": None if available else "provider_field_unavailable",
        }

    return {
        "symbol": audit(
            master.get("symbol"),
            source="baostock",
            available_at=master_observed,
            market_date=as_of.isoformat(),
            raw_fingerprint=master_raw_fingerprint,
        ),
        "name": audit(
            market_item.get("name") or master.get("name"),
            source=market_item.get("provider") or "baostock",
            available_at=spot_observed if market_item.get("name") else master_observed,
            market_date=str(spot_market_date or as_of.isoformat()),
            raw_fingerprint=spot_fingerprint or master_raw_fingerprint,
        ),
        "listing_date": audit(
            master.get("listing_date"),
            source="baostock",
            available_at=master_observed,
            market_date=as_of.isoformat(),
            raw_fingerprint=master_raw_fingerprint,
        ),
        "industry": audit(
            industry_record.get("industry") or market_item.get("industry"),
            source=industry_record.get("provider") or market_item.get("provider"),
            available_at=(
                industry_observed if industry_record.get("industry") else spot_observed
            ),
            market_date=as_of.isoformat(),
            raw_fingerprint=(
                industry_raw_fingerprint if industry_record.get("industry") else spot_fingerprint
            ),
        ),
        **{
            field: audit(
                market_item.get(field),
                source=market_item.get("provider"),
                available_at=spot_observed,
                market_date=str(spot_market_date) if spot_market_date else None,
                raw_fingerprint=spot_fingerprint,
            )
            for field in (
                "price",
                "previous_close",
                "daily_return_pct",
                "amount",
                "turnover_rate",
                "market_cap",
            )
        },
        "trade_status": audit(
            trade_status_provider,
            source=trade_status_provider,
            available_at=(
                universe_observed if trade_status_provider == "baostock" else spot_observed
            ),
            market_date=str(spot_market_date or as_of.isoformat()),
            raw_fingerprint=spot_fingerprint or master_raw_fingerprint,
        ),
        "is_st": audit(
            True,
            source=market_item.get("provider") or "baostock",
            available_at=spot_observed if market_item else master_observed,
            market_date=str(spot_market_date or as_of.isoformat()),
            raw_fingerprint=spot_fingerprint or master_raw_fingerprint,
        ),
    }


def _latest_included_observation(
    observations: dict[str, dict[str, Any]],
) -> datetime:
    values = [
        _aware_datetime(item["available_at"])
        for item in observations.values()
        if item.get("missing_reason") is None and item.get("available_at")
    ]
    if not values:
        raise ValueError("pool member has no auditable included field observation")
    return max(values)


def _latest_pool_observation(members: list[dict[str, Any]]) -> datetime:
    values: list[datetime] = []
    for member in members:
        observations = dict((member.get("payload") or {}).get("field_observations") or {})
        values.extend(
            _aware_datetime(item["available_at"])
            for item in observations.values()
            if item.get("missing_reason") is None and item.get("available_at")
        )
    return max(values) if values else datetime.min.replace(tzinfo=UTC)


def _aware_datetime(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("provider observation timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _parse_tencent_quote_line(
    line: str,
    *,
    available_at: str,
    response_fingerprint: str,
    batch_index: int,
) -> dict[str, Any] | None:
    if "=\"" not in line:
        return None
    variable, payload = line.split("=\"", 1)
    fields = payload.rsplit("\"", 1)[0].split("~")
    if len(fields) <= 45 or not fields[2].strip():
        return None
    raw_symbol = variable.removeprefix("v_").strip().lower()
    if not raw_symbol.startswith(("sh", "sz", "bj")):
        return None
    timestamp = fields[30].strip()
    try:
        provider_time = datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(
            tzinfo=MARKET_TZ
        )
    except ValueError:
        return None
    amount_parts = fields[35].split("/")
    amount = _optional_float(amount_parts[2]) if len(amount_parts) >= 3 else None
    total_market_cap_yi = _optional_float(fields[45])
    price = _optional_float(fields[3])
    previous_close = _optional_float(fields[4])
    daily_return_pct = (
        (price / previous_close - 1.0) * 100.0
        if price is not None and previous_close is not None and previous_close > 0
        else None
    )
    return {
        "symbol": canonical_a_share_symbol(raw_symbol),
        "name": fields[1].strip() or raw_symbol,
        "price": price,
        "previous_close": previous_close,
        "daily_return_pct": daily_return_pct,
        "amount": amount,
        "turnover_rate": _optional_float(fields[38]),
        "market_cap": total_market_cap_yi * 100_000_000.0
        if total_market_cap_yi is not None
        else None,
        "industry": "",
        "provider": "tencent_quote",
        "available_at": available_at,
        "provider_timestamp": provider_time.isoformat(),
        "provider_market_date": provider_time.date().isoformat(),
        "provider_response_fingerprint": response_fingerprint,
        "provider_batch_index": batch_index,
        "market_cap_unit": "CNY",
        "turnover_rate_unit": "percent",
        "amount_unit": "CNY",
    }


def _akshare_sina_spot(as_of: date) -> dict[str, dict[str, Any]]:
    """Second free source with a distinct host and explicit missing-field semantics."""

    market_today = datetime.now(UTC).astimezone(MARKET_TZ).date()
    if as_of != market_today:
        raise ValueError("free current-market snapshot cannot be backfilled into another date")
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover - optional data dependency
        raise RuntimeError("akshare is not installed") from exc
    frame = ak.stock_zh_a_spot()
    if frame is None or frame.empty:
        raise ValueError("akshare Sina A-share snapshot returned no rows")
    columns = {str(column).strip(): column for column in frame.columns}

    def column(*names: str):
        return next((columns[name] for name in names if name in columns), None)

    code_column = column("代码", "证券代码", "code")
    if code_column is None:
        raise ValueError("akshare Sina A-share schema has no security code")
    name_column = column("名称", "证券简称", "name")
    price_column = column("最新价", "现价", "price")
    amount_column = column("成交额", "amount")
    volume_column = column("成交量", "volume")
    timestamp_column = column("时间戳", "时间", "timestamp")
    observed_at = datetime.now(UTC)
    output: dict[str, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        try:
            raw_code = str(row[code_column]).strip().lower().replace(".", "")
            if raw_code.startswith(("sh", "sz")):
                prefixed = raw_code[:2] + raw_code[2:].zfill(6)
            else:
                numeric = raw_code.zfill(6)
                prefixed = ("sh" if numeric.startswith("6") else "sz") + numeric
            symbol = canonical_a_share_symbol(prefixed)
        except ValueError:
            continue
        output[symbol] = {
            "name": str(row[name_column]).strip() if name_column is not None else symbol,
            "price": row[price_column] if price_column is not None else None,
            "amount": row[amount_column] if amount_column is not None else None,
            "volume": row[volume_column] if volume_column is not None else None,
            "turnover_rate": None,
            "market_cap": None,
            "industry": "",
            "provider": "akshare_sina",
            "available_at": observed_at.isoformat(),
            "provider_timestamp": str(row[timestamp_column]).strip()
            if timestamp_column is not None
            else None,
        }
    return output


def _call_with_timeout(provider_key: str, callback, timeout_seconds: float):
    return call_single_flight(provider_key, callback, timeout_seconds)


def _optional_float(value: Any) -> float | None:
    if value is None or str(value).strip().lower() in {"", "nan", "none", "-"}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _optional_signed_float(value: Any) -> float | None:
    if value is None or str(value).strip().lower() in {"", "nan", "none", "-"}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _result_observability(value: Any) -> dict[str, Any]:
    records = list(value.values()) if isinstance(value, dict) else list(value or [])
    mapping_records = [item for item in records if isinstance(item, dict)]
    fields = ("price", "amount", "turnover_rate", "market_cap")
    coverage = {
        field: sum(_optional_float(item.get(field)) is not None for item in mapping_records)
        / max(1, len(mapping_records))
        for field in fields
    }
    provider_dates = sorted(
        {
            str(item.get("provider_market_date"))
            for item in mapping_records
            if item.get("provider_market_date")
        }
    )
    batch_fingerprints = sorted(
        {
            str(item.get("provider_response_fingerprint"))
            for item in mapping_records
            if item.get("provider_response_fingerprint")
        }
    )
    return {
        "record_count": len(records),
        "field_coverage_by_field": coverage if mapping_records else {},
        "provider_market_dates": provider_dates,
        "request_batch_count": len(batch_fingerprints),
        "response_fingerprints": batch_fingerprints,
    }


def _failure(component: str, exc: Exception) -> dict[str, str]:
    return {
        "component": component,
        "type": type(exc).__name__,
        "reason": str(exc),
    }


__all__ = ["FreeTrustedDataAdapter", "TrustedDataAdapter"]
