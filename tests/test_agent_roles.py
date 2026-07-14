from quantlab.agents.roles import aggregate_council, route_roles
from quantlab.agents.schemas import ExpertOpinion


def _opinion(role, score, weight, mode="vote", veto=False):
    return ExpertOpinion(
        role=role,
        perspective=role,
        stance="bullish" if score > 0 else "bearish" if score < 0 else "neutral",
        score=score,
        confidence=1.0,
        weight=weight,
        mode=mode,
        veto=veto,
    )


def test_stock_routing_adds_investment_master_roles():
    names = {item.name for item in route_roles("stock")}
    assert {"technical", "momentum", "value_veto", "risk", "macro"} <= names
    assert {"buffett", "munger", "graham", "fisher", "lynch"} <= names


def test_etf_routing_keeps_risk_agents_but_removes_llm_hard_veto_authority():
    roles = {item.name: item for item in route_roles("etf")}

    assert set(roles) == {"technical", "momentum", "value_veto", "risk", "macro"}
    assert roles["value_veto"].mode == "strategic"
    assert roles["risk"].mode == "strategic"
    assert all(item.mode != "veto_only" for item in roles.values())


def test_council_uses_ironq_weights_and_hard_veto():
    report = aggregate_council(
        [
            _opinion("momentum", 0.8, 1.5),
            _opinion("technical", 0.6, 1.3),
            _opinion("macro", -0.2, 0.7),
            _opinion("risk", -0.8, 1.0, "veto_only", veto=True),
        ],
        "high_volatility",
    )

    assert report.tactical_score > 0
    assert report.momentum_tech_sync is True
    assert report.veto_triggered is True
    assert report.veto_roles == ["risk"]
