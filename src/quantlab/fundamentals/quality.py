from __future__ import annotations

from datetime import date
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel


class CrossValidation(BaseModel):
    metric: str
    period: int
    primary_value: float
    secondary_value: float
    relative_error: float
    status: Literal["consistent", "warning", "conflict"]
    primary_source: str = "Sina via AkShare"
    secondary_source: str = "Tonghuashun via AkShare"


class QualityCriterion(BaseModel):
    name: str
    value: float | None
    threshold: str
    status: Literal["pass", "fail", "unknown"]
    detail: str


class FinancialQualityReport(BaseModel):
    symbol: str
    as_of: date
    annual_periods: int
    latest_year: int
    metrics: dict[str, float | None]
    cross_validation: list[CrossValidation]
    criteria: list[QualityCriterion]
    quality_score: float
    hard_vetoes: list[str]
    warnings: list[str]
    sources: list[str]
    valuation: "ConservativeValuation | None" = None


class ConservativeValuation(BaseModel):
    status: Literal["measured", "partial", "unavailable"]
    current_price: float | None
    lower_value: float | None
    fair_value: float | None
    upper_value: float | None
    margin_of_safety_pct: float | None
    method_count: int
    methods: list[dict]
    assumptions: dict[str, float | str | None]
    warnings: list[str]


def load_a_share_financial_report(
    symbol: str,
    as_of: date | None = None,
    current_price: float | None = None,
):
    import akshare as ak

    code = "".join(character for character in symbol if character.isdigit())
    if len(code) != 6:
        raise ValueError("A-share financial loader requires a six-digit symbol")
    end = as_of or date.today()
    primary = ak.stock_financial_analysis_indicator(
        symbol=code, start_year=str(max(1990, end.year - 12))
    )
    secondary = ak.stock_financial_abstract_ths(symbol=code, indicator="按年度")
    cashflow = ak.stock_financial_cash_ths(symbol=code, indicator="按年度")
    return build_financial_quality_report(
        code,
        primary,
        secondary,
        cashflow,
        end,
        current_price=current_price,
    )


def build_financial_quality_report(
    symbol: str,
    primary: pd.DataFrame,
    secondary: pd.DataFrame,
    cashflow: pd.DataFrame,
    as_of: date,
    current_price: float | None = None,
) -> FinancialQualityReport:
    sina = _annual_primary(primary, as_of)
    ths = _annual_secondary(secondary, as_of)
    cash = _annual_cashflow(cashflow, as_of)
    common_years = sorted(set(sina) & set(ths))
    if not common_years:
        raise ValueError("financial sources have no common annual period")
    latest_year = common_years[-1]

    pairs = {
        "roe_pct": ("净资产收益率(%)", "净资产收益率"),
        "gross_margin_pct": ("销售毛利率(%)", "销售毛利率"),
        "net_margin_pct": ("销售净利率(%)", "销售净利率"),
        "debt_ratio_pct": ("资产负债率(%)", "资产负债率"),
        "current_ratio": ("流动比率", "流动比率"),
        "revenue_growth_pct": ("主营业务收入增长率(%)", "营业总收入同比增长率"),
        "profit_growth_pct": ("净利润增长率(%)", "净利润同比增长率"),
    }
    validations = []
    for metric, (primary_column, secondary_column) in pairs.items():
        first = _number(sina[latest_year].get(primary_column))
        second = _number(ths[latest_year].get(secondary_column))
        if first is None or second is None:
            continue
        error = abs(first - second) / max(abs(first), 1e-12)
        status = "consistent" if error <= 0.01 else "warning" if error <= 0.05 else "conflict"
        validations.append(
            CrossValidation(
                metric=metric,
                period=latest_year,
                primary_value=first,
                secondary_value=second,
                relative_error=error,
                status=status,
            )
        )

    years_10 = sorted(ths)[-10:]
    years_5 = sorted(ths)[-5:]
    avg_roe = _mean(ths[year].get("净资产收益率") for year in years_10)
    avg_gross_margin = _mean(ths[year].get("销售毛利率") for year in years_5)
    avg_net_margin = _mean(ths[year].get("销售净利率") for year in years_10)
    interest_coverage = _number(sina[latest_year].get("利息支付倍数"))
    ocf_to_profit_values = []
    free_cash_flows = []
    for year in years_5:
        net_profit = _number(ths[year].get("净利润"))
        cash_row = cash.get(year, {})
        operating_cash = _number(cash_row.get("经营活动产生的现金流量净额"))
        capex = _number(cash_row.get("购建固定资产、无形资产和其他长期资产支付的现金"))
        if operating_cash is not None and net_profit not in (None, 0):
            ocf_to_profit_values.append(operating_cash / net_profit)
        if operating_cash is not None and capex is not None:
            free_cash_flows.append(operating_cash - abs(capex))
    average_ocf_to_profit = float(np.mean(ocf_to_profit_values)) if ocf_to_profit_values else None
    cumulative_fcf = sum(free_cash_flows) if free_cash_flows else None

    criteria = [
        _criterion("average_roe_10y", avg_roe, ">= 8%", 8.0),
        _criterion("cumulative_fcf_5y", cumulative_fcf, "> 0", 0.0, strict=True),
        _criterion("interest_coverage", interest_coverage, ">= 2x", 2.0),
        _criterion("average_gross_margin_5y", avg_gross_margin, ">= 15%", 15.0),
        _criterion("average_ocf_to_profit_5y", average_ocf_to_profit, ">= 0.7", 0.7),
        _criterion("average_net_margin_10y", avg_net_margin, ">= 5%", 5.0),
        QualityCriterion(
            name="share_dilution_5y",
            value=None,
            threshold="<= 20%",
            status="unknown",
            detail="historical total-share series unavailable from both selected free sources",
        ),
    ]
    known = [item for item in criteria if item.status != "unknown"]
    quality_score = sum(item.status == "pass" for item in known) / len(known) if known else 0.0
    valuation = _build_conservative_valuation(
        ths,
        cash,
        years_5,
        current_price=current_price,
        quality_score=quality_score,
        average_roe=avg_roe,
        latest_growth=_number(ths[latest_year].get("净利润同比增长率")),
    )
    hard_vetoes = []
    if cumulative_fcf is not None and cumulative_fcf < 0:
        hard_vetoes.append("five-year cumulative free cash flow is negative")
    if interest_coverage is not None and 0 <= interest_coverage < 2:
        hard_vetoes.append("interest coverage is below 2x")
    if average_ocf_to_profit is not None and average_ocf_to_profit < 0.5:
        hard_vetoes.append("five-year operating-cash/profit ratio is below 0.5")
    warnings = []
    conflicts = [item.metric for item in validations if item.status == "conflict"]
    if conflicts:
        warnings.append(f"cross-source conflicts: {', '.join(conflicts)}")
    if any(item.status == "unknown" for item in criteria):
        warnings.append("quality screen is incomplete; unknown criteria are not treated as passes")
    warnings.extend(valuation.warnings)
    metrics = {
        "average_roe_10y_pct": avg_roe,
        "average_gross_margin_5y_pct": avg_gross_margin,
        "average_net_margin_10y_pct": avg_net_margin,
        "interest_coverage": interest_coverage,
        "average_ocf_to_profit_5y": average_ocf_to_profit,
        "cumulative_fcf_5y": cumulative_fcf,
        "latest_debt_ratio_pct": _number(ths[latest_year].get("资产负债率")),
        "latest_revenue_growth_pct": _number(ths[latest_year].get("营业总收入同比增长率")),
        "latest_profit_growth_pct": _number(ths[latest_year].get("净利润同比增长率")),
        "normalized_eps_5y": valuation.assumptions.get("normalized_eps_5y"),
        "normalized_fcf_per_share_5y": valuation.assumptions.get("normalized_fcf_per_share_5y"),
        "latest_book_value_per_share": valuation.assumptions.get("latest_book_value_per_share"),
        "annual_information_cutoff_year": float(_annual_cutoff_year(as_of)),
    }
    return FinancialQualityReport(
        symbol=symbol,
        as_of=as_of,
        annual_periods=len(ths),
        latest_year=latest_year,
        metrics=metrics,
        cross_validation=validations,
        criteria=criteria,
        quality_score=quality_score,
        hard_vetoes=hard_vetoes,
        warnings=warnings,
        sources=["Sina via AkShare", "Tonghuashun via AkShare"],
        valuation=valuation,
    )


def _build_conservative_valuation(
    annual: dict[int, dict],
    cash: dict[int, dict],
    years: list[int],
    *,
    current_price: float | None,
    quality_score: float,
    average_roe: float | None,
    latest_growth: float | None,
) -> ConservativeValuation:
    methods = []
    eps_values = [
        value
        for year in years
        if (
            value := _first_number(
                annual[year],
                "基本每股收益",
                "每股收益",
                "扣除非经常性损益后的每股收益",
            )
        )
        is not None
        and value > 0
    ]
    normalized_eps = float(np.median(eps_values)) if eps_values else None
    growth = float(np.clip(latest_growth or 0.0, -5.0, 12.0))
    justified_pe = float(np.clip(8.0 + 6.0 * quality_score + 0.25 * max(growth, 0), 8, 18))
    if normalized_eps is not None:
        fair = normalized_eps * justified_pe
        methods.append(
            {
                "method": "normalized_earnings_power",
                "lower_value": normalized_eps * max(6.0, justified_pe * 0.75),
                "fair_value": fair,
                "upper_value": normalized_eps * min(22.0, justified_pe * 1.15),
                "evidence": {
                    "normalized_eps_5y": normalized_eps,
                    "justified_pe": justified_pe,
                },
            }
        )

    latest_year = max(annual) if annual else None
    book_value = (
        _first_number(
            annual[latest_year],
            "每股净资产",
            "每股净资产_调整后",
            "归属于母公司股东的每股净资产",
        )
        if latest_year is not None
        else None
    )
    if book_value is not None and book_value > 0:
        roe = float(np.clip(average_roe or 0.0, 0.0, 30.0))
        fair_pb = float(np.clip(0.8 + roe / 15.0 * max(0.5, quality_score), 0.8, 2.5))
        methods.append(
            {
                "method": "quality_adjusted_book_value",
                "lower_value": book_value * max(0.6, fair_pb * 0.75),
                "fair_value": book_value * fair_pb,
                "upper_value": book_value * min(3.0, fair_pb * 1.20),
                "evidence": {
                    "latest_book_value_per_share": book_value,
                    "quality_adjusted_pb": fair_pb,
                },
            }
        )

    fcf_per_share_values = []
    for year in years:
        annual_row = annual.get(year, {})
        cash_row = cash.get(year, {})
        operating_cash = _number(cash_row.get("经营活动产生的现金流量净额"))
        capex = _number(cash_row.get("购建固定资产、无形资产和其他长期资产支付的现金"))
        shares = _first_number(annual_row, "总股本", "期末总股本", "总股本(股)")
        if operating_cash is None or capex is None or shares is None or shares <= 0:
            continue
        fcf_per_share = (operating_cash - abs(capex)) / shares
        if np.isfinite(fcf_per_share) and fcf_per_share > 0:
            fcf_per_share_values.append(float(fcf_per_share))
    normalized_fcf_per_share = (
        float(np.median(fcf_per_share_values)) if fcf_per_share_values else None
    )
    dcf_growth = float(np.clip(max(growth, 0.0) * 0.40 / 100, 0.0, 0.08))
    if normalized_fcf_per_share is not None:
        lower = _fcf_value(normalized_fcf_per_share, dcf_growth * 0.5, 0.14, 0.01)
        fair = _fcf_value(normalized_fcf_per_share, dcf_growth, 0.12, 0.02)
        upper = _fcf_value(
            normalized_fcf_per_share,
            min(0.10, dcf_growth * 1.25),
            0.10,
            0.025,
        )
        methods.append(
            {
                "method": "owner_earnings_dcf",
                "lower_value": lower,
                "fair_value": fair,
                "upper_value": upper,
                "evidence": {
                    "normalized_fcf_per_share_5y": normalized_fcf_per_share,
                    "forecast_growth": dcf_growth,
                    "discount_rate": 0.12,
                    "terminal_growth": 0.02,
                },
            }
        )

    lower_value = float(np.median([item["lower_value"] for item in methods])) if methods else None
    fair_value = float(np.median([item["fair_value"] for item in methods])) if methods else None
    upper_value = float(np.median([item["upper_value"] for item in methods])) if methods else None
    margin = (
        (fair_value / current_price - 1) * 100
        if fair_value is not None and current_price is not None and current_price > 0
        else None
    )
    warnings = []
    if len(methods) < 2:
        warnings.append(
            "valuation coverage is partial; fewer than two deterministic methods are available"
        )
    if current_price is None:
        warnings.append("current executable price unavailable; margin of safety was not calculated")
    elif upper_value is not None and current_price > upper_value:
        warnings.append("current price is above the conservative valuation upper bound")
    status: Literal["measured", "partial", "unavailable"] = (
        "measured" if len(methods) >= 2 else "partial" if methods else "unavailable"
    )
    return ConservativeValuation(
        status=status,
        current_price=current_price,
        lower_value=lower_value,
        fair_value=fair_value,
        upper_value=upper_value,
        margin_of_safety_pct=margin,
        method_count=len(methods),
        methods=methods,
        assumptions={
            "normalized_eps_5y": normalized_eps,
            "normalized_fcf_per_share_5y": normalized_fcf_per_share,
            "latest_book_value_per_share": book_value,
            "latest_profit_growth_pct_capped": growth,
            "quality_score": quality_score,
            "valuation_policy": "deterministic_conservative_v1",
        },
        warnings=warnings,
    )


def _fcf_value(fcf_per_share: float, growth: float, discount: float, terminal: float) -> float:
    present_value = 0.0
    cashflow = fcf_per_share
    for year in range(1, 6):
        cashflow *= 1 + growth
        present_value += cashflow / ((1 + discount) ** year)
    terminal_value = cashflow * (1 + terminal) / max(1e-6, discount - terminal)
    return float(present_value + terminal_value / ((1 + discount) ** 5))


def _first_number(row: dict, *keys: str) -> float | None:
    for key in keys:
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def _annual_primary(frame: pd.DataFrame, as_of: date) -> dict[int, dict]:
    output = {}
    cutoff_year = _annual_cutoff_year(as_of)
    for row in frame.to_dict("records"):
        observed = pd.Timestamp(row.get("日期"))
        if pd.isna(observed) or observed.year > cutoff_year or observed.month != 12:
            continue
        output[observed.year] = row
    return output


def _annual_secondary(frame: pd.DataFrame, as_of: date) -> dict[int, dict]:
    output = {}
    cutoff_year = _annual_cutoff_year(as_of)
    for row in frame.to_dict("records"):
        try:
            year = int(row.get("报告期"))
        except (TypeError, ValueError):
            continue
        if year <= cutoff_year:
            output[year] = row
    return output


def _annual_cashflow(frame: pd.DataFrame, as_of: date) -> dict[int, dict]:
    return _annual_secondary(frame, as_of)


def _annual_cutoff_year(as_of: date) -> int:
    """Assume annual reports become usable from May of the following year."""

    return as_of.year - 1 if as_of.month >= 5 else as_of.year - 2


def _number(value) -> float | None:
    if value is None or value is False or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, (int, float, np.number)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"False", "--", "nan"}:
        return None
    multiplier = 1.0
    if text.endswith("%"):
        text = text[:-1]
    elif text.endswith("亿"):
        multiplier = 100_000_000.0
        text = text[:-1]
    elif text.endswith("万"):
        multiplier = 10_000.0
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def _mean(values) -> float | None:
    parsed = [number for value in values if (number := _number(value)) is not None]
    return float(np.mean(parsed)) if parsed else None


def _criterion(
    name: str, value: float | None, threshold: str, cutoff: float, strict: bool = False
) -> QualityCriterion:
    if value is None:
        return QualityCriterion(
            name=name,
            value=None,
            threshold=threshold,
            status="unknown",
            detail="required data unavailable",
        )
    passed = value > cutoff if strict else value >= cutoff
    return QualityCriterion(
        name=name,
        value=value,
        threshold=threshold,
        status="pass" if passed else "fail",
        detail=f"observed={value:.4g}; required {threshold}",
    )
