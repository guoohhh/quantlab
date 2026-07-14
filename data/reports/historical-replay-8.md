# QuantLab 历史盲测回放报告

- 区间：2026-02-02 至 2026-05-29
- 预测周期：20 个交易日
- 完成回合：4
- 证据等级：illustrative
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
| 纯策略 | 4 | 100.0% | 1.98% | 0.50% | 75.0% | -1.16% | ¥101,981.14 |
| 完整系统 | 1 | 25.0% | 0.56% | 0.14% | 100.0% | 0.00% | ¥100,562.39 |
| 沪深300同风险预算 | 4 | 100.0% | 0.60% | 0.15% | 75.0% | -0.12% | ¥100,595.87 |

## 单次回放

| 回合 | 信号日 | 标的 | Agent 动作 | 策略收益 | 完整系统收益 | 基准收益 | 闸门效果 | 实际方向 |
|---:|---|---|---|---:|---:|---:|---|---|
| 1 | 2026-02-02 | sh518880 | watch | 0.82% | 0.00% | 0.11% | missed_gain | up |
| 2 | 2026-03-12 | sh518880 | review_required | -1.16% | 0.00% | -0.12% | avoided_loss | down |
| 3 | 2026-04-14 | sz159915 | watch | 1.54% | 0.00% | 0.43% | missed_gain | up |
| 4 | 2026-05-19 | sz159915 | buy | 0.79% | 0.56% | 0.18% | participated | up |

## ETF decision-gate counterfactual audit

These arms reuse the same reviewed forecasts and executions. They are research-only and do not change the production gate.

| Policy | Trades | Participation | Total return | Increment vs current | Increment vs strategy | Max drawdown | Screening |
|---|---:|---:|---:|---:|---:|---:|---|
| current_strict | 1 | 25.0% | 0.56% | +0.00% | -1.42% | 0.00% | insufficient_sample |
| etf_score_tiered_v1 | 1 | 25.0% | 0.58% | +0.02% | -1.40% | 0.00% | insufficient_sample |
| etf_forecast_confirmed_v1 | 0 | 0.0% | 0.00% | -0.56% | -1.98% | 0.00% | insufficient_sample |
| strategy_unless_bearish_v1 | 2 | 50.0% | 2.35% | +1.79% | +0.37% | 0.00% | insufficient_sample |
| calibrated_strategy_primary_v1 | 4 | 100.0% | 1.98% | +1.42% | +0.00% | -1.16% | insufficient_sample |

- Policy version: 2026-07-14.v2
- Production policy changed: False
- Promotion boundary: counterfactual results may qualify a candidate only for a frozen prospective shadow account; they never replace the production gate automatically

## LLM 执行完整性

- 角色输出：44/44
- 端点尝试：51
- 回退错误：7
- 缺失角色：无
- 回退明细：openai/bull/InternalServerError; openai/bear/InternalServerError; openai/bull/InternalServerError; openai/bear/InternalServerError; openai/bull/InternalServerError; openai/bear/InternalServerError; openai/value_veto/APITimeoutError
- 完整执行：True

## 概率消融

```json
{
  "final_ensemble": {
    "samples": 4,
    "invalid_samples": 0,
    "brier_score": 0.16636157134723162,
    "log_loss": 0.8102234540438957,
    "accuracy": 0.5
  },
  "raw_llm": {
    "samples": 4,
    "invalid_samples": 0,
    "brier_score": 0.15916666666666665,
    "log_loss": 0.7904467144710186,
    "accuracy": 0.5
  },
  "point_in_time_statistical": {
    "samples": 4,
    "invalid_samples": 0,
    "brier_score": 0.17649366776560693,
    "log_loss": 0.8418246731960357,
    "accuracy": 0.75
  }
}
```

## 结论边界

a short replay is illustrative and cannot prove future profitability; expand the fixed window and prospective paper sample before making performance claims

本报告仅用于研究和辅助决策，不构成投资建议。历史数据、回测结果、统计模型与 LLM Agent 结论均不能保证未来收益；系统不会连接券商，任何交易必须由用户人工复核。
