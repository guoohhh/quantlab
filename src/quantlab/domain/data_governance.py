from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DataTrustLevel(StrEnum):
    TEST = "test"
    USER_IMPORTED = "user_imported"
    RESEARCH_EXTERNAL = "research_external"
    SERVER_OBSERVED = "server_observed"
    TRUSTED_LICENSED = "trusted_licensed"
    EXCHANGE_OR_BROKER_CONFIRMED = "exchange_or_broker_confirmed"


TRUST_ORDER: tuple[DataTrustLevel, ...] = tuple(DataTrustLevel)


class DataNamespace(StrEnum):
    TEST = "test"
    RESEARCH = "research"
    PRODUCTION = "production"


class DataProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    source: str
    endpoint: str
    source_version: str
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    available_at: datetime
    license_status: str = "unknown"
    trust_level: DataTrustLevel
    namespace: DataNamespace
    payload_fingerprint: str = ""
    raw_fingerprint: str = ""
    degraded: bool = False
    missing_reason: str | None = None

    @model_validator(mode="after")
    def validate_boundary(self) -> "DataProvenance":
        if self.namespace == DataNamespace.PRODUCTION and self.trust_level in {
            DataTrustLevel.TEST,
            DataTrustLevel.USER_IMPORTED,
            DataTrustLevel.RESEARCH_EXTERNAL,
        }:
            raise ValueError("production namespace requires server-observed or stronger data")
        return self


def trust_rank(value: DataTrustLevel | str) -> int:
    return TRUST_ORDER.index(DataTrustLevel(value))


def trust_at_least(value: DataTrustLevel | str, minimum: DataTrustLevel | str) -> bool:
    return trust_rank(value) >= trust_rank(minimum)


def payload_fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


__all__ = [
    "DataNamespace",
    "DataProvenance",
    "DataTrustLevel",
    "TRUST_ORDER",
    "payload_fingerprint",
    "trust_at_least",
    "trust_rank",
]
