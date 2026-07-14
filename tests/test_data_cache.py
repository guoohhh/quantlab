import json
import os
from datetime import UTC, date, datetime, timedelta

import pytest

from quantlab.data.base import DataProvider, ProviderError
from quantlab.data.cache import CachedProvider
from quantlab.domain.models import Bar


class CountingProvider(DataProvider):
    name = "counting"

    def __init__(self):
        self.calls = 0

    def instruments(self, asset_type=None):
        return []

    def bars(self, symbols, start, end):
        self.calls += 1
        return [
            Bar(
                symbol=symbols[0],
                date=start,
                open=10,
                high=11,
                low=9,
                close=10.5,
                source=self.name,
            )
        ]


def test_cache_hit_avoids_second_provider_call(tmp_path):
    wrapped = CountingProvider()
    provider = CachedProvider(wrapped, tmp_path)

    first = provider.bars(["sh510300"], date(2026, 1, 1), date(2026, 1, 2))
    second = provider.bars(["sh510300"], date(2026, 1, 1), date(2026, 1, 2))

    assert first == second
    assert wrapped.calls == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupted_cache_is_removed_and_refetched(tmp_path):
    wrapped = CountingProvider()
    provider = CachedProvider(wrapped, tmp_path)
    arguments = (["sh510300"], date(2026, 1, 1), date(2026, 1, 2))
    provider.bars(*arguments)
    cache_file = next(tmp_path.glob("bars-*.json"))
    cache_file.write_text("{not-json", encoding="utf-8")

    bars = provider.bars(*arguments)

    assert wrapped.calls == 2
    assert bars[0].symbol == "sh510300"
    assert cache_file.read_text(encoding="utf-8").startswith("{")


def test_fresh_cache_timestamp_does_not_hide_stale_market_tail(tmp_path):
    wrapped = CountingProvider()
    provider = CachedProvider(wrapped, tmp_path)
    symbols = ["sh510300"]
    start = date(2026, 1, 1)
    end = date(2026, 1, 2)
    provider.bars(symbols, start, end)
    cache_file = next(tmp_path.glob("bars-*.json"))
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    payload["fetched_at"] = datetime.now(UTC).isoformat()
    payload["items"][0]["date"] = "2020-01-01"
    cache_file.write_text(json.dumps(payload), encoding="utf-8")

    bars = provider.bars(symbols, start, end)

    assert wrapped.calls == 2
    assert bars[0].date == start


def test_broader_fresh_cache_can_serve_a_narrower_date_range(tmp_path):
    class RangeProvider(DataProvider):
        name = "range"

        def __init__(self):
            self.calls = 0

        def instruments(self, asset_type=None):
            return []

        def bars(self, symbols, start, end):
            self.calls += 1
            return [
                Bar(
                    symbol=symbols[0],
                    date=day,
                    open=10,
                    high=11,
                    low=9,
                    close=10,
                )
                for day in (
                    start + timedelta(days=offset)
                    for offset in range((end - start).days + 1)
                )
            ]

    wrapped = RangeProvider()
    provider = CachedProvider(wrapped, tmp_path)
    provider.bars(["sh510300"], date(2026, 1, 1), date(2026, 1, 10))

    narrowed = provider.bars(["sh510300"], date(2026, 1, 2), date(2026, 1, 9))

    assert wrapped.calls == 1
    assert all(date(2026, 1, 2) <= bar.date <= date(2026, 1, 9) for bar in narrowed)


def test_cache_delegates_instruments_and_rejects_incomplete_fetched_bars(tmp_path):
    wrapped = CountingProvider()
    provider = CachedProvider(wrapped, tmp_path)

    assert provider.instruments() == []
    with pytest.raises(ProviderError, match="incomplete bars"):
        provider.bars(["sh510300"], date(2026, 1, 1), date(2026, 2, 1))


def test_covering_cache_skips_invalid_candidates_before_valid_range(tmp_path):
    class NeverCalledProvider(DataProvider):
        name = "target"

        def __init__(self):
            self.calls = 0

        def instruments(self, asset_type=None):
            return []

        def bars(self, symbols, start, end):
            self.calls += 1
            raise AssertionError("a valid covering cache should be reused")

    wrapped = NeverCalledProvider()
    provider = CachedProvider(wrapped, tmp_path)
    start = date(2026, 1, 1)
    end = date(2026, 1, 30)

    def bars_between(first: date, last: date):
        return [
            Bar(
                symbol="sh510300",
                date=first + timedelta(days=offset),
                open=10,
                high=11,
                low=9,
                close=10,
            )
            for offset in range((last - first).days + 1)
        ]

    def write_candidate(name, source, bars, fetched_at, mtime):
        path = tmp_path / name
        path.write_text(
            json.dumps(
                {
                    "provider": source,
                    "fetched_at": fetched_at.isoformat(),
                    "items": [bar.model_dump(mode="json") for bar in bars],
                }
            ),
            encoding="utf-8",
        )
        os.utime(path, (mtime, mtime))

    now = datetime.now(UTC)
    valid = bars_between(start, end)
    write_candidate("bars-valid.json", "target", valid, now, 1)
    write_candidate("bars-wrong-provider.json", "other", valid, now, 2)
    write_candidate("bars-expired.json", "target", valid, now - timedelta(days=2), 3)
    write_candidate("bars-stale.json", "target", bars_between(start, start), now, 4)
    write_candidate(
        "bars-late-start.json", "target", bars_between(date(2026, 1, 20), end), now, 5
    )
    corrupt = tmp_path / "bars-corrupt.json"
    corrupt.write_text("{not-json", encoding="utf-8")
    os.utime(corrupt, (6, 6))

    bars = provider.bars(["sh510300"], start, end)

    assert wrapped.calls == 0
    assert bars[0].date == start
    assert bars[-1].date == end
