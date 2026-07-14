# A股横截面策略实验室 V1

- 状态：development_failed
- 协议哈希：`595b6530e52e063b`
- 样本协议：`6002d4c696b6c629`
- 开发期冠军：None
- 是否允许打开锁定留出：False

## 开发期

| 候选 | 收益 | 基准 | 平均超额 | Rank IC | 回撤 | 正收益年度 | 准入 | 选择分 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| reversal_low_vol | -7.78% | -2.71% | -0.188% | 0.048 | -9.39% | 0.0% | False | 0.000 |
| quality_momentum | -10.83% | -2.71% | -0.305% | -0.038 | -12.39% | 0.0% | False | 0.000 |
| momentum_reversal_blend | -8.10% | -2.71% | -0.200% | -0.015 | -11.96% | 50.0% | False | 0.000 |
| low_vol_trend | -1.89% | -2.71% | 0.031% | 0.032 | -5.43% | 50.0% | False | 0.000 |
| trend_pullback | -9.09% | -2.71% | -0.240% | -0.032 | -11.21% | 25.0% | False | 0.000 |
| ridge_rank_v1 | -2.84% | -2.71% | -0.004% | 0.069 | -5.23% | 50.0% | False | 0.000 |

## 验证期

没有开发期候选通过基础门槛，因此未运行验证期。

## 边界

development compares preregistered candidates; validation runs only the development winner; the 2026 locked holdout remains unopened by this workflow
