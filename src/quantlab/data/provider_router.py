from __future__ import annotations

import queue
import uuid
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock, Thread
from typing import Any, Callable


class DataCapability(StrEnum):
    TRADING_CALENDAR = "trading_calendar"
    SECURITY_MASTER = "security_master"
    INDUSTRY_MEMBERSHIP = "industry_membership"
    TRADE_STATUS = "trade_status"
    MARKET_SPOT = "market_spot"
    DAILY_BARS = "daily_bars"


@dataclass(frozen=True)
class ProviderRegistration:
    key: str
    capabilities: frozenset[DataCapability]
    priority: int
    trust_level: str
    license_status: str
    version: str = "unknown"


class ProviderRouter:
    """Capability-based routing metadata; execution and auditing remain in workflows."""

    def __init__(self, registrations: list[ProviderRegistration]):
        self.registrations = tuple(registrations)

    def providers_for(self, capability: DataCapability) -> list[ProviderRegistration]:
        return sorted(
            (item for item in self.registrations if capability in item.capabilities),
            key=lambda item: (item.priority, item.key),
        )

    def capability_manifest(self) -> list[dict[str, Any]]:
        return [
            {
                "provider": item.key,
                "capabilities": sorted(capability.value for capability in item.capabilities),
                "priority": item.priority,
                "trust_level": item.trust_level,
                "license_status": item.license_status,
                "version": item.version,
            }
            for item in sorted(self.registrations, key=lambda value: (value.priority, value.key))
        ]


class ProviderCallTimeout(TimeoutError):
    pass


class ProviderCallInFlight(RuntimeError):
    pass


@dataclass
class _Flight:
    token: str
    thread: Thread


_FLIGHT_LOCK = Lock()
_ACTIVE_FLIGHTS: dict[str, _Flight] = {}


def call_single_flight(
    provider_key: str,
    callback: Callable[[], Any],
    timeout_seconds: float,
) -> Any:
    """Run at most one underlying call per provider, including after caller timeout."""

    token = uuid.uuid4().hex
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result_queue.put((True, callback()))
        except BaseException as exc:  # preserve provider exception type for audit
            result_queue.put((False, exc))
        finally:
            with _FLIGHT_LOCK:
                active = _ACTIVE_FLIGHTS.get(provider_key)
                if active and active.token == token:
                    _ACTIVE_FLIGHTS.pop(provider_key, None)

    with _FLIGHT_LOCK:
        active = _ACTIVE_FLIGHTS.get(provider_key)
        if active and active.thread.is_alive():
            raise ProviderCallInFlight(
                f"provider {provider_key} still has an unfinished timed call"
            )
        if active:
            _ACTIVE_FLIGHTS.pop(provider_key, None)
        thread = Thread(
            target=invoke,
            name=f"quantlab-provider-{provider_key}",
            daemon=True,
        )
        _ACTIVE_FLIGHTS[provider_key] = _Flight(token=token, thread=thread)
        thread.start()
    thread.join(timeout=max(0.1, float(timeout_seconds)))
    if thread.is_alive():
        raise ProviderCallTimeout(
            f"provider {provider_key} exceeded {timeout_seconds:.1f}s timeout"
        )
    succeeded, value = result_queue.get_nowait()
    if succeeded:
        return value
    raise value


def provider_flight_active(provider_key: str) -> bool:
    with _FLIGHT_LOCK:
        flight = _ACTIVE_FLIGHTS.get(provider_key)
        return bool(flight and flight.thread.is_alive())


__all__ = [
    "DataCapability",
    "ProviderCallInFlight",
    "ProviderCallTimeout",
    "ProviderRegistration",
    "ProviderRouter",
    "call_single_flight",
    "provider_flight_active",
]
