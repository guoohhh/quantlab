# 宽样本前瞻验证与标准化研究组合

本能力使用独立的 `wide_forward_research` evidence boundary，不属于正式 Primary，
也不写入七影子账户或用户模拟账户。

## 冻结规则

- 每个交易日收盘后只读取同日 production/server-observed 点时池；
- 选择规则按行业、市值、当日强弱和风格确定性分层，默认自动选择 24 只；
- 禁止人工传入 symbol 列表，禁止 operator backfill，禁止使用昨日快照；
- 同一标的冻结 5/20 日七变体预测，并保存 Context、Quote、Prompt、Model、
  Prediction 与原始数据指纹；
- T 日收盘形成信号，标准化研究组合从 T+1 开盘计算，A 股下跌判断只计方向和
  避免买入价值，不创建裸做空收益；
- 同日股票有相关性，因此成绩单同时报告股票数与独立交易日数。

## 标准化组合边界

七个研究组合初始净值为 100，采用等名义权重和碎股计算，分别保存含成本与不含
成本净值。它们不是可执行 A 股账户，不受 100 股整手约束。真实用户模拟账户和
Primary 七影子账户继续执行整手、T+1、停牌、涨跌停、现金和费用约束。

## 调度和 API

- `wide_forward_registration`：15:55，由 Scheduler 创建，禁止回填；
- `wide_research_portfolio_mark`：16:05，只有自然到期且能取得 T+1 开盘和到期日
  收盘点时数据时才更新净值；
- `GET /api/wide-forward/experiments`；
- `GET /api/wide-forward/batches`；
- `GET /api/wide-forward/batches/{batch_id}`；
- `GET /api/wide-forward/scorecard`；
- `GET /api/research-portfolios`；
- `GET /api/research-portfolios/{portfolio_id}`；
- `GET /api/user-simulator/adoption-outcomes`。

## 收盘后即时前瞻观察

`15:10` 是自动刷新默认时间，不是金融证据的硬性起点。只要满足以下条件，系统可以在
当天收盘后、下一交易日开盘前冻结即时宽样本：

- 使用当天最终收盘数据和同日 production/server-observed 点时池；
- 快照 `cutoff_at` 不早于任何纳入字段的观测时间；
- 协议、样本和预测在下一交易日开盘前冻结；
- 明确标记为 `research_only_late_start_forward`，不得冒充收盘前预注册；
- 与严格的 `wide-forward-stratified-v1` 实验、Primary和用户账户隔离。

本地Operator入口：

```powershell
.\.venv\Scripts\python.exe scripts\run_late_start_wide.py 2026-07-21
```

该入口按交易日使用独立协议版本，支持幂等续跑。失败尝试和真实LLM成本会保留，既有
预测不会因恢复而重复调用。

用户订单的研究、AI 建议、是否采纳、交易前价格、成交和费用保存在独立采纳记录
中，固定 `formal_forward_scorecard_eligible=false`。
