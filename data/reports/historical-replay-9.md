# QuantLab 历史盲测回放报告

- 区间：2026-02-02 至 2026-05-29
- 预测周期：5 个交易日
- 完成回合：11
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
| 纯策略 | 11 | 100.0% | -0.02% | -0.00% | 36.4% | -1.49% | ¥99,978.10 |
| 完整系统 | 5 | 45.5% | -0.82% | -0.08% | 20.0% | -0.98% | ¥99,176.17 |
| 沪深300同风险预算 | 11 | 100.0% | -0.11% | -0.01% | 45.5% | -0.64% | ¥99,891.04 |

## 单次回放

| 回合 | 信号日 | 标的 | Agent 动作 | 策略收益 | 完整系统收益 | 基准收益 | 闸门效果 | 实际方向 |
|---:|---|---|---|---:|---:|---:|---|---|
| 1 | 2026-02-02 | sh518880 | review_required | 0.48% | 0.00% | 0.25% | missed_gain | up |
| 2 | 2026-02-11 | sh518880 | buy | 0.24% | 0.15% | -0.01% | participated | up |
| 3 | 2026-03-02 | sh518880 | review_required | -0.73% | 0.00% | -0.37% | avoided_loss | down |
| 4 | 2026-03-11 | sh518880 | buy | -0.44% | -0.31% | -0.18% | participated | down |
| 5 | 2026-03-20 | sh510880 | watch | -0.20% | 0.00% | -0.09% | avoided_loss | flat |
| 6 | 2026-03-31 | sh510880 | review_required | -0.12% | 0.00% | 0.25% | avoided_loss | flat |
| 7 | 2026-04-10 | sz159915 | watch | 1.01% | 0.00% | 0.33% | missed_gain | up |
| 8 | 2026-04-21 | sz159915 | buy | -0.26% | -0.16% | 0.01% | participated | down |
| 9 | 2026-04-30 | sz159915 | review_required | 0.67% | 0.00% | 0.27% | missed_gain | up |
| 10 | 2026-05-14 | sz159915 | buy | -0.46% | -0.36% | -0.40% | participated | down |
| 11 | 2026-05-25 | sz159915 | buy | -0.20% | -0.16% | -0.16% | participated | down |

## ETF decision-gate counterfactual audit

These arms reuse the same reviewed forecasts and executions. They are research-only and do not change the production gate.

| Policy | Trades | Participation | Total return | Increment vs current | Increment vs strategy | Max drawdown | Screening |
|---|---:|---:|---:|---:|---:|---:|---|
| current_strict | 5 | 45.5% | -0.82% | +0.00% | -0.80% | -0.98% | insufficient_sample |
| etf_score_tiered_v1 | 2 | 18.2% | -0.35% | +0.48% | -0.32% | -0.35% | insufficient_sample |
| etf_forecast_confirmed_v1 | 0 | 0.0% | 0.00% | +0.82% | +0.02% | 0.00% | insufficient_sample |
| strategy_unless_bearish_v1 | 7 | 63.6% | -0.32% | +0.50% | -0.30% | -0.91% | insufficient_sample |
| calibrated_strategy_primary_v1 | 11 | 100.0% | -0.02% | +0.80% | +0.00% | -1.49% | insufficient_sample |

- Policy version: 2026-07-14.v2
- Production policy changed: False
- Promotion boundary: counterfactual results may qualify a candidate only for a frozen prospective shadow account; they never replace the production gate automatically

## LLM 执行完整性

- 角色输出：121/121
- 端点尝试：125
- 回退错误：4
- 缺失角色：无
- 回退明细：openai/risk/APITimeoutError; openai/review/APITimeoutError; openai/risk/APITimeoutError; openai/value_veto/APITimeoutError
- 完整执行：True

## 概率消融

```json
{
  "final_ensemble": {
    "samples": 11,
    "invalid_samples": 0,
    "brier_score": 0.19601757826153723,
    "log_loss": 0.9417652215900572,
    "accuracy": 0.45454545454545453
  },
  "raw_llm": {
    "samples": 11,
    "invalid_samples": 0,
    "brier_score": 0.2256848484848485,
    "log_loss": 1.0681775501255517,
    "accuracy": 0.2727272727272727
  },
  "point_in_time_statistical": {
    "samples": 11,
    "invalid_samples": 0,
    "brier_score": 0.17126689165250697,
    "log_loss": 0.8464866569788729,
    "accuracy": 0.6363636363636364
  }
}
```

## 结论边界

a short replay is illustrative and cannot prove future profitability; expand the fixed window and prospective paper sample before making performance claims

本报告仅用于研究和辅助决策，不构成投资建议。历史数据、回测结果、统计模型与 LLM Agent 结论均不能保证未来收益；系统不会连接券商，任何交易必须由用户人工复核。
