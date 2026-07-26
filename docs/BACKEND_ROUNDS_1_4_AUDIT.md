# QuantLab 第一至第四轮后端完整审查

> **历史审计**：本文只能证明当时审查过的实现与边界，不能证明当前运行状态、最新测试结果或盈利能力。请以当前代码、数据库和机器报告为准。

## 1. 审查结论

本次没有把四份交付文档当作完成证据，而是重新对照原始需求，检查了实际 Schema、API、
领域服务、持久化事务、Chat 工具注册表、通知 Outbox、点时策略证据、LLM 治理、Worker、
调度、迁移和反例测试。

结论如下：

| 轮次 | 工程交付状态 | 仍需外部条件 |
|---|---|---|
| 第一轮 | 完成交付 | 多用户身份体系和券商连接本来就不在范围内 |
| 第二轮 | 完成交付 | 免费源无法保证 ETF 份额、两融、北向、监管和宏观数据持续可用 |
| 第三轮 | 完成交付基础设施 | 前瞻 measured、A 股 V4、点时 ETF 主动策略和可转债仍需真实时间/数据 |
| 第四轮 | 完成交付，并在本次审查中补齐剩余信任与运行旁路 | 生产 SLA、商业数据和券商实盘仍不在当前边界 |

这里的“完成交付”指任务要求的后端代码、数据结构、API、Worker 路径和测试已经实现；
不表示策略已经证明未来盈利，也不表示外部免费数据拥有生产 SLA。

## 2. 审查方法

本次逐项核对：

1. 第一轮原始附件和第二、三、四轮原始任务；
2. `docs/BACKEND_ROUND1.md` 至 `BACKEND_ROUND4.md`；
3. 所有 `/api` 路由和严格 Pydantic 请求模型；
4. SQLite 新库初始化、增量迁移、checksum 和恢复；
5. Chat 工具名称、输入 Schema、权限、确认、超时、成本和数据域；
6. Worker handler、依赖、lease、取消、重试、副作用和调度顺序；
7. 反例测试与全量覆盖率，而不是只检查正常路径。

新增 `tests/test_backend_rounds_contract.py` 固定四轮核心表、API、Chat 工具和 Worker handler，
避免后续开发在不知情时破坏已经交付的后端契约。

## 3. 第一轮验收矩阵

| 要求 | 状态 | 主要实现与证据 |
|---|---|---|
| 三类账户隔离 | 已完成 | `paper_*`、`user_paper_*`、`manual_trades` 独立；用户和手工账户不可进入训练/策略证据 |
| 多模拟账户和新赛季 | 已完成 | `UserPaperTradingRepository`，旧赛季关闭而非覆盖 |
| 完整委托生命周期 | 已完成 | pending、partial、filled、cancelled、rejected、expired 和一对多 fills |
| A 股确定性规则 | 已完成 | `execution/rules.py` 和 `execution/costs.py`，API 不复制金融规则 |
| 资金和持仓并发安全 | 已完成 | `BEGIN IMMEDIATE`、冻结现金/数量、幂等键和账户不变量 |
| 盯市与绩效 | 已完成 | 每日快照、净值、回撤、已实现/未实现盈亏和基准比较 |
| 结构化交易前检查 | 已完成 | 费用、仓位、现金、压力损失、证据、反证、Reviewer 和硬风险输出 |
| 严格 API 与错误脱敏 | 已完成 | `StrictRequest`、统一认证中间件、`safe_error_detail` 和 API audit |
| 受限 Chat MVP | 已完成 | 显式 allow-list、账户绑定、订单草稿、独立确认、有限摘要和引用 |
| Transactional Outbox 通知 | 已完成 | 业务事务与 outbox 同事务；去重、冷却、偏好、已读、归档和过期 |
| 重启/迁移一致性 | 已完成 | SQLite 持久化、兼容 ALTER 和统一迁移注册表 |

第一轮文档中旧的“传入行情”表述已修正。正式模拟交易和 Chat 确认现在只能使用服务器
`QuoteService`；直接注入行情仅允许显式测试环境和 test-only 账户。

## 4. 第二轮验收矩阵

| 要求 | 状态 | 主要实现与证据 |
|---|---|---|
| AnalysisContextPack 2.0 | 已完成 | 每块保存 source、as_of、available_at、抓取时间、质量、降级、缺失、版本和指纹 |
| 截止时间和防未来数据 | 已完成 | block 校验、事件/财务 available_at 过滤和 cutoff 反例测试 |
| 大小受控上下文 | 已完成 | 确定性压缩、原始全文排除、LLM payload 字节上限 |
| 市场/行业/个股资金证据 | 已完成 | 可复算计算器、持久化快照、点时行业映射和查询 API |
| 免费源不可用边界 | 已完成 | unavailable、missing reason、last success；signed-turnover 始终标为 proxy |
| 有边界 LLM 委员会 | 已完成 | 多角色证据引用、矛盾、多空情景、动作/仓位、失效条件和确定性裁剪 |
| 角色评价和挑战 | 已完成 | Brier、Log Loss、收益、回撤降低、错误、市场状态、增量、成本和延迟 |
| 角色政策实际生效 | 已完成 | active policy 影响聚合；本次补上按足量市场状态限定 applicability |
| LLM 缓存和成本治理 | 已完成 | Provider/Model/角色模型/Prompt/Schema/治理/Context 隔离 |
| Chat 接入 ContextPack | 已完成 | 事实/量化/LLM/用户假设分离，Context 版本随消息保存 |
| Chat 扩展工具 | 已完成 | 资金、宏观、事件、比较、历史决策、角色表现和通知规则 |
| 工具注册表审计元数据 | 已完成 | 本次补齐所有遗留工具的输入 Schema 和数据域 |
| 资金/数据/LLM 通知 | 已完成 | 持续性、冷却、去重、触发值和来源持久化 |

可靠商业数据不存在时仍返回 unavailable，这是符合第二轮要求的正确降级，不属于未交付。

## 5. 第三轮验收矩阵

| 要求 | 状态 | 主要实现与证据 |
|---|---|---|
| 点时 ETF 可投资池 | 已完成框架 | 主数据、交易状态、流动性、规模、类别代表切换、海外字段和快照指纹 |
| 七变体前瞻消融 | 已完成采集闭环 | 服务器冻结、5/20 日 due、scorecard、30 样本 measured 门槛 |
| A 股 V4 | 已完成研究框架 | 预注册、点时候选、ST/停牌/退市、行业/市值/相关性、Top-K、成本和 Bootstrap |
| 可转债证据 | 已完成研究框架 | 点时池、余额、强赎、评级、到期、流动性、双低基准和 Walk-forward |
| 可转债成本隔离 | 已完成 | 本次新增 `costs.convertible_bond`，不再借用 ETF 成本配置 |
| 动态预算和平滑 | 已完成 | 换仓阈值、漂移、冲击、现金、整手偏差和渐进调整 |
| Job/Worker | 已完成 | submit/claim/progress/cancel/retry/lease/result/幂等/依赖/崩溃恢复 |
| 调度与运行治理 | 已完成 | 交易日、依赖顺序、补跑、运行历史、备份、恢复和状态 API |
| Chat 长任务 | 已完成 | 多标的、研究、深度报告、历史证据和多 Agent 请求进入 `chat_request` job |
| 通知发送 Worker | 已完成 | 站内、email、飞书、desktop adapter；无配置不伪造成功 |
| 晨报/晚报/账户日报 | 已完成 | 调度生成结构化摘要并通过 Outbox 发送 |
| CI、许可和部署 | 已完成 | GitHub Actions、LICENSE、第三方归属、数据许可和部署说明 |

本次关闭了一个第三轮同步旁路：资金 GET 接口现在只查询持久化快照；`refresh=true` 返回
409，所有刷新必须使用 `POST /api/capital-flow/refresh-jobs` 进入 Worker。

## 6. 第四轮验收矩阵

| 要求 | 状态 | 主要实现与证据 |
|---|---|---|
| 模拟交易权威行情 | 已完成 | 公共请求不能提交价格/交易状态；QuoteService 统一执行、盯市、基准和 Chat |
| 测试行情隔离 | 已完成 | 默认关闭、loopback、显式配置、non-authoritative、test-only 账户 |
| 前瞻时间不可伪造 | 已完成 | frozen/registered/due 由服务器生成，公共 API 只接收任务意图 |
| 七预测原子冻结 | 已完成 | 保存起始价、Provider/version、Context、策略/Prompt/治理和预测指纹 |
| Worker 权威结算 | 已完成 | due 交易日行情、收益/成本/换手/回撤计算、pending 原因和不可覆盖 |
| pending 资源冻结 | 已完成 | 买单冻结现金费用，卖单冻结数量，部分成交/撤单/过期正确释放 |
| 资金刷新持久化 | 已完成 | 本次补齐市场与个股异常时 unavailable 快照和 last_success_at |
| LLM 角色政策 | 已完成 | active weight、最低样本、治理版本和适用市场状态进入聚合与审计 |
| 并发 LLM 幂等 | 已完成 | 本次增加同 Context/幂等键跨线程串行化，避免重复付费调用 |
| Worker 失败传播与取消 | 已完成 | dependency_blocked、cooperative cancel、独立 heartbeat 和副作用未知保护 |
| 安全和恢复 | 已完成 | 本地或 Token、在线 restore 移除、维护 CLI、锁和完整性检查 |
| 统一迁移 | 已完成 | component order、checksum、新库、旧库升级和重复执行 |

## 7. 本次审查发现并修复的缺口

1. **前瞻观察时间错误**：行情 `available_at` 可能早于 15:30 due，旧逻辑会让真实样本无法
   结算。现在 outcome `observed_at` 使用 Worker 的服务器 UTC 观察时间，行情可用时间单独审计。
2. **资金失败没有完整数据产品状态**：市场雷达或个股流失败时，旧逻辑可能只让任务失败
   或只写 job result。现在市场、行业和个股均持久化 unavailable、原因和最后成功时间。
3. **资金 GET 存在同步刷新旁路**：现已强制走后台刷新 Job。
4. **角色政策只能全市场状态生效**：现支持 `applicable_regimes`，且晋级前检查该状态至少有
   冻结门槛数量的成熟样本。
5. **并发委员会可能重复付费**：增加同 Context/幂等键跨线程互斥和回收，并发测试验证底层
   Provider 只执行一组角色调用。
6. **可转债借用 ETF 成本**：新增独立可转债成本配置和报告中的成本来源/口径/边界。
7. **Chat 工具元数据不完整**：补齐遗留工具的输入 Schema、数据域、超时和研究成本预算。
8. **第一轮文档仍称客户端可传行情**：已同步为服务器权威行情边界。

## 8. 仍需等待，但不属于四轮代码缺口

- 七变体每个期限至少 30 个真实到期样本；
- A 股 V4、点时 ETF 主动轮动和可转债的前瞻准入；
- LLM 对收益或回撤的真实增量；
- 许可清晰且稳定的 ETF 份额、两融、北向、龙虎榜、行业历史和监管数据；
- 商业数据 SLA、高可用 Worker 集群、多用户 SaaS 和券商连接；
- 前端 UI 重构和通知配置 UI，它们在第三轮明确延期。

这些事项不能通过增加历史重跑或伪造数据“补齐”。下一轮可以直接建立在当前稳定 API、
Schema、Job 和通知契约上继续开发。

## 9. 最终门禁

2026-07-17 四轮合并质量门禁已经完成：

- 326 项测试全部通过；
- Ruff、compileall 和 `git diff --check` 通过；
- 总覆盖率 76.51699%（文档显示 76.52%，终端显示 77%），高于 71% 门槛；
- simulator、forward_ablation、context、capital_flow、worker、jobs 和 chat 的关键覆盖率门槛全部通过；
- Streamlit AppTest 24 个标签页、0 异常；
- 硬编码密钥扫描和导出报告敏感字段扫描通过。

机器可读证据：

- `data/reports/quality-gate-latest.json`
- `data/reports/coverage.json`
- `data/reports/coverage-round4.json`
