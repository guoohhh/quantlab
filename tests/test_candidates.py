from datetime import date

import pandas as pd

from quantlab.workflows import candidates
import pytest


class FailingWestock:
    def __init__(self, *args, **kwargs):
        pass

    def filter(self, *args, **kwargs):
        raise RuntimeError("offline")


def test_reversal_scan_degrades_instead_of_crashing(settings, monkeypatch):
    monkeypatch.setattr(candidates, "WestockToolProvider", FailingWestock)

    result = candidates.scan_reversal(settings, date(2026, 1, 2), 5)

    assert result.signals == []
    assert result.degraded_sources == ["westock reversal scan failed: offline"]


def test_etf_atr_stop_is_deterministic_and_bounded():
    frame = pd.DataFrame(
        {
            "raw_close": [10 + index * 0.01 for index in range(25)],
            "raw_high": [10.2 + index * 0.01 for index in range(25)],
            "raw_low": [9.8 + index * 0.01 for index in range(25)],
        }
    )

    stop, distance = candidates._etf_atr_stop(frame)

    assert 0.04 <= distance <= 0.12
    assert stop < frame.iloc[-1]["raw_close"]


def test_etf_scan_rejects_unknown_allocation_policy(settings):
    with pytest.raises(ValueError, match="unsupported ETF allocation policy"):
        candidates.scan_etf_rotation(settings, date(2026, 1, 2), "unknown")


def test_lot_aware_etf_core_keeps_high_price_bond_executable(settings):
    symbols = [f"etf-{index}" for index in range(6)]
    frame = pd.DataFrame(
        [
            {
                "symbol": symbol,
                "date": pd.Timestamp("2026-07-14"),
                "raw_close": 140.0 if index == 0 else 5.0,
            }
            for index, symbol in enumerate(symbols)
        ]
    )

    weights = candidates._lot_aware_etf_core_weights(
        settings, frame, symbols, 100_000.0
    )

    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights[symbols[0]] > 1 / 6
    bond_absolute_weight = 0.80 * weights[symbols[0]]
    assert int(bond_absolute_weight * 100_000 / 140.0 / 100) >= 1
