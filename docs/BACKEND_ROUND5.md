# QuantLab 第五轮后端交付：科学前瞻、独立影子账户与可信数据

> **历史快照**：本文只记录第五轮当时的交付，不是当前功能清单、运行状态或质量报告。当前事实请从 `../README.md`、`../PROJECT_HANDBOOK.md`、代码、数据库和最新机器报告核验。

> 第六轮已补上自动免费源适配、代码指纹 readiness、持续运行进程和五入口前端。本文保留第五轮冻结设计；当前运行与质量状态以 `BACKEND_ROUND6.md` 和 `quality-gate-latest.json` 为准。

## 1. 本轮结论

第五轮完成的是“实验可信度和真实投资者闭环”，不是新策略、更多 Agent 或前端页面。

工程上已经具备：

- 不允许人工挑选股票进入正式成绩单的 primary forward experiment；
- 每个交易日由调度器自动登记固定数量候选，并保留成功、失败、缺失和跳过记录；
- 七套现金、订单、成交、持仓和净值互相隔离的影子账户；
- `test` 到 `exchange_or_broker_confirmed` 的六级数据可信等级；
- 正式交易日历、行业归属和点时资产池的 production/research/test 隔离；
- `ResearchBarService` 与 `ExecutionQuoteService` 的研究/执行行情分工；
- 与模拟盘、影子盘和历史研究严格隔离的只读真实投资者组合；
- CSV 导入、盯市、AI 检查卡、用户采纳和 5/20 日结果关联；
- Worker、调度、统一迁移、反例测试和覆盖率门禁。

金融结论仍然是：**工程链路完成不等于策略已证明盈利。** 当前正式前瞻到期样本数为 0；必须等待真实交易日、满足 cohort 可信等级的数据和真实到期时间。

## 2. 数据库与迁移

统一迁移顺序定义在 `src/quantlab/persistence/migrations.py`：

```text
simulator → chat → notifications → evidence → strategy_evidence → jobs → round5
```

第五轮相关版本：

| 组件 | 版本 | 作用 |
|---|---:|---|
| `evidence` | 6 | 行业归属增加 namespace、trust level 和 manifest 边界 |
| `evidence` | 7 | 相同 LLM 请求增加数据库级跨 Worker 租约，避免并发重复付费 |
| `strategy_evidence` | 5 | 点时主数据/状态/资产池增加可信等级；前瞻预测增加登记来源 |
| `round5` | 1 | 正式实验、可信 manifest、七影子账户和投资者组合 |

`round5` 新增的主要表：

- `trusted_data_manifests`、`trusted_calendar_days`、`trusted_industry_membership`；
- `forward_experiment_protocols`、`primary_cohort_governance`；
- `forward_registration_runs`、`forward_registration_samples`；
- `forward_milestone_scorecards`、`manual_forward_explorations`；
- `shadow_accounts/orders/fills/positions/nav/events`；
- `investor_portfolios/imports/import_rows/positions/trades/nav`；
- `investor_recommendations/recommendation_adoptions/recommendation_outcomes`。

旧库只要存在尚未安装的当前组件版本，就会先用 SQLite online backup 自动备份，不再只检查“是否首次进入 Round5”；迁移使用组件、版本、顺序和 checksum 登记。重复执行幂等，checksum 冲突失败。实现位置：

- `src/quantlab/persistence/migrations.py`
- `src/quantlab/persistence/round5.py`
- `tests/test_round5_scientific_forward.py`
- `tests/test_backend_rounds_contract.py`

## 3. 正式前瞻实验协议

当前 primary protocol 是 `primary-forward-v2`，只能由非 backfill 的当日 `forward_sample_registration` 调度任务首次启动并原子冻结。公共 API 不能选择实验起跑时间；启动时还会冻结 `activation_origin=scheduler` 及 job/schedule/run-date 引用。V2 相比早期草稿进一步冻结具体 LLM 配置、统计模型 ID、角色策略快照和全部融合/阈值/动作规则：

- 开始时间和 cohort；
- 资产范围与每日采样规则；
- 固定候选数；
- 5/20 交易日周期；
- 模型、Prompt、策略和治理版本；
- 初始资金；
- 成本、整手、T 日收盘信号和 T+1 开盘撮合规则；
- 缺失开盘价保持 pending；
- 最低数据可信等级；
- 10/20/30 到期样本里程碑；
- 协议变更必须创建新 cohort 并留下治理记录。
- 被替换的 protocol 不能再次晋升为 primary，必须使用新版本并保留原治理历史。

正式登记只能使用：

1. production namespace 的可信交易日历；
2. production namespace、达到 cohort trust floor 的点时资产池；
3. 冻结规则自动选出的全部候选；
4. 服务器行情、ContextPack、当前策略/统计模型/LLM 流程实际产生的七种预测。

休市日、未来日期、缺少可信日历或资产池都会生成失败 run，并为每个预期候选和周期保存失败样本占位，不能静默删除。人工指定 symbol 只能进入 `manual_forward_explorations`。operator backfill 明确不能产生 primary forward evidence。

核心代码：

- `src/quantlab/workflows/forward_experiment.py`
- `src/quantlab/workflows/forward_ablation.py`
- `src/quantlab/runtime/scheduler.py`
- `src/quantlab/runtime/worker.py`

## 4. 七个独立影子账户

| 账户 | 冻结定义 |
|---|---|
| `simple_baseline` | 固定、简单、预注册的基准概率与仓位，不读取 quant score |
| `quant_only` | 只使用冻结量化信号，不调用 LLM |
| `raw_llm` | 单一原始 LLM Prompt，不使用委员会、统计模型或角色权重 |
| `statistical_model` | 只使用正式 governed 的统计模型；未准入时持有现金 |
| `llm_stat_fusion` | 只融合 raw LLM 与统计模型 |
| `llm_trade_gate` | 量化产生交易意图，LLM 只能通过、阻止或减仓 |
| `full_system` | ContextPack、委员会、统计模型、组合风险和确定性上限 |

七个账户共享候选集合、信号时点、初始资金、成本和撮合规则，但不共享现金、订单、成交、持仓或结果。

影子交易的关键语义：

- T 日收盘信号，最早 T+1 的可信开盘价成交；
- 无精确可信开盘价时订单保持 pending；
- 多订单按数据库事务读取最新现金和冻结现金，不能重复预留或透支；
- pending 卖单会从后续可卖数量中扣除，避免重复占用；
- 交易成本来自实际 fill；
- 换手来自实际成交金额；
- 最大回撤来自逐日 NAV 路径；
- 同日重复盯市幂等，不把当日盈亏改写为 0；
- 缺价保留上次价格并记录 stale event。

实现与测试：

- `src/quantlab/workflows/shadow_trading.py`
- `src/quantlab/persistence/round5.py`
- `tests/test_round5_scientific_forward.py`

## 5. 数据可信等级与摄取边界

| 等级 | 含义 | 默认用途 |
|---|---|---|
| `test` | 测试夹具 | 只允许 test namespace |
| `user_imported` | 用户上传 | research，不得进入正式 scorecard |
| `research_external` | 外部研究数据 | research，不得进入正式 scorecard |
| `server_observed` | 服务器从普通数据源观测，无 SLA | 研究、降级模拟；是否允许正式实验由 cohort 冻结 |
| `trusted_licensed` | 许可和来源稳定的正式数据 | 可作为更高可信正式输入 |
| `exchange_or_broker_confirmed` | 交易所或券商确认 | 最高可信等级 |

每个正式摄取批次保存 provider、source、endpoint、source version、fetched/available time、license、trust level、namespace、payload/raw fingerprint、记录数、日期范围、状态、缺失原因和失败记录。

服务器只从以下配置路径摄取 production 数据：

- `runtime.trusted_calendar_path`
- `runtime.trusted_industry_path`
- `runtime.trusted_pit_pool_path`

未配置、文件不存在、格式错误或部分记录无效都会生成 `unavailable`、`failed` 或 `partial` manifest，不会用代理值或 public API 上传内容冒充正式数据。public calendar、行业计算和点时池上传仍可用于研究，但固定写入 research/user_imported。

实现位置：

- `src/quantlab/domain/data_governance.py`
- `src/quantlab/workflows/trusted_data.py`
- `src/quantlab/market/calendar.py`
- `src/quantlab/persistence/round5.py`

## 6. 研究行情与执行行情

`ResearchBarService` 用于历史日线、点时研究、前瞻到期结算和影子账户次日开盘。

`ExecutionQuoteService` 用于最新价格和可执行性判断，并检查：

- symbol 和请求日期一致；
- 不能返回未来行情或未来 `available_at`；
- 数据可信等级；
- 多 Provider 价格偏差；
- session status；
- 停牌和数据质量；
- execution quote 时间延迟。

只有 `session_status=open`、价格足够新、没有 actionability reason 的执行行情才能使 AI 建议标记为 actionable。普通免费日线明确是 `server_observed/no SLA`，不能冒充实时执行行情。

## 7. 只读真实投资者组合工作流

```text
创建只读组合
→ 下载/填写 positions 或 trades CSV
→ 预览、字段校验、重复检测
→ 用户显式确认
→ 服务器行情自动盯市
→ 生成持仓、现金、盈亏、行业、集中度和回撤
→ AI 生成只读交易前检查卡
→ 用户记录采纳/部分采纳/拒绝和外部真实成交
→ 系统结算 5/20 日产品效果
```

初始持仓快照会把 `现金 + 持仓成本` 冻结为初始权益，避免把导入持仓误计为当日盈利。同日重复盯市幂等；行情不可用时保留上次价格并标记 stale。

AI 检查卡包含动作、数量区间、操作后仓位、现金变化、最大计划损失、支持/反对证据、失效条件、数据可靠性和可操作状态。它不会发送券商订单。用户组合结果固定标记为产品效果数据，不进入正式策略前瞻实验或训练集。

实现位置：

- `src/quantlab/workflows/investor_portfolio.py`
- `src/quantlab/api/app.py`
- `src/quantlab/api/schemas.py`

## 8. 收盘后调度依赖

```text
可信数据刷新
  → 资金流刷新
  → 前瞻到期结算
  → primary 样本自动登记
  → 七影子账户执行与盯市
  → 模拟账户盯市
  → 投资者组合盯市与建议结果结算
  → 账户日报
  → 通知发送
  → 数据保留清理
  → 数据库备份
```

调度定义在 `src/quantlab/runtime/scheduler.py`，handler 在 `src/quantlab/runtime/worker.py`。缺少 production 可信日历时，除可信数据刷新外的正式交易日任务 fail-closed。

## 9. API 兼容性变化

新增：

- `/api/forward-experiments/*`
- `/api/shadow-accounts*`
- `/api/investor-portfolios*`
- `/api/investor-imports/{import_id}/confirm`
- `/api/investor-recommendations/{recommendation_id}/adoption`

边界变化：

- `POST /api/forward-ablation/samples` 仍兼容旧请求，但只创建 manual exploration；
- `POST /api/forward-ablation/cohorts` 和 `POST /api/forward-experiments/primary/ensure` 固定返回 403，公共客户端不能启动正式 cohort；
- `POST /api/forward-experiments/registration-jobs` 固定返回 403，正式登记只能来自调度器；
- `POST /api/simulator/orders` 会在确认时重新获取服务器行情；若行情指纹已变化，返回 409 并要求重新运行交易前检查；同一幂等键的重复确认仍返回原订单；
- public trading calendar 和 public 点时数据只能写 research/user_imported；
- investor adoption 新增可选 `actual_trade_date` 和 `transaction_cost`；
- 没有新前端 UI，也没有券商自动交易。

## 10. 反例和质量证据

第五轮主要反例位于 `tests/test_round5_scientific_forward.py`、`tests/test_round3_api.py` 和 `tests/test_backend_rounds_contract.py`，覆盖：

- 正式日历缺失或休市日不能登记；
- 失败预期样本不能消失；
- operator backfill 不能进入正式成绩；
- 人工 symbol 不能进入正式 scorecard；
- simple baseline/raw LLM/统计模型定义隔离；
- 七账户、订单、持仓和净值隔离；
- 多候选不能重复预留现金；
- 无可信开盘价保持 pending；
- 最大回撤、换手和成本来自真实账本；
- 同日重复盯市幂等；
- public calendar/PIT 不污染 production；
- execution quote 过期、unknown session 和多源价格冲突阻断；
- trusted refresh 的 completed/partial/unavailable/failed manifest；
- CSV 校验、重复导入、手工交易、盯市、建议、采纳和 outcome；
- 新库、旧库、重复迁移、checksum 和备份恢复契约。
- 公共 API 不能选择 primary cohort 启动时间，被替换的 protocol 不能复活；
- 订单确认行情变化必须重新检查，幂等重放不能被后续行情变化破坏；
- 相同 LLM 上下文在两个 Worker 并发执行时只发生一次真实 Provider 调用；
- 已安装 Round5 的数据库在新增 Evidence 迁移前仍会生成可校验备份。

第五轮当时的实测结果（历史快照，不是当前门禁）：

| 门禁 | 结果 |
|---|---|
| Ruff | 通过 |
| compileall | 通过 |
| pytest | 当时 369 项通过；第六轮当前为 392 项 |
| 总覆盖率 | 77.5%，高于 71% 门槛 |
| `market/calendar.py` | 94.2% |
| `market/quotes.py` | 80.7% |
| `persistence/round5.py` | 86.8% |
| `forward_experiment.py` | 85.5% |
| `forward_ablation.py` | 83.0% |
| `shadow_trading.py` | 86.0% |
| `investor_portfolio.py` | 90.2% |
| `trusted_data.py` | 92.0% |
| `runtime/worker.py` | 至少 80%（硬门禁） |
| `workflows/simulator.py` | 至少 80%（硬门禁） |
| Streamlit AppTest | 当时 24 个工程标签页；第六轮改为普通模式五入口并保留高级模式 |
| 密钥/敏感字段扫描 | 通过 |
| quality gate | `QUALITY GATE PASSED` |

早期 Round5 升级备份仍保留；当前迁移器会为任何缺失的当前组件版本生成带 SHA-256 的 `pre-migration` 备份，全部版本已安装后重复运行不会再次备份。机器结果见 `data/reports/coverage.json` 和 `data/reports/quality-gate-latest.json`。

## 11. 当前不能宣称的内容

- 不能宣称七影子账户已经证明 LLM 或完整系统有正收益增量；
- 不能把历史回放或手工 exploration 当作正式前瞻成绩；
- 不能把 `server_observed` 免费数据描述成交易所权威或有 SLA；
- 不能把投资者手工记录的真实组合结果用于正式策略训练或 scorecard；
- 不能宣称已接券商或自动执行真实订单；
- 不能在未积累至少 30 个真实到期样本前称为 measured；
- 不能把工程完成度等同于未来盈利保证。

当前必须等待的外部条件是：许可清晰且持续可用的日历/行业/点时池/行情数据、真实交易日持续运行、5/20 个交易日自然到期，以及足量不可回写样本。
