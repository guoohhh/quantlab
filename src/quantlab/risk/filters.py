from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from quantlab.domain.models import AssetType


@dataclass(frozen=True)
class InstrumentRisk:
    blocked: bool = False
    review_required: bool = False
    hard_vetoes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)


def assess_instrument_risk(
    asset_type: AssetType | str,
    metadata: dict[str, Any] | None,
    as_of: date,
) -> InstrumentRisk:
    """Apply deterministic pre-trade gates before a signal can become an order."""

    meta = metadata or {}
    kind = AssetType(asset_type)
    vetoes: list[str] = []
    warnings: list[str] = []
    checks: list[str] = []

    if bool(meta.get("market_data_freshness_required")):
        market_data_as_of = _date(meta.get("market_data_as_of") or meta.get("as_of"))
        maximum_age = max(0, int(meta.get("maximum_market_data_age_business_days", 1)))
        if market_data_as_of is None:
            vetoes.append("market data date is unavailable for a tradeable signal")
        else:
            supplied_age = meta.get("market_data_business_day_age")
            age = (
                max(0, int(supplied_age))
                if supplied_age is not None
                else _business_day_age(market_data_as_of, as_of)
            )
            checks.append(
                f"market_data_as_of={market_data_as_of.isoformat()},business_day_age={age}"
            )
            if age > maximum_age:
                vetoes.append(
                    "market data is stale for order generation "
                    f"({age} business days old; maximum {maximum_age})"
                )

    name = str(meta.get("name") or "").upper()
    if bool(meta.get("is_st")) or name.startswith(("ST", "*ST", "SST")):
        vetoes.append("ST/*ST securities are excluded from new positions")
    if bool(meta.get("delisting_risk")) or bool(meta.get("is_delisting")):
        vetoes.append("delisting risk hard veto")
    if bool(meta.get("suspended")):
        vetoes.append("security is suspended")
    if bool(meta.get("limit_up")):
        vetoes.append("cannot plan a buy while the security is limit-up")
    if bool(meta.get("regulatory_investigation")):
        vetoes.append("active regulatory investigation")

    pledge_ratio = _ratio(meta.get("pledge_ratio"))
    if pledge_ratio is not None:
        checks.append(f"pledge_ratio={pledge_ratio:.2%}")
        if pledge_ratio >= 0.50:
            vetoes.append("share pledge ratio is at or above 50%")
        elif pledge_ratio >= 0.30:
            warnings.append("share pledge ratio is between 30% and 50%")

    risk_levels = {str(item).lower() for item in meta.get("risk_levels", []) if item is not None}
    if "high" in risk_levels:
        warnings.append("upstream risk feed contains a high-risk event")

    unlock_level = str(meta.get("unlock_risk_level") or "").lower()
    if unlock_level == "high":
        vetoes.append("near-term restricted-share unlock is classified high risk")
    elif unlock_level == "medium":
        warnings.append("near-term restricted-share unlock requires attention")

    lawsuit_level = str(meta.get("lawsuit_risk_level") or "").lower()
    if lawsuit_level == "high":
        vetoes.append("material lawsuit/arbitration risk")
    elif lawsuit_level == "medium":
        warnings.append("lawsuit/arbitration risk is classified medium")

    if kind == AssetType.STOCK:
        listing_days = _number(meta.get("listing_days"))
        if listing_days is not None and listing_days < 180:
            vetoes.append("listed for fewer than 180 days")
        financial_vetoes = [str(item) for item in meta.get("financial_hard_vetoes", [])]
        vetoes.extend(financial_vetoes)
        financial_score = _number(meta.get("financial_quality_score"))
        if financial_score is not None:
            checks.append(f"financial_quality_score={financial_score:.2f}")
            if financial_score < 0.33 and not financial_vetoes:
                vetoes.append("financial quality score is below 0.33")
            elif financial_score < 0.50:
                warnings.append("financial quality score is below 0.50")
        warnings.extend(str(item) for item in meta.get("financial_warnings", []))
        risk_complete = bool(meta.get("risk_check_complete"))
        financial_complete = bool(meta.get("financial_check_complete"))
        if not risk_complete:
            warnings.append("ST, pledge, unlock and lawsuit checks are incomplete")
        if not financial_complete:
            warnings.append("cross-source financial quality checks are incomplete")
        return InstrumentRisk(
            blocked=bool(vetoes),
            review_required=not (risk_complete and financial_complete),
            hard_vetoes=vetoes,
            warnings=warnings,
            checks=checks,
        )

    if kind == AssetType.CONVERTIBLE_BOND:
        rating = _rating(meta.get("rating"))
        if rating is None:
            warnings.append("credit rating is unavailable")
        else:
            checks.append(f"rating={rating}")
            if _rating_rank(rating) < _rating_rank("AA-"):
                vetoes.append("credit rating is below AA-")

        maturity = _date(meta.get("maturity_date"))
        if maturity is None:
            warnings.append("maturity date is unavailable")
        else:
            days_to_maturity = (maturity - as_of).days
            checks.append(f"days_to_maturity={days_to_maturity}")
            if days_to_maturity <= 180:
                vetoes.append("convertible bond matures within 180 days")
            elif days_to_maturity <= 365:
                warnings.append("convertible bond matures within one year")

        remaining_size = _number(meta.get("remaining_size"))
        if remaining_size is None:
            warnings.append("remaining issue size is unavailable")
        elif remaining_size < 100_000_000:
            vetoes.append("remaining issue size is below CNY 100 million")

        redeem_risk = meta.get("redeem_risk")
        if redeem_risk is None:
            warnings.append("strong-redemption status is unavailable")
        elif bool(redeem_risk):
            vetoes.append("strong-redemption trigger risk")

        outlook = str(meta.get("rating_outlook") or "").lower()
        if outlook in {"negative", "负面"}:
            warnings.append("credit-rating outlook is negative")

        critical_missing = any(
            value is None for value in (rating, maturity, remaining_size, redeem_risk)
        )
        return InstrumentRisk(
            blocked=bool(vetoes),
            review_required=critical_missing,
            hard_vetoes=vetoes,
            warnings=warnings,
            checks=checks,
        )

    return InstrumentRisk(
        blocked=bool(vetoes),
        hard_vetoes=vetoes,
        warnings=warnings,
        checks=checks,
    )


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    multiplier = 1.0
    if "亿元" in text:
        multiplier = 100_000_000.0
    elif "万元" in text:
        multiplier = 10_000.0
    text = text.replace("亿元", "").replace("万元", "").replace("元", "")
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def _ratio(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str) and "%" in value:
        number = _number(value.replace("%", ""))
        return number / 100 if number is not None else None
    number = _number(value)
    if number is None:
        return None
    return number / 100 if number > 1 else number


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _business_day_age(observed: date, as_of: date) -> int:
    """Conservative non-formal fallback; formal flows inject exchange-calendar age."""

    return max(0, (as_of - observed).days)


def _rating(value: Any) -> str | None:
    if not value:
        return None
    rating = str(value).strip().upper().replace("（", "(")
    return rating.split("(", 1)[0].strip() or None


def _rating_rank(rating: str) -> int:
    ranks = {
        "D": 0,
        "C": 1,
        "CC": 2,
        "CCC": 3,
        "B-": 4,
        "B": 5,
        "B+": 6,
        "BB-": 7,
        "BB": 8,
        "BB+": 9,
        "BBB-": 10,
        "BBB": 11,
        "BBB+": 12,
        "A-": 13,
        "A": 14,
        "A+": 15,
        "AA-": 16,
        "AA": 17,
        "AA+": 18,
        "AAA": 19,
    }
    return ranks.get(rating, -1)
