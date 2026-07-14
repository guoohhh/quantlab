from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from quantlab.data.base import DataProvider, ProviderError
from quantlab.data.quality import bar_coverage_issues
from quantlab.domain.models import Bar, Instrument


class CachedProvider(DataProvider):
    def __init__(self, wrapped: DataProvider, cache_dir: str | Path, ttl_hours: int = 24):
        self.wrapped = wrapped
        self.name = f"cached:{wrapped.name}"
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(hours=ttl_hours)

    def instruments(self, asset_type: str | None = None) -> list[Instrument]:
        return self.wrapped.instruments(asset_type)

    def bars(self, symbols: list[str], start: date, end: date) -> list[Bar]:
        key = hashlib.sha256(f"v4|{self.wrapped.name}|{symbols}|{start}|{end}".encode()).hexdigest()
        path = self.cache_dir / f"bars-{key}.json"
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                fetched_at = datetime.fromisoformat(payload["fetched_at"])
                if datetime.now(UTC) - fetched_at < self.ttl:
                    cached = [Bar.model_validate(item) for item in payload["items"]]
                    if not bar_coverage_issues(cached, symbols, end):
                        return cached
                    path.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError, TypeError):
                path.unlink(missing_ok=True)
        covering = self._covering_cached_bars(symbols, start, end, exclude=path)
        if covering is not None:
            return covering
        bars = self.wrapped.bars(symbols, start, end)
        issues = bar_coverage_issues(bars, symbols, end)
        if issues:
            raise ProviderError("incomplete bars cannot be cached: " + "; ".join(issues))
        payload = json.dumps(
            {
                "fetched_at": datetime.now(UTC).isoformat(),
                "provider": self.wrapped.name,
                "request": {
                    "symbols": symbols,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                "items": [bar.model_dump(mode="json") for bar in bars],
            },
            ensure_ascii=False,
        )
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return bars

    def _covering_cached_bars(
        self,
        symbols: list[str],
        start: date,
        end: date,
        *,
        exclude: Path,
    ) -> list[Bar] | None:
        candidates = sorted(
            (item for item in self.cache_dir.glob("bars-*.json") if item != exclude),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                if payload.get("provider") != self.wrapped.name:
                    continue
                fetched_at = datetime.fromisoformat(payload["fetched_at"])
                if datetime.now(UTC) - fetched_at >= self.ttl:
                    continue
                bars = [Bar.model_validate(item) for item in payload["items"]]
                if bar_coverage_issues(bars, symbols, end):
                    continue
                earliest: dict[str, date] = defaultdict(lambda: date.max)
                for bar in bars:
                    if bar.symbol in symbols:
                        earliest[bar.symbol] = min(earliest[bar.symbol], bar.date)
                if any(
                    symbol not in earliest or (earliest[symbol] - start).days > 15
                    for symbol in symbols
                ):
                    continue
                bounded = [
                    bar
                    for bar in bars
                    if bar.symbol in symbols and start <= bar.date <= end
                ]
                if not bar_coverage_issues(bounded, symbols, end):
                    return bounded
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None
