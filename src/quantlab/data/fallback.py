from __future__ import annotations

from datetime import date

from quantlab.data.base import DataProvider, ProviderError
from quantlab.domain.models import Bar, Instrument
from quantlab.security import safe_error_detail
from quantlab.data.quality import bar_coverage_issues


class FallbackProvider(DataProvider):
    name = "fallback"

    def __init__(self, providers: list[DataProvider]):
        self.providers = providers
        self.last_degraded_from: list[str] = []

    def instruments(self, asset_type: str | None = None) -> list[Instrument]:
        errors = []
        for provider in self.providers:
            try:
                return provider.instruments(asset_type)
            except Exception as exc:
                errors.append(f"{provider.name}: {safe_error_detail(exc)}")
        raise ProviderError("; ".join(errors))

    def bars(self, symbols: list[str], start: date, end: date) -> list[Bar]:
        errors = []
        self.last_degraded_from = []
        for provider in self.providers:
            try:
                bars = provider.bars(symbols, start, end)
                issues = bar_coverage_issues(bars, symbols, end)
                if not issues:
                    self.last_degraded_from = errors
                    return bars
                errors.append(f"{provider.name}: incomplete result ({'; '.join(issues)})")
            except Exception as exc:
                errors.append(f"{provider.name}: {safe_error_detail(exc)}")
        raise ProviderError("all data providers failed: " + "; ".join(errors))
