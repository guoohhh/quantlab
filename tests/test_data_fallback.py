from datetime import date

import pytest

from quantlab.data.base import DataProvider, ProviderError
from quantlab.data.fallback import FallbackProvider
from quantlab.domain.models import Bar

FAKE_KEY = "sk-" + "abcdefghijklmnop"


class StubDataProvider(DataProvider):
    def __init__(self, name, *, fail=False, empty=False, bar_date=None, symbols=None):
        self.name = name
        self.fail = fail
        self.empty = empty
        self.bar_date = bar_date
        self.symbols = symbols

    def instruments(self, asset_type=None):
        if self.fail:
            raise RuntimeError(f"failed {FAKE_KEY}")
        return []

    def bars(self, symbols, start, end):
        if self.fail:
            raise RuntimeError(f"failed {FAKE_KEY}")
        if self.empty:
            return []
        requested = self.symbols or symbols[:1]
        return [
            Bar(
                symbol=symbol,
                date=self.bar_date or start,
                open=10,
                high=11,
                low=9,
                close=10,
            )
            for symbol in requested
        ]


def test_fallback_provider_records_sanitized_degradation_before_success():
    provider = FallbackProvider(
        [StubDataProvider("primary", fail=True), StubDataProvider("secondary")]
    )

    bars = provider.bars(["sh510300"], date(2026, 1, 1), date(2026, 1, 2))

    assert bars
    assert "[REDACTED_API_KEY]" in provider.last_degraded_from[0]
    assert FAKE_KEY not in provider.last_degraded_from[0]


def test_fallback_provider_reports_empty_and_failed_sources_without_secrets():
    provider = FallbackProvider(
        [StubDataProvider("empty", empty=True), StubDataProvider("failed", fail=True)]
    )

    with pytest.raises(ProviderError) as captured:
        provider.bars(["sh510300"], date(2026, 1, 1), date(2026, 1, 2))

    assert "empty result" in str(captured.value)
    assert "[REDACTED_API_KEY]" in str(captured.value)
    assert FAKE_KEY not in str(captured.value)


def test_fallback_rejects_stale_or_incomplete_primary_bars():
    provider = FallbackProvider(
        [
            StubDataProvider("stale", bar_date=date(2020, 1, 1), symbols=["sh510300"]),
            StubDataProvider(
                "fresh", bar_date=date(2026, 1, 9), symbols=["sh510300", "sh510880"]
            ),
        ]
    )

    bars = provider.bars(["sh510300", "sh510880"], date(2020, 1, 1), date(2026, 1, 10))

    assert len(bars) == 2
    assert "incomplete result" in provider.last_degraded_from[0]
    assert "missing symbols=sh510880" in provider.last_degraded_from[0]
