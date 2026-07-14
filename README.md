# QuantLab

## 2026-07-14 A股点时市场能力更新

- A股 V3 使用完整 2018—2022 开发期后，只打开一次 2023—2025 冻结验证：策略 +3.68%，同仓位沪深300 +1.70%，Rank IC +0.093，最大回撤 -7.76%；但 Bootstrap 超额为正概率仅59.93%，正式状态保持 `validation_failed`，未打开2026留出。
- 新增证据优先组合政策：主动 ETF 18折 OOS +65.62% 低于可投资 ETF 等权 +83.96%，因此默认订单核心自动选择 ETF 等权；主动轮动和未准入 A股策略保留为研究/影子候选。
- 默认 ETF 核心已冻结为与生产一致的45%暴露协议：半年调仓、2%偏离容差、100股整手、T+1次日开盘、无ATR止损且不受LLM订单覆盖。2015-01-05至2026-06-30成本后收益+47.70%、Sharpe 0.621、最大回撤-14.29%，2倍成本收益+47.24%；80%版本因回撤-24.78%违反系统15%限制，不作为默认。
- 交易级新鲜度闸门与研究数据覆盖分离：新增仓位最多接受1个工作日行情缺口；实测2026-07-15面对截止2026-06-30的数据时，六只ETF全部被硬阻断，不再生成过期 `actionable` 订单。
- 新增交易所版本化证券主数据、BaoStock 历史全市场股票池、历史 ST/停牌状态、退市前价格和证券代码变更连续性。
- `stock-market-replay` 可在沪主板、深主板、科创板和创业板做确定性点时分层抽样，并用同暴露沪深300指数做公平对照。
- Replay #7 已完成30回合 measured 证据：第一名 +6.88%，分散Top-3 -3.41%；最小交易所交叉验证 Jaccard 为99.9806%。
- 数据证据合格不代表策略合格。第一名对沪深300的超额区间仍跨零，平均排名IC为负；两个策略变体均为 `research_only`。
- 5日股票候选模型因 Brier/Log Loss 差于类别先验被拒绝激活，失败结果保留在模型挑战记录中。
- 完整报告：`data/reports/stock-market-replay-7.md`、`data/reports/stock-market-replay-7.json`。

## 2026-07-14 策略与 Agent 能力证据更新

- 新增 ETF 决策闸门反事实审查、逐回合进度、5/20 日真实 LLM 历史盲测和跨 Replay 评分卡。
- Replay #7 显示 LLM 优先闸门会损害 ETF 策略：纯策略 +4.60%，现行完整系统 -1.56%，两个 V1 候选均为负，已明确拒绝。
- V2 改为“量化策略主导、未经校准的 LLM 只解释、点时验证通过的统计模型只能减仓不能加杠杆”。2026 年 20 日 4 回合中 V2 +1.98%，5 日 11 回合中 V2 -0.02%；两者均与纯策略相同，尚无模型交易增益证据。
- 学习样本扩展为 5 日/20 日各 3,322 条。新 20 日模型在 1,622 个验证样本上的 Brier 为 0.1902，优于基线 0.2217，但治理状态仍为 `challenge_pending`。
- 最新 504/126 walk-forward 为 validation #16：18 折 OOS 收益 65.62%，但 ETF 等权为 83.96%，alpha 为正概率仅31.55%；盈利证据保持 `preliminary 60/100`，不宣称稳定 alpha。
- 关键报告：`data/reports/decision-gate-scorecard-latest.*`、`historical-replay-7/8/9.*`、`profitability-evidence-latest.*`。

面向个人投资者的多策略、多 Agent、可审计量化决策系统。

第一版不承诺收益，而是通过可复现数据、真实交易约束、样本外回测、风险预算和概率预测，尽可能提高决策质量。系统只生成手工下单建议，不连接券商自动交易。

## 已实现能力

- 免费数据源优先：westock、AkShare、损坏缓存淘汰、原子写入与显式降级
- ETF 轮动、A 股反转、可转债双低三条策略线
- A 股整手、停牌、涨跌停、T+1、佣金、印花税、过户费和滑点模拟
- 原始价格用于成交、后复权价格用于信号，备用数据源不会混用两类价格
- 市场状态识别、动态策略预算、默认四分之一凯利
- OpenAI Responses API、DeepSeek 和 OpenAI-compatible LLM 接口
- GPT/DeepSeek/本地开源模型自动路由、结构化重试、并发限制和固定回放评测
- Streamlit 前端可选择速度/平衡/质量预设，或按 Agent 单独选择 GPT 模型与推理强度
- LangGraph 多 Agent：量化、基本面、新闻、看多、看空、概率预测、决策与审计
- 确定性决策计算轨迹：综合分、组件权重、证据覆盖率和置信度可逐项复算
- 5/20 交易日预测结果自动回填、Brier Score 和命中率统计
- CLI、Streamlit 页面、SQLite 决策与审计日志
- FastAPI 研究接口、自选池、信号预警、风险画像和手工交易账本
- ETF 横截面训练、嵌套时间概率校准、线上结果回填、模型注册、概率集成和事件归因
- 真实跨资产市场雷达：20/60/120 日强弱、波动率、市场宽度、风险偏好与可选行业热度
- 多候选擂台：2–6 个领先 ETF 接受同一套多 Agent 研究，统一排名后再经过 Reviewer、否决闸门和相关性去重
- 组合压力测试：252 日历史波动/VaR/CVaR/回撤/方差贡献，以及四类透明宏观冲击情景
- 擂台学习闭环：5/20 日真实收益自动结算，持续比较 Agent 第一名、原雷达第一名、入围等权、全候选等权和事后最佳候选
- A股股票发现：支持名称/代码搜索、用户自选最多20只批量快速筛选、自选池分组和历史记录
- 五风格系统推荐：趋势质量、价值质量、成长质量、回撤修复和高股息并行发现，再统一因子重排与相关性去重
- A股点时排名回放：冻结2–20只股票，在历史时点只读取当时行情，比较系统第一名、简单动量、股票池等权、沪深300同预算和分散Top-K
- A股三账户前瞻影子盘：固定池等权、确定性排名、多Agent审核账户，执行T+1、停牌/一字涨跌停、整手和股票交易成本
- 批量股票深度会诊：用户明确选择最多5只股票，逐只运行财务双源校验、战术委员会、Buffett/Munger/Graham/Fisher/Lynch、双周期预测和 Reviewer
- 确定性保守估值：标准化EPS、所有者收益DCF和质量调整净资产形成可审计区间；数据不足不让LLM补造
- ETF / A 股研究工作台：职责路由、投资大师、概率预测、Reviewer 和可复算决策轨迹
- 专家圆桌：从已保存研究中冻结证据，用户自选 2–8 位大师/专家进行 1–3 轮并行交锋，由主持人归纳共识、分歧、证据缺口和待验证问题
- Markdown / JSON 审计报告导出，递归删除敏感字段并脱敏文本中的 Key、Bearer 和 Secret
- 黑客松一键 Demo：真实雷达 → 自动候选 → 多 Agent 研究 → 报告下载
- 今日决策中心：市场状态、建议暴露、最近决策、最大计划损失和下一步事项
- 八账户前瞻模拟盘：五个ETF/基准账户，加A股固定池等权、系统排名和完整多Agent账户
- 证据中心：成本一致的样本外基准比较，以及 LLM/统计/最终概率消融
- 严谨策略认证：隔离期 walk-forward、配对移动块自助法、多参数试验修正、选参过拟合诊断和 1.5/2 倍成本压力测试
- 预注册策略实验室：双动量、绝对趋势过滤、逆波动率、市场宽度防御切换、波动率目标和换手缓冲；开发期选冠军后只打开一次锁定历史留出
- Adaptive ETF V2 前瞻挑战者：相关性去重、收缩协方差最大分散化、连续宽度/波动率/回撤状态控制、动态核心仓位和 2% 再平衡容差；保留 V1 失败证据，不冒充新的历史确认
- 机器证据分级：按数据覆盖、防泄漏、OOS 深度、基准、统计支持、成本压力和前瞻样本自动生成 0–100 分与可声明边界
- 冠军—挑战者学习：新模型必须在现役模型未见过的时间段同场比较，晋级/拒绝/等待样本全部持久化审计
- 数据新鲜度治理：缓存和首选数据源缺标的或尾部陈旧时自动拒绝，并复用覆盖当前请求的更宽区间有效缓存
- 生产策略隔离：未准入主动ETF、A股和可转债只进入研究/复核，订单预算为0；未完成冠军—挑战者治理的统计模型生产融合权重为0
- 匿名化历史盲测：自动固定日期、隐藏身份与日期、点时训练、T+1 开盘假成交和真实未来结算
- 每日统一周期：模拟盘、预测回填、漂移监控和投资者摘要一次执行

未经 walk-forward 和足量预测样本校准的策略会被限制为低资金权重。数据源失败时，系统必须展示降级原因，不会用样例数据冒充真实行情。

## 快速开始

```powershell
cd E:\LLM-Projects\quant-lab\my-system
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,agents,ui,data,api]"
Copy-Item .env.example .env
quantlab doctor
quantlab demo
```

westock 需要 Node.js 18 或更高版本。可通过 `QUANTLAB_NODE_EXECUTABLE` 指定 Node 可执行文件。

没有 API Key 时，系统使用确定性的本地 Mock LLM 跑通全链路。真实模型配置示例：

```powershell
# OpenAI
$env:QUANTLAB_LLM_PROVIDER="openai"
$env:QUANTLAB_LLM_MODEL="gpt-5.6-terra"
$env:QUANTLAB_LLM_BASE_URL="https://code-plan.site/v1"
$env:QUANTLAB_OPENAI_REASONING_EFFORT="medium"
$env:OPENAI_API_KEY="..."

# DeepSeek
$env:QUANTLAB_LLM_PROVIDER="deepseek"
$env:QUANTLAB_LLM_MODEL="deepseek-chat"
$env:DEEPSEEK_API_KEY="..."

# 可选：对 FastAPI 全部接口启用令牌认证
$env:QUANTLAB_API_TOKEN="请使用随机长字符串"
```

## 常用命令

```powershell
quantlab doctor
quantlab etf-backtest --start 2024-01-01
quantlab candidate-scan --reversal-limit 10
quantlab portfolio-plan --reversal-limit 10
quantlab etf-walk-forward --start 2018-01-01
quantlab etf-core-validation --start 2015-01-01 --end 2026-06-30
quantlab adaptive-etf-lab
quantlab etf-variant-research --strategy-variant adaptive_v2 --start 2023-01-03
quantlab forecast-settle
quantlab forecast-calibration
quantlab learning-bootstrap --start 2023-01-01 --asset-scope etf
quantlab learning-train --asset-scope etf
quantlab learning-status
quantlab learning-collect-events sh600519 2026-07-01
quantlab learning-events --symbol sh600519
quantlab learning-cycle
quantlab llm-status
quantlab llm-replay --suite smoke --runs 1
quantlab market-radar
quantlab stock-search 贵州茅台
quantlab stock-screen "600519,000858,300750" --top-n 3
quantlab stock-recommend --styles momentum_quality,value_quality --candidate-limit 20
quantlab stock-research-batch "600519,000858" --no-include-events
quantlab stock-ranking-replay "600519,000858,300750,601318" 2024-01-01 2025-12-31 --episodes 12
quantlab stock-ranking-replays
quantlab stock-master-refresh
quantlab stock-universe-snapshot 2024-04-25 --force
quantlab stock-market-replay 2024-01-01 2025-12-31 --horizon-days 5 --episodes 30 --sample-size 40 --top-k 3
quantlab stock-ranking-replay-report 7 data/reports/stock-market-replay-7.md
quantlab learning-train --asset-scope stock
quantlab candidate-tournament --candidate-limit 2 --shortlist-size 2
quantlab candidate-tournament-settle
quantlab candidate-tournament-scorecard
quantlab analyze-symbol sh510300 --asset-type etf
quantlab research-report <run-id> --format markdown --output data/reports/research.md
quantlab evidence
quantlab evidence-report
quantlab paper-cycle
quantlab stock-paper-cycle "600519,000858,300750,601318"
quantlab paper-scorecard
quantlab historical-replay 2025-01-01 2025-06-30 --horizon-days 20 --episodes 2
quantlab historical-replay-report 3 --output data/reports/historical-replay-3.md
quantlab historical-replay-report 3 --output data/reports/historical-replay-3.json
quantlab today
quantlab daily-cycle
streamlit run dashboard/app.py
uvicorn quantlab.api.app:app --reload --port 8000
```

未配置 `QUANTLAB_API_TOKEN` 时，API 会标记为 `disabled_local_only`，只应绑定本机回环地址；配置后所有 `/api/` 请求必须携带 `X-QuantLab-Token`，令牌不会出现在状态接口或报告中。

`candidate-scan` 输出的是风控前候选，不是订单。`candidate-tournament` 会产生约“候选数 × 11”次真实 LLM 角色调用；它输出的 30% 等权组合只用于横向比较与压力测试，真正订单仍必须由组合规划器生成。`candidate-tournament-settle` 不调用 LLM，只在5/20个交易日到期后回填真实收益；少于2个成功研究候选的场次标为 `not_comparable`，不会污染成绩单或反复拉取行情；成绩单达到30个完整样本前保持 `illustrative`。预测达到第 5/20 个交易日后运行 `forecast-settle`，系统会自动回填真实收益；`forecast-calibration` 查看多分类 Brier Score、命中率和是否达到最低校准样本数。`historical-replay` 会在不知道未来结果的前提下自动抽取非重叠历史节点；报告命令根据输出后缀生成 Markdown 或 JSON。

`stock-screen` 和 `stock-recommend` 不调用 LLM，输出的是“优先研究候选”而不是买入建议。系统推荐会公开每种风格的数据失败，并对高波动、20日过度延伸和 ST 标的施加惩罚。`stock-research-batch` 才会调用真实模型：启用新闻时每只股票预计约18次角色调用，最多5只且由用户明确选择。

`stock-ranking-replay` 使用冻结股票池做点时排名证据，不调用 LLM。它不会把当前股票池冒充历年全市场成分，因此生成的股票历史样本默认 `training_eligible=false`。`stock-paper-cycle` 才是不可回写的前瞻证据；只有同日完整会诊通过的股票才会进入 `stock_full_system_shadow` 新仓。

第一次演示建议先运行 `quantlab market-radar` 验证真实行情，再打开 Streamlit 的“一键 Demo”。完整讲解顺序见 [DEMO_GUIDE.md](DEMO_GUIDE.md)。

## 设计文档

- [ARCHITECTURE.md](ARCHITECTURE.md)：统一架构与关键决策
- [REFERENCE_AUDIT.md](REFERENCE_AUDIT.md)：参考项目吸收与舍弃清单
- [REFERENCE_INTEGRATION_ROADMAP.md](REFERENCE_INTEGRATION_ROADMAP.md)：跨项目能力矩阵、P0/P1/P2 集成顺序和验收条件
- [IRONQ_PARITY.md](IRONQ_PARITY.md)：IronQ 功能逐项对标和未完成项
- [LEARNING_SYSTEM.md](LEARNING_SYSTEM.md)：持续学习、模型准入和事件归因设计
- [LLM_ROUTING.md](LLM_ROUTING.md)：多 API Key、角色路由、熔断和故障切换
- [PROMPT_GOVERNANCE.md](PROMPT_GOVERNANCE.md)：结构化输出、Prompt injection 防护、局部降级和固定回放
- [OPEN_MODEL_ROADMAP.md](OPEN_MODEL_ROADMAP.md)：本地开源模型、训练数据治理和 LoRA/QLoRA 演进路线
- [PORTFOLIO_SYSTEM.md](PORTFOLIO_SYSTEM.md)：三策略统一预算、风险闸门和手工下单清单
- [VALIDATION.md](VALIDATION.md)：walk-forward、参数敏感性和策略准入
- [STRATEGY_RESEARCH_PROTOCOL.md](STRATEGY_RESEARCH_PROTOCOL.md)：自适应 ETF 四候选、开发期和锁定留出预注册规则
- [STRATEGY_V2_RESEARCH_PROTOCOL.md](STRATEGY_V2_RESEARCH_PROTOCOL.md)：Adaptive ETF V2/V2.1 的能力边界、固定参数和前瞻晋级规则
- [DEMO_GUIDE.md](DEMO_GUIDE.md)：黑客松现场演示脚本、验收点和故障回退
- [EVIDENCE_SYSTEM.md](EVIDENCE_SYSTEM.md)：基准、消融和策略准入证据契约
- [PAPER_TRADING.md](PAPER_TRADING.md)：前瞻影子账户、次日开盘成交和每日成绩单
- [A_SHARE_EVIDENCE_PROTOCOL.md](A_SHARE_EVIDENCE_PROTOCOL.md)：A股排名回放、前瞻影子盘、学习与估值边界
- [A_SHARE_STRATEGY_V2_POSTMORTEM.md](A_SHARE_STRATEGY_V2_POSTMORTEM.md)：V2完整开发期失败与早期截断样本偏差
- [A_SHARE_STRATEGY_V3_RESEARCH_PROTOCOL.md](A_SHARE_STRATEGY_V3_RESEARCH_PROTOCOL.md)：V3市场状态策略、分阶段冻结验证和锁定留出规则
- [A_SHARE_STRATEGY_V3_POSTMORTEM.md](A_SHARE_STRATEGY_V3_POSTMORTEM.md)：V3正向验证结果、统计失败与状态归因
- [PORTFOLIO_EVIDENCE_POLICY.md](PORTFOLIO_EVIDENCE_POLICY.md)：默认选择可投资样本外冠军和10万元整手执行规则
- [AI_REVIEW_GUIDE.md](AI_REVIEW_GUIDE.md)：外部 AI 审阅入口、评分维度、证据位置和已知限制

## 风险声明

本项目仅用于研究和辅助决策，不构成投资建议。历史回测、模型预测和 Agent 结论均不能保证未来收益。
