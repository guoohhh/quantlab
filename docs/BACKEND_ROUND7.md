# QuantLab 第七轮交付：生产数据 Readiness、五入口流程与黑客松 Demo

> **历史快照**：本文只记录第七轮当时的交付，不是当前功能清单、运行状态或质量报告。当前事实请从 `../README.md`、`../PROJECT_HANDBOOK.md`、代码、数据库和最新机器报告核验。

## 1. 本轮结论

第七轮没有新增策略、Agent、模型阈值或交易规则。`primary-forward-v2`、七个消融变体、
成本模型和撮合规则均未修改。本轮收口的是三件事：

1. 生产数据能按 Provider/component 独立超时、重试、熔断和降级，并生成可审计的证券主
   数据、行业历史与交易日点时池；
2. 五入口普通模式能完成模拟交易、真实只读组合建议采纳、通知和 Chat 的日常操作，不再
   要求用户进入高级工程页面；
3. Live Demo、冻结历史研究 Demo、Windows 自启管理和实际时长 Soak 报告形成稳定边界。

工程完成仍不等于已证明未来盈利。正式 primary、5/20 日样本和七影子账户必须继续等待
真实交易日、真实数据和自然到期时间。

## 2. 生产数据链与真实结果

正式自动链为：

```text
服务器配置文件
→ BaoStock（日历、证券主数据、行业、交易状态）
→ AkShare EastMoney（价格、成交额、换手、市值、可得行业）
→ AkShare Sina（第二免费现货源，价格、成交额、成交量）
→ unavailable
```

每个 Provider/component 独立记录尝试次数、延迟、连续失败、最后成功、错误类型和熔断
截止时间。一个现货源失败后会继续尝试下一个现货源；同一 Provider 的底层调用一旦超时，
本轮后续同 Provider 调用不会重叠启动，避免后台线程继续占用连接时形成级联阻塞。

2026-07-18 的真实周末刷新结果：

| 数据域 | 实际结果 | 记录数 | 可信边界 |
|---|---|---:|---|
| 交易日历 | completed | 491 | BaoStock，`server_observed/unverified_no_sla` |
| A股证券主数据 | completed | 5,536 | BaoStock；代码变更保留 lineage/aliases |
| 行业归属 | completed | 5,203 | BaoStock 官方行业接口；覆盖率 93.98% |
| 当日正式点时池 | skipped_non_trading_day | 0 | 当天休市，按规则不得生成正式池 |

证券代码迁移会在当前快照保留一条有效记录，同时把旧代码写入
`source_symbol_aliases/lineage_records`。主数据不可变指纹只覆盖稳定业务内容，不再把每次
抓取的 `available_at/manifest_id` 当成内容篡改。

周末允许刷新日历、主数据和行业；`trusted_data_refresh` 因此改为每日可运行。点时池、
primary 登记、结算和盯市仍由正式日历关闭。

## 3. Readiness 与 primary 启动边界

readiness 显示：

- 日历、主数据、行业和点时池记录数；
- eligible 数量；
- 逐字段覆盖率与关键字段最低覆盖率；
- Provider 分布、最后成功、数据延迟和连续失败；
- LLM、Worker、Scheduler、质量门禁和上海市场日期；
- 是否允许启动、是否允许登记样本以及逐项 blocker。

休市日的点时池是 `skipped_non_trading_day`，不会累计新的失败；如果日历本身不可用，则仍
返回 `unavailable`，不能用“休市”掩盖数据故障。正式 primary 只能由 scheduler-owned 的
真实交易日任务原子创建；public API、backfill、Demo 和人工研究都不能启动。

## 4. 五入口产品流程

普通模式仍固定为：

1. 首页；
2. 行情与发现；
3. AI研究；
4. 模拟交易；
5. 我的。

普通模式不再调用 `st.json` 展示 ContextPack、readiness、Brier、Prompt、Worker 或数据库
结构。必要信息被翻译为数据状态、来源、时间、质量、阻断原因和下一步操作；完整 JSON
继续保留在高级/审计模式。

### 模拟交易

- 创建和切换账户；
- 买入、加仓、减仓、清仓；
- 服务器行情交易前检查和二次确认；
- pending、部分成交、全部成交、撤单、拒绝和过期；
- 订单详情、事件时间线、成交明细；
- 现金、冻结现金、持仓、已实现/未实现盈亏和费用；
- 净值、基准和单笔复盘；
- 行情变化后必须重新检查，非 actionable 行情不展示确认按钮。

### 真实只读组合

- CSV 预览/确认、盯市和持仓；
- AI 检查卡、建议动作/数量区间、操作后仓位/现金、最大计划损失；
- 支持证据、反对证据、失效条件和数据可靠性；
- 采纳、部分采纳、拒绝和外部成交；
- 未结算前允许修正并保存版本化审计；已有后续同标的交易或到期结果时拒绝直接改写；
- 5/20 日产品效果和采纳/拒绝分组比较；始终
  `training_eligible=false/forward_scorecard_eligible=false`。

### 通知与 Chat

- 通知未读、全部已读、归档、严重程度/账户/标的筛选、去重键、冷却、静默和渠道状态；
- Chat 创建/切换会话、证据引用、后台 Job 进度、取消和失败后安全重试；
- Chat 订单与预警只生成草稿，必须独立确认；订单确认重新获取服务器行情。

## 5. Demo 隔离

### Live Demo

只读取当前服务器真实数据，展示来源、时间和状态。数据或执行行情不可用时保持 blocked，
不会自动切换到历史数据冒充实时成功。

### Historical Research Demo

冻结数据集：`data/demo/historical-research-v1.json`。数据来源于已缓存的服务器观测，保存
源缓存 SHA-256、版本、日期和研究边界。它使用独立 Demo SQLite 数据库完成：

```text
冻结候选 → 支持/反对证据 → 交易前检查 → 用户确认
→ 次交易日模拟成交 → 持仓、费用和盈亏
```

该模式固定 `research_only=true`，不进入用户正常账户、训练、primary 或正式 scorecard；
重复运行复用同一 Demo 订单和成交，不重复扣款。

## 6. Windows 自启与 Soak

新增 CLI：

```powershell
quantlab runtime-autostart-install
quantlab runtime-autostart-status
quantlab runtime-autostart-disable
quantlab runtime-autostart-remove
quantlab runtime-soak-observe
quantlab runtime-soak-report
```

安装自启必须由用户显式执行。任务计划程序使用固定工作目录、固定 Python 环境和隐藏窗口，
命令行与 launcher 不包含 API Key；现有数据库单实例租约继续防止重复运行。

Soak 记录 API/Worker/Scheduler/通知心跳、任务、数据状态、Provider 切换、primary、正式样本、
影子账户、订单、通知、备份、数据库增长和 LLM 24 小时调用。报告时长只取实际观测点的
首尾间隔；加速测试不计为真实多日运行。

## 7. Schema、API 与兼容性

迁移顺序增加 `round7:1`：

```text
simulator → chat → notifications → evidence → strategy_evidence
→ jobs → round5 → round6 → round7
```

新增表：

- `trusted_provider_health`；
- `trusted_industry_records`；
- `runtime_soak_observations`；
- `investor_adoption_revisions`。

新增/扩展 API：

- `GET /api/simulator/orders/{order_id}/events`；
- 通知 GET 增加 `severity/symbol/notification_type` 筛选；
- `GET /api/investor-recommendations/{id}`；
- `GET /api/investor-portfolios/{id}/recommendation-effects`；
- `GET/POST /api/runtime/soak*`；
- `GET /api/demo/live/status`；
- `POST /api/demo/historical/run`。

现有安全认证和 loopback 边界不变。

## 8. 反例测试

`tests/test_round7_readiness_product_demo.py` 覆盖：

- EastMoney 失败后 Sina fallback；
- Provider 熔断和超时级联阻断；
- 休市日刷新主数据/行业但不生成正式池；
- 严格逐字段覆盖，partial 不能通过 readiness；
- 相同主数据重复刷新幂等；
- 证券代码变更保留 lineage；
- 采纳、部分采纳、修订、幂等和产品效果隔离；
- Historical Demo 隔离、幂等和正式证据为 0；
- Windows 自启安装/查询/禁用/删除且不暴露密钥；
- Soak 只报告真实观测区间；
- 五入口显示部分成交并能撤单；
- 普通模式无原始 JSON，高级模式仍保留审计能力。

原有 Round1–Round6 的服务器行情、公共数据污染、休市、primary 幂等、通知、Chat 独立确认、
Worker 恢复和迁移反例继续全量运行。

## 9. 仍需等待的事项

1. 下一个真实交易日生成当日 production 点时池并验证成交额、换手、市值、ST、停牌、行业
   等逐字段覆盖；
2. AkShare EastMoney 当前在本机代理环境下可能失败；Sina 缺少换手和市值时只能形成
   partial，不能降低门槛；
3. primary 只能在交易日 readiness 全部通过后自然启动；
4. 正式 5/20 日样本、七影子账户和 LLM 增量仍需真实时间；
5. 免费源无 SLA，完整 ETF 份额、两融、跨境资金等仍依赖许可清晰的外部数据；
6. 系统仍不连接券商、不自动交易、不承诺收益。

## 10. 最终质量与运行证据

2026-07-18 最终验收结果：

- Ruff：通过；
- `compileall`：通过；
- pytest：406 项通过，0 失败；
- 总覆盖率：78.60%，最低门槛 71%；
- 第七轮新增模块：`round7.py` 81.2%、`autostart.py` 87.3%、`soak.py` 86.9%、`product_demo.py` 100%；
- 第七轮关键数据模块：`baostock.py` 92.9%、`trusted_data_adapters.py` 80.0%；
- Streamlit AppTest：5 个普通入口、19 个高级页签、0 异常；
- `git diff --check`：通过，仅有 Windows 换行提示；
- 源码敏感信息扫描和质量门禁硬编码 Key 扫描：0 命中；
- 迁移注册表重复执行：无新增迁移、无新备份，证明幂等；数据库完整性为 `ok`；
- 真实运行：四进程健康，Scheduler 完成 4 次 tick，Worker 完成周末可信数据刷新；Soak 7 个观测点、实际间隔 159.33 秒；
- 正常停止：4 个进程均合作式停止，0 个强制终止；自启任务仍为 `not_installed`。

机器证据见 `data/reports/quality-gate-latest.json`、`data/reports/coverage.json` 和
`data/reports/streamlit-apptest.json`。短时运行验收不能替代多日 Soak，也不能替代下一个真实
交易日的 production 点时池和 primary 自然启动验证。
