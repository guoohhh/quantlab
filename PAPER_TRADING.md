# 前瞻模拟盘与每日成绩单

> **历史账户设计与实验快照**：账户名称、资金、样本和闸门状态可能已经变化。当前账户与正式
> 证据必须查询目标数据库，并区分用户模拟、Historical Demo、影子、外部账本和 Primary。

## 决策闸门 V2 尚未进入前瞻账户

`calibrated_strategy_primary_v1` 当前状态为 `insufficient_evidence_not_promoted`。20日只有4个新回合，5日只有11个，且统计模型驱动减仓0次；因此不会新增或改写正式模拟账户。现有 `adaptive_v2_shadow` 继续只验证确定性 Adaptive ETF V2.1，不能被当作决策闸门 V2 的前瞻成绩。

## 账户

默认初始资金均为 100,000 元：

| 账户 | 作用 |
|---|---|
| `benchmark_hs300` | 80% 沪深300 ETF 买入持有 |
| `benchmark_equal_weight` | 80% ETF 轮动池等权买入持有 |
| `etf_strategy` | 只跟随确定性 ETF 轮动信号 |
| `adaptive_v2_shadow` | 冻结的Adaptive ETF V2.1确定性前瞻挑战者 |
| `full_system` | ETF 目标叠加当日多 Agent 决策闸门 |
| `stock_radar_equal_weight` | 用户冻结A股研究池按风险预算等权 |
| `stock_top_rank_shadow` | A股统一排名与相关性去重后的Top-N |
| `stock_full_system_shadow` | 只有同日多Agent审核通过才允许新增A股仓位 |

完整系统缺少当日研究时不会自动退化成纯策略。新标的保持现金，已有标的仅保留原仓或按风险动作减仓。

## 时间契约

1. T 日收盘后运行 `quantlab paper-cycle`。
2. 信号、目标权重、参考价格和待成交单写入 SQLite。
3. 订单不能在 T 日收盘成交。
4. 下一次周期找到 T 日之后第一个真实交易日，使用该日原始开盘价、佣金和滑点撮合。
5. 每个交易日保存现金、持仓、净值、回撤和待成交单快照。

A股账户额外使用股票佣金、最低收费、卖出印花税、过户费和滑点，并检查100股整手、T+1可卖数量、停牌和一字涨跌停。不可成交订单保持待处理，直到出现首个可执行开盘，而不是按理论价格强行成交。

不允许用已经知道的未来价格回填模拟盘起点。

## 日常命令

```powershell
quantlab paper-cycle
quantlab paper-cycle --run-research
quantlab stock-paper-cycle "600519,000858,300750,601318"
quantlab paper-scorecard
quantlab today
quantlab daily-cycle
```

`daily-cycle` 会依次运行模拟盘、预测结算/模型漂移监控和今日决策摘要，并把各步骤状态写入调度日志。操作系统级定时任务仍需用户明确配置。

Windows 可以选择手工安装每日任务：

```powershell
.\scripts\install-daily-task.ps1 -At "18:30"
```

只有明确希望每天产生真实 LLM 调用时才使用 `-RunResearch`。安装脚本不会把 API Key 写入任务参数；Key 仍从本地 `.env` 读取。
