# QuantLab 外部 AI 验收指南

先读 `docs/AI_HANDOVER.md`。本文只定义验收方法、证据边界和输出要求，不保存容易过期的测试数、
Provider 状态、数据库行数、实验样本数或收益结论。

## 验收目标

请分别回答四个问题，不要合并成一个“完成度”分数：

1. 产品是否形成普通用户可走通、可理解、可恢复的决策闭环；
2. 工程、数据、LLM、订单和 Runtime 的权限边界是否真正由代码执行；
3. 历史研究、Demo、用户模拟、影子账户和正式前瞻是否严格隔离；
4. 目前有哪些投资价值证据，以及哪些结论仍未被证明。

## 证据优先级

1. `AGENTS.md` 与 `PRODUCT_STRATEGY.md`；
2. 当前代码、迁移注册表、API Schema 和自动化测试；
3. 明确目标路径的生产 SQLite、Runtime 状态和同次验收的命令输出；
4. 能验证源码、配置、数据和协议指纹的机器报告；
5. 预注册协议和不可回写的自然到期结果；
6. 静态手册、Round 文档、日期报告和 Postmortem。

低优先级叙述不能覆盖高优先级证据。发现冲突时应报告冲突，而不是选择更乐观的文本。

## 建议验收顺序

### 1. 静态接管

阅读：

```text
AGENTS.md
PRODUCT_STRATEGY.md
docs/AI_HANDOVER.md
PROJECT_HANDBOOK.md
docs/handbook/03_ARCHITECTURE_AND_DATA_FLOW.md
docs/handbook/04_FEATURES_AND_USER_WORKFLOWS.md
docs/handbook/08_RUN_VERIFY_AND_CODE_MAP.md
```

然后核对 `dashboard/ui_foundation.py`、`dashboard/product_ui.py`、`src/quantlab/domain/`、
`src/quantlab/workflows/`、`src/quantlab/runtime/`、`src/quantlab/persistence/migrations.py`、
`src/quantlab/api/app.py`、`src/quantlab/cli.py` 与相关测试。

### 2. 工程验证

```powershell
cd E:\LLM-Projects\quant-lab\my-system
git status --short
git diff --check
.\.venv\Scripts\python.exe -m quantlab.cli --help
.\.venv\Scripts\python.exe -m pytest -q
```

完整项目门禁可运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\quality-gate.ps1
```

记录准确命令、退出码、收集数、失败项和工作树状态。旧 `quality-gate-latest.json` 只有在源码指纹与
当前工作树匹配时才可作为当前工程质量证据。测试通过不证明数据 readiness 或盈利。

### 3. 隔离产品演示

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-hackathon-demo.ps1
```

按 `docs/HACKATHON_DEMO.md` 检查候选、冻结研究、确定性风控、用户确认、模拟成交、盈亏和复盘。
确认使用隔离 SQLite、Mock LLM 和冻结数据，不读取 `.env`，不创建 Primary、正式样本或生产影子账户。

### 4. 动态运行与数据库验收

```powershell
.\.venv\Scripts\python.exe -m quantlab.cli runtime-status
.\.venv\Scripts\python.exe -m quantlab.cli llm-status
```

先用 `--help` 核对其他子命令参数。读取数据库时必须明确路径并优先只读，至少检查：

- SQLite `integrity_check`、foreign keys 与迁移 registry；
- Runtime instance、PID、heartbeat、staleness 和观测时间；
- Job 的 `status`、`attempt`、source identity、result payload 与下游实体；
- Provider selection、manifest、实际市场日、字段覆盖和失败原因；
- PIT pool 的 namespace、cutoff、source fingerprint 与每个字段的 `available_at`；
- Schedule run 的自然/恢复/backfill provenance 与终态；
- 实验协议、cohort、sample、variant、账户和结算身份是否一致。

只读验收不得触发 Scheduler、Worker、Provider refresh、Job、Primary、实验、样本或影子账户创建。

## 必须对抗的风险

| 维度 | 重点寻找的失败 |
|---|---|
| 时间一致性 | 未来数据、晚到字段、恢复后回填、错误交易日、幸存者偏差 |
| 数据治理 | fallback 语义漂移、旧缓存冒充当前、Provider 健康冒充实际选择 |
| 交易真实性 | 错误价格、T+1/整手/涨跌停/费用遗漏、旧检查复用、前端绕过确认 |
| 组合风控 | 总/单标的/行业上限未执行、研究策略获得正式预算、现金冻结错误 |
| LLM 治理 | 伪造事实、结构化输出失效、第三方 Key 转发、模型越权放宽硬规则 |
| 证据隔离 | Demo/用户模拟/外部账本/恢复样本污染正式成绩或训练 |
| Runtime | 重复 Job、lease/attempt 错误、`completed` 掩盖 partial/unavailable |
| 恢复与迁移 | 无备份升级、checksum 不匹配、活跃进程写入、恢复测试污染生产 |
| 前端体验 | 移动端遮挡、页面状态串线、长任务阻塞、空/错/降级状态不可理解 |
| 声明诚实性 | 把测试、历史正收益、概率准确或小样本写成 Alpha/盈利保证 |

## 产品 Golden Path

按桌面和窄屏分别走一遍：

```text
今日
  -> 市场与发现
  -> 研究台 / 研究详情 / 专家圆桌
  -> 组合与交易（服务器侧检查 + 用户确认）
  -> 决策复盘
```

同时检查专业空间、帮助中心、设置、全局 AI 助手、通知和后台任务。五个一级决策工作区、两个工具
入口和三个二级路由的层级应保持一致；切换标的、研究、账户或订单后上下文不得串线。

## 投资证据判定

以下层级必须分开：

| 可证明的结论 | 最低证据 |
|---|---|
| 工程链路可运行 | 当前代码测试或隔离 Demo |
| 历史研究可复现 | 数据/配置/源码/协议指纹一致的回测或回放 |
| 自然前瞻样本存在 | 原始 Scheduler provenance、PIT cutoff 和不可回写到期记录 |
| 模型概率有增量 | 足量自然样本上的 Brier/Log Loss、校准和基线比较 |
| 策略有投资增量 | 含成本组合曲线、可投资基准、回撤、区间和跨状态稳定性 |
| LLM 有交易增量 | 同期同协议的纯量化/LLM/融合消融，且差异不是来源污染 |

工程质量、页面完整、历史回测正收益和某次模型调用成功都不能单独证明 Alpha。

## 输出格式

最终报告应按严重度先列问题，每项包含：

- 文件/行、数据库表/主键或命令证据；
- 可复现条件和用户影响；
- 已证明、未证明和证据时间；
- 最小修复或追加证据；
- 是否阻断黑客松演示、成熟产品验收或投资声明。

随后分别给出产品、前端体验、架构、数据、金融真实性、LLM 治理、Runtime、测试和文档的一致性
结论。可以评分，但必须附评分口径；不得用功能数量抵消严重的时间泄漏、订单越权或证据污染。

推荐任务文本：

> 请按 `docs/AI_HANDOVER.md` 接管 QuantLab，并以产品负责人、前端体验设计师、金融风控工程师、
> 量化研究负责人和 Python 架构师五个视角做最终验收。先核对当前代码和工作树，再做隔离 Demo 与
> 只读动态检查。问题按严重度列出并给出文件/行或数据库证据。明确区分工程可用、历史研究、自然
> 前瞻和盈利增量；不得从 Round 文档、恢复样本或旧报告推断当前成功。
