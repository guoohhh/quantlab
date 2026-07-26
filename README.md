# QuantLab

QuantLab 是面向个人投资者的可审计 AI 投资研究与模拟交易系统。它把市场发现、量化策略、
多 Agent 研究、反对证据、确定性风控、用户确认、模拟成交和事后复盘连接成一条可追溯链路。

它不是自动炒股工具，不连接券商自动下单，也不承诺收益。LLM 负责解释、比较、反证和形成
操作草稿；行情可用性、费用、仓位、T+1、涨跌停、订单状态和证据准入由确定性代码决定。

## 三分钟隔离演示

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-hackathon-demo.ps1
```

这条路线不需要 API Key 或外部行情，只启动本机 Streamlit，并使用隔离数据库完成“候选 → 冻结研究
→ 确定性风控 → 用户确认 → 模拟成交 → 盈亏与复盘”。完整现场脚本与失败回退见
[黑客松现场手册](docs/HACKATHON_DEMO.md)。

## 当前产品入口

Streamlit 产品界面当前有 5 个一级决策工作区：

- `今日`：资产、持仓、风险、提醒和待处理事项；
- `市场与发现`：市场状态、资金线索、搜索、自选与候选；
- `研究台`：冻结研究、多空证据、AI 追问和研究任务；
- `组合与交易`：用户模拟账户、交易前检查、委托、成交和盈亏；
- `决策复盘`：研究、论文、订单、结果与 Reflection。

AI 助手、账户、通知、设置和帮助中心位于工具区。研究详情、专家圆桌和设置是从工作区进入的
独立页面，不属于额外一级入口。

## 快速开始

```powershell
cd E:\LLM-Projects\quant-lab\my-system
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,agents,ui,data,api]"
Copy-Item .env.example .env
quantlab doctor
streamlit run dashboard/app.py --server.port 8510
```

没有 API Key 时可使用 Mock Provider 验证工程链路。配置真实模型前，请在设置页或本机 `.env`
中明确选择 DeepSeek、OpenAI 官方端点或用户确认过的 OpenAI-compatible 端点。不要把官方
API Key 静默发送到第三方网关。

常用运行命令：

```powershell
quantlab doctor
quantlab llm-status
quantlab runtime-start
quantlab runtime-status
quantlab runtime-stop
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\quality-gate.ps1
```

黑客松隔离演示使用 [docs/HACKATHON_DEMO.md](docs/HACKATHON_DEMO.md)，不会写入正式前瞻证据。

## 文档权威边界

不要用文件修改日期猜测真实性。不同问题使用不同权威来源：

| 问题 | 首选依据 |
|---|---|
| 产品定位、非目标和合规边界 | [PRODUCT_STRATEGY.md](PRODUCT_STRATEGY.md) |
| 当前实现是否存在 | 当前代码、Schema、API 和测试 |
| 当前运行、数据和实验状态 | `quantlab runtime-status`、生产数据库、`data/reports/*-latest.json` |
| 面向用户的功能与使用方式 | [PROJECT_HANDBOOK.md](PROJECT_HANDBOOK.md) 和 [docs/README.md](docs/README.md) |
| 策略能否进入正式证据 | 对应预注册协议、不可回写样本和到期结果 |
| 历史设计过程 | 标有“历史快照”的 Round 文档，仅用于追溯 |

静态 Markdown 不再宣称“当前 Provider 数量、当前样本数、当前进程状态或最新测试数”。这些值
会随运行变化，必须从机器可读来源重新核验。

## 关键文档

- [docs/AI_HANDOVER.md](docs/AI_HANDOVER.md)：最终验收 AI 的 15 分钟接管入口与动态检查清单；
- [PROJECT_HANDBOOK.md](PROJECT_HANDBOOK.md)：产品使用、核心流程和边界；
- [docs/README.md](docs/README.md)：文档地图和历史资料使用规则；
- [docs/DOCUMENT_STATUS.md](docs/DOCUMENT_STATUS.md)：文档权威、维护状态和历史资料分类；
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)：本机部署、Worker、Scheduler 和备份；
- [docs/HACKATHON_DEMO.md](docs/HACKATHON_DEMO.md)：隔离演示路线；
- [docs/DATA_SOURCE_LICENSES.md](docs/DATA_SOURCE_LICENSES.md)：数据许可与再分发边界；
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)：第三方许可证；
- [PROMPT_GOVERNANCE.md](PROMPT_GOVERNANCE.md)：LLM Prompt、结构化输出和降级约束；
- [STRATEGY_ROUND3_PREREGISTRATION.md](STRATEGY_ROUND3_PREREGISTRATION.md)：冻结实验协议示例；
- [AI_REVIEW_GUIDE.md](AI_REVIEW_GUIDE.md)：外部审阅所需证据与声明边界。

## 证据与安全

- 历史回测、Historical Demo、用户模拟账户和正式前瞻实验严格隔离；
- 后台任务完成不等于数据 readiness 或实验有效；
- 免费数据源没有商业 SLA，缺失或陈旧时必须 `unavailable` / fail closed；
- 正式前瞻样本不得回填、改写或用恢复运行冒充首次自然窗口；
- 所有模拟订单仍需用户明确确认；真实交易需在券商端再次核对。

本项目仅用于研究和辅助决策，不构成投资建议。
