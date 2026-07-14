from quantlab.stock_reporting import render_stock_ranking_replay_markdown


def test_stock_ranking_markdown_reports_scope_and_paired_statistics():
    replay = {
        "replay_id": 3,
        "requested_range": {"start": "2024-01-01", "end": "2025-12-31"},
        "horizon_days": 5,
        "completed_episodes": 30,
        "evidence_status": "measured",
        "universe_hash": "abc123",
        "metrics": {
            "system_top_rank": {
                "total_return": 0.02,
                "episode_win_rate": 0.53,
                "max_drawdown": -0.02,
                "participation_rate": 0.8,
            }
        },
        "paired_comparisons": {
            "simple": {
                "system": "system_top_rank",
                "baseline": "simple_momentum",
                "mean_excess_return": 0.001,
                "positive_excess_rate": 0.57,
                "probability_mean_excess_positive": 0.94,
                "bootstrap_90pct_interval": [-0.0001, 0.002],
            }
        },
        "selection_rule": "point-in-time",
        "execution_contract": "next open",
        "evidence_qualification": {
            "qualified": True,
            "qualification_scope": "fixed_user_supplied_universe_only",
            "point_in_time_market_universe_available": False,
        },
        "learning_samples": {"count": 180, "training_eligible": False},
        "claim_boundary": "fixed pool only",
    }

    markdown = render_stock_ranking_replay_markdown(replay)

    assert "A股固定池点时排名回放" in markdown
    assert "system_top_rank" in markdown
    assert "94.00%" in markdown
    assert "fixed_user_supplied_universe_only" in markdown
    assert "training_eligible=False" in markdown
