# QuantLab 外部 AI 审阅指南

## A股新增首要审阅入口

1. `data/reports/a-share-strategy-lab-v3-development.json/.md` 与 `a-share-strategy-lab-v3-validation.json/.md`：严格分阶段的完整开发期和单次冻结验证；验证为正收益、正超额，但 Bootstrap 门槛失败；
2. `A_SHARE_STRATEGY_V2_POSTMORTEM.md` 与 `A_SHARE_STRATEGY_V3_POSTMORTEM.md`：截断样本偏差、失败保留、状态归因和组合层反证；
3. `PORTFOLIO_EVIDENCE_POLICY.md` 与 `src/quantlab/workflows/portfolio.py`：主动策略不如可投资基准时，默认选择 ETF 等权核心；A股未准入时订单预算为0；
4. `data/reports/stock-market-replay-7.json/.md`：30回合点时全市场分层证据、同暴露基准、快照完整性和策略准入失败原因；
5. `src/quantlab/workflows/universe.py`：交易所主数据、点时快照交叉验证和确定性分层抽样；
6. `src/quantlab/data/baostock.py`：历史ST/停牌、原始/复权行情和代码连续性；
7. `src/quantlab/workflows/stock_market_replay.py`：退市终值、同暴露指数、学习准入和策略双门禁；
8. 股票模型挑战记录：5日候选差于 Brier/Log Loss 基线，未激活。

评审时请区分：Replay #7 的数据证据是 `measured`，但策略状态是 `research_only`。把前者误读为盈利或上线资格，应视为错误评审。

## 2026-07-14 首要审阅入口

1. `data/reports/decision-gate-scorecard-latest.json/.md`：跨 Replay 的 V1 失败、V2 小样本、概率消融和未晋级原因。
2. `data/reports/historical-replay-7.json`：独立12回合负面证据，证明 LLM-first 闸门损害收益。
3. `data/reports/historical-replay-8.json` 与 `historical-replay-9.json`：V2 的20日/5日挑战，包含真实角色完整性、统计概率和逐策略资金曲线。
4. `DECISION_GATE_AUDIT_PROTOCOL.md` 与 `DECISION_GATE_V2_RESEARCH_PROTOCOL.md`：事前阈值、失败保留与晋级边界。
5. `data/reports/profitability-evidence-latest.json`：validation #16、18折504/126 OOS、基准、bootstrap、成本压力和 `preliminary 60/100`。

审阅时应特别检查：V2 是否被错误宣传为模型盈利增益（当前没有）；Reviewer 文本是否再次越权；统计模型是否只使用 `evaluated_at < cutoff` 的样本；5日和20日是否被错误合并；失败的V1/V3证据是否仍保留。

这份文件是给独立 AI、黑客松评审或代码审计者的入口。请不要根据 README 的功能数量直接评分；先运行质量门禁，再检查下列关键断言是否能由代码和持久化证据复现。

## 一键验收

```powershell
cd E:\LLM-Projects\quant-lab\my-system
.\scripts\quality-gate.ps1
```

质量门禁会执行 Ruff、`compileall`、全量测试、总覆盖率、关键金融模块覆盖率、Streamlit `AppTest`、报告敏感字段扫描和必需 Demo 产物检查。它不会读取或打印 `.env`。

## 建议评分维度

| 维度 | 权重 | 重点检查 |
|---|---:|---|
| 时间一致性与防泄漏 | 20 | 信号截止日、点时标签、未来行情隔离、历史快照过滤 |
| 交易与组合真实性 | 20 | T+1、整手、费用、滑点、连续资金曲线、总/单标的/行业暴露 |
| 多 Agent 与 LLM 治理 | 15 | 独立职责、结构化输出、完整角色覆盖、回退、熔断、Reviewer |
| 学习与概率验证 | 15 | 时间切分、模型准入、Brier/Log Loss、原始/统计/融合消融 |
| 工程质量与测试 | 15 | 自动化测试、关键模块覆盖率、异常路径、持久化与可复现命令 |
| 产品与可解释性 | 10 | 今日决策、手工下单边界、前端模型选择、审计报告 |
| 结论诚实性 | 5 | 是否明确区分“链路可用”“历史有效”和“未来盈利” |

## 关键断言与证据位置

| 断言 | 主要实现 | 自动化证据 |
|---|---|---|
| 历史盲测不向 LLM 暴露真实代码、日期和绝对价格 | `src/quantlab/workflows/replay.py` | `tests/test_historical_replay.py` |
| 点时模型只使用截止日前已经到期的标签 | `src/quantlab/learning/trainer.py` | `test_point_in_time_predictor_excludes_labels_not_known_at_cutoff` |
| 不能用重复调用冒充完整 Agent 委员会 | `expected_llm_role_keys`、`_episode_llm_validation` | 缺失 Reviewer/重复 Forecast 的反例测试 |
| 三条回放曲线分别连续复利 | `path_capital`、`capital_before/after` | 回合间资金连续性断言 |
| 主动组合执行单标的、总暴露和行业上限 | `src/quantlab/portfolio/planner.py` | `tests/test_portfolio_planner.py` |
| 可转债策略不会读取未来快照 | `src/quantlab/strategies/convertible_bond.py` | `test_convertible_bond_strategy_ignores_future_snapshots` |
| LLM客户端在事件循环关闭前释放，且关闭异常不覆盖主异常 | `await_with_provider_close` | `tests/test_security.py` 及真实 Replay #3 |
| 报告既删除敏感字段，也脱敏普通文本中的 Key/Bearer/Secret | `src/quantlab/security.py`、`reporting.py` | 对抗性回显测试与质量门禁扫描 |
| AkShare原始价用于成交、后复权价用于信号 | `src/quantlab/data/akshare.py` | `tests/test_akshare_provider.py` |
| Reviewer 可复核30日原始/后复权 OHLC、120日归一化路径、收益、波动、回撤、均线与成交额，且截止日之后数据不会进入证据 | `build_price_history_evidence`、`ResearchContext.price_history` | `tests/test_research.py`、`test_price_history_reaches_reviewer_and_restores_coverage_contribution` |
| 当前价与 MA5/20/60/120 的关系由系统确定性计算，Agent 不再自行心算；stance 与 score 方向在委员会聚合前强制一致 | `latest_signal_close_vs_moving_averages`、`_normalize_opinion_score` | `tests/test_research.py`、`test_expert_score_sign_is_normalized_to_structured_stance` |
| 损坏缓存不会污染后续研究 | `src/quantlab/data/cache.py` | 截断JSON自动淘汰与原子重写测试 |
| API可启用全接口令牌认证，公开错误不会回显Key或本地路径 | `src/quantlab/api/app.py` | API认证、非法代码和错误脱敏测试 |
| 候选横向排名不能绕过 Reviewer、veto 或人工复核要求 | `src/quantlab/workflows/tournament.py` | `tests/test_candidate_tournament.py` 的否决与入围反例 |
| 高正相关候选被去重，但负相关候选不会被错误排除 | `rank_tournament_candidates` | 合成收益序列的正/负相关断言 |
| 压力测试的情景盈亏、VaR/CVaR和风险贡献可复算 | `stress_test_portfolio` | 手算情景 P&L、尾部风险和贡献求和测试 |
| 擂台排名会在5/20日后用真实收益结算并与原雷达基准比较 | `settle_candidate_tournaments`、`candidate_tournament_scorecard` | 第一名超额、排名IC、pending与重复结算测试 |
| 用户自选股票会批量计算而不是逐只重复拉取行情 | `src/quantlab/workflows/stock_discovery.py` | 代码归一化、批量Bars和最多20只边界测试 |
| 系统推荐不会被第一个风格垄断，也不会在空结果时造股票 | `recommend_stocks` | 多风格合并、单风格失败与空结果测试 |
| 暴涨高波动股票不会只因动量强而无惩罚排在前列 | `risk_adjustment` | 抛物线高波动合成路径测试 |
| 最多5只股票可独立运行投资大师和完整委员会，单股失败不拖垮批次 | `run_stock_research_batch` | 批量成功/失败隔离测试 |
| 专家圆桌只能读取冻结研究证据，不能修改正式决策、仓位或订单 | `src/quantlab/agents/roundtable.py`、`workflows/roundtable.py` | 多轮并行、持久化、未知角色、敏感字段删除和 `formal_decision_changed=false` 测试 |
| 策略正收益不能绕过等权基准、统计显著性和2倍成本压力 | `backtest/statistics.py`、`workflows/validation.py` | embargo、块 bootstrap、多重试验 PSR、OOS 排名和成本缩放测试 |
| 陈旧但刚写入缓存的数据不能冒充当前行情 | `data/quality.py`、`data/fallback.py`、`data/cache.py` | 缺标的、1999天尾部缺口、宽区间缓存复用和自动 fallback 测试 |
| 新统计模型不能只因优于类别基线就替换冠军 | `learning/trainer.py`、`learning/repository.py` | 前瞻冠军—挑战者、配对 Brier bootstrap、pending 与晋级测试 |
| 新策略不能看完留出后改候选或放宽 alpha 门槛 | `STRATEGY_RESEARCH_PROTOCOL.md`、`strategies/adaptive_etf.py`、`workflows/strategy_lab.py` | 四候选注册表、开发/留出分区、失败 benchmark/statistical gate 测试 |
| V2 高相关 ETF 去重、协方差退化和状态冲击不能导致仓位失控 | `strategies/adaptive_etf_v2.py`、`workflows/etf.py` | 相关性惩罚、权重上限、动态核心、压力缩放、再平衡容差和配置反例测试 |
| A股动态市场状态政策不能使用信号日之后的沪深300数据 | `resolve_a_share_ranking_policy`、`stock_strategy_lab_v3.py` | 未来价格扰动不改变信号日状态、历史不足自动风险关闭 |
| 验证失败的主动策略不会压过更强的可投资基准进入默认订单 | `select_evidence_first_etf_policy`、`generate_portfolio_plan` | ETF等权政策选择、A股零预算和10万元高价债券ETF整手测试 |

## 可直接审阅的真实产物

- `data/reports/latest-demo.md`：真实市场一键研究报告；
- `data/reports/latest-demo.json`：结构化审计包；
- `data/reports/historical-replay-3.md`：最新固定区间历史盲测；
- `data/reports/historical-replay-3.json`：包含逐角色覆盖和连续资金曲线的完整审计数据；
- `data/reports/coverage.json`：最新测试覆盖率。
- `data/reports/quality-gate-latest.json`：机器可读质量门禁与最新真实回放摘要，不包含自评分。
- `data/reports/profitability-evidence-latest.json`：validation #16 的机器评分、数据/源码/参数指纹、18折 OOS、证据优先组合政策、A股V3冻结验证、成本压力和可声明边界；
- `data/reports/profitability-evidence-latest.md`：供评审快速阅读的盈利能力证据摘要；
- `data/reports/strategy-lab-latest.json`：四个预注册候选、开发期选择、锁定留出、2倍成本和准入失败详情；
- `data/reports/strategy-lab-latest.md`：自适应 ETF 候选快速摘要；
- `data/reports/adaptive-v2-diagnostic-latest.json` 与 `.md`：V2.1 的相关性/状态/动态核心能力及回顾性探索结果，明确标记为不可准入证据；
- `data/reports/stock-research-sh600519-final.md`：最终贵州茅台真实18角色研究报告；
- `data/reports/stock-research-sh600519-final.json`：包含332条价格证据、120日路径、完整委员会、双周期预测、确定性轨迹和18/18真实调用审计；
- `data/reports/stock-research-sh600519-price-evidence.json` 与 `stock-research-sh600519-score-sign-rejected.json`：保留 Reviewer 分别拒绝均线事实错误和 stance/score 矛盾的对抗性证据。

Replay #3 只有2个样本，纯策略为负、完整系统未交易、沪深300同风险预算为正。第一回合 Bear 角色首选端点超时后成功回退，最终规定角色22/22完整。这个结果被原样保留，证据等级为 `illustrative`，不能证明未来盈利。

## 已知限制与应扣分项

1. LLM 历史盲测与A股市场回放必须分开看：A股Replay #7已达30回合 measured，但ETF/LLM各挑战的样本量仍有限；
2. ETF 池是当前配置的固定标的池，不是逐日成分历史，仍存在可交易池/幸存者偏差限制；
3. A股反转和可转债双低尚未达到 ETF 轮动同等级的长区间 walk-forward 证据；
4. 免费数据源没有 SLA，接口变化会导致显式降级；
5. 总覆盖率门槛为71%，核心金融、安全和数据契约模块要求更高，但 CLI、westock外部适配器和部分编排工作流覆盖率仍低；
6. 不连接券商，成交只是假设成交或用户手工登记；
7. LLM输出存在非确定性，模型质量和代理端点可用性不由本项目保证。
8. 候选擂台会线性增加 LLM 调用成本，且当前排名权重是透明先验，尚未由足量前瞻收益样本学习得到。
9. 擂台成绩单在30个完整结算样本前只是 `illustrative`；系统已经收集标签，但不会在小样本上自动改权重。
10. 股票发现排名公式仍是透明先验；Replay #7虽优于简单动量，但相对同暴露沪深300的区间跨零且平均Rank IC为负，不能宣称稳定Alpha。
11. 股票估值已形成EPS、FCF/股和净资产三类确定性区间，但免费财务数据的点时覆盖、市场价值和净现金口径仍需扩展。
12. 正式 ETF validation #14 虽跑赢沪深300且通过2倍成本压力，但未跑赢 ETF 池等权，统计超额也未通过，因此证据等级仍是 `preliminary 60/100`。
13. 现役5/20日统计模型是在冠军—挑战者治理上线前激活的 legacy 版本，生产融合权重已降为0；需要新的到期样本形成真正前瞻留出并完成晋级后才能影响概率或仓位。
14. `adaptive_defensive` 显著改善 Sharpe 和回撤，但锁定历史留出仍落后 ETF 等权10.95个百分点，不能作为已证明 alpha；同一留出不得继续用于下一版策略的确认性评分。
15. `adaptive_v2` 在复用历史区间取得44.66%收益、1.53 Sharpe和-5.41%回撤，但仍落后ETF等权9.85个百分点；它只能作为前瞻挑战者，不能用更高风险效率替代未通过的收益与统计alpha门槛。
16. A股V3冻结验证取得+3.68%，同仓位沪深300为+1.70%，但超额为正概率只有59.93%；这是方向性正证据，不是稳定alpha证明，2026留出仍锁定。
17. +83.96%仍只是折级等权方向证据；当前订单另有同协议验证：45%暴露、半年调仓、2%容差、整手、无LLM覆盖和无ATR止损，历史成本后+47.70%、最大回撤-14.29%、2倍成本+47.24%。该固定六ETF资产池仍存在事后成员选择边界，不能声明未来保证。

这些限制应当影响评分。若审阅者仍给出90分以上，应来自防泄漏、风险约束、审计性、核心测试和产品闭环，而不是来自收益承诺。

## 推荐给审阅 AI 的任务文本

> 请以量化研究负责人、金融风控工程师、Python架构师和黑客松评审四个视角审阅本项目。先运行 `scripts/quality-gate.ps1`，再阅读 `AI_REVIEW_GUIDE.md` 指向的实现与测试。请重点寻找时间泄漏、幸存者偏差、错误成交假设、概率评估误用、LLM降级失效、风险上限未执行、Key泄露和文档夸大。按严重级别列出发现，给出0-100总分及分项分，并明确说明达到95分还缺什么。不要因为功能数量多而忽略证据质量。
