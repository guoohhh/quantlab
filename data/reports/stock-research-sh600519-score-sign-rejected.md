# QuantLab 研究审计报告：sh600519

- 决策日期：2026-07-13
- Run ID：`8af15784-4d52-4788-9791-ddeb08734583`
- 行情来源：cached:fallback
- 行情样本：332 根
- 执行边界：manual_orders_only

## 最终决策

- 动作：**review_required**
- 置信度：54.5%
- 目标权重：0.0%
- 需要人工复核：是
- 入场价：1210.990
- 止损价：N/A
- 目标价 1 / 2：N/A / N/A

### 决策理由

- composite_score=-0.102
- strategy_signal_score=-0.118
- council_score=-0.088
- forecast_score=-0.051
- evidence_coverage=1.00
- conflict=0.910
- abs_composite=0.102
- high_conflict_review_triggered=True
- momentum_technical_sync=True
- buy_requires_composite>0.35
- review_required_when_high_conflict=conflict>0.80 and abs(composite)<0.12

### 风险与否决项

- quant: Volume_asymmetry_20 score = +0.111 (long, higher volume on up days)
- quant: RSI_14 = 54.5, score = +0.150 (long, not oversold but neutral-bullish)
- quant: Return_skewness_60 score = +0.144 (long, positive skew)
- fundamental: The supplied interest-coverage metric fails the stated hard threshold, despite no hard veto being listed in the payload.
- fundamental: Latest revenue and profit growth are both negative.
- fundamental: Share dilution over five years is unknown; the supplied warning states that unknown criteria are not treated as passes.
- fundamental: The cross-source discrepancy in 2025 ROE reduces confidence in that point estimate.
- news: 股价持续下跌，从1530元跌至1180元，跌幅约23%
- news: 主力资金净流出，食品饮料行业资金流出榜显示净流出超5000万元
- news: 失去A股股王地位，市场信心可能受挫
- technical: Price is above 5-day and 20-day moving averages, suggesting short-term bounce potential.
- momentum: Positive momentum acceleration (score 0.106) could signal early reversal.
- value_veto: Earnings risk: declining 2025 revenue and profit could reduce normalized owner earnings if contraction persists.
- risk: Trend and drawdown risk: medium- and longer-horizon momentum remain negative, and the recent maximum drawdown is material.
- macro: Price is above short-term ma_5 and ma_20, suggesting possible short-term bounce
- buffett: A sustained volume, mix, or pricing deterioration could turn the currently modest revenue and profit declines into a more durable owner-earnings decline; the supplied data do not decompose the 2025 contraction.
- munger: Permanent-loss path: if the reported revenue and profit contraction reflects a sustained normalization rather than a temporary fluctuation, the market may reset both earnings expectations and the valuation assigned to the franchise. High historical margins and ROE can magnify disappointment because they leave little room for investors assuming permanence of exceptional economics.
- graham: Valuation risk: absent normalized EPS/FCF per share, share count, dividend per share, net cash, and a conservative earnings-power estimate, a buyer could pay a price with no demonstrable downside buffer.
- fisher: Growth deceleration risk: negative 2025 revenue and profit growth may indicate a more durable reset rather than a transient comparison effect; absent segment and channel data, the permanence cannot be determined.
- lynch: Earnings-deceleration risk: the observed 2025 declines in both revenue and profit can persist or deepen. For a mature premium franchise, a sustained volume, pricing, or product-mix deterioration could impair earnings power and create permanent-loss risk if bought at an unsupported valuation.
- Material council-score inconsistency: the `munger` and `lynch` opinions are labeled bearish and contain bearish theses, but each has a positive score of `+0.30`. Elsewhere in the same council, bearish opinions use negative scores and bullish support uses positive scores. These role scores feed the reported `council_score` and therefore may have biased the aggregate composite upward.
- The score-sign inconsistency can affect the policy action, not merely presentation. The reported composite is `-0.102234`, only about `0.0178` above the reduce threshold of `-0.12`; correcting materially inconsistent council inputs could cross that threshold. The deterministic arithmetic matches the supplied trace, but the trace's council component is not sufficiently reliable for approval.
- The quant report incorrectly lists fundamental and news data as missing even though both are supplied and decision-calculation evidence coverage marks them available. This is a secondary internal reporting contradiction, though it is not independently the reason for rejection.

## Agent 委员会

- 战术分：-0.364
- 战略分：0.425
- 综合分：-0.088
- 否决触发：否
- 委员会摘要：tactical=-0.364; strategic=0.4245807259073842; veto=none

### 专家意见

- **technical**：bearish，分数 -0.300，置信度 70.0%
- **momentum**：bearish，分数 -0.300，置信度 60.0%
- **value_veto**：neutral，分数 0.000，置信度 72.0%
- **risk**：bearish，分数 -0.650，置信度 82.0%
- **macro**：bearish，分数 -0.600，置信度 70.0%
- **buffett**：neutral，分数 0.630，置信度 66.0%
- **munger**：bearish，分数 0.300，置信度 72.0%
- **graham**：neutral，分数 0.420，置信度 74.0%
- **fisher**：neutral，分数 0.480，置信度 62.0%
- **lynch**：bearish，分数 0.300，置信度 74.0%
- **financial_quality_gate**：neutral，分数 0.000，置信度 100.0%

## 概率预测

| 周期 | 上涨 | 震荡 | 下跌 | 预期收益 | 区间 | 模型 |
|---:|---:|---:|---:|---:|---:|---|
| 5日 | 41.0% | 24.0% | 35.0% | 0.150% | -3.800% ~ 3.400% | gpt-5.6-sol |
| 20日 | 29.0% | 22.0% | 49.0% | -2.000% | -10.500% ~ 7.000% | gpt-5.6-sol |

## Reviewer 复核

- 状态：rejected
- 摘要：Rejected for a material consistency failure in council inputs that may change the aggregate decision classification. Based strictly on the supplied trace, the current `review_required` action is otherwise policy-consistent: conflict `0.91 > 0.80`, absolute composite `0.102234 < 0.12`, confidence is correctly calculated as `1.0 × (1 - 0.5 × 0.91) = 0.545`, and the non-buy target weight is correctly zero. Required evidence is present, risks are discussed, and no degraded sources are silently omitted. Rejection preserves human review and zero target weight under the supplied policy.

### 复核问题

- Material council-score inconsistency: the `munger` and `lynch` opinions are labeled bearish and contain bearish theses, but each has a positive score of `+0.30`. Elsewhere in the same council, bearish opinions use negative scores and bullish support uses positive scores. These role scores feed the reported `council_score` and therefore may have biased the aggregate composite upward.
- The score-sign inconsistency can affect the policy action, not merely presentation. The reported composite is `-0.102234`, only about `0.0178` above the reduce threshold of `-0.12`; correcting materially inconsistent council inputs could cross that threshold. The deterministic arithmetic matches the supplied trace, but the trace's council component is not sufficiently reliable for approval.
- The quant report incorrectly lists fundamental and news data as missing even though both are supplied and decision-calculation evidence coverage marks them available. This is a secondary internal reporting contradiction, though it is not independently the reason for rejection.

## 数据质量与降级

- 无降级项

## 审计链

- `start` / ok：analysis started for sh600519
- `analysts` / ok：independent analyst reports completed
- `council` / ok：11 specialist opinions; tactical=-0.364; strategic=0.4245807259073842; veto=none
- `debate` / ok：bull and bear cases completed
- `forecast` / ok：5-day and 20-day probability forecasts completed
- `review` / needs_review：Rejected for a material consistency failure in council inputs that may change the aggregate decision classification. Based strictly on the supplied trace, the current `review_required` action is otherwise policy-consistent: conflict `0.91 > 0.80`, absolute composite `0.102234 < 0.12`, confidence is correctly calculated as `1.0 × (1 - 0.5 × 0.91) = 0.545`, and the non-buy target weight is correctly zero. Required evidence is present, risks are discussed, and no degraded sources are silently omitted. Rejection preserves human review and zero target weight under the supplied policy.

## 风险声明

本报告仅用于研究和辅助决策，不构成投资建议。历史数据、回测结果、统计模型与 LLM Agent 结论均不能保证未来收益；系统不会连接券商，任何交易必须由用户人工复核。
