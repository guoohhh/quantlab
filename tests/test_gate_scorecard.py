from quantlab.workflows.gate_scorecard import (
    render_decision_gate_scorecard_markdown,
    summarize_decision_gate_replays,
)


def _trade(result: float, traded: bool = True) -> dict:
    return {"traded": traded, "net_return": result}


def _row(index: int, result: float, tier: str = "strategy_primary") -> dict:
    outcome = "up" if result > 0 else "down"
    return {
        "actual_as_of": f"2026-0{index + 1}-02",
        "outcome_date": f"2026-0{index + 1}-27",
        "outcome": outcome,
        "strategy_trade": _trade(result),
        "full_system_trade": _trade(0.0, False),
        "council": {"veto_triggered": False},
        "reviewer": {"approved": True},
        "forecast": {
            "up_probability": 0.50,
            "flat_probability": 0.20,
            "down_probability": 0.30,
            "raw_llm_up_probability": 0.55,
            "raw_llm_flat_probability": 0.20,
            "raw_llm_down_probability": 0.25,
            "statistical_up_probability": 0.45,
            "statistical_flat_probability": 0.25,
            "statistical_down_probability": 0.30,
        },
        "gate_counterfactuals": {
            "calibrated_strategy_primary_v1": {
                "tier": tier,
                "trade": _trade(result),
            }
        },
    }


def _replay(replay_id: int, rows: list[dict]) -> dict:
    strategy_return = 1.0
    for row in rows:
        strategy_return *= 1 + row["strategy_trade"]["net_return"]
    strategy_return -= 1
    return {
        "replay_id": replay_id,
        "requested_range": {"start": "2026-01-01", "end": "2026-06-01"},
        "horizon_days": 20,
        "completed_episodes": len(rows),
        "episodes": rows,
        "degraded_sources": [],
        "llm_validation": {"live_llm_complete": True},
        "decision_gate_audit": {"policy_version": "2026-07-14.v2"},
        "metrics": {
            "strategy_only": {"total_return": strategy_return},
            "full_system": {"total_return": 0.0},
            "decision_gate_counterfactuals": {
                "calibrated_strategy_primary_v1": {"total_return": strategy_return}
            },
        },
    }


def test_gate_scorecard_keeps_v2_unpromoted_without_threshold_activation():
    scorecard = summarize_decision_gate_replays(
        [_replay(8, [_row(1, 0.01), _row(2, -0.005)])]
    )

    assert scorecard["promotion_status"] == "insufficient_evidence_not_promoted"
    assert scorecard["v2_model_driven_risk_reductions"] == 0
    assert scorecard["v2_horizon_challenges"]["20"]["episodes"] == 2
    assert "V1 LLM-first ETF gates are rejected" in scorecard["conclusion"]
    assert "Promotion status" in render_decision_gate_scorecard_markdown(scorecard)
