from datetime import date, timedelta

import numpy as np

from quantlab.workflows.stock_strategy_lab import (
    _evaluate_scores,
    _fit_ridge,
    _ridge_scores,
    _static_scores,
    _static_v2_scores,
    _validation_admission,
    a_share_ranking_policy_hash,
)


def _sample(symbol: str, realized: float, momentum_rank: float, volatility_rank: float):
    features = {
        "cross_section_momentum_20_rank": momentum_rank,
        "cross_section_momentum_60_rank": momentum_rank,
        "cross_section_momentum_120_rank": momentum_rank,
        "cross_section_volatility_20_rank": volatility_rank,
        "factor_path_quality_60": 2 * momentum_rank - 1,
        "factor_volume_asymmetry_20": 2 * momentum_rank - 1,
        "factor_price_position_60": 2 * momentum_rank - 1,
        "factor_composite": 2 * momentum_rank - 1,
        "pullback_strength": 1 - momentum_rank,
        "factor_momentum_20": 2 * momentum_rank - 1,
        "factor_momentum_60": 2 * momentum_rank - 1,
        "factor_momentum_acceleration": 0.0,
        "factor_return_skewness_60": 0.0,
        "factor_ma_spread_5_20": 2 * momentum_rank - 1,
        "factor_rsi_14": 2 * momentum_rank - 1,
        "mtf_consensus": 2 * momentum_rank - 1,
        "cross_section_relative_return_20": momentum_rank - 0.5,
    }
    return {
        "symbol": symbol,
        "as_of": "2020-01-01",
        "realized_return_pct": realized,
        "features": features,
    }


def test_static_candidates_express_reversal_and_quality_momentum_differently():
    loser = _sample("sh600001", 2.0, 0.1, 0.2)
    winner = _sample("sh600002", -1.0, 0.9, 0.8)

    reversal = _static_scores([loser, winner], "reversal_low_vol")
    momentum = _static_scores([loser, winner], "quality_momentum")

    assert reversal["sh600001"] > reversal["sh600002"]
    assert momentum["sh600002"] > momentum["sh600001"]


def test_v2_broad_contrarian_rewards_low_volatility_and_reversal():
    defensive = _sample("sh600001", 2.0, 0.1, 0.1)
    crowded = _sample("sh600002", -1.0, 0.9, 0.9)

    scores = _static_v2_scores([defensive, crowded], "broad_contrarian_four")

    assert scores["sh600001"] > scores["sh600002"]


def test_ridge_rank_learns_cross_sectional_order_from_prior_dates():
    groups = {}
    start = date(2018, 1, 2)
    for offset in range(15):
        day = start + timedelta(days=offset * 7)
        groups[day] = [
            _sample(f"sh60{offset:02d}{rank:02d}", float(rank), rank / 11, 1 - rank / 11)
            for rank in range(12)
        ]
    policy = _fit_ridge(groups, l2=10.0)
    scores = _ridge_scores(groups[max(groups)], policy)

    ordered = sorted(scores, key=scores.get)
    assert ordered[-1].endswith("11")
    assert policy["training_samples"] == 180


def test_candidate_evaluation_compounds_returns_and_applies_validation_gate():
    groups = {}
    scores = {}
    benchmark = {}
    start = date(2023, 1, 3)
    for offset in range(12):
        day = start + timedelta(days=offset * 14)
        items = [
            _sample(f"sh600{rank:03d}", float(rank - 5), rank / 11, 0.5)
            for rank in range(12)
        ]
        groups[day] = items
        scores[day] = {item["symbol"]: rank for rank, item in enumerate(items)}
        benchmark[day] = 0.005
    result = _evaluate_scores(
        groups,
        scores,
        benchmark,
        exposure=0.15,
        round_trip_cost=0.0035,
    )
    admission = _validation_admission(result)

    assert result["total_return"] > result["benchmark_total_return"] > 0
    assert result["mean_rank_ic"] > 0.9
    assert result["paired_comparison"]["probability_mean_excess_positive"] > 0.99
    assert admission["passed"] is False  # fixture spans only one calendar year
    assert admission["checks"]["at_least_two_positive_years"] is False
    assert np.isfinite(result["max_drawdown"])


def test_policy_hash_is_stable_after_embedding_the_hash():
    policy = {"kind": "static", "name": "low_vol_trend"}
    digest = a_share_ranking_policy_hash(policy)

    assert a_share_ranking_policy_hash({**policy, "policy_hash": digest}) == digest
