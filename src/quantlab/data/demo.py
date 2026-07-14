from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta

from quantlab.data.base import DataProvider
from quantlab.domain.models import AssetType, Bar, Instrument


class DemoDataProvider(DataProvider):
    """Deterministic synthetic data. Every consumer can see that it is demo data."""

    name = "demo_synthetic"
    ETF_SYMBOLS = ["sh510300", "sz159915", "sh510880", "sh518880", "sh513100", "sh511010"]

    def instruments(self, asset_type: str | None = None) -> list[Instrument]:
        items = [
            Instrument(symbol=s, name=f"DEMO-{s}", asset_type=AssetType.ETF, t_plus_one=False)
            for s in self.ETF_SYMBOLS
        ]
        return items if asset_type in (None, AssetType.ETF.value) else []

    def bars(self, symbols: list[str], start: date, end: date) -> list[Bar]:
        bars: list[Bar] = []
        days: list[date] = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                days.append(current)
            current += timedelta(days=1)
        for index, symbol in enumerate(symbols):
            rng = random.Random(10_000 + index)
            price = 1.0 + index * 0.15
            previous = price
            for i, day in enumerate(days):
                drift = [0.00025, 0.00045, 0.00018, 0.00012, 0.00035, 0.00008][index % 6]
                cycle = math.sin(i / (30 + index * 4)) * 0.002
                shock = rng.gauss(0, 0.008 + index * 0.001)
                ret = drift + cycle + shock
                open_price = max(0.2, previous * (1 + rng.gauss(0, 0.002)))
                close = max(0.2, previous * (1 + ret))
                high = max(open_price, close) * (1 + abs(rng.gauss(0, 0.004)))
                low = min(open_price, close) * (1 - abs(rng.gauss(0, 0.004)))
                bars.append(
                    Bar(
                        symbol=symbol,
                        date=day,
                        open=open_price,
                        high=high,
                        low=low,
                        close=close,
                        volume=10_000_000,
                        amount=10_000_000 * close,
                        prev_close=previous,
                        available_at=datetime.combine(day, datetime.min.time()),
                        source=self.name,
                    )
                )
                previous = close
        return bars
