from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.domain import AssetType, Bar, DataQuality, MarketQuote
from quantlab.domain.data_governance import DataNamespace, DataTrustLevel, trust_at_least
from quantlab.persistence.evidence import EvidenceRepository
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository


class PriceDisagreementError(ValueError):
    pass


class ResearchBarObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bar: Bar
    provider: str
    source: str
    source_version: str
    endpoint: str
    license_status: str
    trust_level: DataTrustLevel
    payload_fingerprint: str
    raw_fingerprint: str


class QuoteProvider(Protocol):
    provider_name: str
    provider_version: str
    authoritative: bool

    def quote(
        self,
        symbol: str,
        *,
        asset_type: AssetType,
        as_of: date,
    ) -> MarketQuote: ...


class InMemoryQuoteProvider:
    """Explicit test provider; it is never selected by production defaults."""

    provider_name = "in_memory_test_quote_provider"
    provider_version = "test-v1"
    authoritative = False

    def __init__(self, quotes: list[MarketQuote] | dict[tuple[str, date], MarketQuote]):
        if isinstance(quotes, dict):
            self._quotes = dict(quotes)
        else:
            self._quotes = {(item.symbol, item.as_of): item for item in quotes}

    def quote(
        self,
        symbol: str,
        *,
        asset_type: AssetType,
        as_of: date,
    ) -> MarketQuote:
        eligible = [
            item
            for (item_symbol, item_date), item in self._quotes.items()
            if item_symbol == symbol and item_date <= as_of
        ]
        if not eligible:
            raise ValueError("test quote is unavailable")
        selected = max(eligible, key=lambda item: item.as_of).model_copy(deep=True)
        selected.asset_type = asset_type
        # Preserve the fixture's field-level source for citations.  The provider
        # remains explicit and non-authoritative, so a test quote can never be
        # mistaken for production evidence while its provenance is still
        # auditable by callers.
        selected.provider = self.provider_name
        selected.authoritative = False
        selected.evidence_stage = "test"
        selected.trust_level = DataTrustLevel.TEST
        selected.license_status = "test_fixture"
        selected.endpoint = "in-memory"
        return selected


class StoredTestQuoteProvider:
    """SQLite-backed test provider gated by an explicit environment switch."""

    provider_name = "stored_test_quote_provider"
    provider_version = "test-v1"
    authoritative = False

    def __init__(self, path: str | Path):
        if os.getenv("QUANTLAB_ENABLE_TEST_QUOTES", "").lower() not in {"1", "true", "yes"}:
            raise PermissionError("stored test quotes are disabled")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS internal_test_quotes (
                    symbol TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    PRIMARY KEY(symbol,as_of)
                )"""
            )

    def save(self, quote: MarketQuote) -> None:
        payload = quote.model_copy(deep=True)
        payload.source = self.provider_name
        payload.source_version = self.provider_version
        payload.provider = self.provider_name
        payload.authoritative = False
        payload.evidence_stage = "test"
        payload.trust_level = DataTrustLevel.TEST
        payload.license_status = "test_fixture"
        payload.endpoint = "sqlite/internal_test_quotes"
        with sqlite3.connect(self.path) as db:
            db.execute(
                """INSERT INTO internal_test_quotes(symbol,as_of,payload,imported_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(symbol,as_of) DO UPDATE SET
                     payload=excluded.payload,imported_at=excluded.imported_at""",
                (
                    payload.symbol,
                    payload.as_of.isoformat(),
                    payload.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def quote(
        self,
        symbol: str,
        *,
        asset_type: AssetType,
        as_of: date,
    ) -> MarketQuote:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                """SELECT payload FROM internal_test_quotes
                   WHERE symbol=? AND as_of<=? ORDER BY as_of DESC LIMIT 1""",
                (symbol, as_of.isoformat()),
            ).fetchone()
        if row is None:
            raise ValueError("stored test quote is unavailable")
        result = MarketQuote.model_validate_json(row[0])
        result.asset_type = asset_type
        return result


class _ProductionQuoteProvider:
    provider_name = "quantlab_market_fallback"
    provider_version = "bars-v1"
    authoritative = True

    def __init__(self, settings: Settings):
        self.settings = settings
        self.fallback = FallbackProvider(
            [WestockProvider(settings.root.parent), AkShareProvider()]
        )
        self.provider = CachedProvider(
            self.fallback,
            settings.resolve(settings.get("system.data_dir")) / "cache",
        )

    def quote(
        self,
        symbol: str,
        *,
        asset_type: AssetType,
        as_of: date,
    ) -> MarketQuote:
        bars = self.provider.bars([symbol], as_of - timedelta(days=30), as_of)
        eligible = [bar for bar in bars if bar.symbol == symbol and bar.date <= as_of]
        if not eligible:
            raise ValueError("server-observed market quote is unavailable")
        bar = max(eligible, key=lambda item: item.date)
        trade_lot = int(
            self.settings.get(
                f"costs.{asset_type.value}.trade_lot",
                100 if asset_type in {AssetType.STOCK, AssetType.ETF} else 10,
            )
        )
        industry, industry_source = _point_in_time_industry(self.settings, symbol, bar.date)
        degraded = list(self.fallback.last_degraded_from)
        if industry is None and asset_type == AssetType.STOCK:
            degraded.append("trusted_point_in_time_industry_unavailable")
        quality = DataQuality.DEGRADED if degraded else DataQuality.AVAILABLE
        source_version = _source_version(self.provider.name, bar.available_at)
        raw_fingerprint = hashlib.sha256(bar.model_dump_json().encode("utf-8")).hexdigest()
        observed_now = datetime.now(UTC)
        local_now = observed_now.astimezone(ZoneInfo("Asia/Shanghai"))
        market_today = local_now.date()
        if bar.date < market_today:
            quote_kind = "previous_close"
        elif local_now.time() >= time(15, 5):
            quote_kind = "current_close"
        else:
            quote_kind = "unavailable"
        return MarketQuote(
            symbol=symbol,
            asset_type=asset_type,
            raw_price=bar.close,
            as_of=bar.date,
            available_at=bar.available_at,
            source=self.provider.name,
            provider=self.provider.name,
            source_version=source_version,
            data_quality=quality,
            degraded_from=degraded,
            suspended=bar.suspended,
            limit_up=bar.limit_up,
            limit_down=bar.limit_down,
            is_st=bar.is_st,
            industry=industry,
            trade_lot=trade_lot,
            t_plus_one=asset_type == AssetType.STOCK,
            session_status="closed" if quote_kind in {"current_close", "previous_close"} else "unknown",
            quote_kind=quote_kind,
            observed_at=observed_now,
            authoritative=True,
            evidence_stage="production",
            trust_level=DataTrustLevel.SERVER_OBSERVED,
            license_status="provider_terms_unverified_no_sla",
            endpoint="historical_bars/latest_close",
            raw_payload_fingerprint=raw_fingerprint,
            actionable=False,
            actionability_reasons=[
                "daily_bar_is_not_a_realtime_execution_quote",
                f"quote_kind_{quote_kind}",
            ],
            risk_metadata={
                "industry_source": industry_source,
                "quote_provider_version": source_version,
            },
        )


class ExecutionQuoteService:
    """Latest quote boundary with freshness, session and cross-source checks."""

    def __init__(
        self,
        provider: QuoteProvider | list[QuoteProvider],
        *,
        repository: Round5Repository | None = None,
        maximum_price_deviation_bps: float = 50.0,
        maximum_quote_age_seconds: int = 120,
    ):
        self.providers = provider if isinstance(provider, list) else [provider]
        self.provider = self.providers[0]
        self.repository = repository
        self.maximum_price_deviation_bps = maximum_price_deviation_bps
        self.maximum_quote_age_seconds = max(1, int(maximum_quote_age_seconds))
        self._last_health: dict[str, Any] = {
            "status": "not_checked",
            "providers": [],
            "last_success_at": None,
        }

    @classmethod
    def from_settings(cls, settings: Settings) -> "ExecutionQuoteService":
        path = settings.resolve(settings.get("system.database_path"))
        if os.getenv("QUANTLAB_ENABLE_TEST_QUOTES", "").lower() in {"1", "true", "yes"}:
            return cls(StoredTestQuoteProvider(path), repository=Round5Repository(path))
        return cls(
            _ProductionQuoteProvider(settings),
            repository=Round5Repository(path),
            maximum_price_deviation_bps=float(
                settings.get("runtime.maximum_execution_price_deviation_bps", 50.0)
            ),
            maximum_quote_age_seconds=int(
                settings.get("runtime.maximum_execution_quote_age_seconds", 120)
            ),
        )

    def get(
        self,
        symbol: str,
        *,
        asset_type: AssetType | str,
        as_of: date | None = None,
        require_authoritative: bool = True,
        minimum_trust: DataTrustLevel | str | None = None,
        require_actionable: bool = False,
    ) -> MarketQuote:
        resolved_asset = asset_type if isinstance(asset_type, AssetType) else AssetType(asset_type)
        requested_date = as_of or date.today()
        quotes = []
        provider_states = []
        for provider in self.providers:
            started = datetime.now(UTC)
            try:
                item = provider.quote(symbol, asset_type=resolved_asset, as_of=requested_date)
                quotes.append(item)
                provider_states.append(
                    {
                        "provider": getattr(provider, "provider_name", "unknown"),
                        "status": "available",
                        "observed_at": started.isoformat(),
                        "as_of": item.as_of.isoformat(),
                        "available_at": item.available_at.isoformat()
                        if item.available_at
                        else None,
                    }
                )
            except Exception as exc:
                provider_states.append(
                    {
                        "provider": getattr(provider, "provider_name", "unknown"),
                        "status": "unavailable",
                        "observed_at": started.isoformat(),
                        "error_type": type(exc).__name__,
                    }
                )
        if not quotes:
            self._last_health = {
                "status": "unavailable",
                "providers": provider_states,
                "last_success_at": self._last_health.get("last_success_at"),
            }
            raise ValueError("execution quote is unavailable from all configured providers")
        if any(item.symbol != symbol for item in quotes):
            raise ValueError("quote provider returned a different symbol")
        if any(item.as_of > requested_date for item in quotes):
            raise ValueError("quote provider returned future market data")
        quote = quotes[0]
        deviation_bps = 0.0
        if len(quotes) > 1:
            low = min(item.raw_price for item in quotes)
            high = max(item.raw_price for item in quotes)
            deviation_bps = (high / low - 1.0) * 10_000
            if deviation_bps > self.maximum_price_deviation_bps:
                self._last_health = {
                    "status": "conflict",
                    "providers": provider_states,
                    "price_deviation_bps": deviation_bps,
                    "last_success_at": self._last_health.get("last_success_at"),
                }
                raise PriceDisagreementError(
                    f"execution quote providers disagree by {deviation_bps:.1f} bps"
                )
        quote.provider = quote.provider or self.provider.provider_name
        quote.source_version = quote.source_version or self.provider.provider_version
        quote.authoritative = bool(
            getattr(self.provider, "authoritative", False) and quote.authoritative
        )
        if require_authoritative and not quote.authoritative:
            raise ValueError("test or research quote cannot be used by a production account")
        if minimum_trust and not trust_at_least(quote.trust_level, minimum_trust):
            raise ValueError("execution quote does not meet the required trust level")
        reasons = list(quote.actionability_reasons)
        observed_now = datetime.now(UTC)
        quote.observed_at = observed_now
        quote.price_deviation_bps = deviation_bps
        quote.provider_health = {
            "providers": provider_states,
            "maximum_price_deviation_bps": self.maximum_price_deviation_bps,
        }
        quote.quote_kind, quote.delay_seconds = _classify_quote(
            quote,
            requested_date=requested_date,
            observed_now=observed_now,
            maximum_quote_age_seconds=self.maximum_quote_age_seconds,
        )
        if quote.available_at and quote.available_at.astimezone(UTC) > observed_now:
            if require_authoritative or require_actionable:
                raise ValueError("quote provider returned a future availability timestamp")
            reasons.append("future_availability_allowed_only_for_non_authoritative_research")
        if quote.as_of != requested_date:
            reasons.append("quote_is_not_for_the_requested_session")
        if quote.session_status == "unknown":
            reasons.append("session_status_is_unknown")
        if quote.data_quality in {DataQuality.STALE, DataQuality.MISSING}:
            reasons.append("quote_is_stale_or_missing")
        if quote.session_status == "open" and quote.available_at:
            quote_age = (observed_now - quote.available_at.astimezone(UTC)).total_seconds()
            if quote_age > self.maximum_quote_age_seconds:
                reasons.append("execution_quote_exceeds_freshness_limit")
        if quote.quote_kind in {"current_close", "previous_close", "unavailable"}:
            reasons.append(f"quote_kind_{quote.quote_kind}_is_not_intraday_actionable")
        if quote.suspended:
            reasons.append("security_is_suspended")
        quote.actionability_reasons = list(dict.fromkeys(reasons))
        quote.actionable = not quote.actionability_reasons and quote.session_status == "open"
        quote.quote_fingerprint = _quote_fingerprint(quote)
        self._persist_manifest(quote)
        self._last_health = {
            "status": "available" if quote.data_quality == DataQuality.AVAILABLE else "degraded",
            "providers": provider_states,
            "price_deviation_bps": deviation_bps,
            "quote_kind": quote.quote_kind,
            "delay_seconds": quote.delay_seconds,
            "last_success_at": observed_now.isoformat(),
        }
        if require_actionable and not quote.actionable:
            raise ValueError(
                "execution quote is not actionable: " + ",".join(quote.actionability_reasons)
            )
        return quote

    def health_snapshot(self) -> dict[str, Any]:
        return dict(self._last_health)

    def _persist_manifest(self, quote: MarketQuote) -> None:
        if self.repository is None:
            return
        if quote.trust_level == DataTrustLevel.TEST:
            namespace = DataNamespace.TEST
        elif trust_at_least(quote.trust_level, DataTrustLevel.SERVER_OBSERVED):
            namespace = DataNamespace.PRODUCTION
        else:
            namespace = DataNamespace.RESEARCH
        self.repository.create_manifest(
            batch_type="execution_quote",
            namespace=namespace,
            trust_level=quote.trust_level,
            provider=quote.provider or quote.source,
            source=quote.source,
            endpoint=quote.endpoint,
            source_version=quote.source_version,
            available_at=quote.available_at or datetime.now(UTC),
            license_status=quote.license_status,
            payload=quote.model_dump(mode="json"),
            raw_fingerprint=quote.raw_payload_fingerprint or quote.quote_fingerprint,
            record_count=1,
            date_start=quote.as_of,
            date_end=quote.as_of,
        )


QuoteService = ExecutionQuoteService


class ResearchBarService:
    """Point-in-time bars for research, settlement and next-open shadow fills."""

    def __init__(
        self,
        provider: Any,
        *,
        provider_name: str,
        provider_version: str,
        trust_level: DataTrustLevel = DataTrustLevel.SERVER_OBSERVED,
        license_status: str = "provider_terms_unverified_no_sla",
        endpoint: str = "historical_bars",
        repository: Round5Repository | None = None,
    ):
        self.provider = provider
        self.provider_name = provider_name
        self.provider_version = provider_version
        self.trust_level = trust_level
        self.license_status = license_status
        self.endpoint = endpoint
        self.repository = repository

    @classmethod
    def from_settings(cls, settings: Settings) -> "ResearchBarService":
        fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
        provider = CachedProvider(
            fallback,
            settings.resolve(settings.get("system.data_dir")) / "cache",
        )
        return cls(
            provider,
            provider_name=provider.name,
            provider_version="bars-v1",
            repository=Round5Repository(
                settings.resolve(settings.get("system.database_path"))
            ),
        )

    def get(
        self,
        symbol: str,
        *,
        as_of: date,
        minimum_trust: DataTrustLevel | str = DataTrustLevel.SERVER_OBSERVED,
        exact: bool = True,
    ) -> ResearchBarObservation:
        if not trust_at_least(self.trust_level, minimum_trust):
            raise ValueError("research bar does not meet the required trust level")
        bars = self.provider.bars([symbol], as_of - timedelta(days=10), as_of)
        eligible = [item for item in bars if item.symbol == symbol and item.date <= as_of]
        if not eligible:
            raise ValueError("research bar is unavailable")
        bar = max(eligible, key=lambda item: item.date)
        if exact and bar.date != as_of:
            raise ValueError("exact research bar is unavailable for the requested trading day")
        raw = bar.model_dump(mode="json")
        fingerprint = hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        observation = ResearchBarObservation(
            bar=bar,
            provider=self.provider_name,
            source=bar.source or self.provider_name,
            source_version=self.provider_version,
            endpoint=self.endpoint,
            license_status=self.license_status,
            trust_level=self.trust_level,
            payload_fingerprint=fingerprint,
            raw_fingerprint=fingerprint,
        )
        if self.repository is not None:
            namespace = (
                DataNamespace.PRODUCTION
                if trust_at_least(self.trust_level, DataTrustLevel.SERVER_OBSERVED)
                else DataNamespace.TEST
                if self.trust_level == DataTrustLevel.TEST
                else DataNamespace.RESEARCH
            )
            self.repository.create_manifest(
                batch_type="research_bar",
                namespace=namespace,
                trust_level=self.trust_level,
                provider=self.provider_name,
                source=observation.source,
                endpoint=self.endpoint,
                source_version=self.provider_version,
                available_at=bar.available_at or datetime.now(UTC),
                license_status=self.license_status,
                payload=raw,
                raw_fingerprint=fingerprint,
                record_count=1,
                date_start=bar.date,
                date_end=bar.date,
            )
        return observation


def _point_in_time_industry(
    settings: Settings,
    symbol: str,
    as_of: date,
) -> tuple[str | None, str | None]:
    trusted = Round5Repository(
        settings.resolve(settings.get("system.database_path"))
    ).industry_as_of(symbol, as_of=as_of)
    if trusted is not None:
        return str(trusted["industry"]), f"trusted_industry:{trusted['manifest_id']}"
    evidence = EvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    ).industry_as_of(
        symbol,
        as_of=as_of.isoformat(),
        namespace=DataNamespace.PRODUCTION,
        minimum_trust=DataTrustLevel.SERVER_OBSERVED,
    )
    if evidence is not None:
        return str(evidence["industry"]), (
            f"industry_membership:{evidence['source_version']}"
        )
    repository = StrategyEvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    record = repository.latest_security_record("stock", symbol, as_of=as_of)
    if record is None or record.get("namespace") != "production":
        return None, None
    industry = record.get("payload", {}).get("industry") or record.get("category")
    if not industry:
        return None, None
    return str(industry), f"pit_security_master:{record['master_version']}"


def _source_version(source: str, available_at: datetime | None) -> str:
    normalized = available_at.astimezone(UTC).isoformat() if available_at else "unknown"
    return hashlib.sha256(f"{source}:{normalized}".encode("utf-8")).hexdigest()[:16]


def _quote_fingerprint(quote: MarketQuote) -> str:
    payload = quote.model_dump(
        mode="json",
        exclude={
            "quote_fingerprint",
            "observed_at",
            "provider_health",
            "delay_seconds",
        },
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _classify_quote(
    quote: MarketQuote,
    *,
    requested_date: date,
    observed_now: datetime,
    maximum_quote_age_seconds: int,
) -> tuple[str, float | None]:
    if quote.quote_kind != "unavailable":
        delay = (
            max(0.0, (observed_now - quote.available_at.astimezone(UTC)).total_seconds())
            if quote.available_at
            else None
        )
        return quote.quote_kind, delay
    market_now = observed_now.astimezone(ZoneInfo("Asia/Shanghai"))
    if quote.as_of < requested_date or quote.as_of < market_now.date():
        return "previous_close", None
    if quote.session_status == "closed":
        return "current_close", None
    if quote.session_status != "open" or quote.available_at is None:
        return "unavailable", None
    delay = max(0.0, (observed_now - quote.available_at.astimezone(UTC)).total_seconds())
    return ("realtime" if delay <= max(5, maximum_quote_age_seconds / 4) else "delayed"), delay


__all__ = [
    "ExecutionQuoteService",
    "InMemoryQuoteProvider",
    "PriceDisagreementError",
    "QuoteProvider",
    "QuoteService",
    "ResearchBarObservation",
    "ResearchBarService",
    "StoredTestQuoteProvider",
]
