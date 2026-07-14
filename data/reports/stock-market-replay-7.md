# A股全市场点时分层回放

- 回放编号：7
- 区间：2024-01-01 至 2025-12-31
- 周期：5 个交易日
- 有效回合：30
- 证据等级：measured
- 分层样本协议哈希：`eb023d9cc075485b`

## 组合结果

| 组合 | 累计收益 | 单回合胜率 | 最大回撤 | 参与率 |
|---|---:|---:|---:|---:|
| system_top_rank | 6.88% | 50.00% | -5.90% | 100.00% |
| simple_momentum | -4.34% | 40.00% | -8.70% | 96.67% |
| pool_equal_weight | -3.43% | 30.00% | -5.52% | 100.00% |
| benchmark_hs300 | 1.41% | 66.67% | -1.54% | 100.00% |
| benchmark_hs300_multi_name | 4.21% | 66.67% | -4.57% | 100.00% |
| system_diversified_top_k | -3.41% | 53.33% | -15.81% | 100.00% |

## 配对超额统计

| 系统 | 基准 | 平均超额 | 超额为正比例 | bootstrap均值为正概率 | 90%区间 |
|---|---|---:|---:|---:|---:|
| system_top_rank | simple_momentum | 0.376% | 46.67% | 96.65% | [0.031%, 0.777%] |
| system_diversified_top_k | pool_equal_weight | 0.022% | 53.33% | 53.35% | [-0.563%, 0.611%] |
| system_top_rank | benchmark_hs300 | 0.187% | 46.67% | 75.30% | [-0.261%, 0.655%] |
| system_diversified_top_k | benchmark_hs300_multi_name | -0.233% | 50.00% | 26.70% | [-0.866%, 0.369%] |

## 幸存者偏差审计

- 历史市场快照：30 个
- 样本中的最终退市观测：0
- 持有期内退市观测：0
- 历史ST日状态排除：14

## 策略准入

- 状态：research_only
- 是否通过：False
- 推荐模式：None
- 部署建议：do not promote to live recommendation

## 证据契约

- 选点规则：query the actual A-share list on each signal date; remove only same-date non-trading and special-treatment names; draw an exchange/board-stratified deterministic hash sample rotated by calendar year without using future returns or future listing status
- 成交规则：rank at T close; buy at the first later executable open; enforce historical ST, suspension, one-price limit, 100-share lot and stock costs; if a selected stock disappears before the horizon, use its last sellable close or a zero terminal recovery; the HS300 comparison is a non-tradeable index return scaled to the same exposure
- 点时市场证据是否合格：True
- 合格范围：stratified_point_in_time_a_share_market_sample
- 是否具备全市场点时股票池：True
- 学习样本：947 条，training_eligible=True

## 声明边界

this removes current-constituent survivorship bias and tests deterministic stratified samples of each historical A-share market; it is not an exhaustive daily ranking of every listed stock; evidence qualification describes data rigor, while strategy deployment additionally requires the separate performance admission gate
