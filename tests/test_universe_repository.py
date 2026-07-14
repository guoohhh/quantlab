from datetime import date

from quantlab.persistence import AShareUniverseRepository


def test_a_share_master_versions_snapshots_and_daily_status_round_trip(tmp_path):
    repository = AShareUniverseRepository(tmp_path / "universe.db")
    records = [
        {
            "symbol": "sz000005",
            "name": "ST星源",
            "exchange": "SZ",
            "board": "main",
            "listing_date": date(1990, 12, 10),
            "delisting_date": date(2024, 4, 26),
            "status": "delisted",
            "source": "szse",
            "payload": {"fixture": True},
        }
    ]
    first = repository.save_master_build(
        as_of=date(2026, 7, 14),
        version_hash="version-1",
        records=records,
        sources=["szse"],
        audit={"records": 1},
    )
    repeated = repository.save_master_build(
        as_of=date(2026, 7, 14),
        version_hash="version-1",
        records=records,
        sources=["szse"],
        audit={"records": 1},
    )
    assert repeated == first
    assert repository.latest_master_build()["audit"]["records"] == 1
    assert repository.master_records()[0]["payload"]["fixture"] is True

    snapshot = [
        {
            "symbol": "sz000005",
            "name": "ST星源",
            "exchange": "SZ",
            "board": "main",
            "trade_status": False,
        }
    ]
    repository.save_snapshot(date(2024, 4, 25), snapshot, "baostock")
    assert repository.snapshot(date(2024, 4, 25))[0]["trade_status"] is False
    assert repository.snapshot_dates()[0]["securities"] == 1
    repository.save_snapshot(
        date(2024, 4, 25),
        [
            {
                "symbol": "sz302132",
                "source_symbol": "sz300114",
                "name": "canonicalized fixture",
                "exchange": "SZ",
                "board": "chinext",
                "trade_status": True,
            }
        ],
        "baostock",
    )
    replaced = repository.snapshot(date(2024, 4, 25))
    assert [item["symbol"] for item in replaced] == ["sz302132"]
    assert replaced[0]["source_symbol"] == "sz300114"

    repository.save_daily_status(
        [
            {
                "symbol": "sz000005",
                "trade_date": date(2024, 4, 25),
                "trade_status": False,
                "is_st": True,
                "source": "baostock",
            }
        ]
    )
    status = repository.daily_status("sz000005", date(2024, 4, 25))
    assert status["trade_status"] is False
    assert status["is_st"] is True
