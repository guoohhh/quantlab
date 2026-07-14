# QuantLab 研究审计报告：sh600519

- 决策日期：2026-07-13
- Run ID：`23623f55-981c-496a-b15a-9db79b4ecc16`
- 行情来源：cached:fallback
- 行情样本：332 根
- 执行边界：manual_orders_only

## 最终决策

- 动作：**review_required**
- 置信度：56.0%
- 目标权重：0.0%
- 需要人工复核：是
- 入场价：1210.990
- 止损价：N/A
- 目标价 1 / 2：N/A / N/A

### 决策理由

- composite_score=-0.107
- strategy_signal_score=-0.118
- council_score=-0.191
- forecast_score=-0.004
- evidence_coverage=1.00
- conflict=0.880
- abs_composite=0.107
- high_conflict_review_triggered=True
- momentum_technical_sync=True
- buy_requires_composite>0.35
- review_required_when_high_conflict=conflict>0.80 and abs(composite)<0.12

### 风险与否决项

- Price above 5-day and 20-day moving averages (short-term bullish)
- RSI_14 = 54.5 (neutral, not oversold) but slightly positive score
- Volume_asymmetry_20 positive (0.111) suggests buying pressure on up days
- Momentum acceleration positive (0.106) could indicate slowing downtrend
- Pullback/reversal not triggered but conditions could change
- Fact: Interest coverage is reported as -13,976.2501x and fails the required threshold of at least 2x. The payload provides no underlying interest-expense or earnings components to establish whether this is an economic warning or a metric-definition/calculation artifact.
- Fact: Five-year share dilution is unknown because historical total-share data was unavailable from both selected free sources. The payload explicitly warns that unknown criteria are not treated as passes.
- Fact: The quality screen is explicitly described as incomplete.
- Fact: The ROE cross-validation result is a warning, with a 3.33% relative error between the two supplied sources.
- Inference: If the interest-coverage result is economically representative rather than a data artifact, it would materially weaken the otherwise favorable quality profile.
- Inference: Continued revenue and profit contraction could pressure future cash generation and returns.
- 2026-07-09: 食品饮料行业资金流出榜显示贵州茅台净流出超5000万元
- 2026-07-13: 百亿主观私募业绩分化，林园投资半年回撤超10%，贵州茅台股价从1530元跌至1180元
- Price is above 5-day and 20-day moving averages, suggesting short-term bounce potential.
- Volume asymmetry (20-day) positive at 0.111, indicating higher volume on up days.
- RSI_14 at 54.5 is neutral, not oversold, limiting reversal conviction.
- Positive momentum acceleration could signal early reversal if sustained.
- Positive volume asymmetry suggests accumulation on up days.
- 2025 revenue and profit contracted; sustained deterioration could reduce normalized owner earnings and expose valuation risk.
- Cash conversion is strong but below 1.0, so reported profit has not been fully converted into operating cash on average over five years.
- Reported interest coverage of -13,976.2501 fails the supplied threshold and is economically difficult to interpret without the underlying interest-expense and finance-income components.
- Share dilution over five years is unknown.
- At the supplied price of CNY 1,210.99, valuation risk cannot be bounded without per-share owner earnings, normalized free cash flow, net cash, or an independently supported intrinsic-value range.
- The observed -19.397256% maximum drawdown shows material mark-to-market loss potential; a larger position could cause unacceptable portfolio drawdown.
- Medium-term trend risk remains adverse because the latest signal close is below both MA60 and MA120 despite being above MA5 and MA20.
- Fundamental growth has weakened: latest reported revenue growth is -1.2% and profit growth is -4.53%. Persistence could raise valuation and permanent-loss risk, but valuation data are not supplied.
- Interest coverage is reported at -13976.2501 and fails the stated threshold. Its economic cause is not supplied, so it is a data or accounting issue requiring verification rather than a demonstrated solvency veto.
- Liquidity evidence is based on trading amount only. Bid-ask spread, order-book depth, market-impact estimates, suspension risk, daily price-limit conditions, and proposed order size are absent.
- Portfolio concentration cannot be assessed because current holdings, intended position size, portfolio NAV, correlation exposures, and risk limits are not supplied.
- News includes dividends, repurchase references, management changes, and fund-flow reports, but no supplied evidence identifies a specific imminent event capable of creating unbounded loss.
- The bearish risk interpretation would weaken if momentum and the reported MA60/MA120 relationships reverse; no prospective invalidation date or stop-loss rule is supplied.
- Short-term price above MA5/MA20 could indicate a bounce, but not enough to reverse bear regime.
- Liquidity appears adequate (average daily amount ~5B CNY), no execution risk.
- Permanent-loss risk: the negative 2025 revenue and profit growth could signal a deterioration in demand, pricing, mix, or channel health. Without segment, volume, inventory, and realized-price data, it is not possible to distinguish a temporary slowdown from a weakening of the economic franchise.
- Execution/capital-allocation risk: management integrity and stewardship cannot be affirmatively assessed from appointment and meeting notices. Related-party transactions, audit qualifications, executive incentives, insider transactions, allocation decisions, and governance-control details are absent.
- Owner-earnings risk: cumulative FCF and OCF/profit conversion do not reveal maintenance versus growth capex, working-capital movements, customer/dealer financing, taxes, or cash that is economically unavailable to owners.
- Valuation risk: without an intrinsic-value bridge and share count, the supplied price cannot establish a margin of safety. A great business can still produce poor returns if acquired without adequate valuation protection.
- The supplied market data show adjusted 60- and 120-trading-day returns of -11.952% and -10.522%, respectively. This is not a moat finding, but it reinforces that the current market backdrop is unfavorable and should not be mistaken for evidence of value.
- Permanent-loss pathway—franchise erosion or growth reset: if the reported revenue and profit declines represent a sustained demand, pricing, mix, or channel problem rather than a temporary fluctuation, historic premium profitability may not prevent a durable earnings and multiple reset. The payload does not identify the cause or reversibility of the decline.
- Valuation-risk pathway: because no valuation multiple, market capitalization, enterprise value, dividend yield, forward estimates, or intrinsic-value range is supplied, an investor cannot test whether the raw price of 1,210.99 embeds a return to growth. A high-quality business can still produce poor or permanently impaired investment returns when acquired with inadequate margin of safety.
- Trend-and-behavioral pathway: buying because the stock is above its 5- and 20-day averages, has RSI 54.51, or has a small positive momentum-acceleration score risks anchoring on a short-term bounce while the supplied 60- and 120-day trends remain negative. This is a classic recency and mean-reversion trap.
- Accounting-classification pathway: the failed interest-coverage metric is anomalous relative to the other supplied cash-flow and debt measures. If it reflects unusual interest-income/expense classification rather than debt service capacity, screen-based users can draw a false conclusion; if it reflects an underlying financial change, the payload lacks the underlying statements needed to assess it.
- Capital-allocation and agency pathway: the payload mentions dividend implementation, a dividend-per-share adjustment, buyback-related reporting, executive/board changes, and remuneration rules, but supplies no underlying terms, ownership/control analysis, payout policy, buyback execution details, or incentive metrics. It therefore cannot establish alignment between management, controlling stakeholders, and minority shareholders.
- Liquidity is not the central identified risk—the supplied 20-day average trading amount is 5.4199245 billion—but broad market stress can still cause correlation-driven selling. The quant regime is explicitly classified as bear with 0.6707 confidence.
- The positive-news labels and snippets are not a substitute for underwriting. Dividend, buyback, financing-flow, and sector-rebound mentions may support sentiment, but they do not resolve the negative reported operating growth or establish durable intrinsic-value protection.
- A continuing earnings decline could reduce normalized earning power and turn an apparently high-quality franchise into a value trap if the entry valuation embeds prior profitability.
- Unresolved negative interest coverage is a financial-reporting/interpretation risk. Without its components, the committee cannot determine whether it reflects a benign net-interest accounting effect or inadequate operating coverage.
- Unknown five-year share dilution leaves per-share value preservation unverified; aggregate free cash flow alone is insufficient if claims on that cash flow have expanded.
- The market is identified as bear regime and the supplied factor composite is -0.118 with a weak-downtrend verdict; downside price pressure can persist independently of balance-sheet quality.
- Negative reported revenue and profit growth could represent a more persistent demand, pricing, channel, or mix problem; the payload supplies no segment, volume, pricing, inventory, or channel data to distinguish a temporary normalization from structural erosion.
- Margin durability is historically exceptional, but there is no current-year gross-margin bridge, product mix, cost trend, or evidence on pricing power. The historical average cannot establish that margins will remain durable.
- No R&D spending, innovation pipeline, product-development productivity, or long-run category-expansion evidence is supplied. This prevents assessment of whether future growth has internally generated reinvestment drivers.
- No insider ownership, insider buying/selling, share-count history, executive compensation metrics, or detailed succession information is supplied. Alignment and management execution cannot be verified.
- The reported interest-coverage metric is negative despite the low debt ratio and strong cash-generation metrics. Without the underlying interest expense and earnings definitions, it is not interpretable as a leverage-risk conclusion, but it warrants reconciliation.
- Earnings-deceleration/execution risk: if the reported revenue and profit declines persist, a mature franchise may not deliver the earnings growth needed to support a GARP outcome.
- Valuation risk is unbounded within this payload: without valuation multiples, per-share earnings, market capitalization, or a growth forecast, there is no numerical basis to judge whether the current price discounts the weaker earnings trajectory.
- Trend/regime risk remains elevated: the supplied factor process assigns zero target weight in a bear regime and identifies a weak downtrend.
- The reported interest-coverage value of -13,976.2501 fails the stated screen, but it is difficult to interpret alongside the low debt ratio and high current ratio. It should be reconciled before treating it as evidence of financing stress.
- Share dilution over five years is unknown, so per-share compounding cannot be verified.

## Agent 委员会

- 战术分：-0.421
- 战略分：0.235
- 综合分：-0.191
- 否决触发：否
- 委员会摘要：tactical=-0.421; strategic=0.23509573810994444; veto=none

### 专家意见

- **technical**：bearish，分数 -0.400，置信度 70.0%
- **momentum**：bearish，分数 -0.350，置信度 65.0%
- **value_veto**：neutral，分数 0.050，置信度 74.0%
- **risk**：bearish，分数 -0.450，置信度 82.0%
- **macro**：bearish，分数 -0.600，置信度 70.0%
- **buffett**：neutral，分数 0.680，置信度 64.0%
- **munger**：bearish，分数 -0.620，置信度 78.0%
- **graham**：neutral，分数 0.380，置信度 72.0%
- **fisher**：neutral，分数 0.580，置信度 66.0%
- **lynch**：bearish，分数 0.380，置信度 72.0%
- **financial_quality_gate**：neutral，分数 0.000，置信度 100.0%

## 概率预测

| 周期 | 上涨 | 震荡 | 下跌 | 预期收益 | 区间 | 模型 |
|---:|---:|---:|---:|---:|---:|---|
| 5日 | 38.0% | 32.0% | 30.0% | 0.150% | -4.200% ~ 3.400% | gpt-5.6-sol |
| 20日 | 34.0% | 23.0% | 43.0% | -1.100% | -8.000% ~ 6.500% | gpt-5.6-sol |

## Reviewer 复核

- 状态：approved
- 摘要：The provisional decision is valid under the supplied policy. The deterministic composite is correctly calculated as -0.10731997, confidence is correctly calculated as 1.00 × (1 - 0.5 × 0.88) = 0.56, and all required evidence categories are represented. Because conflict is 0.88, above the 0.80 threshold, while |composite| is 0.1073, inside the 0.12 neutral band, review_required is the correct action. Its zero target weight also complies with the rule that non-buy actions must have zero weight. Forecast probabilities each sum to 1.0, material trend, drawdown, fundamental, valuation, data-quality, liquidity, and portfolio risks are discussed, and no degraded source is silently omitted. The noted report-level inconsistencies do not alter the deterministic trace or create an unsafe allocation, so they are non-blocking.

### 复核问题

- Non-blocking inconsistency: the quant report says fundamentals and news were not provided, although both are present and used by the deterministic calculation.
- Non-blocking wording error: the fundamental and news reports describe the current market price as missing, although CNY 1,210.99 is supplied. Valuation inputs such as EPS, share count, and valuation multiples are genuinely missing.
- Non-blocking overstatement: one macro opinion says there is “no execution risk” based primarily on trading amount. Spread, depth, order size, market impact, and price-limit conditions are unavailable. The final decision’s risk section correctly restores these caveats.
- The final risk list is highly repetitive and could be consolidated without changing the decision.

## 数据质量与降级

- 无降级项

## 审计链

- `start` / ok：analysis started for sh600519
- `analysts` / ok：independent analyst reports completed
- `council` / ok：11 specialist opinions; tactical=-0.421; strategic=0.23509573810994444; veto=none
- `debate` / ok：bull and bear cases completed
- `forecast` / ok：5-day and 20-day probability forecasts completed
- `review` / ok：The provisional decision is valid under the supplied policy. The deterministic composite is correctly calculated as -0.10731997, confidence is correctly calculated as 1.00 × (1 - 0.5 × 0.88) = 0.56, and all required evidence categories are represented. Because conflict is 0.88, above the 0.80 threshold, while |composite| is 0.1073, inside the 0.12 neutral band, review_required is the correct action. Its zero target weight also complies with the rule that non-buy actions must have zero weight. Forecast probabilities each sum to 1.0, material trend, drawdown, fundamental, valuation, data-quality, liquidity, and portfolio risks are discussed, and no degraded source is silently omitted. The noted report-level inconsistencies do not alter the deterministic trace or create an unsafe allocation, so they are non-blocking.

## 风险声明

本报告仅用于研究和辅助决策，不构成投资建议。历史数据、回测结果、统计模型与 LLM Agent 结论均不能保证未来收益；系统不会连接券商，任何交易必须由用户人工复核。
