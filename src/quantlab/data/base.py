from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from quantlab.domain.models import Bar, Evidence, Instrument


class ProviderError(RuntimeError):
    pass


class DataProvider(ABC):
    name: str

    @abstractmethod
    def instruments(self, asset_type: str | None = None) -> list[Instrument]: ...

    @abstractmethod
    def bars(self, symbols: list[str], start: date, end: date) -> list[Bar]: ...

    def fundamentals(self, symbols: list[str], as_of: date) -> dict[str, dict]:
        return {}

    def news(self, symbols: list[str], as_of: date, limit: int = 20) -> list[Evidence]:
        return []
