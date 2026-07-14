import sys
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from quantlab.data.akshare import AkShareProvider, _trade_flags
from quantlab.data.base import ProviderError


def _frame(multiplier: float):
    return pd.DataFrame(
        [
            {
                "日期": "2026-01-02",
                "开盘": 10.0 * multiplier,
                "最高": 12.0 * multiplier,
                "最低": 9.0 * multiplier,
                "收盘": 11.0 * multiplier,
                "成交量": 1_000,
                "成交额": 10_000,
            }
        ]
    )


@pytest.mark.parametrize(
    ("symbol", "function_name"),
    [("sh510300", "fund_etf_hist_em"), ("sh600000", "stock_zh_a_hist")],
)
def test_akshare_separates_raw_execution_prices_from_adjusted_signal_prices(
    monkeypatch, symbol, function_name
):
    calls = []

    def history(kind):
        def fetch(**kwargs):
            calls.append({"kind": kind, **kwargs})
            return _frame(10.0 if kwargs["adjust"] == "hfq" else 1.0)

        return fetch

    fake_akshare = SimpleNamespace(
        fund_etf_hist_em=history("fund_etf_hist_em"),
        stock_zh_a_hist=history("stock_zh_a_hist"),
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)

    bars = AkShareProvider().bars([symbol], date(2026, 1, 1), date(2026, 1, 3))

    assert len(bars) == 1
    assert bars[0].close == 11.0
    assert bars[0].adjusted_close == 110.0
    assert bars[0].signal_close == 110.0
    assert bars[0].available_at.hour == 15
    assert [item["adjust"] for item in calls] == ["", "hfq"]
    assert {item["kind"] for item in calls} == {function_name}


def test_akshare_failure_is_wrapped_as_provider_error(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(fund_etf_hist_em=fail, stock_zh_a_hist=fail),
    )

    with pytest.raises(ProviderError, match="akshare failed for sh510300"):
        AkShareProvider().bars(["sh510300"], date(2026, 1, 1), date(2026, 1, 3))


def test_akshare_retries_transient_connection_failure(monkeypatch):
    calls = 0

    def flaky(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("remote disconnected")
        return _frame(10.0 if kwargs["adjust"] == "hfq" else 1.0)

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(fund_etf_hist_em=flaky, stock_zh_a_hist=flaky),
    )

    bars = AkShareProvider().bars(["sh510300"], date(2026, 1, 1), date(2026, 1, 3))

    assert len(bars) == 1
    assert calls == 3


def test_a_share_one_price_limit_and_suspension_flags_are_deterministic():
    assert _trade_flags("600000", 10.0, 10.0, 10.0, 10.0, 0.0, 9.0) == (
        True,
        False,
        False,
    )
    assert _trade_flags("600000", 11.0, 11.0, 11.0, 11.0, 1_000.0, 10.0) == (
        False,
        True,
        False,
    )
    assert _trade_flags("600000", 9.0, 9.0, 9.0, 9.0, 1_000.0, 10.0) == (
        False,
        False,
        True,
    )
    assert _trade_flags("300001", 11.0, 12.0, 10.5, 11.0, 1_000.0, 10.0) == (
        False,
        False,
        False,
    )
