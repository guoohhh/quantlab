# QuantLab 第六轮交付：持续运行、生产 Readiness 与五入口产品化

> **历史快照**：本文只记录第六轮当时的交付，不是当前功能清单、运行状态或质量报告。当前事实请从 `../README.md`、`../PROJECT_HANDBOOK.md`、代码、数据库和最新机器报告核验。

## 1. 本轮结论

第六轮没有增加策略或 Agent。它完成了四件事：

1. 修复第五轮发布后的四项真实回归；
2. 把可信数据、执行行情和正式前瞻实验之间的启动边界做成可复算 readiness；
3. 把 API、Worker、Scheduler 和通知 Worker 统一为 Windows 单机持续运行服务；
4. 把默认 Streamlit 界面从工程型多标签页改为五个普通投资者入口，同时保留高级审计能力。

工程闭环完成不等于未来盈利已经得到证明。正式 primary cohort、正式前瞻样本和七影子账户成绩仍必须等待真实数据、真实交易日和 5/20 个交易日自然到期。

## 2. 发布回归的根因与修复

### 2.1 Auto Router 被本机 Key 改变测试结果

根因：测试进程会继承本机 `OPENAI_API_KEY`/`DEEPSEEK_API_KEY`，因此测试中的 `provider=auto` 可能真实调用外部模型；同时 LLM 缓存只保存 Pydantic 正文，缓存命中后会丢失实际 Provider/Model 身份。

修复：

- `tests/conftest.py` 自动隔离外部 LLM 凭证，只有测试自己显式设置的凭证才生效；
- `EvidenceRepository.cached_llm_entry()` 同时恢复结果、Provider 和 Model；
- `GovernedLLMProvider` 在本地缓存和跨 Worker 共享缓存命中时恢复 `_llm_provider`、`_llm_model`；
- Mock、fallback、unknown 和实际降级到 Mock 的路由继续保持 `complete=false`，不能触发正式 shadow order。

代码：

- `src/quantlab/llm/governance.py`
- `src/quantlab/persistence/evidence.py`
- `tests/test_foundation_adversarial.py`

### 2.2 固定日期测试随服务器日期变化失效

根因：三项 Round4 测试固定使用 `2026-07-17`，但正式 cohort 的 `frozen_at` 使用服务器当前 UTC 时间。日期推进后，正确的“禁止历史回填”检查会拒绝这些夹具。

修复：测试使用服务器当前 UTC 信号日动态构造 quote 和 ContextPack；正式 `as_of < frozen_at.date()` 检查未删除、未放宽。

代码：`tests/test_round4_trust_boundaries.py`。

## 3. 可信数据自动运行链路

### 3.1 输入优先级

1. 服务器配置文件仍然最高优先：
   - `runtime.trusted_calendar_path`
   - `runtime.trusted_industry_path`
   - `runtime.trusted_pit_pool_path`
2. 文件未配置且 `trusted_data_auto_refresh_enabled=true` 时，服务器自动尝试 BaoStock/AkShare；
3. public API 上传仍固定进入 research/user_imported，不能污染 production；
4. 免费数据统一是 `server_observed/unverified_no_sla`，不是交易所确认或许可数据。

### 3.2 自动生成内容

- BaoStock 明确交易日历，不使用普通工作日猜节假日；
- BaoStock 上市、退市和证券主数据；
- BaoStock 当日可交易状态；
- AkShare 当前价格、成交额、换手和市值等可获得字段；
- 可获得时的行业归属；
- A 股点时池、流动性排序、eligible/exclusion reason；
- manifest、来源、版本、许可状态、指纹、失败记录；
- 日期覆盖、标的数、字段覆盖、最后成功时间和连续失败次数。

任一字段不可获得时保留 missing/partial/unavailable，不使用代理值冒充正式字段。

代码：

- `src/quantlab/data/baostock.py`
- `src/quantlab/workflows/trusted_data_adapters.py`
- `src/quantlab/workflows/trusted_data.py`
- `src/quantlab/persistence/round6.py`

## 4. 生产 Readiness

`primary_start_readiness()` 检查：

- 质量门禁报告通过、未过期，并且代码/测试/配置指纹与当前工作区一致；
- 信号日等于服务器上海市场日期；
- production 交易日历覆盖信号日和未来 20 个交易日；
- 当日 production A 股点时池存在；
- eligible 数量达到冻结候选数；
- 行业和点时池字段覆盖达到最低要求；
- 正式 LLM Provider 明确，Mock/fallback 不算；
- Worker 和 Scheduler 心跳健康；
- 当日是正式开市日。

结果分为：

- `start_allowed`：允许冻结实验协议；
- `sample_registration_allowed`：同时是开市日，允许登记当日正式样本；
- `blockers`：逐项说明不能启动的原因。

primary 启动规则：

- public API 仍不能启动；
- backfill 仍不能启动；
- 非交易日不创建样本；
- readiness 未通过时 `register_primary_forward_samples()` 返回 skipped，且数据库中不创建 primary experiment；
- 只有 scheduler-owned 当日任务能首次启动。

代码：

- `src/quantlab/runtime/readiness.py`
- `src/quantlab/workflows/forward_experiment.py`
- `src/quantlab/runtime/scheduler.py`
- `src/quantlab/runtime/worker.py`

API：

- `GET /api/runtime/readiness`
- `GET /api/runtime/formal-experiment`
- `POST /api/runtime/trusted-data/refresh`
- `GET /api/runtime/status`

## 5. ExecutionQuoteService 边界

`MarketQuote.quote_kind` 明确区分：

- `realtime`
- `delayed`
- `current_close`
- `previous_close`
- `unavailable`

同时保存：

- `delay_seconds`
- `observed_at`
- `price_deviation_bps`
- `provider_health`
- 来源、版本、available_at、trust level 和 license status。

规则：

- 日线端点只能形成收盘或不可用状态；
- 昨日收盘和当日收盘不能在盘中标记 actionable；
- unknown session、过期、停牌和多源冲突保持 review/blocked；
- 行情确认指纹排除本次请求时间和健康快照等易变运行字段，同一行情可幂等确认，价格或关键状态变化仍要求重新检查。

代码：

- `src/quantlab/domain/trading.py`
- `src/quantlab/market/quotes.py`
- `src/quantlab/execution/rules.py`

## 6. Windows 单机持续运行

统一命令：

```powershell
quantlab runtime-start
quantlab runtime-status
quantlab runtime-stop
```

四个进程：

- API
- Worker
- Scheduler
- Notification Worker

`runtime_processes` 保存 instance、PID、状态、心跳、停止请求和运行摘要。数据库事务租约阻止两个相同进程同时运行；心跳过期后允许恢复。停止流程先请求 cooperative shutdown，超过宽限期才向记录的 PID 发送终止信号。

健康检查包含：

- SQLite quick check 和 WAL；
- 数据源 coverage/readiness；
- Worker/Scheduler/通知/API 心跳；
- 最近调度；
- 通知 outbox；
- LLM 24 小时调用、失败率和费用；
- 数据库备份；
- 磁盘空间；
- primary、正式样本和 overdue pending。

代码：

- `src/quantlab/runtime/service.py`
- `src/quantlab/runtime/readiness.py`
- `src/quantlab/persistence/round6.py`
- `src/quantlab/cli.py`

## 7. 预测成绩与影子交易成绩分离

`formal_experiment_status()` 同时返回两个独立对象：

- `prediction_scorecard`：5/20 日 Brier、Log Loss、方向准确率、校准和样本数；
- `shadow_trading_scorecard`：七个账户的现金、订单、成交、持仓、NAV、收益、真实回撤、换手和成本。

这两个 scorecard 不共享口径。目标权重近似回报不能描述成真实影子账户交易成绩。

## 8. 五入口产品

普通模式只有：

1. 首页：账户、现金、盈亏、持仓、风险、数据状态、待办、最新建议、待确认订单和通知；
2. 行情与发现：市场、行业、资金活跃度、搜索、自选和候选；
3. AI研究：建议卡、支持/反对证据、失效条件、ContextPack、质量和 Chat；
4. 模拟交易：账户、交易前检查、订单确认、持仓、费用、生命周期和净值；
5. 我的：真实只读组合、CSV 导入、盯市、通知、Chat、数据状态和高级入口。

普通模式不展示内部 Agent、Prompt、Brier、模型路由和 Worker 页面。原工程页面未删除，统一保留在高级/审计模式。

代码：

- `dashboard/product_ui.py`
- `dashboard/app.py`
- `src/quantlab/workflows/product.py`

## 9. 产品使用数据边界

`product_usage_events` 可记录建议查看、交易前检查、模拟订单确认、Chat 问题和数据不可用中断等事件。它不保存不必要隐私，并强制：

- `training_eligible=false`
- `forward_scorecard_eligible=false`

因此产品试用数据不能自动进入策略训练或正式前瞻成绩。

## 10. Schema 和迁移

迁移顺序增加 `round6`：

```text
simulator → chat → notifications → evidence → strategy_evidence → jobs → round5 → round6
```

Round6 新表：

- `runtime_processes`
- `trusted_data_source_state`
- `product_usage_events`

版本：`round6:1`。全新数据库和旧数据库增量升级继续走统一 checksum 注册表和升级前备份。

## 11. 测试与覆盖率

主要新增测试：

- 自动可信数据无需手工文件；
- 数据不可用不创建 primary；
- 非交易日不创建正式样本；
- 进程单实例、停止和重启；
- Worker/Scheduler/通知/API 托管循环；
- readiness 成功、失败和代码指纹；
- 实时/昨日收盘分类；
- 预测和 shadow scorecard 分离；
- 产品事件不进入训练/正式成绩；
- 五入口与高级模式 AppTest；
- BaoStock 日历/证券主数据和 AkShare schema 变化。

当前全量：392 项测试通过，总覆盖率约 78%。新增关键模块：

| 模块 | 覆盖率 |
|---|---:|
| `runtime/readiness.py` | 94% |
| `runtime/service.py` | 87% |
| `persistence/round6.py` | 86% |
| `workflows/product.py` | 88% |
| `workflows/trusted_data.py` | 86% |
| `workflows/trusted_data_adapters.py` | 95% |

## 12. 当前真实运行状态

截至 2026-07-18 真实启动、备份和后台刷新后的实际检查：

- SQLite：健康，WAL；
- API、Worker、Scheduler、通知 Worker：均为 running，心跳健康；API 只监听 `127.0.0.1:8000`；
- `/api/health`：`status=ok`，`api_auth=disabled_local_only`，`execution_mode=manual_orders_only`；
- 正式 primary experiment：未启动；
- 正式样本：0；
- 七影子账户：0（只有 primary 启动时才创建）；
- production 交易日历：自动免费源成功写入 491 条，覆盖 2025-07-13 至 2026-11-15，可信等级为 `server_observed/unverified_no_sla`；
- production 行业与当日点时池：免费源本次未返回记录，均持久化为 `unavailable`，各有 manifest、失败原因和连续失败次数；
- readiness：未通过，当前阻断项为当日点时池、候选数量、行业归属和点时池字段覆盖；
- 当前日期为非交易日，不应产生正式样本；
- Scheduler 已按正式日历识别休市，并以 `non_trading_day` 跳过正式登记、结算、盯市和报告任务；
- LLM 路由存在真实端点配置，运行服务已健康；readiness 当前仅受行业和点时池数据阻塞；
- 已创建 SQLite 在线备份及 SHA-256 manifest；
- 真实 `trusted_data_refresh` Job 已由 Worker 完成，运行中 lease heartbeat 正常；部分源失败没有降低门槛或生成替代数据。

## 13. 仍需真实时间或外部条件

1. 自动数据刷新在真实交易日持续成功；
2. 正式日历覆盖未来 20 个交易日；
3. 当日点时池和行业字段达到冻结最低标准；
4. Worker/Scheduler 长期在线；
5. primary 在真实交易日自然首次启动；
6. 5/20 日样本自然到期；
7. 每个变体至少 30 个真实到期样本后才能进入 measured；
8. LLM 是否带来收益增量仍需正式 prediction scorecard 和真实 shadow NAV 同时证明。

系统不会通过历史回填、降低可信等级或制造影子成交来缩短这些等待时间。
