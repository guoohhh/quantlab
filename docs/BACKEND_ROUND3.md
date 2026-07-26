# 第三轮后端交付：点时策略证据、前瞻消融与生产运行

> **历史快照**：本文只记录第三轮当时的交付，不是当前功能清单、运行状态或质量报告。当前事实请从 `../README.md`、`../PROJECT_HANDBOOK.md`、代码、数据库和最新机器报告核验。

> 当前状态：第三轮建立点时证据、前瞻采集和后台任务框架；第四轮已收紧公共写入边界、
> 接入服务器权威行情并补上 Worker 可靠性。这里的“生产运行”指可部署的后端运行机制，
> 不等于券商实盘、数据 SLA 或策略已证明未来盈利。

## 策略与数据协议

预注册协议位于 `STRATEGY_ROUND3_PREREGISTRATION.md`，实现版本
`round3-pit-forward-v1`。历史重跑只能写入 `strategy_research_runs`，状态固定为
`research_replay`；前瞻预测只能在 cohort 冻结后登记，并经过真实 `due_at` 才能结算。
同一协议版本不可修改，协议、主数据、每日状态和池快照都有 SHA-256 指纹。

点时资产基础设施覆盖：

- ETF 主数据、上市/退市、每日交易状态、成交额、规模、类别、海外溢价与时差；
- 类别代表按当时成交额和规模切换，不再用今天固定六只 ETF 回测过去；
- A股 V4 的历史 ST、停牌、退市、代码 lineage、行业/市值/相关性约束和 Top-K；
- 可转债余额、强赎、评级、到期/退市、停牌与流动性，关键字段缺失即不可投；
- ETF 点时回放、A股 V4 成本后 Rank IC/回撤/换手/区块 Bootstrap；
- 可转债 Walk-forward、显式交易成本估算模型和简单双低基准。

所有历史结果继续标记 research，失败追加保存。可转债系统内部订单预算仍为 0；A股 V4 和新点时
ETF 主动策略不会因历史重跑自动准入。

## 前瞻消融

冻结七个变体：简单基准、纯量化、原始 LLM、统计模型、LLM+统计融合、LLM 交易门控和
完整系统。分别统计 5/20 日及账户 scorecard：Brier、Log Loss、准确率、成本后收益、
回撤、换手、相对量化/简单基准增量、实际触发次数和数据/角色完整性。每个变体/期限少于
30 个真实到期样本时只能是 `forward_shadow`。

仍必须等待真实时间的结论：各变体真实增量、冻结日后的稳定性、可转债影子账户以及
LLM 门控能否降低回撤。公共 API 不能写入冻结时间、到期时间、预测结果或结算结果；
七种变体由服务器执行并冻结，Worker 到期后使用服务器行情计算结果。历史重跑只写入
`research_replay`，不进入 forward scorecard。

## Worker 和调度

任务状态为 `queued/running/cancelled/completed/failed`，依赖失败使用
`failed + dependency_blocked` 作为 blocked 等价终态。`background_jobs` 支持原子 claim、
幂等、依赖传播、进度、独立 lease 心跳、超时、崩溃恢复、指数重试、并发限制、协作式
取消、结果引用和成本预算。已开始但结果不明的副作用不会被另一个 Worker 自动重放。
研究、历史回放、资金刷新、训练、模拟结算、日周期、策略证据和 Chat 长任务都有 handler。

每日依赖图：开盘前摘要；收盘后数据刷新 → 前瞻到期扫描；数据刷新 → 盯市；两者完成
后账户日报 → 通知发送 → 保留清理 → 数据库备份。同日重复运行保持幂等。交易日表可
装载正式日历；缺失时仅使用明确 degraded 的工作日估计。

## Chat 与通知可靠性

多标的、深度追问、历史证据、压力分析、新研究、大 ContextPack 和多 Agent 请求返回
`message_id/job_id`，Worker 写入唯一回复。重复提交、崩溃和重试不会重复回复。用户可
查询进度、事件和取消；完成/失败进入站内通知。

通知 Outbox 支持站内送达、指数退避、死信、幂等、冷却合并、发送历史、静默时段和每日
上限。email、Feishu、desktop 有真实适配器；缺少配置/凭证时保持禁用，不记录占位成功。

## 稳定 API

- `/api/jobs`, `/api/jobs/{id}`, `/events`, `/cancel`
- `/api/runtime/status`, `/schedules`, `/backups`；恢复操作已移到 maintenance-mode CLI
- `/api/point-in-time/security-master`, `/trade-status` 和三类池接口
- `/api/forward-ablation/*`
- `/api/strategies/*/research-jobs`
- `/api/portfolio/smoothed-rebalance`
- `/api/notifications/channels`
- 原有 ContextPack、资金流、委员会、模拟交易和 Chat API 保持兼容

第四轮有意移除了公共在线数据库 restore，并收紧了模拟交易与前瞻接口；具体破坏性变化
见 `BACKEND_ROUND4.md`。

## 仍未准入和延期

未准入：可转债、A股 V4、新点时 ETF 主动轮动，以及任何未满 30 个真实样本的 LLM/
统计变体。已有历史支持的 ETF 核心策略仍受原准入协议约束。

按任务要求继续延期：前端 UI 重构、多用户 SaaS、券商自动交易、复杂盘口撮合、通用
自由 Chat、通知渠道 UI，以及为追求非核心功能覆盖而扩张产品边界。

## 能力状态

- **研究计算框架**：点时池、A 股 V4、可转债证据、七变体消融和成本估算已实现。
- **已形成自动闭环**：长任务 Worker、调度、前瞻到期扫描、模拟账户盯市、站内通知和备份已实现。
- **等待真实时间验证**：每个变体/期限未满 30 个真实到期样本前只能是 `forward_shadow`。
- **未生产准入**：可转债、A 股 V4、新点时 ETF 主动轮动和 LLM/统计交易增量仍为 0 预算或影子状态。

## 最终质量门禁

- 326 项测试全部通过；
- Ruff 与 compileall 通过；
- 总覆盖率 76.52%（终端显示 77%），最低门槛 71%；
- 新增 Job、策略证据、Worker、调度、通知和点时策略模块均通过独立关键覆盖率门槛；
- Streamlit 24 个标签页、0 异常，证明本轮后端改造没有破坏现有前端；
- 硬编码密钥扫描和导出报告敏感字段扫描通过；
- `git diff --check` 通过，仅有 Windows CRLF 提示，无空白错误。

机器可读门禁：`data/reports/quality-gate-latest.json`；覆盖率明细：
`data/reports/coverage.json`。
