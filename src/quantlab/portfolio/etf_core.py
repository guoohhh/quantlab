from __future__ import annotations

from datetime import date


def lot_aware_etf_core_weights(
    prices: dict[str, float],
    symbols: list[str],
    capital: float,
    total_exposure: float,
    maximum_single_position: float,
    trade_lot: int = 100,
) -> dict[str, float]:
    """Return relative core weights while keeping each target executable by whole lots."""

    if capital <= 0 or not symbols:
        return {}
    desired = total_exposure / len(symbols)
    absolute: dict[str, float] = {}
    flexible = []
    for symbol in symbols:
        price = float(prices.get(symbol) or 0.0)
        minimum_lot_weight = (
            price * trade_lot / capital if price > 0 else float("inf")
        )
        if desired < minimum_lot_weight <= maximum_single_position:
            absolute[symbol] = min(
                maximum_single_position,
                minimum_lot_weight * 1.001,
            )
        elif minimum_lot_weight > maximum_single_position:
            absolute[symbol] = 0.0
        else:
            flexible.append(symbol)
    remaining = max(0.0, total_exposure - sum(absolute.values()))
    per_flexible = (
        min(maximum_single_position, remaining / len(flexible)) if flexible else 0.0
    )
    absolute.update({symbol: per_flexible for symbol in flexible})
    allocated = sum(absolute.values())
    if allocated <= 0:
        return {symbol: 1 / len(symbols) for symbol in symbols}
    return {symbol: absolute.get(symbol, 0.0) / allocated for symbol in symbols}


def rebalance_period(day: date, frequency: str) -> tuple[int, int]:
    normalized = frequency.lower()
    if normalized == "monthly":
        return day.year, day.month
    if normalized == "quarterly":
        return day.year, (day.month - 1) // 3
    if normalized == "semiannual":
        return day.year, (day.month - 1) // 6
    if normalized == "annual":
        return day.year, 0
    raise ValueError(f"unsupported rebalance frequency: {frequency}")
