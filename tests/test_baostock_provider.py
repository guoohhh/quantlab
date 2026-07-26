from datetime import date
import socket
from types import SimpleNamespace

import pytest

from quantlab.data.baostock import BaoStockProvider, _from_baostock_code, _limit_flags
from quantlab.data.base import ProviderError


class FakeResult:
    def __init__(self, fields, rows, error_code="0", error_msg="success"):
        self.fields = fields
        self.rows = rows
        self.error_code = error_code
        self.error_msg = error_msg
        self.index = -1

    def next(self):
        self.index += 1
        return self.index < len(self.rows)

    def get_row_data(self):
        return self.rows[self.index]


class FakeBaoStock:
    def __init__(self, login_code="0"):
        self.login_code = login_code
        self.logout_calls = 0

    def login(self):
        return SimpleNamespace(error_code=self.login_code, error_msg="login error")

    def logout(self):
        self.logout_calls += 1

    def query_all_stock(self, day):
        assert day == "2024-04-25"
        return FakeResult(
            ["code", "tradeStatus", "code_name"],
            [
                ["sh.000001", "1", "上证指数"],
                ["sh.600000", "1", "浦发银行"],
                ["sh.688001", "0", "华兴源创"],
                ["sz.000005", "1", "ST星源"],
                ["sz.159915", "1", "创业板ETF"],
                ["sz.300750", "1", "宁德时代"],
            ],
        )

    def query_trade_dates(self, start_date, end_date):
        assert start_date == "2024-04-24"
        assert end_date == "2024-04-26"
        return FakeResult(
            ["calendar_date", "is_trading_day"],
            [
                ["2024-04-24", "1"],
                ["2024-04-25", "1"],
                ["2024-04-26", "0"],
            ],
        )

    def query_stock_basic(self):
        return FakeResult(
            ["code", "code_name", "ipoDate", "outDate", "type", "status"],
            [
                ["sh.600000", "浦发银行", "1999-11-10", "", "1", "1"],
                ["sz.300750", "宁德时代", "2018-06-11", "", "1", "1"],
                ["sh.000001", "上证指数", "", "", "2", "1"],
            ],
        )

    def query_stock_industry(self):
        return FakeResult(
            [
                "code",
                "code_name",
                "industry",
                "industryClassification",
                "updateDate",
            ],
            [
                ["sh.600000", "浦发银行", "银行", "申万一级", "2024-01-02"],
                ["sh.600000", "浦发银行", "金融", "旧分类", "2023-01-02"],
                ["sz.300114", "中航电测", "软件服务", "", "2024-02-01"],
                ["sh.000001", "上证指数", "指数", "申万一级", "2024-01-02"],
                ["sh.688001", "华兴源创", "", "申万一级", "2024-01-02"],
                ["sz.300750", "宁德时代", "电池", "申万一级", "2025-01-02"],
            ],
        )

    def query_history_k_data_plus(self, code, fields, start_date, end_date, frequency, adjustflag):
        assert code == "sz.000005"
        assert frequency == "d"
        if adjustflag == "1":
            return FakeResult(
                fields.split(","),
                [
                    ["2024-04-24", "20", "20", "20", "20"],
                    ["2024-04-25", "21", "21", "21", "21"],
                    ["2024-04-26", "21", "21", "21", "21"],
                ],
            )
        return FakeResult(
            fields.split(","),
            [
                [
                    "2024-04-24",
                    "sz.000005",
                    "10",
                    "10",
                    "10",
                    "10",
                    "10",
                    "1000",
                    "10000",
                    "1",
                    "0",
                    "0",
                ],
                [
                    "2024-04-25",
                    "sz.000005",
                    "10.5",
                    "10.5",
                    "10.5",
                    "10.5",
                    "10",
                    "1000",
                    "10500",
                    "1",
                    "5",
                    "1",
                ],
                [
                    "2024-04-26",
                    "sz.000005",
                    "10.5",
                    "10.5",
                    "10.5",
                    "10.5",
                    "10.5",
                    "",
                    "",
                    "0",
                    "",
                    "1",
                ],
            ],
        )


def test_baostock_point_in_time_universe_filters_non_a_share_codes():
    client = FakeBaoStock()
    output = BaoStockProvider(client).point_in_time_universe(date(2024, 4, 25))

    assert [item.symbol for item in output] == [
        "sh600000",
        "sh688001",
        "sz000005",
        "sz300750",
    ]
    assert output[1].board == "star"
    assert output[1].trade_status is False
    assert output[-1].board == "chinext"
    assert client.logout_calls == 1


def test_baostock_explicit_calendar_and_security_master():
    client = FakeBaoStock()
    provider = BaoStockProvider(client)

    calendar = provider.trading_calendar(date(2024, 4, 24), date(2024, 4, 26))
    master = provider.security_master_records()

    assert calendar[-1] == {"trade_date": "2024-04-26", "is_open": False}
    assert [item["symbol"] for item in master] == ["sh600000", "sz300750"]
    assert master[0]["listing_date"] == "1999-11-10"
    assert master[0]["delisting_date"] is None
    assert client.logout_calls == 2


def test_baostock_industry_membership_is_point_in_time_and_keeps_latest_record():
    client = FakeBaoStock()
    provider = BaoStockProvider(client)

    records = provider.industry_records(as_of=date(2024, 12, 31))

    assert records == [
        {
            "symbol": "sh600000",
            "name": "浦发银行",
            "industry": "银行",
            "classification": "申万一级",
            "effective_date": "2024-01-02",
            "source_symbol": "sh600000",
        },
        {
            "symbol": "sz302132",
            "name": "中航电测",
            "industry": "软件服务",
            "classification": "unknown",
            "effective_date": "2024-02-01",
            "source_symbol": "sz300114",
        },
    ]
    assert provider.instruments() == []
    assert client.logout_calls == 1


def test_baostock_calendar_rejects_reversed_range():
    with pytest.raises(ValueError, match="start date"):
        BaoStockProvider(FakeBaoStock()).trading_calendar(
            date(2024, 4, 26), date(2024, 4, 24)
        )


def test_baostock_history_preserves_raw_adjusted_st_and_suspension_state():
    client = FakeBaoStock()
    bars = BaoStockProvider(client).bars(["sz000005"], date(2024, 4, 24), date(2024, 4, 26))

    assert len(bars) == 3
    assert bars[0].close == 10.0
    assert bars[0].adjusted_close == 20.0
    assert bars[1].is_st is True
    assert bars[1].limit_up is True
    assert bars[2].suspended is True
    assert bars[2].limit_up is False
    assert client.logout_calls == 1


def test_baostock_history_cache_is_atomic_and_reusable(tmp_path):
    client = FakeBaoStock()
    provider = BaoStockProvider(client, cache_dir=tmp_path)
    first = provider.bars(["sz000005"], date(2024, 4, 24), date(2024, 4, 26))
    client.query_history_k_data_plus = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("cache hit must not query BaoStock")
    )
    second = provider.bars(["sz000005"], date(2024, 4, 24), date(2024, 4, 26))

    assert second == first
    assert len(list(tmp_path.glob("sz000005-*.json"))) == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_baostock_history_reuses_a_covering_cache_range(tmp_path):
    client = FakeBaoStock()
    provider = BaoStockProvider(client, cache_dir=tmp_path)
    provider.bars(["sz000005"], date(2024, 4, 24), date(2024, 4, 26))
    client.query_history_k_data_plus = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("covering cache hit must not query BaoStock")
    )

    subset = provider.bars(["sz000005"], date(2024, 4, 25), date(2024, 4, 26))

    assert [item.date for item in subset] == [date(2024, 4, 25), date(2024, 4, 26)]


def test_baostock_cached_bars_merges_fragments_without_network(tmp_path):
    client = FakeBaoStock()
    provider = BaoStockProvider(client, cache_dir=tmp_path)
    expected = provider.bars(["sz000005"], date(2024, 4, 24), date(2024, 4, 26))
    client.query_history_k_data_plus = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("cached_bars must not query BaoStock")
    )

    assert provider.cached_bars(["sz000005"]) == expected


def test_baostock_history_retries_one_symbol_after_transient_network_error():
    class FlakyBaoStock(FakeBaoStock):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def query_history_k_data_plus(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return FakeResult([], [], error_code="10054", error_msg="network reset")
            return super().query_history_k_data_plus(*args, **kwargs)

    client = FlakyBaoStock()
    bars = BaoStockProvider(client, max_retries=2).bars(
        ["sz000005"], date(2024, 4, 24), date(2024, 4, 26)
    )

    assert len(bars) == 3
    assert client.logout_calls == 2


def test_baostock_session_applies_and_restores_a_hard_socket_timeout():
    original_timeout = socket.getdefaulttimeout()

    class TimeoutAwareBaoStock(FakeBaoStock):
        def login(self):
            assert socket.getdefaulttimeout() == 7.5
            return super().login()

        def logout(self):
            assert socket.getdefaulttimeout() == 7.5
            super().logout()

    client = TimeoutAwareBaoStock()
    BaoStockProvider(client, request_timeout_seconds=7.5).point_in_time_universe(
        date(2024, 4, 25)
    )

    assert socket.getdefaulttimeout() == original_timeout


def test_baostock_rejects_non_positive_socket_timeout():
    with pytest.raises(ValueError, match="request_timeout_seconds must be positive"):
        BaoStockProvider(FakeBaoStock(), request_timeout_seconds=0)


def test_baostock_login_and_query_errors_are_explicit():
    with pytest.raises(ProviderError, match="login failed"):
        BaoStockProvider(FakeBaoStock(login_code="1")).point_in_time_universe(date(2024, 4, 25))

    client = FakeBaoStock()
    client.query_all_stock = lambda day: FakeResult([], [], error_code="1", error_msg="bad query")
    with pytest.raises(ProviderError, match="bad query"):
        BaoStockProvider(client).point_in_time_universe(date(2024, 4, 25))
    assert client.logout_calls == 1


def test_baostock_symbol_and_limit_helpers_cover_market_rules():
    assert _from_baostock_code("sh.600000") == "sh600000"
    assert _from_baostock_code("sz.300114") == "sz302132"
    assert _from_baostock_code("sz.300114", canonicalize=False) == "sz300114"
    assert _from_baostock_code("sz.302132") == "sz302132"
    assert _from_baostock_code("sz.159915") is None
    assert _limit_flags("sz300750", 12, 12, 12, 12, 10, False, True) == (True, False)
    assert _limit_flags("sz302132", 12, 12, 12, 12, 10, False, True) == (True, False)
    assert _limit_flags("sh600000", 9, 9, 9, 9, 10, False, True) == (False, True)
    assert _limit_flags("sh600000", 10, 10, 10, 10, None, False, True) == (
        False,
        False,
    )
