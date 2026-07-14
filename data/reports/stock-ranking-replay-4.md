# A股固定池点时排名回放

- 回放编号：4
- 区间：2024-01-01 至 2025-12-31
- 周期：5 个交易日
- 有效回合：30
- 证据等级：measured
- 固定股票池哈希：`fd8126af34498343`

## 组合结果

| 组合 | 累计收益 | 单回合胜率 | 最大回撤 | 参与率 |
|---|---:|---:|---:|---:|
| system_top_rank | 2.05% | 53.33% | -2.15% | 80.00% |
| simple_momentum | -0.86% | 26.67% | -3.19% | 63.33% |
| pool_equal_weight | 1.16% | 53.33% | -1.35% | 96.67% |
| benchmark_hs300 | 0.79% | 66.67% | -1.70% | 100.00% |
| system_diversified_top_k | 2.02% | 43.33% | -2.00% | 100.00% |

## 配对超额统计

| 系统 | 基准 | 平均超额 | 超额为正比例 | bootstrap均值为正概率 | 90%区间 |
|---|---|---:|---:|---:|---:|
| system_top_rank | simple_momentum | 0.097% | 56.67% | 94.20% | [-0.004%, 0.194%] |
| system_diversified_top_k | pool_equal_weight | 0.030% | 46.67% | 63.20% | [-0.092%, 0.164%] |
| system_top_rank | benchmark_hs300 | 0.042% | 60.00% | 70.85% | [-0.078%, 0.166%] |

## 证据契约

- 选点规则：freeze the supplied symbols before replay; select non-overlapping benchmark trading dates evenly without reading future returns; require at least 120 prior observations
- 成交规则：rank at T close, buy at the first tradable later open, evaluate at the first tradable close on/after T+5 or T+20; use 100-share lots, stock fees, stamp duty and slippage
- 固定池声明是否合格：True
- 合格范围：fixed_user_supplied_universe_only
- 是否具备全市场点时股票池：False
- 学习样本：180 条，training_eligible=False

## 声明边界

this measures ranking value inside a frozen research pool and does not remove the pool's selection/survivorship bias; it is not evidence that the system can select from all A-shares
