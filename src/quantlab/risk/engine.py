from __future__ import annotations

from dataclasses import dataclass, field

from quantlab.domain.models import DecisionCard, Position


@dataclass
class RiskAssessment:
    approved: bool
    adjusted_weight: float
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class RiskEngine:
    def __init__(self, max_total=0.80, max_single=0.15, max_industry=0.30, drawdown_limit=0.15):
        self.max_total = max_total
        self.max_single = max_single
        self.max_industry = max_industry
        self.drawdown_limit = drawdown_limit

    def assess(
        self,
        card: DecisionCard,
        positions: dict[str, Position],
        portfolio_equity: float,
        financial_risk: dict | None = None,
        portfolio_drawdown: float = 0.0,
    ) -> RiskAssessment:
        reasons: list[str] = []
        warnings: list[str] = []
        financial_risk = financial_risk or {}
        if card.action not in {"buy", "add"}:
            return RiskAssessment(True, 0.0, ["non-increasing action does not consume new risk"])
        if card.requires_human_review:
            return RiskAssessment(False, 0.0, ["human review is required before adding risk"])
        if portfolio_equity <= 0:
            return RiskAssessment(False, 0.0, ["portfolio equity must be positive"])
        if financial_risk.get("is_st"):
            return RiskAssessment(False, 0.0, ["ST/*ST hard veto"])
        if financial_risk.get("z_score") is not None and financial_risk["z_score"] < 1.0:
            return RiskAssessment(False, 0.0, ["Altman Z-Score below 1.0"])
        if financial_risk.get("pledge_ratio", 0) > 0.50:
            return RiskAssessment(False, 0.0, ["pledge ratio above 50%"])
        if financial_risk.get("goodwill_to_equity", 0) > 0.50:
            return RiskAssessment(False, 0.0, ["goodwill/equity above 50%"])
        if portfolio_drawdown <= -abs(self.drawdown_limit):
            warnings.append("portfolio drawdown limit reached; new risk reduced by 50%")
            card_weight = card.target_weight * 0.5
        else:
            card_weight = card.target_weight
        adjusted = min(card_weight, self.max_single)
        if adjusted < card_weight:
            warnings.append("single-position cap applied")
        current_total = sum(position.market_value for position in positions.values()) / max(
            portfolio_equity, 1
        )
        available = max(0.0, self.max_total - current_total)
        adjusted = min(adjusted, available)
        industry = financial_risk.get("industry")
        if industry:
            industry_weight = sum(
                p.market_value for p in positions.values() if p.industry == industry
            ) / max(portfolio_equity, 1)
            adjusted = min(adjusted, max(0.0, self.max_industry - industry_weight))
        approved = adjusted > 0 or card.action in {"hold", "watch", "reduce", "sell"}
        if not approved:
            reasons.append("no risk budget available")
        return RiskAssessment(approved, adjusted, reasons, warnings)


def altman_z_score(financials: dict[str, float]) -> float | None:
    required = {
        "working_capital",
        "total_assets",
        "retained_earnings",
        "ebit",
        "market_cap",
        "total_liabilities",
        "revenue",
    }
    if (
        not required.issubset(financials)
        or financials["total_assets"] == 0
        or financials["total_liabilities"] == 0
    ):
        return None
    f = financials
    return (
        1.2 * f["working_capital"] / f["total_assets"]
        + 1.4 * f["retained_earnings"] / f["total_assets"]
        + 3.3 * f["ebit"] / f["total_assets"]
        + 0.6 * f["market_cap"] / f["total_liabilities"]
        + f["revenue"] / f["total_assets"]
    )
