# QuantLab 历史盲测回放报告

- 区间：2025-01-01 至 2025-06-30
- 预测周期：20 个交易日
- 完成回合：2
- 证据等级：illustrative
- 数据来源：cached:fallback

## 防泄漏契约

- 标的身份向 LLM 隐藏：True
- 真实日期向 LLM 隐藏：True
- 提供120日归一化价格路径：True
- 归一化路径不含绝对价格：True
- 统计模型：each episode refits using samples whose as_of and evaluated_at are before the signal date
- 交易执行：T close signal, T+1 open buy, horizon-day close sell, ETF costs and whole lots
- 抽样规则：eligible common trading dates with >=200 prior observations; non-overlapping episodes selected evenly across the requested range without using outcomes

## 组合结果

| 版本 | 交易数 | 参与率 | 累计收益 | 单次平均 | 胜率 | 最大回撤 | 期末权益 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 纯策略 | 2 | 100.0% | -0.33% | -0.17% | 0.0% | -0.33% | ¥99,668.58 |
| 完整系统 | 0 | 0.0% | 0.00% | 0.00% | N/A | 0.00% | ¥100,000.00 |
| 沪深300同风险预算 | 2 | 100.0% | 1.31% | 0.65% | 100.0% | 0.00% | ¥101,312.51 |

## 单次回放

| 回合 | 信号日 | 标的 | Agent 动作 | 策略收益 | 完整系统收益 | 基准收益 | 闸门效果 | 实际方向 |
|---:|---|---|---|---:|---:|---:|---|---|
| 1 | 2025-01-02 | sh513100 | watch | -0.21% | 0.00% | 0.30% | avoided_loss | down |
| 2 | 2025-06-20 | sh518880 | watch | -0.12% | 0.00% | 1.01% | avoided_loss | flat |

## LLM 执行完整性

- 角色输出：22/22
- 端点尝试：22
- 回退错误：0
- 完整执行：True

## 概率消融

```json
{
  "final_ensemble": {
    "samples": 2,
    "brier_score": 0.29370822446533107,
    "log_loss": 1.4211144172595738,
    "accuracy": 0.0
  },
  "raw_llm": {
    "samples": 2,
    "brier_score": 0.35126666666666667,
    "log_loss": 1.7147984280919266,
    "accuracy": 0.0
  },
  "point_in_time_statistical": {
    "samples": 2,
    "brier_score": 0.2451282775184103,
    "log_loss": 1.2114705357445428,
    "accuracy": 0.5
  }
}
```

## 结论边界

a short replay is illustrative and cannot prove future profitability; expand the fixed window and prospective paper sample before making performance claims

本报告仅用于研究和辅助决策，不构成投资建议。历史数据、回测结果、统计模型与 LLM Agent 结论均不能保证未来收益；系统不会连接券商，任何交易必须由用户人工复核。
