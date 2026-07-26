# 第四轮后端交付：信任边界、真实数据闭环与后台可靠性

> **历史快照**：本文只记录第四轮当时的交付，不是当前功能清单、运行状态或质量报告。当前事实请从 `../README.md`、`../PROJECT_HANDBOOK.md`、代码、数据库和最新机器报告核验。

## 1. 本轮结论

第四轮没有扩张产品功能，而是把前三轮中可能被客户端伪造、被历史重跑污染、被 Worker
竞态破坏或被文档夸大的边界收紧为服务器可审计实现。

当前状态必须分开理解：

| 能力层级 | 当前结论 |
|---|---|
| 研究计算框架 | 点时池、七变体消融、成本估算、资金代理和角色评价可运行 |
| 已形成自动闭环 | 服务器行情、模拟订单资源冻结、到期结算、资金快照、角色政策和 Worker 已闭环 |
| 等待真实时间验证 | 七种前瞻变体、LLM 增量、A 股 V4、可转债必须继续积累不可回写样本 |
| 系统内部准入 | 只代表可进入模拟或手工建议预算，不代表券商实盘、数据 SLA 或收益保证 |

## 2. 修改文件清单

### 新增

- `src/quantlab/market/__init__.py`
- `src/quantlab/market/quotes.py`
- `src/quantlab/market/calendar.py`
- `src/quantlab/persistence/migrations.py`
- `tests/test_round4_trust_boundaries.py`
- `docs/BACKEND_ROUND4.md`

### 重点修改

- API 与请求边界：`src/quantlab/api/app.py`、`src/quantlab/api/schemas.py`
- 领域模型：`src/quantlab/domain/trading.py`、`src/quantlab/domain/context.py`
- 模拟交易：`src/quantlab/persistence/simulator.py`、`src/quantlab/workflows/simulator.py`
- 前瞻证据：`src/quantlab/persistence/strategy_evidence.py`、`src/quantlab/workflows/forward_ablation.py`
- 资金证据：`src/quantlab/persistence/evidence.py`、`src/quantlab/workflows/capital_flow.py`
- LLM 治理：`src/quantlab/llm/governance.py`、`src/quantlab/workflows/llm_committee.py`、`src/quantlab/workflows/role_governance.py`
- Chat：`src/quantlab/workflows/chat.py`、`src/quantlab/workflows/chat_jobs.py`
- Job/Worker：`src/quantlab/persistence/jobs.py`、`src/quantlab/runtime/worker.py`
- 运维与 CLI：`src/quantlab/runtime/operations.py`、`src/quantlab/cli.py`
- 配置与门禁：`.env.example`、`config/default.toml`、`scripts/quality-gate.ps1`
- 边界文档：`README.md`、`docs/BACKEND_ROUND2.md`、`docs/BACKEND_ROUND3.md`、`docs/handbook/*`

工作区原有修改和未跟踪文件均保留；本轮没有执行 reset、checkout 覆盖或清理操作。

## 3. 根因与修复

### 3.1 模拟交易行情信任边界

**根因**：客户端请求曾能携带完整报价或权威交易属性，导致价格、停牌、涨跌停、ST、
行业、交易日和数据质量可能参与正式结果；Chat 二次确认也可能复用草稿中的旧报价。

**修复**：

- `UserPreTradeRequest` 只接收账户、标的、方向、数量/金额和研究上下文；所有请求模型
  使用 `extra="forbid"`：`src/quantlab/api/schemas.py:16`、`:240`。
- `QuoteService` 统一返回价格、行业、交易状态、trade lot、T+1、`available_at`、Provider、
  source/version、质量和指纹：`src/quantlab/market/quotes.py:210`。
- pretrade、结算、mark-to-market、benchmark 和 Chat confirm 均重新获取服务器行情：
  `src/quantlab/workflows/simulator.py`、`src/quantlab/workflows/chat.py`。
- 测试报价入口仅在 `QUANTLAB_ENABLE_TEST_QUOTES=1` 且 loopback 时可用，并写成
  `authoritative=false/evidence_stage=test`：`src/quantlab/api/app.py:314`。
- 测试账户使用 `test_only` 隔离；非权威报价不能进入生产账户或 forward scorecard。

### 3.2 前瞻证据不可由公共 API 写结果

**根因**：第三轮的前瞻表结构和计算能力已经存在，但公共请求仍可能提交冻结时间、
到期时间、预测或结算结果，历史研究与前瞻证据的信任域也不够明确。

**修复**：

- cohort 的 `frozen_at`、sample 的 `registered_at` 均由服务器 UTC 生成；`due_at` 由正式
  交易日历加 5/20 个交易日计算：`src/quantlab/workflows/forward_ablation.py:89`、`:127`。
- 公共 sample 只接收 cohort、symbol、account 和 horizon；公共 settlement 只提交扫描任务：
  `src/quantlab/api/schemas.py:525`、`src/quantlab/api/app.py:2093`。
- 服务器执行并原子冻结七个变体，保存起始价、行情 Provider/version、Context 指纹、
  策略/Prompt/治理版本和每个预测指纹：`src/quantlab/workflows/forward_ablation.py:120`。
- Worker 固定请求 `due_at` 对应交易日的权威行情，而不是 Worker 实际晚跑的日期，再计算
  收益、方向、换手、成本估算和回撤：`src/quantlab/workflows/forward_ablation.py:210`。
- 权威行情不可用、尚未到达 due 日或 `available_at` 在未来时保持 pending，并写入
  `forward_settlement_attempts` 的明确原因；已结算结果不可覆盖。
- 历史重跑只写 `strategy_research_runs/research_replay`；forward scorecard 只读服务器冻结并
  结算的 forward 表。

### 3.3 模拟订单完整语义

**根因**：pending 委托没有完整冻结资源，并发请求可能同时通过旧账户快照；交易日、T+1、
节假日和部分成交后的资源释放也缺少统一事务边界。

**修复**：

- 正式交易日历决定 `eligible_trade_date`、T+1 可卖日、订单过期和特殊休市：
  `src/quantlab/market/calendar.py:13`。
- pending 买单冻结预计成交额和费用，pending 卖单冻结可卖数量。
- 提交、撤销、拒绝、过期、部分/全部成交在 `BEGIN IMMEDIATE` 事务中释放或消耗冻结资源：
  `src/quantlab/persistence/simulator.py:55`、`:478`、`:1019`。
- 每次状态变化执行账户不变量检查，拒绝负现金、冻结现金超现金、冻结数量超持仓和非法
  总资产：`src/quantlab/persistence/simulator.py:1720`。

### 3.4 资金流成为持久化数据产品

**根因**：`capital_flow_refresh` 曾只返回临时计算结果，调度结束后 GET 接口无法读取；行业
点时映射和不可用状态也没有形成完整查询链路。

**修复**：

- Worker 将市场、行业、持仓和自选标的快照写入 `capital_flow_snapshots`：
  `src/quantlab/runtime/worker.py:242`。
- `industry_membership_history` 保存行业点时映射：`src/quantlab/persistence/evidence.py:100`。
- 行业实时快照在没有净流入源时只输出价格、成交和参与度；5/20 日净流入字段保持
  unavailable：`src/quantlab/workflows/capital_flow.py:336`。
- signed-turnover 明确命名为 proxy：`sign(raw close return) × amount`，不得解释为机构持仓、
  主力净流入或已确认资金流。
- ETF 份额、两融、北向和大中小单只有在许可清晰且源可用时填充；否则返回 unavailable、
  missing reason 和可获得的最后成功时间，不使用代理值冒充。
- Chat 中资金证据不能单独生成买入/加仓：`src/quantlab/workflows/chat.py`。

### 3.5 LLM 角色治理闭环

**根因**：角色挑战结果虽可记录，但没有稳定地改变后续委员会；聚合仍可能过度相信模型
自行声明的 importance，缓存也可能在切换模型后复用错误结果。

**修复**：

- promoted/rejected challenge 生成版本化 active `llm_role_policies`：
  `src/quantlab/persistence/evidence.py:625`。
- 委员会读取 active weight、最低样本数、适用市场状态和治理版本，并保存实际使用值：
  `src/quantlab/workflows/llm_committee.py:45`、`:131`。
- 聚合使用治理权重 × 置信度，不以模型 importance 作为最终权威；结果仍裁剪到确定性最大
  仓位和数据完整性边界：`src/quantlab/workflows/llm_committee.py:329`。
- 缓存 Key 包含 Provider、Model、角色专属模型、Prompt、Schema、治理版本和 Context 指纹：
  `src/quantlab/llm/governance.py:221`。

### 3.6 Job/Worker 可靠性

**根因**：依赖失败后下游可能永久 queued；running cancel 可能提前标记 cancelled；长网络/
LLM 调用期间没有独立续租；崩溃重试可能重复外部副作用；批量盯市可能被单账户失败中断。

**修复**：

- 依赖失败/取消传播为 `failed + dependency_blocked`，同时写 blocked 事件，作为等价终态：
  `src/quantlab/persistence/jobs.py`。
- running cancel 只设置 `cancel_requested`；Worker 在 handler 检查并确认后写 cancelled。
- 独立 heartbeat 线程在长调用期间续租：`src/quantlab/runtime/worker.py:115`、`:176`。
- side effect 状态保存 started/completed/result；崩溃后结果不明时标记
  `side_effect_outcome_unknown`，不自动重复执行：`src/quantlab/runtime/worker.py:103`。
- mark-to-market 逐账户捕获失败并继续其他账户：`src/quantlab/runtime/worker.py:418`。
- `capital_flow_refresh` 真正持久化；`forward_settlement_scan` 真正执行到期样本结算。

### 3.7 安全、迁移与恢复

**根因**：无 Token 时远程 GET 仍可能读取敏感数据；在线 restore 会在 Worker 和连接仍活动
时替换数据库；各仓储迁移缺少统一顺序和跨组件校验。

**修复**：

- 无 `QUANTLAB_API_TOKEN` 时，除 `/api/health` 外所有远程 `/api` 请求返回 403；配置 Token
  后所有敏感 `/api` 请求统一校验 `X-QuantLab-Token`：`src/quantlab/api/app.py:230`。
- 公共在线 restore API 已移除；`quantlab database-restore` 要求显式确认、maintenance mode、
  维护锁、Worker 停止、备份校验和与 SQLite integrity check：`src/quantlab/cli.py:1408`、
  `src/quantlab/runtime/operations.py:46`。
- 统一迁移顺序为 simulator → chat → notifications → evidence → strategy_evidence → jobs，
  每项保存 checksum：`src/quantlab/persistence/migrations.py:10`。
- `quantlab database-migrate` 支持新库初始化和旧库增量升级；重复执行幂等，checksum 冲突
  立即失败。

## 4. API 兼容性变化

以下变化是有意的安全收紧，旧客户端需要调整：

1. `POST /api/simulator/pretrade-check` 不再接受 `quote`、价格、日期、停牌、涨跌停、ST、
   行业、trade lot、数据质量或行情来源；未知字段返回 422。
2. `POST /api/simulator/orders/{order_id}/settle` 只接收可选 `fill_quantity` 和 `fill_key`；
   成交价格和交易状态由服务器取得。
3. `POST /api/simulator/accounts/{account_id}/mark` 请求体为空，不再接收客户端行情。
4. `POST /api/chat/actions/{action_id}/confirm` 只允许可选数量；确认时重新跑服务器行情检查。
5. `POST /api/forward-ablation/cohorts` 和 `POST /api/forward-experiments/primary/ensure`
   现在固定返回 403；sample 只创建 research-only exploration，settlement 不接收 realized
   return、observed_at、来源、换手、回撤或成本。
6. `/internal/test/quotes` 默认 404，仅显式测试配置和 loopback 可用；其数据非权威。
7. 在线 `/api/runtime/restore` 已移除，改用 maintenance-mode CLI。
8. 未配置 Token 的远程 `/api` 从可访问变为 403；配置 Token 后所有敏感 `/api` 均需认证。

## 5. 数据库迁移

统一注册表 `quantlab_migration_registry` 保存 component、version、ordinal、checksum 和
`applied_at`。当前组件版本：

| 组件 | 版本 | 第四轮关键内容 |
|---|---:|---|
| simulator | 5 | `test_only`、冻结现金、订单冻结现金/数量、事件和不变量迁移 |
| chat | 4 | Context/任务/动作确认所需字段与幂等升级 |
| notifications | 4 | 通知可靠发送和审计字段 |
| evidence | 5 | 行业点时映射、资金快照、按市场状态生效的 active role policy 和挑战门槛 |
| strategy_evidence | 4 | 前瞻行情/Context/治理/预测指纹、结算尝试表 |
| jobs | 5 | cancel request、side-effect 状态/结果、依赖阻断和 lease 字段 |

执行方式：

```powershell
quantlab database-migrate
quantlab database-restore --backup-path <path> --sha256 <sha256> --confirm
```

恢复只允许在 Worker 停止的维护窗口运行，并会先生成 pre-restore safety backup。

## 6. 新增反例测试

主要证据位于 `tests/test_round4_trust_boundaries.py`：

- 客户端伪造模拟行情和 Chat confirm 行情被 422 拒绝：`:179`
- 无 Token 远程读取账户、Chat、通知被 403：`:226`
- 多笔 pending 买单不能超用现金：`:236`
- 多笔 pending 卖单不能超卖并在撤单后释放：`:301`
- 单账户 mark-to-market 失败不影响其他账户：`:845`
- 节假日不会被工作日估计覆盖：`:922`
- 公共前瞻 API 不能写服务器时间、预测和结果：`:944`
- 七变体冻结保存服务器行情、版本和指纹：`:1043`
- future `observed_at` 被拒绝，research replay 不进入 scorecard：`:1043`
- Worker 使用 due 交易日权威行情结算并计算收益：`:1096`
- due 日行情不可用时保持 pending 并记录原因：`:1162`
- 资金刷新后市场/行业 GET 仓储立即可查：`:1222`
- 资金 GET 保持纯查询，刷新只能提交 Worker Job：`tests/test_round4_trust_boundaries.py`
- 市场、行业或个股源失败仍持久化 unavailable、原因和最后成功时间：`tests/test_round4_trust_boundaries.py`
- promoted role policy 改变实际聚合：`:1302`
- promoted policy 仅在足量成熟样本覆盖的适用市场状态生效：`tests/test_round4_trust_boundaries.py`
- Provider/Model/治理版本缓存隔离：`:1496`
- 同 Context/幂等键的并发委员会只产生一组 Provider 调用：`tests/test_round4_trust_boundaries.py`
- 上游失败向下游传播 dependency blocked：`:1526`
- 独立 heartbeat 防第二 Worker、running cancel 协作停止：`:1548`
- side effect 结果不明时不重复执行：`:1584`
- 新库、重复迁移、旧库升级和 checksum 失败：`:1613`
- 备份/恢复与 schema 迁移：`tests/test_round3_runtime.py:192`

## 7. 质量门禁

2026-07-17 最终命令：

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests dashboard
.\.venv\Scripts\python.exe -m compileall -q src dashboard tests
.\.venv\Scripts\python.exe -m pytest --cov=quantlab --cov-report=term --cov-report=json:data/reports/coverage-round4.json -q
.\scripts\quality-gate.ps1
git diff --check
```

最终门禁确认 326 项测试全部通过，总覆盖率 76.52%（终端显示 77%），且以下重点模块达到要求：

| 模块 | 覆盖率 |
|---|---:|
| simulator workflow | 80% |
| simulator persistence | 85% |
| forward_ablation | 85% |
| context workflow/domain | 91% / 94% |
| capital_flow | 82% |
| worker | 86% |
| jobs | 82% |
| chat workflow/persistence | 80% / 87% |

## 8. 仍必须等待真实时间或外部数据

1. 七个前瞻变体每个期限至少 30 个服务器登记并真实到期样本后，才能进入 measured。
2. LLM 是否提高收益、降低回撤或只增加成本，仍没有足量前瞻证据。
3. A 股 V4、点时 ETF 主动轮动和可转债仍未生产准入；历史重跑不能改变这一点。
4. 免费行情和事件源没有 SLA；Provider 失败时系统会 pending/degraded/unavailable，但不能
   保证每天都有可结算数据。
5. ETF 份额、两融、北向/跨境、龙虎榜、大中小单、完整行业历史和监管事件仍需要许可
   清晰且稳定的数据源。
6. signed-turnover 只是方向性成交活跃度代理，不是净申购、机构持仓或“主力资金”。
7. 交易成本来自佣金、税费、滑点和冲击的模型估算，不是券商逐笔真实成交回报。
8. 系统没有券商连接，不会自动确认或执行用户订单。
