from datetime import date

import pandas as pd

from quantlab.fundamentals import build_financial_quality_report


def _frames(negative_fcf=False):
    primary_rows = []
    secondary_rows = []
    cash_rows = []
    for year in range(2016, 2026):
        primary_rows.append(
            {
                "日期": f"{year}-12-31",
                "净资产收益率(%)": 20.0,
                "销售毛利率(%)": 40.0,
                "销售净利率(%)": 12.0,
                "资产负债率(%)": 25.0,
                "流动比率": 2.0,
                "主营业务收入增长率(%)": 10.0,
                "净利润增长率(%)": 10.0,
                "利息支付倍数": 8.0,
            }
        )
        secondary_rows.append(
            {
                "报告期": year,
                "净资产收益率": "20.00%",
                "销售毛利率": "40.00%",
                "销售净利率": "12.00%",
                "资产负债率": "25.00%",
                "流动比率": "2.00",
                "营业总收入同比增长率": "10.00%",
                "净利润同比增长率": "10.00%",
                "净利润": "10亿",
                "基本每股收益": "2.00",
                "每股净资产": "10.00",
                "总股本": "10亿",
            }
        )
        cash_rows.append(
            {
                "报告期": year,
                "经营活动产生的现金流量净额": "15亿" if not negative_fcf else "1亿",
                "购建固定资产、无形资产和其他长期资产支付的现金": "3亿",
            }
        )
    return (
        pd.DataFrame(primary_rows),
        pd.DataFrame(secondary_rows),
        pd.DataFrame(cash_rows),
    )


def test_financial_quality_report_cross_validates_and_scores():
    report = build_financial_quality_report("600519", *_frames(), as_of=date(2026, 7, 1))

    assert report.latest_year == 2025
    assert report.quality_score == 1.0
    assert report.hard_vetoes == []
    assert all(item.status == "consistent" for item in report.cross_validation)
    assert any(
        item.name == "share_dilution_5y" and item.status == "unknown" for item in report.criteria
    )


def test_negative_five_year_fcf_triggers_deterministic_veto():
    report = build_financial_quality_report(
        "600519", *_frames(negative_fcf=True), as_of=date(2026, 7, 1)
    )

    assert "five-year cumulative free cash flow is negative" in report.hard_vetoes


def test_conservative_valuation_combines_earnings_cashflow_and_book_value():
    report = build_financial_quality_report(
        "600519",
        *_frames(),
        as_of=date(2026, 7, 1),
        current_price=12.0,
    )

    valuation = report.valuation
    assert valuation.status == "measured"
    assert valuation.method_count == 3
    assert valuation.lower_value <= valuation.fair_value <= valuation.upper_value
    assert valuation.margin_of_safety_pct > 0
    assert valuation.assumptions["valuation_policy"] == "deterministic_conservative_v1"


def test_annual_report_cutoff_does_not_use_unpublished_year_end_data():
    report = build_financial_quality_report(
        "600519",
        *_frames(),
        as_of=date(2026, 1, 15),
    )

    assert report.latest_year == 2024
    assert report.metrics["annual_information_cutoff_year"] == 2024.0
