# QuantLab 研究审计报告：sh513100

- 决策日期：2026-07-13
- Run ID：`967bc027-a092-40e5-836c-d378a1a03d13`
- 行情来源：cached:fallback
- 行情样本：333 根
- 执行边界：manual_orders_only

## 最终决策

- 动作：**watch**
- 置信度：46.8%
- 目标权重：0.0%
- 需要人工复核：否
- 入场价：2.168
- 止损价：N/A
- 目标价 1 / 2：N/A / N/A

### 决策理由

- composite_score=0.286
- strategy_signal_score=0.716
- council_score=0.135
- forecast_score=0.087
- evidence_coverage=0.90
- momentum_technical_sync=False
- buy_requires_composite>0.35

### 风险与否决项

- Momentum acceleration negative (-0.592) suggests slowing short-term momentum.
- MA spread 5/20 negative (-0.187) indicates short-term average below medium-term.
- Multi-timeframe consensus is mixed.
- 20-day momentum and RSI are neutral, not confirming strength.
- Mixed trend could resolve sharply in either direction; no clear support/resistance levels provided.
- Volume asymmetry slightly negative (-0.021) may hint at distribution, but magnitude is small.
- Momentum deceleration could lead to a trend reversal or consolidation.
- Negative volume asymmetry suggests potential distribution.
- Without NAV and premium/discount data, the purchase price cannot be compared with the value of the underlying assets.
- Without holdings, concentration, and look-through financials, owner earnings, leverage, cash conversion, and constituent solvency cannot be assessed.
- Without assets under management, trading liquidity, bid-ask spread, creation/redemption information, and authorized-participant data, ETF liquidity and execution risks cannot be bounded.
- The target weight of zero may indicate a portfolio-construction constraint or unresolved risk, but the reason is not supplied and therefore cannot support a veto.
- Strong recent relative momentum may reverse; no valuation evidence establishes protection against permanent capital loss.
- Potential trend reversal after strong medium-term performance, indicated by negative short-term acceleration and moving-average spread.
- Unknown maximum drawdown and tail-loss profile.
- Unknown trading liquidity, bid-ask spread, market depth, and creation/redemption conditions; execution feasibility cannot be verified.
- Unknown holdings concentration, geographic or sector exposure, currency exposure, leverage, derivatives use, and counterparty risk.
- Unknown tracking error and ETF premium/discount behavior.
- No event-risk assessment or news evidence is supplied.
- Signal validity is unspecified because valid_until is null.
- Momentum acceleration negative (-0.592) suggests short-term deceleration
- MA spread 5/20 negative (-0.187) indicates short-term weakness
- Multi-timeframe consensus 'mixed' with daily negative

## Agent 委员会

- 战术分：0.135
- 战略分：N/A
- 综合分：0.135
- 否决触发：否
- 委员会摘要：tactical=0.135; strategic=None; veto=none

### 专家意见

- **technical**：neutral，分数 0.000，置信度 60.0%
- **momentum**：neutral，分数 0.000，置信度 60.0%
- **value_veto**：neutral，分数 0.100，置信度 62.0%
- **risk**：bearish，分数 0.300，置信度 38.0%
- **macro**：bullish，分数 0.600，置信度 70.0%

## 概率预测

| 周期 | 上涨 | 震荡 | 下跌 | 预期收益 | 区间 | 模型 |
|---:|---:|---:|---:|---:|---:|---|
| 5日 | 36.3% | 39.6% | 24.1% | 0.300% | -2.800% ~ 3.200% | gpt-5.6-sol+online-v7 |
| 20日 | 45.6% | 27.3% | 27.1% | 1.500% | -7.000% ~ 8.500% | gpt-5.6-sol+online-v7 |

## Reviewer 复核

- 状态：approved
- 摘要：The provisional decision is internally consistent with the supplied deterministic trace and policy. The composite score recalculates to 0.285761, which is above the 0.12 watch threshold but below the 0.35 buy threshold, so “watch” is correct. Confidence recalculates to 0.468 from 0.90 evidence coverage and 0.96 conflict, exceeding the policy minimum of 0.25. The zero target weight is required for a non-buy action. Required tactical evidence is represented, no hard veto or degraded source is reported, optional ETF fundamentals and news are not grounds for rejection, forecast probabilities are coherent, and material short-term, liquidity, execution, and data-limit risks are disclosed.

### 复核问题

- 无

## 数据质量与降级

- 无降级项

## 审计链

- `start` / ok：analysis started for sh513100
- `analysts` / ok：independent analyst reports completed
- `council` / ok：5 specialist opinions; tactical=0.135; strategic=None; veto=none
- `debate` / ok：bull and bear cases completed
- `forecast` / ok：5-day and 20-day probability forecasts completed
- `review` / ok：The provisional decision is internally consistent with the supplied deterministic trace and policy. The composite score recalculates to 0.285761, which is above the 0.12 watch threshold but below the 0.35 buy threshold, so “watch” is correct. Confidence recalculates to 0.468 from 0.90 evidence coverage and 0.96 conflict, exceeding the policy minimum of 0.25. The zero target weight is required for a non-buy action. Required tactical evidence is represented, no hard veto or degraded source is reported, optional ETF fundamentals and news are not grounds for rejection, forecast probabilities are coherent, and material short-term, liquidity, execution, and data-limit risks are disclosed.

## 风险声明

本报告仅用于研究和辅助决策，不构成投资建议。历史数据、回测结果、统计模型与 LLM Agent 结论均不能保证未来收益；系统不会连接券商，任何交易必须由用户人工复核。
