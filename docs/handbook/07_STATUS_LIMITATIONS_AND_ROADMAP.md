# 当前限制与动态验收

本文只记录稳定限制、验收方法和声明边界，不记录测试总数、CLI/API 数量、数据库行数、自评分、
覆盖率快照或某一天的运行状态。此类值会随代码、数据和时间变化，必须从当前机器证据重新获取。

## 1. 当前定位

QuantLab 是面向个人投资者的 AI 辅助研究、模拟交易和决策复盘系统。用户保留最终决定；系统组织
证据、反证、失效条件、组合影响、确定性风控、模拟成交和后续结果，但不连接券商自动交易，也不
承诺收益。

默认声明边界是：

> 工程链路是否可运行、历史研究是否有参考价值、前瞻预测是否有效和策略是否存在可交易 Alpha，
> 是四个不同结论。缺少当前、匹配指纹且自然到期的证据时，只能声明已验证到相应边界。

## 2. 动态事实从哪里获取

任何“当前”“已通过”“已运行”“已准备好”结论都应记录观察时间、配置与数据库路径，并至少核对：

| 事实 | 当前证据源 | 不能单独证明什么 |
|---|---|---|
| 源码工程质量 | 当前执行的 `scripts/quality-gate.ps1` 与匹配源码指纹的 latest report | 策略有效或服务长期稳定 |
| Runtime 健康 | `quantlab runtime-status`、进程心跳、`/api/health` | 业务任务成功或长期 SLA |
| 正式收集 readiness | `/api/runtime/readiness`、blockers、当前质量指纹 | Alpha、策略准入或盈利能力 |
| Trusted Data | Provider selection、manifest、provider market date、字段覆盖、PIT fingerprint | 只看 Provider health 或总行数不能证明正式可用 |
| Scheduler | schedule run、run date、attempt、is_backfill、依赖和 job 终态 | `job.status=completed` 不能证明业务结果合格 |
| 前瞻证据 | 冻结协议、自然注册样本、到期结果、账户 NAV 和消融 scorecard | 历史回放、Demo 或 recovery 不能替代自然窗口 |
| 数据库可靠性 | SQLite quick/integrity check、foreign key check、在线备份、verify 和 restore dry-run | 单次检查不能证明未来不会损坏 |

历史交付文档、旧报告和人工叙述只能解释当时发生了什么，不能覆盖当前代码和数据库。

## 3. 当前稳定限制

### 3.1 数据与外部依赖

- 免费市场数据没有商业 SLA，可能超时、限流、返回空结果或改变字段；
- 当前日期、行业、成交额、换手率、市值、ST、停牌和交易状态必须逐字段验证，不能用证券总数代替；
- production 点时池必须保留来源、market date、cutoff、`available_at` 和 fingerprint；晚于 cutoff 的
  字段不能作为严格点时证据；
- 缺失资金、财务、宏观、监管或事件证据时，系统应明确 unavailable/partial，而不是推断或补写；
- 用户上传、Demo、research 和 test namespace 不能提升为正式 production 证据。

### 3.2 Runtime 与部署

- 当前支持单机 SQLite 和本机进程主管，不是多主机高可用平台；
- Streamlit 与四个后台进程分别启动，进程存在不等于数据和任务健康；
- Windows 自启需要用户显式安装，短时 Soak 不能外推为长期稳定性；
- 通知渠道在缺少用户凭证时保持禁用；系统不能假装已经外部送达；
- 数据库、WAL、任务、审计和观察记录会增长，需要动态监控磁盘、备份年龄和 retention。

### 3.3 真实交易边界

- 没有券商连接，也不从券商自动同步委托和成交；
- AI 建议、交易前检查和订单草稿不能自动确认；
- 真实成交只能由用户在外部券商执行后手工登记；
- 免费执行行情不可用、过期、冲突或交易状态未知时，建议必须降级为 review/blocked；
- 用户模拟账户、系统影子账户和外部手工账本必须保持隔离。

### 3.4 研究证据缺口

以下问题在当前匹配指纹的证据没有明确通过前，一律视为未解决：

1. **自然前瞻时间不足。** 历史回放可以发现工程和研究错误，但不能制造未来；正式 5/20 交易日
   结果必须自然到期。
2. **主动策略增量未被默认承认。** 正收益不等于 Alpha；必须与同资产范围、同成本、同执行约束的
   可投资基准比较，并给出回撤、稳定性和统计不确定性。
3. **LLM 交易增量未被默认承认。** 需要冻结规则下比较纯量化、统计模型、原始 LLM、融合与门控，
   分开验证概率质量和成本后组合结果。
4. **资产池点时性需要持续证明。** ETF 代表选择、A 股全市场成员、ST/停牌/退市和行业市值暴露
   都必须使用当时可得信息，不能从今天的成员倒推历史。
5. **可转债和其他扩展资产不能借用 A 股或 ETF 证据。** 各资产域需要独立数据、成本、规则、基准
   和前瞻成绩单。
6. **外部有效性仍需时间与对抗审查。** 单一市场区间、单一 Provider、单一模型或内部评审不足以
   支撑稳定收益声明。

## 4. 为什么不能宣称 Alpha

除非当前机器证据同时满足预注册协议，否则不能对外宣称“已证明 Alpha”“稳定盈利”“显著跑赢”
或“LLM 提高收益”。最低证据结构包括：

- 研究问题、资产池、阈值、成本、基准和停止条件在观察结果前冻结；
- 样本来自自然 Scheduler 路径，非 backfill、Demo、人工补写或事后恢复伪装；
- 5/20 交易日结果自然到期，并达到冻结协议规定的最低样本要求；
- 使用真实可投资基准、费用、滑点、交易规则、成交率、换手和最大回撤；
- 报告置信区间、不同市场状态和失败区间，而不只选择最好窗口；
- LLM 与 Agent 的增量通过消融证明，不把减少全部交易误当成智能提升；
- 数据字段满足严格 PIT invariance，报告指纹与当前源码和数据身份对应；
- 独立评审没有未关闭的高严重级别证据或风控问题。

任一项缺失时，允许的表达应收缩为“工程闭环已验证”“历史研究有参考价值”或“前瞻收集中”，并
明确尚未证明的部分。

## 5. 动态验收清单

### 5.1 工程验收

- 在当前 checkout 运行完整质量门禁；
- latest report 的源码 fingerprint 与当前输入一致且未过期；
- 失败项必须保留，不能删除测试、降低阈值或复用旧报告；
- 文档中的命令、API 路径和配置字段与当前 `--help`、代码和 schema 一致。

### 5.2 Runtime 验收

- 记录配置路径、生产数据库绝对路径、时区和观察时间；
- API、Worker、Scheduler、Notification Worker 的实例、PID、心跳和停止状态可解释；
- Scheduler 延迟、队列、重试、死信、通知 outbox、磁盘和备份均在当前阈值内；
- 重启后状态从数据库恢复，不依赖进程内存；
- 只把实际保存的 Soak 区间声明为连续运行证据。

### 5.3 Trusted Data 与 Readiness 验收

- 信号日等于服务器市场日期，且 production calendar 明确该日是否开市；
- 日历覆盖冻结协议要求的未来交易日；
- 检查实际选中的 Provider，而不是只看探针健康；
- manifest、market date、response fingerprint、必需字段覆盖和 eligible 数量完整；
- production pool 的每个正式字段 `available_at <= cutoff_at`；
- `start_allowed` 和 `sample_registration_allowed` 的每个 blocker 都有可追溯原因；
- 休市、数据不足、Provider 失败或质量指纹不符时保持 fail closed。

### 5.4 Scheduler 与自然窗口验收

- 核对原始 schedule run、依赖、计划时间、首次 attempt、job 和 result payload；
- 区分普通 run、backfill 和 append-only same-day recovery；
- recovery 可以证明后来恢复，但不能修改首次自然窗口结论；
- `completed` 任务还需检查是否产生符合协议的 PIT、样本、订单、NAV、报告或通知；
- 不手工创建正式 pool、实验、样本或影子账户来通过验收。

### 5.5 研究与 Alpha 验收

- 正式样本身份、冻结版本、注册时间和到期时间连续可追溯；
- 预测指标与组合指标分开报告；
- 成本后策略与可投资基准、纯量化和 LLM 消融同时比较；
- 未到期、失败、跳过和不可用样本保留在分母和审计中；
- Demo、Historical Replay、用户模拟和真实手工账本不进入正式 scorecard；
- 结论只覆盖实际样本、市场和时间区间，不外推为收益保证。

### 5.6 备份恢复验收

- 使用 SQLite online backup，而不是复制活动数据库文件；
- 保存 backup path、SHA-256 和 manifest；
- backup verify 和 restore dry-run 均通过；
- dry-run 明确生产数据库未修改；
- 正式恢复前停止 Runtime，恢复后重新检查完整性、迁移、心跳和 readiness。

## 6. 证据驱动路线图

路线图按未关闭的验收条件排序，不按历史开发轮次排序。

### P0：保证黑客松演示可理解且必成

- 建立隔离、可重置、不污染 production 的完整 Golden Path；
- 修复移动导航、遮挡、开发工具栏和主要空状态；
- 三分钟内展示“发现 → 研究与反证 → 用户确认模拟订单 → 成交/费用 → 复盘”；
- 数据失败时展示明确 blocker 和安全退路，不伪造 readiness 或成交。

### P0：保持正式证据链自然运行

- 在真实交易日由 Scheduler 自然刷新 trusted data 和注册前瞻证据；
- 对首次 attempt、Provider selection、PIT cutoff 和 readiness 做事件时间验收；
- 等待 5/20 交易日自然到期，不 backfill、不补样本、不改标签；
- 持续检查七影子账户、宽样本研究和用户账户之间的隔离。

### P1：证明或否定研究增量

- 按冻结协议完成策略、基准、成本压力和市场状态比较；
- 完成纯量化、统计模型、原始 LLM、融合和门控消融；
- 补齐资产池成员选择、退市/ST、行业/市值暴露和可转债独立证据；
- 对失败结果执行版本化 postmortem，不为提高成绩反复打开同一 holdout。

### P1：提高运维成熟度

- 建立数据库与 WAL 增长预算、retention、归档和备份恢复演练；
- 验证重启、任务恢复、通知重试和长时间 Soak；
- 收紧部署包、配置模板、Git/CI 和一键验收；
- 公开部署前补齐认证、授权、HTTPS、密钥轮换和多用户隔离。

### P2：只在证据和用户价值需要时扩展

- 新 Agent、新模型、新策略和新资产必须绑定明确的消融问题；
- 券商连接、自动实盘、多租户或多主机部署需要独立安全与合规设计；
- 不以功能数量替代核心流程、证据质量和用户可理解性。

## 7. 验收记录模板

每次动态验收至少保存：

```text
observed_at:
checkout/revision:
source_fingerprint:
config_path:
database_path:
commands_or_queries:
schedule_run_and_attempt:
provider_and_manifest:
result:
blockers:
claim_boundary:
```

`result=passed` 必须同时写明“通过了什么”和“没有证明什么”。无法取得当前证据时，应报告 unknown
或 fail closed，而不是沿用本文、旧报告或上一次对话中的数字。
