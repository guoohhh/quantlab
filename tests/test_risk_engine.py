from datetime import date

import pytest

from quantlab.domain.models import DecisionCard, Position
from quantlab.risk.engine import RiskEngine, altman_z_score


def _card(action="buy", target_weight=0.20, requires_human_review=False):
    return DecisionCard(
        symbol="sh600000",
        as_of=date(2026, 7, 13),
        action=action,
        confidence=0.7,
        target_weight=target_weight,
        requires_human_review=requires_human_review,
    )


@pytest.mark.parametrize(
    ("financial_risk", "reason"),
    [
        ({"is_st": True}, "ST/*ST hard veto"),
        ({"z_score": 0.9}, "Altman Z-Score below 1.0"),
        ({"pledge_ratio": 0.51}, "pledge ratio above 50%"),
        ({"goodwill_to_equity": 0.51}, "goodwill/equity above 50%"),
    ],
)
def test_financial_hard_vetoes_block_new_risk(financial_risk, reason):
    result = RiskEngine().assess(_card(), {}, 100_000, financial_risk)

    assert result.approved is False
    assert result.adjusted_weight == 0.0
    assert result.reasons == [reason]


def test_risk_engine_applies_drawdown_single_total_and_industry_caps():
    positions = {
        "existing-tech": Position(
            symbol="existing-tech",
            quantity=2_500,
            market_price=10,
            industry="technology",
        ),
        "existing-other": Position(
            symbol="existing-other",
            quantity=3_500,
            market_price=10,
            industry="consumer",
        ),
    }

    result = RiskEngine(max_total=0.80, max_single=0.15, max_industry=0.30).assess(
        _card(target_weight=0.50),
        positions,
        100_000,
        {"industry": "technology"},
        portfolio_drawdown=-0.20,
    )

    assert result.approved is True
    assert result.adjusted_weight == pytest.approx(0.05)
    assert "portfolio drawdown limit reached" in result.warnings[0]
    assert "single-position cap applied" in result.warnings[1]


def test_non_increasing_action_uses_no_new_risk_budget():
    result = RiskEngine().assess(_card(action="watch", target_weight=0.10), {}, 100_000)

    assert result.approved is True
    assert result.adjusted_weight == 0.0


def test_human_review_and_invalid_equity_block_new_risk():
    engine = RiskEngine()

    review = engine.assess(_card(requires_human_review=True), {}, 100_000)
    invalid_equity = engine.assess(_card(), {}, 0)

    assert review.approved is False
    assert "human review" in review.reasons[0]
    assert invalid_equity.approved is False
    assert "positive" in invalid_equity.reasons[0]


def test_altman_z_score_requires_complete_nonzero_inputs():
    financials = {
        "working_capital": 10,
        "total_assets": 100,
        "retained_earnings": 20,
        "ebit": 8,
        "market_cap": 120,
        "total_liabilities": 50,
        "revenue": 90,
    }

    assert altman_z_score(financials) == pytest.approx(3.004)
    assert altman_z_score({"total_assets": 100}) is None
    assert altman_z_score({**financials, "total_liabilities": 0}) is None
