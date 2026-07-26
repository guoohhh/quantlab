from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from quantlab.config import Settings
from quantlab.domain.data_governance import DataNamespace, DataTrustLevel
from quantlab.persistence.jobs import JobRepository
from quantlab.persistence.round5 import Round5Repository


MARKET_TZ = ZoneInfo("Asia/Shanghai")


class TradingCalendarService:
    """One calendar boundary with a fail-closed production namespace."""

    def __init__(
        self,
        repository: JobRepository,
        trusted_repository: Round5Repository | None = None,
    ):
        self.repository = repository
        self.trusted_repository = trusted_repository or Round5Repository(repository.path)

    @classmethod
    def from_settings(cls, settings: Settings) -> "TradingCalendarService":
        path = settings.resolve(settings.get("system.database_path"))
        return cls(JobRepository(path), Round5Repository(path))

    def ingest(
        self,
        records: list[dict[str, Any]],
        *,
        namespace: DataNamespace | str,
        trust_level: DataTrustLevel | str,
        provider: str,
        source: str,
        endpoint: str,
        source_version: str,
        available_at: datetime,
        license_status: str,
        raw_fingerprint: str,
    ) -> dict[str, Any]:
        dates = [date.fromisoformat(str(item["trade_date"])[:10]) for item in records]
        manifest = self.trusted_repository.create_manifest(
            batch_type="trading_calendar",
            namespace=namespace,
            trust_level=trust_level,
            provider=provider,
            source=source,
            endpoint=endpoint,
            source_version=source_version,
            available_at=available_at,
            license_status=license_status,
            payload=records,
            raw_fingerprint=raw_fingerprint,
            record_count=len(records),
            date_start=min(dates) if dates else None,
            date_end=max(dates) if dates else None,
            status="completed" if records else "unavailable",
            missing_reason=None if records else "calendar provider returned no records",
        )
        saved = self.trusted_repository.save_calendar_days(manifest["manifest_id"], records)
        return {"manifest": manifest, "records_saved": saved}

    def day(
        self,
        value: date,
        *,
        cutoff_at: datetime | None = None,
        formal: bool = False,
        minimum_trust: DataTrustLevel | str = DataTrustLevel.SERVER_OBSERVED,
    ) -> dict[str, Any]:
        trusted = self.trusted_repository.calendar_day(
            value,
            namespace=DataNamespace.PRODUCTION,
            minimum_trust=minimum_trust,
            cutoff_at=cutoff_at,
        )
        if trusted:
            return trusted
        if formal:
            raise ValueError("trusted production trading calendar is unavailable")
        legacy = self.repository.trading_day(value, cutoff_at=cutoff_at)
        return {
            **legacy,
            "namespace": DataNamespace.RESEARCH.value,
            "trust_level": DataTrustLevel.RESEARCH_EXTERNAL.value,
            "formal_eligible": False,
        }

    def is_open(self, value: date, **kwargs: Any) -> bool:
        return bool(self.day(value, **kwargs)["is_open"])

    def on_or_next_open_day(
        self,
        value: date,
        *,
        cutoff_at: datetime | None = None,
        maximum_days: int = 40,
        formal: bool = False,
        minimum_trust: DataTrustLevel | str = DataTrustLevel.SERVER_OBSERVED,
    ) -> date:
        cursor = value
        for _ in range(maximum_days + 1):
            if self.is_open(
                cursor,
                cutoff_at=cutoff_at,
                formal=formal,
                minimum_trust=minimum_trust,
            ):
                return cursor
            cursor += timedelta(days=1)
        raise ValueError("no open trading day is available in the calendar window")

    def next_open_day(self, value: date, **kwargs: Any) -> date:
        return self.on_or_next_open_day(value + timedelta(days=1), **kwargs)

    def add_open_days(
        self,
        value: date,
        sessions: int,
        *,
        cutoff_at: datetime | None = None,
        maximum_days: int = 120,
        formal: bool = False,
        minimum_trust: DataTrustLevel | str = DataTrustLevel.SERVER_OBSERVED,
    ) -> date:
        if sessions < 0:
            raise ValueError("trading sessions cannot be negative")
        cursor = value
        remaining = sessions
        scanned = 0
        while remaining:
            cursor += timedelta(days=1)
            scanned += 1
            if scanned > maximum_days:
                raise ValueError("trading calendar does not cover the requested horizon")
            if self.is_open(
                cursor,
                cutoff_at=cutoff_at,
                formal=formal,
                minimum_trust=minimum_trust,
            ):
                remaining -= 1
        return cursor

    def business_day_age(
        self,
        observed: date,
        current: date,
        *,
        formal: bool = False,
        minimum_trust: DataTrustLevel | str = DataTrustLevel.SERVER_OBSERVED,
    ) -> int:
        if observed >= current:
            return 0
        age = 0
        cursor = observed
        while cursor < current:
            cursor += timedelta(days=1)
            if self.is_open(cursor, formal=formal, minimum_trust=minimum_trust):
                age += 1
        return age

    def due_at(
        self,
        value: date,
        sessions: int,
        *,
        formal: bool = False,
        minimum_trust: DataTrustLevel | str = DataTrustLevel.SERVER_OBSERVED,
    ) -> datetime:
        due_date = self.add_open_days(
            value,
            sessions,
            formal=formal,
            minimum_trust=minimum_trust,
        )
        return datetime.combine(due_date, time(15, 30), tzinfo=MARKET_TZ).astimezone(UTC)


__all__ = ["TradingCalendarService"]
