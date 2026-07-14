from datetime import date

import pandas as pd

from quantlab.config import Settings
from quantlab.data import PointInTimeSecurity
from quantlab.persistence import AShareUniverseRepository
from quantlab.workflows.universe import (
    _board,
    _is_supported_a_share,
    capture_point_in_time_universe,
    refresh_a_share_security_master,
    select_stratified_point_in_time_sample,
)


def test_universe_supports_the_szse_302_code_change_segment():
    assert _is_supported_a_share("SZ", "302132") is True
    assert _board("sz302132") == "chinext"


def _settings(tmp_path):
    return Settings(values={"system": {"database_path": "universe.db"}}, root=tmp_path)


def test_exchange_master_merges_active_and_delisted_without_reusing_future_state(tmp_path):
    frames = {
        "sse_main_active": pd.DataFrame(
            [{"证券代码": "600000", "证券简称": "浦发银行", "上市日期": "1999-11-10"}]
        ),
        "sse_star_active": pd.DataFrame(
            [{"证券代码": "688001", "证券简称": "华兴源创", "上市日期": "2019-07-22"}]
        ),
        "szse_active": pd.DataFrame(
            [
                {
                    "A股代码": "300750",
                    "A股简称": "宁德时代",
                    "A股上市日期": "2018-06-11",
                }
            ]
        ),
        "sse_delisted": pd.DataFrame(
            [
                {
                    "公司代码": "600001",
                    "公司简称": "邯郸钢铁",
                    "上市日期": "1998-01-22",
                    "暂停上市日期": "2009-12-29",
                }
            ]
        ),
        "szse_delisted": pd.DataFrame(
            [
                {
                    "证券代码": "000005",
                    "证券简称": "ST星源",
                    "上市日期": "1990-12-10",
                    "终止上市日期": "2024-04-26",
                }
            ]
        ),
    }
    output = refresh_a_share_security_master(_settings(tmp_path), date(2026, 7, 14), frames=frames)

    assert output["audit"]["records"] == 5
    assert output["audit"]["active"] == 3
    assert output["audit"]["delisted"] == 2
    assert output["audit"]["boards"]["star"] == 1


class FakeUniverseProvider:
    name = "fixture"

    def point_in_time_universe(self, day):
        return [
            PointInTimeSecurity("sh600000", "浦发银行", "SH", "main", True),
            PointInTimeSecurity("sh688001", "华兴源创", "SH", "star", True),
            PointInTimeSecurity("sz000005", "ST星源", "SZ", "main", False),
            PointInTimeSecurity("sz300750", "宁德时代", "SZ", "chinext", True),
        ]


def test_point_in_time_snapshot_is_cached_and_cross_validated(tmp_path):
    settings = _settings(tmp_path)
    refresh_a_share_security_master(
        settings,
        frames={
            "sse_main_active": pd.DataFrame(
                [{"证券代码": "600000", "证券简称": "浦发银行", "上市日期": "1999-11-10"}]
            ),
            "sse_star_active": pd.DataFrame(
                [{"证券代码": "688001", "证券简称": "华兴源创", "上市日期": "2019-07-22"}]
            ),
            "szse_active": pd.DataFrame(
                [
                    {
                        "A股代码": "300750",
                        "A股简称": "宁德时代",
                        "A股上市日期": "2018-06-11",
                    }
                ]
            ),
        },
    )
    first = capture_point_in_time_universe(
        settings, date(2024, 4, 25), provider=FakeUniverseProvider()
    )
    second = capture_point_in_time_universe(
        settings, date(2024, 4, 25), provider=FakeUniverseProvider()
    )

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert first["securities"] == 4
    assert first["cross_validation"]["overlap"] == 3


def test_point_in_time_snapshot_retries_incomplete_market_response(tmp_path):
    settings = _settings(tmp_path)
    AShareUniverseRepository(tmp_path / "universe.db").save_master_build(
        as_of=date(2024, 4, 25),
        version_hash="fixture-complete-master",
        records=[
            {
                "symbol": f"sh60000{index}",
                "name": f"sample-{index}",
                "exchange": "SH",
                "board": "main",
                "listing_date": "2010-01-01",
                "delisting_date": None,
                "status": "active",
                "source": "fixture",
                "payload": {},
            }
            for index in range(4)
        ],
        sources=["fixture"],
        audit={},
    )

    class FlakyUniverseProvider:
        name = "fixture"

        def __init__(self):
            self.calls = 0

        def point_in_time_universe(self, day):
            self.calls += 1
            count = 3 if self.calls == 1 else 4
            return [
                PointInTimeSecurity(
                    f"sh60000{index}", f"sample-{index}", "SH", "main", True
                )
                for index in range(count)
            ]

    provider = FlakyUniverseProvider()
    output = capture_point_in_time_universe(
        settings, date(2024, 4, 25), provider=provider
    )

    assert provider.calls == 2
    assert output["capture_attempts"] == 2
    assert output["cross_validation"]["jaccard"] == 1.0


def test_stratified_sample_excludes_st_suspended_and_is_reproducible():
    records = []
    for index in range(8):
        records.extend(
            [
                {
                    "symbol": f"sh6000{index:02d}",
                    "name": f"沪主{index}",
                    "exchange": "SH",
                    "board": "main",
                    "trade_status": True,
                },
                {
                    "symbol": f"sh6880{index:02d}",
                    "name": f"科创{index}",
                    "exchange": "SH",
                    "board": "star",
                    "trade_status": True,
                },
                {
                    "symbol": f"sz0000{index:02d}",
                    "name": f"深主{index}",
                    "exchange": "SZ",
                    "board": "main",
                    "trade_status": True,
                },
                {
                    "symbol": f"sz3000{index:02d}",
                    "name": f"创业{index}",
                    "exchange": "SZ",
                    "board": "chinext",
                    "trade_status": True,
                },
            ]
        )
    records[0]["name"] = "*ST样本"
    records[1]["trade_status"] = False
    first = select_stratified_point_in_time_sample(
        records, 12, seed="protocol-v1", snapshot_date=date(2024, 6, 3)
    )
    second = select_stratified_point_in_time_sample(
        records, 12, seed="protocol-v1", snapshot_date=date(2024, 6, 3)
    )

    assert first == second
    assert first["sample_size"] == 12
    assert set(first["strata"]) == {"SH_main", "SZ_main", "star", "chinext"}
    assert all("ST" not in item["name"] for item in first["records"])
