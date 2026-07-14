from datetime import date

from quantlab.data import DemoDataProvider
from quantlab.workflows.radar import calculate_market_radar


def test_market_radar_calculates_ranked_cross_asset_snapshot_without_network():
    provider = DemoDataProvider()
    symbols = provider.ETF_SYMBOLS
    bars = provider.bars(symbols, date(2023, 1, 1), date(2025, 12, 31))

    radar = calculate_market_radar(
        bars,
        symbols=symbols,
        requested_as_of=date(2025, 12, 31),
        source=provider.name,
    )

    assert radar["coverage"] == {"requested": 6, "available": 6}
    assert radar["risk_appetite"] in {"risk_on", "neutral", "risk_off"}
    assert 0 <= radar["breadth"]["positive_20"] <= 1
    assert radar["leaders"][0] == radar["instruments"][0]["symbol"]
    assert [item["strength_score"] for item in radar["instruments"]] == sorted(
        [item["strength_score"] for item in radar["instruments"]], reverse=True
    )
    assert {item["rank_20"] for item in radar["instruments"]} == set(range(1, 7))
    assert all(item["observations"] > 120 for item in radar["instruments"])


def test_market_radar_discloses_partial_symbol_coverage():
    provider = DemoDataProvider()
    bars = provider.bars(["sh510300"], date(2023, 1, 1), date(2025, 12, 31))

    radar = calculate_market_radar(
        bars,
        symbols=["sh510300", "missing"],
        source=provider.name,
        degraded_sources=["secondary source unavailable"],
    )

    assert radar["coverage"] == {"requested": 2, "available": 1}
    assert radar["degraded_sources"] == ["secondary source unavailable"]
