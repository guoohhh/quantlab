# QuantLab 第八轮交付：生产运行可信度、投资决策生命周期与统一实验账本

> **历史快照**：本文只记录第八轮当时的交付，不是当前功能清单、运行状态或质量报告。当前事实请从 `../README.md`、`../PROJECT_HANDBOOK.md`、代码、数据库和最新机器报告核验。

## 1. 本轮结论

第八轮没有新增策略、投资大师人格、模型阈值、券商连接或自动真实交易。它收口的是三个基础问题：

1. 免费数据 Provider 超时后不能形成重叠底层调用，实际选中的 Provider 能被审计；
2. AI 建议、用户采纳决定和用户在外部券商完成的成交是三类独立事实；
3. 研究、建议、投资论文、到期结果和 Reflection 可以通过统一 `run_id`、实体关联和 Artifact 追溯。

本轮完成的是工程可信度和持续复盘机制，不是盈利证明。正式 5/20 交易日结果、LLM 增量、Alpha 和策略准入仍必须等待不可回写的真实到期样本。

## 2. Provider 能力路由与 single-flight

`src/quantlab/data/provider_router.py` 建立按能力路由的 Provider 注册表，而不是让所有数据域共用一个全局顺序。当前能力包括：

- 交易日历；
- 证券主数据；
- 行业归属；
- 交易状态；
- 全市场现货；
- 日线行情。

当前免费源声明：

| Provider | 主要能力 | 优先级 | 许可/服务边界 |
|---|---|---:|---|
| BaoStock | 日历、主数据、行业、状态、日线 | 10 | `server_observed / unverified_no_sla` |
| AkShare EastMoney | A 股现货 | 20 | `server_observed / unverified_no_sla` |
| AkShare Sina | A 股现货降级源 | 30 | `server_observed / unverified_no_sla` |

single-flight 的范围是 Provider，而不是单一 component。调用方超时后，底层线程在真正结束前仍登记为活动调用：

- 本轮重试不能再次启动同 Provider；
- 后续 component 不能与它重叠；
- 下一轮 refresh 也不能与它重叠；
- 底层调用结束后活动登记自动释放；
- 超时、in-flight、熔断、fallback 和最终选中来源均进入审计。

`provider_refresh_selections` 保存每个 refresh/component 的实际选中来源、原因、相关失败、实际 attempts 和指纹。Soak 只比较相邻观测中的实际 `selected_provider`；多个健康 Provider 同时存在不会被误算为切换。

## 3. 外部成交方向与建议采纳

外部真实成交继续由用户手工在券商完成，QuantLab 只记录用户报告的事实。新语义如下：

- `adopted`：采纳建议；
- `partially_adopted`：部分采纳建议；
- `rejected`：拒绝建议，不能携带成交；
- `user_override`：用户执行了与建议不同或建议不可操作时的独立交易。

只要记录外部成交，就必须显式提供：

- `trade_side=buy|sell`；
- `actual_quantity`；
- `actual_price`；
- `actual_trade_date`；
- 非负 `transaction_cost`。

采纳或部分采纳时，系统还会校验建议是否 actionable、方向是否一致、数量是否超过建议上限。`review_required`、不可操作建议、零数量区间和逆建议交易不能再被默认解释为买入。修订会先反向冲销上一版本的用户报告成交，再写入新版本；到期结果已经结算后禁止改写。

## 4. Historical Demo 与 Live Demo

Historical Demo 的隔离数据库身份、账户幂等键、订单幂等键和成交幂等键均包含完整数据集 SHA-256。数据集版本不变但内容指纹变化时会进入新的隔离数据库，不能复用旧订单。

每次返回前都会核对候选、订单、成交和持仓属于同一标的和同一数据集指纹。该模式固定为：

```text
research_only=true
training_eligible=false
forward_scorecard_eligible=false
```

Live Demo 分开返回：

- `has_state_records`；
- `data_available`；
- `minimum_ready`；
- `actionable`；
- `primary_start_allowed`。

存在状态行不再等同于数据可用。`unavailable`、记录数为 0、关键字段不足和数据过期都会明确显示降级或不可用。

## 5. Investment Thesis 生命周期

用户采纳或部分采纳一份 AI 建议后，系统创建并冻结 Investment Thesis。论文包含：

- 组合、标的、建议、研究、ContextPack 和统一 run 关联；
- 初始价格、核心论文、3—7 条假设；
- 支持和反对证据；
- 红线、失效条件和估值锚；
- 下一次检查时间；
- 用户最终决定与外部成交关联；
- 数据来源、时间、质量和指纹。

论文状态为：

```text
active / strengthened / unchanged / weakened / damaged / broken / closed
```

Thesis Check 会逐条核验假设并保存引用证据。没有证据时假设保持 `needs_review`，不能被 LLM 判定为成立。红线优先于股价上涨：即使价格上涨，只要红线触发，论文仍必须进入 `damaged` 或 `broken`。用户可以确认、忽略或关闭检查结论；红线和缺证据复评会进入站内通知。

用户在结算前修订采纳决定时，论文会同步最新用户决定和外部成交关联。由采纳改为拒绝或逆建议交易时，原 AI 论文关闭；在合规修订回采纳且论文只因该决定关闭时可以重新激活。

## 6. Outcome Reflection 与受控研究记忆

只有满足以下全部条件的结果才能创建 Reflection：

- 统一实验 run 存在且已完成；
- run 的证据边界与 Reflection 一致；
- 边界为 `production` 或 `forward_shadow`；
- 5/20 交易日冻结期限已经自然到期；
- 结算时间不是未来时间。

Reflection 保存原始收益、冻结基准收益、Alpha、成本、MAE/MFE、方向正确性、支持/反对证据表现和错误诊断。历史报告不会被覆盖。

研究记忆只保存 candidate lesson：

- 必须关联真实到期 Reflection；
- Demo、test、user 和 pending 结果不能进入正式记忆；
- 同标的经验可作为后续研究上下文；
- 跨标的经验权重最多为 0.25；
- 未达到预设成熟样本门槛不能进入 challenge；
- 不能自动修改策略、Agent 权重、阈值或硬风险规则。

## 7. 统一 Experiment Recorder

第八轮新增 QuantLab 自有的 Experiment/Run/Artifact/Checkpoint 抽象，没有引入 Qlib 调度器，也没有替代现有 Job 系统。

核心表：

- `unified_experiments`；
- `unified_experiment_runs`；
- `unified_run_links`；
- `unified_run_artifacts`；
- `research_step_checkpoints`。

run 冻结代码、配置、Prompt、数据集、股票池、ContextPack、行情和模型路由指纹。相同幂等键只能绑定相同冻结输入；输入漂移会被拒绝。Artifact 保存可复算 SHA-256。Checkpoint 签名包含工作流结构、模型路由、Prompt 版本、ContextPack 指纹和配置指纹；签名变化时在执行 callback/LLM 前就拒绝错误复用。

多 Agent 个股研究链路已接入统一 run，保存 ContextPack checkpoint、委员会完成 checkpoint、决策 Artifact 和原 DecisionRun 关联。

## 8. 数据库迁移

迁移顺序现在是：

```text
simulator → chat → notifications → evidence → strategy_evidence
→ jobs → round5 → round6 → round7 → round8
```

`round8:1` 新增：

- `provider_refresh_selections`；
- `unified_experiments`；
- `unified_experiment_runs`；
- `unified_run_links`；
- `unified_run_artifacts`；
- `research_step_checkpoints`；
- `investment_theses`；
- `thesis_assumptions`；
- `thesis_checks`；
- `outcome_reflections`；
- `controlled_research_memories`；
- `next_trading_day_acceptance_reports`。

并为投资者建议采纳表增加成交方向、交易日期、成本和建议关系字段。迁移继续使用 checksum 注册、顺序初始化、旧库升级前备份和重复执行幂等机制。

## 9. 新增 API 与 Chat 工具

API：

- `GET /api/investment-theses`；
- `GET /api/investment-theses/{thesis_id}`；
- `POST /api/investment-theses/{thesis_id}/checks`；
- `POST /api/experiment-runs`；
- `GET /api/experiment-runs/{run_id}`；
- `POST /api/experiment-runs/{run_id}/artifacts`；
- `GET /api/research-memory/{symbol}`；
- `POST /api/runtime/next-trading-day-acceptance`。

Chat 只读工具：

- `query_investment_theses`；
- `query_investment_thesis`；
- `query_research_memory`。

论文检查和关闭没有注册为无需确认的 Chat 写工具。现有 API Token/loopback 安全边界继续覆盖全部新增 `/api` 路由。

## 10. 下一真实交易日自动验收报告

`next_trading_day_acceptance_reports` 检查：

- 当日 production 点时池；
- 必填字段覆盖；
- 实际 selected Provider 与 fallback；
- primary 是否最多创建一次；
- 正式样本和七影子账户；
- 重复 Job、重复样本和 Demo 污染；
- 数据、Worker 或 LLM 不可用时是否 fail closed。

非交易日不会伪造报告通过。该报告的真实生产结论必须等待下一真实交易日由调度链运行。

## 11. 测试与证据边界

新增 `tests/test_round8_lifecycle_recorder.py`，覆盖：

- Provider 首次超时后 max attempts、跨 component 和跨 refresh 不重叠；
- Soak 不产生虚假切换，只记录实际选中来源变化；
- Experiment Recorder 幂等、输入漂移拒绝、Artifact 和 run link；
- checkpoint 恢复不重复 callback，签名变化在副作用前拒绝；
- Thesis 红线优先、缺证据 `needs_review`；
- Reflection 正式边界、完成状态和到期时间；
- 跨标的记忆降权；
- 下一交易日验收的重复检测和 fail-closed。

质量门禁为本轮关键模块设置至少 80% 覆盖率。最终结果：Ruff、`compileall`、414 项 pytest、Streamlit AppTest、敏感信息扫描和完整质量门禁全部通过；总覆盖率 78.99%。本轮关键覆盖率为 Provider Router 98.59%、Round8 Repository 97.19%、Experiment Recorder 98.88%、Investment Thesis 82.14%、Reflection 80.36%。机器结果见 `data/reports/quality-gate-latest.json` 和 `data/reports/coverage.json`。

## 12. 仍不能宣称的事项

- 不能宣称系统已经产生前瞻盈利；
- 不能宣称 LLM 已经带来正 Alpha；
- 不能用 Historical Demo 代替正式前瞻证据；
- 不能把免费数据源描述成有 SLA 的商业生产数据；
- 不能把 signed-turnover 等代理指标描述成确认的机构净流入；
- 不能把工程完成等同于券商实盘准入；
- 下一真实交易日、5/20 交易日自然到期和足量成熟样本仍必须等待真实时间。

## 13. 2026-07-18 本机真实运行验收

- `round8:1` 在现有 `data/quantlab.db` 上完成升级；升级前备份为 `data/backups/quantlab-20260718T141805236976Z-pre-migration.db`，SHA-256 为 `0efdbe4cb9e31d802ed84fc2823a3b617b08b550e025dfff8ec5ed08de8eb802`；
- 重复运行迁移返回 `pre_upgrade_backup=null`，证明没有重复应用；
- SQLite `integrity_check=ok`，外键违规为 0；
- API、Worker、Scheduler、Notification Worker 四进程均成功启动并保持健康心跳；
- 同日 Scheduler 重复执行返回 `idempotent=true`，重复 Job 幂等键组为 0；
- 当天为休市日，12 个交易日任务正确跳过，primary、正式样本和七影子账户仍为 0；
- 实际 Provider refresh 中 BaoStock 日历调用在 45 秒超时，security master 和 industry 被 single-flight 级联阻止，没有形成重叠调用；三个 component 的 unavailable/失败原因、attempt 和指纹已持久化；
- Soak 共 17 个真实观测点，首尾实际间隔 8,903.20 秒，四进程观测可用率 100%，实际 Provider 切换为 0；
- `runtime-stop` 请求停止 4 个进程，强制终止列表为空；交付时四进程均为 `stopped`；
- Windows 自启保持 `not_installed`。

本次 BaoStock 超时没有被改写成成功，最新数据源状态为 unavailable，同时保留 2026-07-18 11:55 UTC 左右的最后成功时间和历史成功记录。这证明 fail-closed 与审计链工作正常，但不证明免费源具备稳定 SLA。
