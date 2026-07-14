# QuantLab 历史盲测回放报告

- 区间：2015-01-01 至 2017-12-29
- 预测周期：20 个交易日
- 完成回合：12
- 证据等级：preliminary
- 证据资格：False
- 数据来源：cached:fallback

## 防泄漏契约

- 标的身份向 LLM 隐藏：True
- 真实日期向 LLM 隐藏：True
- 提供120日归一化价格路径：True
- 归一化路径不含绝对价格：True
- 统计模型：each episode refits using samples whose as_of and evaluated_at are before the signal date
- 交易执行：T close signal, T+1 open buy, horizon-day close sell, ETF costs, whole lots, and path-dependent capital per comparison arm
- 抽样规则：eligible common trading dates with >=200 prior observations; non-overlapping episodes selected evenly across the requested range without using outcomes

## 组合结果

| 版本 | 交易数 | 参与率 | 累计收益 | 单次平均 | 胜率 | 最大回撤 | 期末权益 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 纯策略 | 12 | 100.0% | 4.60% | 0.39% | 58.3% | -1.79% | ¥104,599.19 |
| 完整系统 | 2 | 16.7% | -1.56% | -0.13% | 0.0% | -1.56% | ¥98,439.41 |
| 沪深300同风险预算 | 12 | 100.0% | 2.90% | 0.24% | 58.3% | -1.90% | ¥102,903.78 |

## 单次回放

| 回合 | 信号日 | 标的 | Agent 动作 | 策略收益 | 完整系统收益 | 基准收益 | 闸门效果 | 实际方向 |
|---:|---|---|---|---:|---:|---:|---|---|
| 1 | 2015-01-05 | sh510300 | buy | -1.53% | -1.30% | -1.53% | participated | down |
| 2 | 2015-04-15 | sz159915 | review_required | 4.45% | 0.00% | 1.19% | missed_gain | up |
| 3 | 2015-07-20 | sh513100 | watch | 0.47% | 0.00% | -0.11% | missed_gain | up |
| 4 | 2015-10-29 | sh513100 | watch | 0.46% | 0.00% | 1.25% | missed_gain | up |
| 5 | 2016-02-01 | sh518880 | watch | 1.52% | 0.00% | 1.41% | missed_gain | up |
| 6 | 2016-05-12 | sh518880 | watch | 0.31% | 0.00% | -0.02% | missed_gain | up |
| 7 | 2016-08-16 | sh513100 | review_required | -0.02% | 0.00% | -0.68% | avoided_loss | flat |
| 8 | 2016-11-25 | sh510880 | watch | -1.35% | 0.00% | -1.20% | avoided_loss | down |
| 9 | 2017-03-07 | sh513100 | buy | -0.40% | -0.27% | 0.29% | participated | down |
| 10 | 2017-06-14 | sh513100 | watch | -0.02% | 0.00% | 0.70% | avoided_loss | down |
| 11 | 2017-09-14 | sh510300 | watch | 0.44% | 0.00% | 0.44% | missed_gain | up |
| 12 | 2017-12-22 | sh513100 | watch | 0.31% | 0.00% | 1.19% | missed_gain | up |

## ETF decision-gate counterfactual audit

These arms reuse the same reviewed forecasts and executions. They are research-only and do not change the production gate.

| Policy | Trades | Participation | Total return | Increment vs current | Increment vs strategy | Max drawdown | Screening |
|---|---:|---:|---:|---:|---:|---:|---|
| current_strict | 2 | 16.7% | -1.56% | +0.00% | -6.16% | -1.56% | retrospective_screen_fail |
| etf_score_tiered_v1 | 9 | 75.0% | -1.82% | -0.26% | -6.42% | -2.06% | retrospective_screen_fail |
| etf_forecast_confirmed_v1 | 8 | 66.7% | -0.94% | +0.62% | -5.53% | -1.28% | retrospective_screen_fail |
| strategy_unless_bearish_v1 | 10 | 83.3% | 0.17% | +1.73% | -4.43% | -1.77% | retrospective_screen_fail |

- Policy version: 2026-07-14.v1
- Production policy changed: False
- Promotion boundary: counterfactual results may qualify a candidate only for a frozen prospective shadow account; they never replace the production gate automatically

## LLM 执行完整性

- 角色输出：132/132
- 端点尝试：137
- 回退错误：5
- 缺失角色：无
- 回退明细：openai/risk/APITimeoutError; openai/review/APITimeoutError; openai/forecast/APITimeoutError; openai/review/APITimeoutError; openai/forecast/APITimeoutError
- 完整执行：True

## 概率消融

```json
{
  "final_ensemble": {
    "samples": 12,
    "invalid_samples": 0,
    "brier_score": 0.1907444444444444,
    "log_loss": 0.9423748582903776,
    "accuracy": 0.5833333333333334
  },
  "raw_llm": {
    "samples": 12,
    "invalid_samples": 0,
    "brier_score": 0.1907444444444444,
    "log_loss": 0.9423748582903776,
    "accuracy": 0.5833333333333334
  },
  "point_in_time_statistical": {
    "samples": 0,
    "invalid_samples": 0,
    "brier_score": null,
    "log_loss": null,
    "accuracy": null
  }
}
```

## 结论边界

a short replay is illustrative and cannot prove future profitability; expand the fixed window and prospective paper sample before making performance claims

本报告仅用于研究和辅助决策，不构成投资建议。历史数据、回测结果、统计模型与 LLM Agent 结论均不能保证未来收益；系统不会连接券商，任何交易必须由用户人工复核。
