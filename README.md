# QuantLab · 可审计的 AI 投研工作台

> 面向个人投资者与小团队的 **AI 投资研究 + 模拟交易** 系统。基于 WorkBuddy 搭建,可运行、可验证、可复用。
>
> 一句话:**AI 负责提议,确定性代码负责把关,人负责拍板。**

**合规声明**:本项目仅用于技术研究与辅助决策,不自动连接券商下单、不承诺收益、不构成投资建议。所有模拟订单均需用户明确确认;真实交易请在券商端二次核对。

---

## 为什么做这个

大多数「AI 炒股」工具的问题是:你**无法验证它为什么这么说**,也**无法阻止它说错话时造成的后果**。

QuantLab 换了个思路 —— 把大模型关进笼子:

- LLM 只做它擅长的事:**解释、比较、反证、形成操作草稿**;
- 仓位、回撤、集中度、ST、T+1、涨跌停、交易成本这些**风控红线,交给确定性代码强制执行**;
- 每一步都留痕、可回放,**人必须亲自确认才能模拟成交**。

结果是一条完整可追溯的决策链路,而不是一个"黑盒建议"。

---

## 核心链路

```
市场发现 → 冻结研究 → 多空证据 → 确定性风控 → 用户确认 → 模拟成交 → 事后复盘
```

每个环节都可追溯、可复现,并与历史回测、演示数据严格隔离。

---

## 亮点

- **五角色多 Agent 圆桌**:技术面、动量、宏观按权重投票,风险与价值角色拥有 **veto(否决)** 权;缺失估值数据不会被当成"通过"。
- **确定性风控引擎**:仓位上限、单票 / 行业集中度、组合回撤、ST 否决、人工复核标记等全部是**硬约束**,由代码而非 LLM 判定。
- **决策闸门(Decision Gate)**:`human_review_required` 与 `council_veto` 会直接拦截下一步,AI 说服不了闸门。
- **证据隔离与 fail-closed**:历史回测、隔离演示、用户模拟账户、正式前瞻实验四者互不污染;免费数据源缺失或陈旧时明确标记 `unavailable`,绝不编造。
- **6 类量化策略 + 56 个业务工作流**:自适应 ETF(v1/v2/v3)、ETF 轮动、可转债、个股反转等,配套候选发现、资金流、证据构建、复盘等完整工作流。
- **三分钟无 Key 隔离演示**:不需要 API Key 或外部行情,用隔离数据库跑通完整闭环。

---

## 技术栈

- 语言:Python 3.11+
- 多 Agent:`LangGraph` + OpenAI-compatible 模型(支持 Mock Provider 离线验证)
- 计算:`pandas` / `numpy`
- CLI:`Typer` + `Rich`
- 数据:`akshare` / `baostock`(A 股),`pyarrow` 缓存
- 界面:`Streamlit` + `Plotly`(5 个一级决策工作区)
- 服务:`FastAPI` + `Uvicorn`
- 校验:`pydantic` 全链路结构化输出,88 组测试

---

## 三分钟隔离演示

无需 API Key、无需外部行情,直接跑通「候选 → 冻结研究 → 确定性风控 → 用户确认 → 模拟成交 → 盈亏与复盘」:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-hackathon-demo.ps1
```

演示使用隔离数据库,不会写入正式前瞻证据。

---

## 从零启动

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,agents,ui,data,api]"
Copy-Item .env.example .env

quantlab doctor          # 环境自检
quantlab llm-status      # 查看模型 Provider 状态
streamlit run dashboard/app.py --server.port 8510
```

没有 API Key 时使用内置 Mock Provider 即可验证完整工程链路。配置真实模型请在设置页或 `.env` 中明确选择 DeepSeek / OpenAI 官方端点,不要把官方 Key 静默发往第三方网关。

常用命令:

```powershell
quantlab doctor            # 环境与依赖自检
quantlab llm-status        # 模型 Provider 状态
quantlab demo              # 命令行跑通一次决策演示
quantlab recent-decisions  # 查看最近的决策记录
quantlab runtime-start / runtime-status / runtime-stop
```

---

## 产品界面

Streamlit 界面包含 5 个一级决策工作区:

| 工作区 | 你在这里做什么 |
|---|---|
| **今日** | 资产、持仓、风险、提醒和待处理事项一屏总览 |
| **市场与发现** | 市场状态、资金线索、搜索、自选与候选池 |
| **研究台** | 冻结研究、多空证据、AI 追问、研究任务 |
| **组合与交易** | 模拟账户、交易前检查、委托、成交与盈亏 |
| **决策复盘** | 研究、论文、订单、结果与 Reflection 复盘 |

---

## 项目结构

```
src/quantlab/
  agents/        # 多 Agent 圆桌、角色权重、决策闸门、否决策略
  risk/          # 确定性风控引擎与过滤器(硬约束)
  strategies/    # 6 类量化策略(自适应ETF/轮动/可转债/个股反转...)
  execution/     # 模拟成交、交易成本、T+1/涨跌停规则
  portfolio/     # 组合配置、仓位规划、市场状态识别
  backtest/      # 回测引擎、统计、Walk-Forward
  workflows/     # 56 个业务工作流(候选/资金流/证据/复盘...)
  learning/      # 决策归因、漂移检测、特征与训练
  data/          # 数据源适配、缓存、质量校验、fail-closed 回退
  llm/           # Provider 治理、结构化输出、模型路由
  persistence/   # 决策、证据、订单、通知的持久化
dashboard/       # Streamlit 产品界面(5 个工作区)
scripts/         # 隔离演示、每日任务、质量闸门
tests/           # 88 组测试
```

---

## 设计原则(可验证的边界)

- **职责分离**:LLM 提议,确定性代码判定,人确认。三者缺一不可。
- **证据准入**:历史回测、演示、模拟账户、正式前瞻实验严格隔离,互不回填。
- **fail-closed**:数据缺失或陈旧一律标记 `unavailable`,宁可不给结论,不给错结论。
- **不可回写**:正式前瞻样本不得回填、改写,或用恢复运行冒充首次自然窗口。

---

## 后续方向

- 接入更多带来源与时间戳的真实行情 / 公告 / 财报数据源;
- 决策闸门策略的可视化配置与 A/B 对比;
- 组合层相关性与风险预算的自动约束;
- 研究报告一键导出为 Markdown / PDF。

---

*本项目仅用于研究与辅助决策,不构成投资建议。*
