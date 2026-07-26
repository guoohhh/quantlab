# QuantLab 文档状态清单

> 本清单于 2026-07-26 按当前工作树复核。日期只表示文档审计时间，不证明 Runtime、Provider、
> 数据或实验在该日可用。动态事实必须重新查询。

## 权威顺序

1. `AGENTS.md`：AI 行为、证据和工作树边界；
2. `PRODUCT_STRATEGY.md`：产品定位、非目标和合规边界；
3. `docs/AI_HANDOVER.md`：验收路径和证据导航；
4. 当前代码、迁移注册表、API Schema 与测试：能力是否真正存在；
5. 生产数据库、Runtime 与匹配源码指纹的机器报告：运行、数据和实验事实；
6. `PROJECT_HANDBOOK.md` 与 `docs/README.md`：面向用户的稳定说明；
7. 协议、Postmortem、Round 文档和日期报告：历史研究或交付证据。

低层级资料不能覆盖高层级权威。任何 Markdown 中的测试数、路由数、数据库行数、Provider 状态、
样本数和收益数字都可能过期。

## 当前维护文档

| 文档 | 作用 |
|---|---|
| `README.md` | 产品总览、启动入口和边界 |
| `PROJECT_HANDBOOK.md` | 用户流程、权限和证据隔离 |
| `docs/AI_HANDOVER.md` | 外部 AI 快速接管与最终验收 |
| `AI_REVIEW_GUIDE.md` | 外部 AI 的验收方法、对抗风险与输出格式 |
| `docs/README.md` | `docs/` 导航与历史资料规则 |
| `docs/handbook/01_PROJECT_OVERVIEW.md` | 产品与系统总览 |
| `docs/handbook/03_ARCHITECTURE_AND_DATA_FLOW.md` | 当前架构、数据流和代码边界 |
| `docs/handbook/04_FEATURES_AND_USER_WORKFLOWS.md` | 当前前端功能与工作流 |
| `docs/FIVE_ENTRY_USER_FLOW.md` | 五个一级决策工作区、工具入口与二级路由 |
| `docs/handbook/05_STRATEGIES_AND_EVIDENCE.md` | 稳定的策略准入和证据判定方法 |
| `docs/handbook/07_STATUS_LIMITATIONS_AND_ROADMAP.md` | 稳定限制与动态验收方法 |
| `docs/handbook/08_RUN_VERIFY_AND_CODE_MAP.md` | 运行、验证和代码地图 |
| `docs/DEPLOYMENT.md` | 单机部署、Runtime、迁移、备份和恢复 |
| `docs/HACKATHON_DEMO.md` | 隔离黑客松演示路线 |
| `docs/DATA_SOURCE_STATUS.md` | 数据状态的动态核验方法 |
| `docs/DATA_SOURCE_LICENSES.md` | 数据许可与再分发边界 |
| `docs/CONTINUOUS_RUNTIME_STATUS.md` | Runtime/Soak 的动态核验方法 |
| `docs/WINDOWS_AUTOSTART.md` | Windows 自启动操作 |
| `docs/WIDE_FORWARD_RESEARCH.md` | 宽样本研究隔离协议 |
| `PROMPT_GOVERNANCE.md` | LLM 结构化输出、安全和降级约束 |
| `THIRD_PARTY_NOTICES.md` | 第三方许可声明 |

`docs/handbook/02_BEGINNER_GUIDE.md`、`05_STRATEGIES_AND_EVIDENCE.md` 和
`06_MULTI_AGENT_LLM_AND_LEARNING.md` 主要用于解释概念。其中带日期、数量、具体成绩或模型名称的
段落属于历史示例；验收时不得直接当成当前事实。

## 版本化协议与研究记录

根目录的 `*_PROTOCOL.md`、`*_PREREGISTRATION.md`、`*_POSTMORTEM.md`、`VALIDATION.md`、
`EVIDENCE_SYSTEM.md`、`LEARNING_SYSTEM.md`、`PORTFOLIO_EVIDENCE_POLICY.md` 和
`PAPER_TRADING.md` 是研究审计资料。它们回答“某个版本事前约束了什么、当时观察到什么”，
只有数据库中的实验身份明确绑定对应版本和指纹时，才能约束该实验。

`ARCHITECTURE.md` 是第一版历史设计基线；`IRONQ_PARITY.md`、`REFERENCE_AUDIT.md`、
`REFERENCE_INTEGRATION_ROADMAP.md` 与 `OPEN_MODEL_ROADMAP.md` 是历史对标或路线资料。
它们不构成当前能力或当前待办清单。

## 历史交付与设计快照

- `docs/BACKEND_ROUND*.md` 与 `docs/BACKEND_ROUNDS_1_4_AUDIT.md`：阶段交付记录；
- `design/quantlab-vnext/`：特定日期的产品设计、原型和验收快照；
- `data/reports/`：机器或人工报告，必须检查生成时间、源码/配置/数据指纹和证据身份；
- `*_POSTMORTEM.md`：失败与决策形成过程，不代表当前版本仍有同一缺陷或已经完成修复。

历史资料应保留，不通过改写数字让旧实验看起来像当前成功。

## 动态事实核验

| 要回答的问题 | 必须读取的证据 |
|---|---|
| Runtime 是否健康 | `quantlab runtime-status`、进程心跳、实例与观测时间 |
| 数据是否可用于当日决策 | Provider selection、manifest、`provider_market_date`、字段覆盖与 `available_at <= cutoff_at` |
| 数据库是否可用 | 目标数据库路径、SQLite `integrity_check`、foreign keys、迁移状态 |
| Job 是否完成业务目标 | Job 状态以及 result payload、attempt、来源、下游实体和失败边界 |
| 正式样本是否成立 | 协议版本、自然调度 provenance、PIT 指纹、不可回写身份与到期记录 |
| 工程质量是否通过 | 当前工作树运行测试，或验证 `quality-gate-latest.json` 的源码指纹 |
| 是否存在 Alpha/LLM 增量 | 自然到期样本、含成本曲线、可投资基准、区间和消融；工程测试不作证明 |

发现文档与代码冲突时，先记录冲突，再按本页权威顺序判定，不要静默选择更乐观的版本。
