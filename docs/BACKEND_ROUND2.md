# 第二轮后端交付：证据上下文、资金流与有边界的 LLM

> **历史快照**：本文只记录第二轮当时的交付，不是当前功能清单、运行状态或质量报告。当前事实请从 `../README.md`、`../PROJECT_HANDBOOK.md`、代码、数据库和最新机器报告核验。

> 当前状态：第二轮建立研究计算框架；第四轮已补上资金快照持久化、角色策略生效和
> 服务器行情信任边界。数据源覆盖与前瞻盈利结论仍受本文末尾边界约束。

## 交付结果

第二轮建立了 schema `2.0` 的 `AnalysisContextPack`，统一价格、OHLCV、20/60/120/250
日路径、技术、资金、财务、估值、事件、宏观、组合和策略证据。每个 EvidenceBlock
保存来源、口径、`as_of`、`available_at`、抓取时间、新鲜度、质量、降级、缺失原因、
版本和可复现指纹。截止时间后的数据会被模型校验拒绝。

资金流分为市场、行业和个股三层。没有真实净流量时只允许使用“收益符号 × 成交额”
代理，并声明它不是机构持仓或已确认的所谓主力资金。来源和 methodology 不同的数据
不能混算。融资、ETF份额、北向和大中小单不可得时返回 unavailable。

第四轮后，`capital_flow_refresh` 会把市场、行业和当前持仓/自选标的结果写入
`capital_flow_snapshots`，刷新完成后 GET 查询立即可见。行业实时快照在缺少净流入源时
只描述价格、成交和参与度；字段名、methodology 和报告均保留 signed-turnover proxy 标记。

LLM 从解释层升级为受治理的研究委员会。技术、资金、基本面、事件、宏观和组合风险
角色可以引用 ContextPack 形成动作、仓位区间、反证和失效条件。确定性系统仍决定最大
仓位、现金、行业、T+1、整手、停牌和涨跌停边界，用户仍是唯一执行者。

## 持久化 Schema

- `analysis_context_packs`, `capital_flow_snapshots`
- `llm_governed_calls`, `llm_role_observations`, `llm_role_challenges`
- Chat 消息的 `context_id/context_version`
- 模拟订单的 `context_id/context_version/context_fingerprint`
- 结构化 `notification_rules`

角色权重遵守 `shadow_observation → frozen_challenge_required → frozen → promoted/rejected`，
至少 30 个真实到期样本才可挑战，不会因少数样本自动修改权重。

第四轮把 challenge 结果落成版本化 `llm_role_policies`。委员会读取 active policy、适用
市场状态、最低样本数和治理版本，实际使用的权重随决策保存；权重只影响软建议，不能
突破确定性仓位和数据完整性上限。

## API 与通知

稳定接口包括 `/api/context-packs`、三层 `/api/capital-flow/*`、
`/api/llm/context-committee`、角色 scorecard/challenge、通知规则和 ContextPack 增强 Chat。
Chat 引用具体 EvidenceBlock，区分事实、量化结果、LLM 判断与用户假设；写操作仍需确认。

Context 缺失、陈旧、降级、冲突，Reviewer 拒绝、AI 观点变化、LLM 预算达到和 Provider
回退均会生成可复算通知。

## 明确缺口

完整且许可清晰的 ETF份额、两融、跨境资金、龙虎榜、历史行业成分和监管事件源仍需
商业数据合同。事件异常收益和匹配对照组在 ContextPack 中明确标为 unavailable，不补造。
第三轮为这些数据提供点时 master/status/snapshot 接口和后台刷新入口。

## 能力边界

- **研究计算框架**：ContextPack、三层资金证据、LLM 结构化委员会和角色评价已具备。
- **已形成自动闭环**：ContextPack/资金快照持久化、角色政策生效、缓存隔离和通知触发已实现。
- **等待真实时间验证**：角色晋级和 LLM 增量仍至少需要 30 个服务器登记并到期的前瞻样本。
- **未形成生产数据 SLA**：免费源失败时只返回 degraded/unavailable，不保证 ETF 份额、两融、北向或大中小单连续可用。

## 验收结果

第四轮收口后的合并最终门禁于 2026-07-17 通过：326 项测试、Ruff、compileall、关键模块
覆盖率、密钥扫描和报告敏感字段扫描全部通过；总覆盖率 76.52%（终端显示 77%），Streamlit 24 个标签页
冒烟测试 0 异常。机器可读结果见 `data/reports/quality-gate-latest.json`。
