# QuantLab 第九轮交付：可信证据边界、自动决策生命周期与黑客松稳定收口

> **历史快照**：本文只记录第九轮当时的交付，不是当前功能清单、运行状态或质量报告。当前事实请从 `../README.md`、`../PROJECT_HANDBOOK.md`、代码、数据库和最新机器报告核验。

## 1. 本轮结论

第九轮没有增加新策略、人格 Agent、券商连接或自动真实交易，而是修复第八轮审查发现的
信任边界，并把研究、用户决定、论文、结算、Reflection 和后续记忆串成可追溯生命周期。

本轮可以诚实宣称：

- 正式 Reflection 只能由服务器读取已到期、已结算、与 Decision Run 关联的权威结果生成；
- 委员会 LLM 步骤会在调用前原子 claim checkpoint，完全相同输入恢复时不会重复付费调用；
- 投资论文检查不再信任客户端提交的 `supports/contradicts`，而是核验真实 ContextPack；
- 研究、论文、用户决定、订单、结果和 Reflection 可以沿统一 Decision Run 导出审计包；
- 历史演示成绩单固定为 `research_only`，不能进入正式前瞻成绩、训练集或 primary；
- 备份可校验，恢复可在一次性数据库副本中 dry-run；
- 普通用户仍通过五入口使用系统，高级模式保留完整审计能力。

本轮不能宣称：

- 系统已经产生稳定 Alpha；
- LLM 已经带来可重复的交易收益增量；
- 2026-07-19 的非交易日可以替代下一真实交易日验收；
- 历史 Demo 是独立前瞻证据；
- 免费数据源具有商业 SLA；
- 系统已经具备券商自动交易或生产级实盘资格。

## 2. P0 证据边界修复

### 2.1 Reflection 只绑定权威到期结果

`workflows/reflection.py` 不再使用调用方传入的收益、到期时间、基准、MAE/MFE 或交易成本。
这些旧字段即使仍由兼容调用传入也会被丢弃。`Round8Repository.authoritative_outcome()` 从正式
结算表读取冻结样本和结果，并验证：

- source 真实存在；
- source 状态已结算且冻结到期时间不晚于服务器当前时间；
- 结果来自 production、authoritative、server-observed 或更高可信度服务器行情；
- horizon 与冻结样本一致；
- source 通过实体 link、Context 指纹或冻结字段与 run 关联；
- Demo、test、research-only、pending 和 user 边界不能进入正式研究记忆。

底层 `save_reflection()` 也会重新执行权威查询，因此绕过 workflow 直接调用 repository 仍不能写入
伪造正式 Reflection。同一 `source_type + source_id + horizon` 使用唯一约束和幂等读取。

`authoritative_reflection_settlement` 只扫描 `automatic_primary` 的 `full_system` 已结算样本。表或
数据不足时返回 `unavailable`；单个样本失败不会伪造结果。

### 2.2 下一真实交易日验收 fail closed

`next_trading_day_acceptance_report()` 现在从数据库查询真实 registration run，而不是通过
“是否存在实验对象”推断 primary 次数。正式交易日必须同时满足：

- 当日 production 点时池存在、日期一致、候选数和字段覆盖达标；
- selected Provider 属于本次 refresh；
- primary 恰好一次；
- 正式样本大于零；
- 七个互相独立的影子账户均存在；
- 重复 Job、重复正式样本、Demo/test/user 污染均为零；
- Worker、Scheduler、数据与 LLM readiness 健康。

报告状态区分 `passed`、`blocked`、`skipped_non_trading_day`、
`waiting_for_scheduled_jobs` 和 `unavailable`。同日重复生成会更新同一报告，而不会冻结早期的错误
通过状态。当前日期 2026-07-19 是周日，因此不得生成正式交易日通过结论。

### 2.3 真正的调用前 Checkpoint

`ExperimentRecorder` 将 ContextPack、委员会、Reviewer/Context Committee 和最终决策作为独立
可恢复步骤。每个昂贵步骤执行前先原子 claim：

- `completed`：直接重建结构化 Pydantic 结果，LLM 调用数为 0；
- `running`：阻止第二个 Worker 重复执行；
- `failed`：允许重新 claim，只重跑未完成步骤；
- callback 异常：保存 failed checkpoint；
- 取消或失败的顶层 run：按完全相同输入显式恢复，并记录 resume event。

checkpoint 签名包含 source/build fingerprint、workflow 版本、Prompt 版本、ContextPack 指纹、
Provider、模型、角色模型、reasoning effort、角色集合、Schema、治理版本和关键配置。模型、Prompt、
Context 或源码变化都会得到不同签名，不会错误命中旧缓存。

### 2.4 投资论文核验真实 ContextPack

论文检查 API 只接受 ContextPack 身份、指纹、证据引用和用户 resolution。后端验证 ContextPack
存在、指纹、标的、`available_at`、证据块、质量、降级和缺失状态。支持、反对、红线和缺失分类由
结构化证据计算，不接受客户端布尔结论。

不存在 ContextPack 时假设保持 `needs_review`；只有价格上涨不能把论文升级为
`strengthened`；重大监管、处罚、诉讼、欺诈或退市风险优先于价格变化。`ignored` 只影响最终应用
状态，不覆盖底层 proposed status。每次检查保存已验证证据快照和指纹。

### 2.5 稳定源码指纹与版本化幂等

源码指纹由核心源码、配置 Schema 和 Prompt 相关实现计算，不纳入数据库、data、报告和运行产物。
质量门禁指纹只表示该源码是否已验证，不再作为唯一源码身份。研究幂等键包含 workflow/source
版本；合法代码升级会产生新 run，同一冻结版本和输入仍保持幂等。

## 3. P1 自动决策生命周期

### 3.1 结构化 Investment Thesis

用户采纳或部分采纳建议后，系统基于大小受控的 AnalysisContextPack 生成严格 Schema 的论文
草稿。论文包含核心逻辑、3—7 个可验证假设、验证指标、支持/反对引用、检查频率、下一检查日期、
红线、失效条件和估值/价格锚点。

LLM 或证据不可用时使用确定性 `needs_review` 草稿，不补造事实。用户编辑始终创建新 revision；
冻结后旧 revision 不可覆盖。

### 3.2 Worker/Scheduler 生命周期任务

现有 Job 平台已注册或接入：

- `thesis_due_scan`：到期论文生成复核任务；
- `thesis_event_check`：用最新事件 ContextPack 自动复核；
- `thesis_price_invalidation_check`：分离价格变化与事实变化；
- `authoritative_reflection_settlement`：只从权威到期结果生成 Reflection；
- `controlled_memory_refresh`：达到门槛后只进入人工 challenge 资格，不自动改规则。

任务保持幂等、可重试、可恢复；缺数据返回 `needs_review/unavailable`。任何红线只生成提醒和复核
建议，不能自动买卖。

### 3.3 统一 Decision Run

顶层 Decision Run 通过实体链接关联 research、ContextPack、Agent checkpoints、Reviewer、最终
建议、用户决定、交易前检查、订单/外部成交、论文、论文检查、结果和 Reflection。多 Agent 产出的
旧局部 run 会重新绑定统一账本 ID；必要的子 run 保存 `parent_run_id`。

`GET /api/decision-runs/{run_id}/audit-bundle` 导出脱敏、幂等审计包，并展开 checkpoint、恢复历史、
实体链接与可用的 ContextPack、论文 revision、订单和结果快照。Artifact 指纹可用于重复验证。

### 3.4 受控历史经验

ContextPack 的 bounded historical lessons：

- 只读取关联 production/forward-shadow 权威到期结果的候选经验；
- 同标的优先；跨标的最高权重 0.25；
- 限制数量、字节和回看时间；
- 标记 candidate、auxiliary、challenge_eligible 或 rejected；
- 记录每次研究实际使用的 `memory_id`；
- 单条 Reflection 不会自动修改策略、Agent 权重、模型阈值或硬风控。

## 4. P2 产品与运行收口

### 4.1 Provider 能力矩阵

`trading_calendar`、`security_master`、`industry_membership`、`trade_status`、`market_spot` 和
`daily_bars` 均可通过 capability router 选择。Provider 声明 capability、priority、trust、
license 和 version；实际 selected Provider、attempts、fallback 原因与 refresh_id 被持久化。

日线网络探针改为配置启用，避免与当前任务无关的网络调用阻塞 refresh。Sina 缺失的换手率、市值
或行业不会被代理值补齐；缺字段保持 partial 并阻断正式池。旧数据库中的历史 source version
不会被改写，新 refresh 使用当前代码声明的版本。

### 4.2 决策任务中心与上下文 Chat

通知中心之上增加 Decision Task：立即操作、需要复核、仅供了解、系统/数据异常四类任务，支持
`open/acknowledged/resolved/dismissed`、去重、冷却和操作审计。

从研究页进入 Chat 时绑定 symbol、research/decision run 和 ContextPack。Chat 二次确认会重新获取
服务器最新行情；生产环境拒绝客户端行情，测试行情仅在显式 test mode 中使用并标记
`test/non_authoritative`。订单、预警和论文编辑仍只生成草稿，必须独立确认。

### 4.3 五入口与历史 Demo

Streamlit 普通模式保持：`首页 / 行情与发现 / AI研究 / 模拟交易 / 我的`。首页只显示最重要的
决策任务；“我的”提供完整任务中心。普通模式不展示原始 JSON 或未脱敏异常，高级模式保留审计。

`GET /api/historical-scorecards` 返回隔离的历史研究成绩单。历史数据实际抓取时间晚于信号日时，
报告明确保存 `point_in_time_verified=false` 和 lookahead risk；无法核验的策略、统计模型或 LLM
融合指标返回 `unavailable`。该成绩单永远不能进入 forward scorecard。

### 4.4 备份与恢复

新增：

```powershell
quantlab database-backup-verify <backup-path> --expected-sha256 <sha256>
quantlab database-restore-dry-run <backup-path> --expected-sha256 <sha256>
```

verify 只检查校验和与 SQLite integrity；dry-run 在临时数据库副本中执行迁移和完整性检查，不修改
生产逻辑数据。真正 restore 仍要求 maintenance mode、停止 Worker 和显式确认。

## 5. Schema 与迁移

统一迁移注册表中的 `round9` 当前版本为 `3`：

- `round9:1`
  - `investment_thesis_revisions`
  - `decision_run_memory_usage`
  - `decision_tasks`
  - `decision_run_exports`
  - `historical_pit_scorecards`
- `round9:2`
  - `unified_run_resume_events`
  - 审计导出幂等与恢复历史语义
- `round9:3`
  - 冻结 revision 的实际生效指针、不可变假设快照和检查版本绑定
  - Provider refresh、manifest 与 point-in-time pool 的同交易日关联
  - 假设级复评日期、检查调度审计字段和 Decision Task 状态事件
  - 旧论文兼容迁移与系统任务 reconciliation

迁移继续使用 component/version/checksum 注册。全新数据库初始化、旧数据库增量升级、重复迁移、
升级前备份和恢复 dry-run 均有自动化测试。

## 6. API 与兼容性变化

新增或扩展：

- `GET /api/decision-runs/{run_id}/audit-bundle`
- `GET /api/historical-scorecards`
- `GET /api/decision-tasks`
- `PATCH /api/decision-tasks/{task_id}`
- `GET /api/investment-theses`
- `GET /api/investment-theses/{thesis_id}`
- `POST /api/investment-theses/{thesis_id}/checks`
- `POST /api/investment-theses/{thesis_id}/revisions`
- `POST /api/investment-theses/{thesis_id}/revisions/{revision_id}/freeze`
- `GET /api/research-memory/{symbol}`

安全/证据兼容性变化：

- 论文检查不再信任客户端 `supports/contradicts`；
- Reflection 的收益、到期时间和成本字段不再作为权威输入；
- Chat confirm 中的客户端行情在生产环境被拒绝或忽略，测试模式也不会进入正式证据；
- failed/cancelled/blocked Decision Run 只能通过显式、可审计恢复继续。

## 7. 对抗性测试

第九轮新增或强化的反例覆盖：

- source 不存在、未到期、非权威、与 run 不关联或伪造 99% 收益时 Reflection 被拒绝；
- Reflection Worker 重复运行不产生重复记录；
- primary 为 0/大于 1、formal sample 为 0、影子账户不是 7、Schema 缺失时验收不通过；
- 相同 checkpoint 第二次 LLM 调用数为 0；模型、Prompt、Context 或源码变化不复用；
- 不存在、错误指纹、其他标的或未来 ContextPack 不能改变论文；
- 红线优先于价格上涨；用户 ignored 不覆盖 proposed status；
- 用户论文编辑创建 revision，冻结历史不被覆盖；
- 自动论文任务、自动 Reflection、Decision Run 导出和恢复保持幂等；
- 受控记忆执行年龄、数量、字节和跨标的权重限制；
- 历史 Demo 保持 research-only 且明确点时缺口；
- 备份验证和 restore dry-run 不修改生产数据库。

## 8. 质量门禁

最终交付以 `data/reports/quality-gate-latest.json` 和 `data/reports/coverage.json` 为机器证据。
本轮要求并已纳入门禁：Ruff、compileall、442 项 pytest、总覆盖率至少 71%、第九轮关键模块至少
80%、Streamlit AppTest、硬编码 Key 扫描、报告敏感字段扫描和 `git diff --check`。

第九轮关键模块包括：

- `persistence/round8.py`
- `persistence/round9.py`
- `workflows/experiment_recorder.py`
- `workflows/investment_thesis.py`
- `workflows/reflection.py`
- `workflows/decision_lifecycle.py`
- `workflows/decision_tasks.py`

2026-07-19 尾部收口最终机器门禁结果：总覆盖率 79.16%；Round8 repository 85.7%、Round9
repository 89.2%、Experiment Recorder 88.0%、Investment Thesis 90.1%、Reflection 95.1%、
Decision Lifecycle 95.2%、Decision Tasks 85.7%、Worker 81.9%。Streamlit 普通模式 5 个入口、高级模式 19 个标签页，
异常数为 0。后续源码变化后仍应以机器报告为准。

## 9. 证据分层

### 代码和自动化测试已经证明

- 权威 Reflection、ContextPack 论文核验、调用前 checkpoint、统一 Decision Run、论文 revision、
  受控记忆、历史 Demo 隔离和备份 dry-run 的实现与反例边界；
- 五入口普通模式和高级模式可由 Streamlit AppTest 启动；
- 全新建库、迁移与重复执行具备机器测试。

### 本机短时运行可以证明

- 四进程可以启动、心跳、执行调度并 cooperative stop；
- 这只证明本机短时运行，不等于多日稳定性或数据 SLA。

2026-07-19 本轮实测中 API、Worker、Scheduler 和 Notification Worker 全部进入
`running/healthy`，质量报告源码指纹匹配；约 101 秒后 cooperative stop 返回
`stop_requested=4`、`forced=[]`，四进程均记录 stopped。当天是周日，`is_trading_day=false`、
`start_allowed=false`、正式实验和正式样本均为 0，系统没有伪造交易日成功。

### 历史点时回放只能证明

- 产品流程、成本模型、报告隔离和结果可复现；
- 如果 `point_in_time_verified=false`，不能把该案例称为严格点时验证；
- 即使历史收益为正，也不等于未来盈利。

### 仍需真实时间或外部数据

- 下一真实交易日的 production 点时池、primary 恰好一次和七影子账户验收；
- 5/20 个交易日自然到期的至少 30 个正式样本；
- LLM/统计融合相对纯量化的真实增量；
- A 股 V4、可转债和主动 ETF 的正式准入；
- 免费数据源长期稳定性、字段许可和商业 SLA。

## 10. 黑客松演示边界

建议 3—5 分钟路线：

```text
首页任务与数据状态
→ 行情与候选
→ AI 支持证据、反对证据和失效条件
→ 交易前检查
→ 用户二次确认模拟订单
→ 成交、费用、持仓与盈亏
→ 投资论文、红线与复评任务
→ research-only 历史成绩单
→ Live readiness 与正式前瞻边界
```

现场数据源失败时可以使用冻结 Historical Research Demo，但必须展示其 `research_only` 和
`point_in_time_verified` 状态，不能把备用演示改写为 Live 或正式前瞻结果。

## 11. 第九轮尾部收口

本次尾部收口把四条链路冻结为数据库可审计契约：

1. 论文初建状态是 `draft_pending_confirmation`。只有用户冻结 revision 后，核心逻辑、3—7 条假设、
   验证指标、证据引用、频率、红线、估值锚点、日期和 data provenance 才在同一事务中成为当前生效论文。
   未冻结论文不会进入 active 列表、到期扫描、事件/价格自动检查、首页风险任务或正式记忆。
2. 每次论文检查永久保存当时的 revision ID 和 fingerprint。后续草稿不会影响当前自动检查，新 revision
   也不会改写历史 check；没有冻结版本时明确返回 `waiting_for_user_confirmation`。
3. 成功检查按可信生产交易日历推进假设级日期：daily/weekly/monthly/quarterly 约为 1/5/20/60 个交易日；
   event-driven 和 manual 不制造固定日期；红线进入下一交易日附近复核。ContextPack 或交易日历不可用时不推进。
4. 到期任务身份固定为 `thesis_id + frozen_revision_id + due_at`；重复扫描保持幂等，成功检查后旧任务自动 resolved。
5. Provider 验收只读取验收交易日 production pool 的 refresh，并要求六个组件齐全、Provider 非空、能力和
   source version 完整、时间不来自未来、manifest/snapshot/fingerprint/refresh 一致。server-configured file
   和 fallback 可以通过，但必须保留选择原因、attempts 和 failures。
6. Decision Task 只自动对账 `system_managed` 条目：条件消失时 open/acknowledged 自动 resolved；dismissed
   同指纹不重开，新证据指纹可形成新任务；所有状态变化保留 actor、reason、evidence fingerprint 和时间。

隔离生命周期测试已覆盖“建议 → 用户采纳 → 论文草稿 → 冻结 → 到期任务 → 已保存 ContextPack 检查 →
日期推进 → 任务关闭 → Decision Run 审计包”。该数据库和 revision provenance 明确标记
`research_only/test_only`，不会进入 production primary、正式 scorecard 或正式研究记忆。

这次收口证明的是后端状态与审计语义，不是稳定 Alpha。2026-07-19 仍是休市日，正式 Provider 交易日验收、
下一次 production primary 和 5/20 日样本自然到期仍必须等待真实时间。

## 12. 后端冻结最终修复

本节是第九轮尾部审查后的最终修复记录，并覆盖前文仍引用 `round9:3` 或 442 项测试的旧机器快照。

### 12.1 可信调度

- `check_frequency` 的数据库、Pydantic、LLM 结构化输出和调度内部值固定为 `daily`、`weekly`、
  `monthly`、`quarterly`、`event_driven`、`manual`。中文频率只在输入边界规范化，未知值直接校验失败。
- ContextPack 缺失或指纹错误、空引用、过期/缺失质量、neutral、未绑定假设的证据都不会推进日期。
  部分假设核验成功时只推进对应假设；只要仍有到期假设未核验，论文级 due 条件和任务就继续保留。
- daily/weekly/monthly/quarterly 使用正式生产交易日历推进约 1/5/20/60 个交易日；event-driven/manual
  只记录有效检查、不制造固定日期；红线进入下一交易日附近复核。
- 检查、通知、到期扫描和自动检查统一使用 `system.timezone` 的市场日期，默认 `Asia/Shanghai`，不再以
  UTC 日期代替市场日期。

### 12.2 Provider 正式验收

- Provider 名称去空白后必须非空，status 改为 `available/completed` 成功白名单，未知状态默认拒绝。
- 六个组件逐项核验交易日、observed_at、capability、source version、生产 namespace、最低 trust、Manifest
  Provider/版本/batch/日期覆盖/available_at，以及 PIT snapshot 的日期、refresh、manifest、fingerprint、
  cutoff/created 时间和生产信任边界。
- fallback 只有在 related failures 和 attempts 同时保存主源失败、成功 fallback、Provider 一致性且
  selection reason 明确包含 fallback 时才通过；server-configured file 可直接通过，但不豁免 Manifest、
  版本、时间和 PIT 链路。
- 每个失败都保存在 `component_checks.failed_checks` 和 `unavailable_reasons`，畸形时间或 JSON 也 fail closed，
  不抛含糊异常、不补造数据。自动数据源同时保存组件级 Manifest，避免聚合池掩盖真实 Provider。

### 12.3 `round9:4` 迁移

`round9:1`、`round9:2`、`round9:3` 的 identity 和 checksum 未修改；新增
`round9-frozen-revision-payload-reapply-and-task-recovery-v4`。迁移会选择每篇论文最新有效 frozen revision，
把其他历史 frozen 标记为 superseded，并在同一事务中重新应用 core thesis、正反证据、红线、失效条件、
估值锚点、data provenance、论文 fingerprint、next check 和 3—7 条当前假设。当前假设绑定
`active_revision_id`，其余假设安全 superseded；历史 thesis check 的 revision ID、revision fingerprint 和
report fingerprint 不改写。没有有效 frozen revision 的论文保持 `draft_pending_confirmation`。

真实旧库测试使用 `STALE BASE CONTENT` 与 `NEW FROZEN CONTENT`，升级后基础论文、当前假设和
current frozen revision 全部来自 NEW；重复升级不产生新备份，论文语义摘要保持一致。生产库升级前备份为
`data/backups/quantlab-20260719T122414266746Z-pre-migration.db`，SHA-256 为
`e1ae66a82c35a8b1f12a6cc4076153587a4ca58c5aec91d1e2f7b19615a4083d`。

### 12.4 Decision Task 边界

- resolved 的系统 due 任务在同一到期条件仍存在时安全重开，并记录 system actor、
  `source_condition_recurred`、时间和 evidence fingerprint；dismissed 且同指纹时不重开。
- 有效核验真正把全部到期条件推进到未来或使条件消失后，旧 due 任务才 resolved。论文关闭时 due、
  weakened、red-line 三类 system-managed 任务明确关闭；用户自建任务不修改。
- open、acknowledged、resolved、dismissed 和 reopen 都保留状态事件；新条件指纹仍可形成新任务。

### 12.5 最终机器验收

- `tests/test_round9_tail_closure.py`：41 项通过；新增覆盖空证据、neutral/missing、部分核验、中文规范化、
  未知频率、1/5/20/60 交易日、event/manual、配置时区、17 类 Provider 篡改、完整 fallback、`round9:4`
  旧库与重复迁移、due 重开、closed 对账和隔离 Demo。
- 全量 pytest：475 项收集并通过；Ruff、compileall、`git diff --check` 均通过。
- 总覆盖率 79.33%；Round8 85.8%、Round9 89.3%、Experiment Recorder 88.3%、Investment Thesis 90.8%、
  Reflection 95.1%、Decision Lifecycle 93.1%、Decision Tasks 85.7%、Worker 81.9%，均高于各自门槛。
- Streamlit AppTest：普通入口 5、高级页签 19、异常 0。
- 生产 SQLite：137 张表，`integrity_check=ok`，foreign-key violation 0；`demo/test/research_only` evidence
  boundary、`test/research_only` evidence stage、test namespace、论文/任务/运行测试标记污染均为 0。
- 质量报告：`data/reports/quality-gate-latest.json`，源码 fingerprint 为
  `30488f795620aaea60c04529299c01c3b1a59341ce167cf07226cdb725a6dbac`。

以上结果证明的是工程可信性、迁移可恢复性和审计边界，不证明稳定 Alpha、未来盈利或 LLM 带来收益增量。
2026-07-19 仍是休市日，真实交易日 Provider 验收、自然成熟的 5/20 日样本和 LLM 增量价值仍需真实时间证明。

后端可以冻结，下一阶段转向前端体验打磨。

## 13. 可信产品化最终收口

在后端冻结后的产品审查中，又关闭了四类会破坏用户信任或核心闭环的缺口：

1. 自动 Provider 不能再用 `selected_directly` 伪装 fallback。除 `server_configured_file`
   外，selected Provider 必须是 attempts 中最后且唯一的成功项；前序失败、priority 顺序、
   related failures 与明确 fallback reason 必须互相一致。删除 attempts、删除 failures、
   Provider 不匹配或倒置优先级均 fail closed。`round9:1`—`round9:4` identity 和 checksum 未修改。
2. 普通五入口由 `st.tabs` 改为 session-state 单页导航。首次只执行首页，切页只执行当前
   renderer；`symbol/research run/account/order` 上下文跨页保留，页面 rerun 不会因为导航结构
   重复执行其他页面或重复调用其 LLM 工作流。高级模式仍保留完整审计页签。
3. 页面和 GET 读取不再隐式对账 Decision Task 或消费 notification outbox；这些写操作由
   Scheduler/Worker 或显式“刷新任务”动作承担。会执行并持久化多 Agent 研究的旧 GET 改为
   POST；点时股票池 GET 只读已有 snapshot，抓取/持久化改为 POST。
4. 用户模拟委托必须提交 `confirmed=true`，并绑定原交易前检查的 check、account、symbol、
   side、quantity 和 confirmation source。API Schema、workflow 与 repository 三层都 fail closed，
   缺失、伪造或不一致确认无法创建 pending 委托；幂等 key 也不能重放为另一笔确认。

质量门禁现在验证普通模式有五个稳定导航项、初始页为首页、五页逐一运行无异常、普通模式
不存在五页 tabs，以及高级模式仍保留至少 15 个审计页签。工程门禁仍不构成未来盈利、稳定
Alpha 或 LLM 交易增量价值证明。

最终机器验收为 496 项测试全部通过，总覆盖率 79.32%；Ruff、compileall、Key/敏感字段扫描和
`git diff --check` 通过。Streamlit 普通导航 5 项、普通 tabs 0、高级 tabs 19，五页异常均为 0。
最新质量源码指纹为 `51a99242ef797eb74173d555b96cc843a799172f820d7af9d778658b8b688cb5`。

生产 SQLite 当前共有 138 张表（137 张业务表，不含 `sqlite_sequence`），`integrity_check=ok`，
foreign-key violation 0；生产库中 Context/Decision/Task/Experiment/User Paper/Historical Scorecard、
formal prediction/strategy stage 和 trusted namespace 的 test/demo/research_only 污染检查均为 0。
关键计数：production 交易日历 491、A 股证券主数据 5,536、可信行业归属 5,203 个标的
（20,812 条版本记录）、Decision Run 11、
用户模拟账户/订单 0、正式消融预测 0、Historical Scorecard 0。`round9:1`—`:4` checksum 保持不变，
重复迁移没有生成备份、没有改变生产库 SHA-256。

最终备份 `data/backups/quantlab-20260719T142935Z-product-freeze-final.db` 已通过 integrity/quick check，
包含 137 张业务表；restore dry-run 的 `production_database_modified=false`、迁移无 pending、
post-migration integrity 为 `ok`，生产库 SHA-256 保持
`d936be4895d06aac72eaae46595221fbb798a9d2bea670e85d0ef7fd197b1713`。
