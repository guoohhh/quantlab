# 运行、验证与代码地图

本文只保留稳定入口，不写死测试、API、页面或表的数量，也不记录 Runtime 当前状态。

## 本机启动

```powershell
cd E:\LLM-Projects\quant-lab\my-system
.\.venv\Scripts\python.exe -m quantlab.cli doctor
.\.venv\Scripts\streamlit.exe run dashboard/app.py --server.port 8510
```

后台运行：

```powershell
.\.venv\Scripts\python.exe -m quantlab.cli runtime-start
.\.venv\Scripts\python.exe -m quantlab.cli runtime-status
.\.venv\Scripts\python.exe -m quantlab.cli runtime-stop
```

实际命令与参数先运行 `quantlab --help` 或子命令 `--help` 核对。

## 验证原则

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\quality-gate.ps1
git diff --check
```

质量报告只有在源码指纹、生成时间和当前工作树匹配时才有效。测试通过证明工程行为，不证明数据
readiness、自然窗口成功或盈利。生产数据库验收还需检查 SQLite integrity/foreign keys、任务来源、
attempt、Provider 选择、manifest、点时池、字段时间、实验身份与污染边界。

## 代码地图

| 目录 | 主要职责 |
|---|---|
| `dashboard/app.py` | Streamlit 总入口与高级/审计界面 |
| `dashboard/product_ui.py` | 五个一级决策工作区、工具入口、二级路由和全局助手 |
| `dashboard/ui_foundation.py` | 导航、上下文和视觉基础 |
| `src/quantlab/api/` | FastAPI 路由与公共请求 Schema |
| `src/quantlab/market/` | 行情、执行行情与交易日历 |
| `src/quantlab/data/` | 数据 Provider、缓存、适配与 fallback |
| `src/quantlab/strategies/`、`factors/` | 策略与可复算因子 |
| `src/quantlab/risk/`、`execution/`、`portfolio/` | 硬风险、成本、执行和组合预算 |
| `src/quantlab/agents/`、`llm/` | 多 Agent、圆桌、Provider 与 LLM 治理 |
| `src/quantlab/workflows/` | 研究、Chat、模拟交易、前瞻实验和复盘编排 |
| `src/quantlab/persistence/` | SQLite 仓储、迁移和事务边界 |
| `src/quantlab/runtime/` | Job、Worker、Scheduler、通知和运行主管 |
| `tests/` | 领域、信任边界、API、Runtime 与 Streamlit 回归 |

## 动态状态入口

- 运行状态：`quantlab runtime-status`；
- 数据状态：`docs/DATA_SOURCE_STATUS.md` 中的核验方法；
- 持续运行：`quantlab runtime-soak-report`；
- 模型路由：`quantlab llm-status`；
- 工程质量：`data/reports/quality-gate-latest.json`，必须验证源码指纹；
- 盈利证据：对应协议、生产数据库和 `profitability-evidence-latest.json`，不得只读摘要文字。

## 交接给其他 AI

先读根目录 `AGENTS.md`、`PRODUCT_STRATEGY.md` 和 `docs/AI_HANDOVER.md`，再按其中的 15 分钟路径
进入 `PROJECT_HANDBOOK.md`、当前代码、Schema、测试与动态证据。
不要从 `BACKEND_ROUND*.md` 或日期验收报告推断当前状态，也不要在未查询数据库时声称实验已启动、
样本已成熟或系统已证明盈利。
