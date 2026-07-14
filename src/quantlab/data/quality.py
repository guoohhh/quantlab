from __future__ import annotations

from collections import defaultdict
from datetime import date

from quantlab.domain.models import Bar


def bar_coverage_issues(
    bars: list[Bar],
    symbols: list[str],
    end: date,
    *,
    maximum_end_gap_days: int = 15,
) -> list[str]:
    if not bars:
        return ["empty result"]
    by_symbol: dict[str, list[date]] = defaultdict(list)
    for bar in bars:
        by_symbol[bar.symbol].append(bar.date)
    issues = []
    missing = sorted(set(symbols) - set(by_symbol))
    if missing:
        issues.append("missing symbols=" + ",".join(missing))
    stale = []
    for symbol in symbols:
        dates = by_symbol.get(symbol)
        if not dates:
            continue
        latest = max(dates)
        gap = (end - latest).days
        if gap > maximum_end_gap_days:
            stale.append(f"{symbol}:{latest.isoformat()}({gap}d gap)")
    if stale:
        issues.append("stale tail=" + ",".join(stale))
    return issues
