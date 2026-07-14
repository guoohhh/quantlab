from datetime import date

import pandas as pd

from quantlab.workflows.research import build_price_history_evidence


def test_price_history_evidence_respects_cutoff_and_keeps_price_bases_separate():
    dates = pd.bdate_range("2025-07-01", periods=131)
    rows = []
    for index, timestamp in enumerate(dates, start=1):
        raw_close = 10.0 + index * 0.1
        rows.append(
            {
                "symbol": "sh600519",
                "date": timestamp,
                "open": raw_close - 0.1,
                "high": raw_close + 0.2,
                "low": raw_close - 0.2,
                "close": raw_close,
                "adjusted_open": raw_close * 10 - 1,
                "adjusted_high": raw_close * 10 + 2,
                "adjusted_low": raw_close * 10 - 2,
                "adjusted_close": raw_close * 10,
                "volume": 1_000_000 + index,
                "amount": (1_000_000 + index) * raw_close,
            }
        )
    cutoff = dates[-2].date()

    evidence = build_price_history_evidence(pd.DataFrame(rows), cutoff)

    expected_raw_close = rows[-2]["close"]
    expected_adjusted_close = rows[-2]["adjusted_close"]
    assert evidence["requested_cutoff_date"] == cutoff.isoformat()
    assert evidence["cutoff_date"] == cutoff.isoformat()
    assert evidence["contains_observations_after_cutoff"] is False
    assert evidence["observations"] == 130
    assert evidence["latest"]["raw_close"] == expected_raw_close
    assert evidence["latest"]["adjusted_close"] == expected_adjusted_close
    assert evidence["latest"]["signal_close"] == expected_adjusted_close
    assert evidence["recent_raw_and_adjusted_bars_30"][-1]["date"] == cutoff.isoformat()
    assert evidence["recent_raw_and_adjusted_bars_30"][-1]["raw_close"] == expected_raw_close
    assert (
        evidence["recent_raw_and_adjusted_bars_30"][-1]["adjusted_close"] == expected_adjusted_close
    )
    path = evidence["normalized_adjusted_close_path_120"]
    assert path["observations"] == 120
    assert path["values"][-1] == 100.0
    assert evidence["moving_averages_adjusted"]["ma_120"] > expected_raw_close
    relationships = evidence["latest_signal_close_vs_moving_averages"]
    assert relationships["ma_5"]["relation"] == "above"
    assert relationships["ma_120"]["relation"] == "above"
    assert relationships["ma_5"]["latest_signal_close"] == expected_adjusted_close
    assert evidence["price_semantics"]["raw_and_adjusted_fields_are_separate"] is True


def test_price_history_evidence_returns_empty_when_cutoff_precedes_all_bars():
    frame = pd.DataFrame(
        [
            {
                "symbol": "sh600519",
                "date": "2026-01-02",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
            }
        ]
    )

    assert build_price_history_evidence(frame, date(2025, 12, 31)) == {}
