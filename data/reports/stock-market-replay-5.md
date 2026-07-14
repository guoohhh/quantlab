# A股全市场点时分层回放

- 回放编号：5
- 区间：2024-01-01 至 2025-12-31
- 周期：5 个交易日
- 有效回合：12
- 证据等级：preliminary
- 固定股票池哈希：`eb023d9cc075485b`

## 组合结果

| 组合 | 累计收益 | 单回合胜率 | 最大回撤 | 参与率 |
|---|---:|---:|---:|---:|
| system_top_rank | -2.84% | 41.67% | -4.57% | 91.67% |
| simple_momentum | -5.43% | 25.00% | -5.43% | 91.67% |
| pool_equal_weight | 8.00% | 75.00% | -0.88% | 100.00% |
| benchmark_hs300 | 4.52% | 75.00% | -0.71% | 100.00% |
| system_diversified_top_k | 8.93% | 41.67% | -6.39% | 100.00% |

## 配对超额统计

| 系统 | 基准 | 平均超额 | 超额为正比例 | bootstrap均值为正概率 | 90%区间 |
|---|---|---:|---:|---:|---:|
| system_top_rank | simple_momentum | 0.225% | 50.00% | 94.45% | [-0.006%, 0.465%] |
| system_diversified_top_k | pool_equal_weight | 0.135% | 41.67% | 57.35% | [-1.150%, 1.485%] |
| system_top_rank | benchmark_hs300 | -0.607% | 25.00% | 0.40% | [-1.048%, -0.201%] |

## 幸存者偏差审计

- 历史市场快照：12 个
- 样本中的最终退市观测：0
- 持有期内退市观测：0
- 历史ST日状态排除：6

## 证据契约

- 选点规则：query the actual A-share list on each signal date; remove only same-date non-trading and special-treatment names; draw an exchange/board-stratified deterministic hash sample rotated by calendar year without using future returns or future listing status
- 成交规则：rank at T close; buy at the first later executable open; enforce historical ST, suspension, one-price limit, 100-share lot and stock costs; if a selected stock disappears before the horizon, use its last sellable close or a zero terminal recovery; the HS300 comparison is a non-tradeable index return scaled to the same exposure
- 固定池声明是否合格：False
- 合格范围：stratified_point_in_time_a_share_market_sample
- 是否具备全市场点时股票池：True
- 学习样本：366 条，training_eligible=True

## 声明边界

this removes current-constituent survivorship bias and tests deterministic stratified samples of each historical A-share market; it is not an exhaustive daily ranking of every listed stock, and statistical uncertainty remains
