from datetime import date, timedelta

import pandas as pd

from quantlab.workflows.stock_strategy_lab import _market_samples, resolve_a_share_ranking_policy
from quantlab.workflows.stock_strategy_lab_v3 import (
    EXPECTED_SAMPLE_PROTOCOL_HASH,
    FROZEN_POLICY,
    _development_admission,
    _validation_admission,
    a_share_v3_protocol_hash,
    freeze_a_share_v3_locked_holdout_policy,
)


def test_v3_regime_resolution_uses_only_signal_date_history():
    start = date(2020, 1, 1)
    rows = []
    for offset in range(150):
        day = start + timedelta(days=offset)
        price = 100.0 + offset
        if offset > 130:
            price = 10_000.0  # Future prices must not affect the signal-date regime.
        rows.append({"symbol": "sh000300", "date": day, "close": price})
    frame = pd.DataFrame(rows)

    effective, diagnostics = resolve_a_share_ranking_policy(
        FROZEN_POLICY, frame, start + timedelta(days=130)
    )

    assert diagnostics["regime"] == "risk_on"
    assert diagnostics["trend_return"] == 230 / 110 - 1
    assert effective["name"] == "broad_contrarian_four"
    assert effective["portfolio"]["top_k"] == 4
    assert effective["portfolio"]["total_exposure"] == 0.40


def test_v3_insufficient_history_resolves_to_risk_off():
    frame = pd.DataFrame(
        [
            {"symbol": "sh000300", "date": date(2020, 1, 1), "close": 100.0},
            {"symbol": "sh000300", "date": date(2020, 1, 2), "close": 101.0},
        ]
    )

    effective, diagnostics = resolve_a_share_ranking_policy(
        FROZEN_POLICY, frame, date(2020, 1, 2)
    )

    assert diagnostics["regime"] == "risk_off"
    assert diagnostics["history_available"] is False
    assert effective["name"] == "pure_low_vol_two"
    assert effective["portfolio"]["top_k"] == 2


def test_v3_development_gate_requires_yearly_excess_consistency():
    result = {
        "total_return": 0.05,
        "mean_rank_ic": 0.10,
        "max_drawdown": -0.05,
        "positive_year_fraction": 0.80,
        "positive_excess_year_fraction": 0.40,
        "leg_fill_rate": 1.0,
        "paired_comparison": {"mean_excess_return": 0.002},
    }

    admission = _development_admission(result)

    assert admission["passed"] is False
    assert admission["checks"]["positive_excess_year_fraction_at_least_60pct"] is False


def test_v3_validation_gate_rejects_weak_bootstrap_probability():
    result = {
        "total_return": 0.05,
        "benchmark_total_return": 0.02,
        "mean_rank_ic": 0.10,
        "max_drawdown": -0.05,
        "participation_rate": 1.0,
        "leg_fill_rate": 1.0,
        "annual": {
            "2023": {"strategy_return": 0.02, "excess_return": 0.01},
            "2024": {"strategy_return": 0.02, "excess_return": 0.01},
            "2025": {"strategy_return": 0.01, "excess_return": -0.01},
        },
        "paired_comparison": {
            "mean_excess_return": 0.001,
            "probability_mean_excess_positive": 0.79,
            "bootstrap_90pct_interval": [-0.001, 0.003],
        },
    }

    admission = _validation_admission(result)

    assert admission["passed"] is False
    assert admission["checks"]["bootstrap_probability_at_least_80pct"] is False


def test_v3_freeze_embeds_stable_dynamic_policy_hash():
    report = {
        "locked_holdout_ready": True,
        "protocol_hash": a_share_v3_protocol_hash(),
        "sample_audit": {
            "selected_sample_protocol_hash": EXPECTED_SAMPLE_PROTOCOL_HASH,
        },
    }

    policy = freeze_a_share_v3_locked_holdout_policy(report)

    assert policy["kind"] == "market_regime_switch"
    assert policy["policy_hash"]
    assert policy["governance"]["holding_horizon_days"] == 20


def test_market_samples_can_require_the_frozen_protocol_hash():
    class Repository:
        def completed_samples(self, horizon_days, asset_scope):
            assert horizon_days == 5
            assert asset_scope == "stock"
            return [
                {
                    "source": "stock_market_point_in_time",
                    "asset_scope": "stock",
                    "symbol": "sh600001",
                    "as_of": "2023-01-03",
                    "realized_return_pct": 1.0,
                    "context": {
                        "training_eligible": True,
                        "sample_protocol_hash": protocol,
                    },
                }
                for protocol in ("newer-but-wrong", EXPECTED_SAMPLE_PROTOCOL_HASH)
            ]

    samples, audit = _market_samples(
        Repository(),
        date(2023, 1, 1),
        date(2023, 12, 31),
        5,
        required_protocol_hash=EXPECTED_SAMPLE_PROTOCOL_HASH,
    )

    assert len(samples) == 1
    assert audit["selected_sample_protocol_hash"] == EXPECTED_SAMPLE_PROTOCOL_HASH
