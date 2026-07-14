# QuantLab ETF decision-gate evidence scorecard

- Replays: 5, 7, 8, 9
- V2 new episodes: 15
- Model-driven risk reductions: 0
- Promotion status: insufficient_evidence_not_promoted

## Replay-level evidence

| Replay | Range | Horizon | Episodes | Policy | Strategy | Current | V2 | Veto rate | Reviewer reject rate |
|---:|---|---:|---:|---|---:|---:|---:|---:|---:|
| 5 | 2018-01-01..2023-12-29 | 20 | 12 | 2026-07-14.v1 | 1.37% | 0.00% | N/A | 100.0% | 91.7% |
| 7 | 2015-01-01..2017-12-29 | 20 | 12 | 2026-07-14.v1 | 4.60% | -1.56% | N/A | 0.0% | 16.7% |
| 8 | 2026-02-02..2026-05-29 | 20 | 4 | 2026-07-14.v2 | 1.98% | 0.56% | 1.98% | 0.0% | 25.0% |
| 9 | 2026-02-02..2026-05-29 | 5 | 11 | 2026-07-14.v2 | -0.02% | -0.82% | -0.02% | 0.0% | 18.2% |

## V2 horizon challenges

### 5-day

- Episodes: 11
- Strategy return: -0.02%
- Current return: -0.82%
- V2 return: -0.02%
- Raw LLM Brier: 0.2256848484848485
- Statistical Brier: 0.17126689165250697
- Final ensemble Brier: 0.19601757826153723
- Model-driven reductions: 0

### 20-day

- Episodes: 4
- Strategy return: 1.98%
- Current return: 0.56%
- V2 return: 1.98%
- Raw LLM Brier: 0.15916666666666665
- Statistical Brier: 0.17649366776560693
- Final ensemble Brier: 0.16636157134723162
- Model-driven reductions: 0

## Promotion checks

- at_least_12_new_episodes_in_one_horizon: False
- at_least_one_model_driven_risk_reduction: False
- all_required_live_roles_complete: True
- clean_data_paths: True

## Conclusion

V1 LLM-first ETF gates are rejected. The V2 strategy-primary architecture prevents uncalibrated LLM and reviewer variance from suppressing the quantitative strategy. The statistical model improves short-horizon probability quality, but no frozen bearish threshold fired, so there is not yet evidence that the model improves trading profit.

historical blind replay and stitched short samples do not prove future profitability; V2 remains research-only until a frozen prospective shadow sample is sufficient
