# 运行、验证与代码地图

## 1. 环境要求

- Windows PowerShell；
- Python 3.11 或更高；
- Node.js 18 或更高，供 westock 工具使用；
- 可选的 GPT、DeepSeek 或本地模型 API；
- 项目目录：`E:\LLM-Projects\quant-lab\my-system`。

## 2. 首次安装

```powershell
cd E:\LLM-Projects\quant-lab\my-system
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,agents,ui,data,api]"
Copy-Item .env.example .env
quantlab doctor
```

不要把真实 API Key 写进 README、配置 TOML、代码或报告。它们只应出现在被 Git 忽略的本地 `.env` 中。

## 3. 启动图形前端

```powershell
cd E:\LLM-Projects\quant-lab\my-system
.\.venv\Scripts\Activate.ps1
streamlit run dashboard/app.py
```

前端标题为“QuantLab 多 Agent 量化决策系统”。

## 4. 启动 API

```powershell
uvicorn quantlab.api.app:app --reload --port 8000
```

如果设置了 `QUANTLAB_API_TOKEN`，请求需要携带 `X-QuantLab-Token`。

未配置令牌时只应绑定本机回环地址，不应直接暴露到公网。

## 5. 检查 LLM

```powershell
quantlab llm-status
quantlab llm-replay --suite smoke --runs 1
quantlab llm-replay --suite committee --runs 1
```

Smoke 用于检查基本结构化输出；Committee 还检查风险否决和 Reviewer。

真实模型检查成功，不等于该模型已被证明能提高交易收益。

## 6. 推荐的日常运行

```powershell
quantlab daily-cycle
```

它会尝试串联 ETF 模拟盘、配置中的 A 股影子盘、到期预测结算、学习与漂移检查，以及今日决策摘要。

某一步失败时，其他成功步骤仍保存，并标记 `degraded`，不会伪装成全链路成功。

## 7. 关键策略命令

### ETF

```powershell
quantlab market-radar
quantlab etf-backtest --start 2024-01-01
quantlab etf-walk-forward --start 2018-01-01
quantlab etf-core-validation --start 2015-01-01 --end 2026-06-30
quantlab adaptive-etf-lab
quantlab etf-variant-research --strategy-variant adaptive_v2 --start 2023-01-03
quantlab strategy-robustness-audit
```

### A 股

```powershell
quantlab stock-search 贵州茅台
quantlab stock-screen "600519,000858,300750" --top-n 3
quantlab stock-recommend --styles momentum_quality,value_quality --candidate-limit 20
quantlab stock-research-batch "600519,000858" --no-include-events
quantlab stock-market-replay 2024-01-01 2025-12-31 --horizon-days 5 --episodes 30 --sample-size 40 --top-k 3
quantlab stock-strategy-lab-v3-validation
```

### 组合与模拟盘

```powershell
quantlab portfolio-plan --reversal-limit 10
quantlab paper-cycle
quantlab stock-paper-cycle "600519,000858,300750,601318"
quantlab paper-scorecard
quantlab today
```

### 学习

```powershell
quantlab forecast-settle
quantlab forecast-calibration
quantlab learning-bootstrap --start 2015-01-01 --asset-scope etf
quantlab learning-train --asset-scope etf
quantlab learning-status
quantlab learning-cycle
```

### 历史盲测

```powershell
quantlab historical-replay 2025-01-01 2025-06-30 --horizon-days 20 --episodes 2
quantlab historical-replay-report 3 --output data/reports/historical-replay-3.md
```

## 8. 质量门禁

完整验收：

```powershell
.\scripts\quality-gate.ps1
```

质量门禁执行 Ruff、`compileall`、三分片 pytest 覆盖率、总覆盖率和关键模块门槛、Streamlit AppTest、硬编码 Key 扫描、报告敏感字段扫描、必需评审产物检查，并生成 `quality-gate-latest.json`。

最近一次结果：

- 238 项测试通过；
- 总覆盖率 72.49%；
- 关键模块覆盖通过；
- 前端 0 异常；
- Key 扫描和报告敏感字段扫描通过。

## 9. 最重要的证据文件

| 文件 | 用途 |
|---|---|
| `data/reports/quality-gate-latest.json` | 机器可读工程质量状态 |
| `data/reports/etf-core-protocol-validation-latest.json` | 默认 45% ETF 核心同协议历史验证 |
| `data/reports/profitability-evidence-latest.json` | 主动策略、A股、LLM、统计模型和前瞻证据总表 |
| `data/reports/decision-gate-scorecard-latest.json` | LLM 决策闸门消融 |
| `data/reports/historical-replay-9.json` | 5日11回合盲测和概率消融 |
| `data/reports/stock-market-replay-7.json` | A股30回合全市场点时分层回放 |
| `data/reports/a-share-strategy-lab-v3-validation.json` | A股V3冻结验证 |
| `data/reports/strategy-lab-latest.json` | Adaptive V1 预注册实验 |
| `data/reports/adaptive-v2-diagnostic-latest.json` | Adaptive V2 回顾性诊断 |
| `data/reports/strategy-v3-diagnostic-latest.json` | Adaptive V3 诊断 |

Markdown 适合人阅读，JSON 适合其他 AI 和程序审阅。

## 10. 根目录设计文档

| 文件 | 内容 |
|---|---|
| `ARCHITECTURE.md` | 原始统一架构和关键设计 |
| `IRONQ_PARITY.md` | 与 IronQ 功能逐项对标 |
| `REFERENCE_AUDIT.md` | 参考项目源码、许可证和吸收决策 |
| `REFERENCE_INTEGRATION_ROADMAP.md` | 参考能力的 P0/P1/P2 集成路线 |
| `LEARNING_SYSTEM.md` | 学习、训练、挑战和事件归因 |
| `LLM_ROUTING.md` | GPT/DeepSeek/本地模型路由 |
| `PROMPT_GOVERNANCE.md` | Prompt 安全、Schema 和降级 |
| `PORTFOLIO_SYSTEM.md` | 统一组合和手工清单 |
| `VALIDATION.md` | 回测、盲测和正式验证记录 |
| `AI_REVIEW_GUIDE.md` | 外部 AI 审阅入口 |

本手册负责提供统一新手视角；上述文档保留更具体的研究协议和历史过程。

## 11. 代码目录地图

| 目录 | 职责 |
|---|---|
| `dashboard/app.py` | Streamlit 15个一级页面 |
| `src/quantlab/cli.py` | 62条CLI命令 |
| `src/quantlab/api/` | FastAPI 和请求Schema |
| `src/quantlab/data/` | 数据源、缓存、fallback和质量 |
| `src/quantlab/factors/` | 因子计算 |
| `src/quantlab/fundamentals/` | 财务质量与确定性估值输入 |
| `src/quantlab/strategies/` | ETF、A股、可转债策略 |
| `src/quantlab/backtest/` | 回测、统计和Walk-forward |
| `src/quantlab/execution/` | 交易成本 |
| `src/quantlab/risk/` | 硬风险和过滤 |
| `src/quantlab/portfolio/` | 市场状态、预算、核心和组合规划 |
| `src/quantlab/agents/` | 多Agent角色、编排、决策闸门和圆桌 |
| `src/quantlab/llm/` | Provider、模型配置和固定回放 |
| `src/quantlab/learning/` | 特征、训练、模型、归因和漂移 |
| `src/quantlab/persistence/` | SQLite仓储 |
| `src/quantlab/workflows/` | 业务工作流 |
| `tests/` | 48个测试文件 |
| `config/default.toml` | 默认系统、风险、成本、模型和策略参数 |
| `data/reports/` | 可审计报告 |

## 12. 关键代码阅读顺序

如果其他 AI 想快速理解代码，建议：

1. `config/default.toml`；
2. `src/quantlab/workflows/portfolio.py`；
3. `src/quantlab/portfolio/planner.py`；
4. `src/quantlab/portfolio/etf_core.py`；
5. `src/quantlab/agents/orchestrator.py`；
6. `src/quantlab/agents/decision_gate.py`；
7. `src/quantlab/llm/providers.py`；
8. `src/quantlab/backtest/engine.py`；
9. `src/quantlab/backtest/walk_forward.py`；
10. `src/quantlab/learning/trainer.py`；
11. `src/quantlab/workflows/evidence.py`；
12. 对应测试。

## 13. 数据和数据库

- `data/quantlab.db`：本地 SQLite，已被 Git 忽略；
- `data/cache/`：行情和数据缓存，已被 Git 忽略；
- `data/reports/`：审计报告，部分大文件被忽略；
- `.env`：本地密钥，已被 Git 忽略；
- `.env.example`：无真实密钥的配置示例。

不要把 `quantlab.db` 或 `.env` 发给外部评审。

## 14. 外部 AI 审阅方式

让其他 AI 先读：

1. `PROJECT_HANDBOOK.md`；
2. 本手册；
3. `AI_REVIEW_GUIDE.md`；
4. 三份 latest JSON；
5. 关键代码和测试。

推荐审阅问题：

> 请从量化研究负责人、金融风控工程师、Python架构师和黑客松评审四个视角审阅。重点寻找时间泄漏、幸存者偏差、错误成交假设、概率评估误用、LLM越权、风险上限未执行、Key泄漏和文档夸大。请给出分项评分，并明确达到95分还缺什么。

## 15. 出现问题时先看什么

| 问题 | 先检查 |
|---|---|
| 没有订单 | 数据最新日期、`blocked`原因、策略准入和整手金额 |
| LLM 调用失败 | `quantlab llm-status`、回放、Provider熔断和Base URL |
| 股票找不到 | 证券主数据刷新、代码格式和BaoStock状态 |
| 报告缺财务 | 双源财务是否可用、截止日和质量警告 |
| 回测异常漂亮 | 是否使用未来数据、复权语义、成交时点和成本 |
| 模型不能激活 | Brier/Log Loss、时间折、冠军训练截止日和挑战样本 |
| 前端卡住 | 长任务仍是同步执行，可先使用CLI并查看终端 |

## 16. 安全提醒

- 已经在聊天中出现过的 API Key 应视为暴露并轮换；
- 不要在截图、日志或外部报告中展示 `.env`；
- 第三方兼容网关并不等于官方 OpenAI 服务；
- 不要把 API 直接暴露公网；
- 系统生成的研究报告可能包含投资偏差，不应自动执行；
- 任何真实交易必须由用户在券商端人工核对。
