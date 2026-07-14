# 参考能力集成路线图

## 目标架构

参考项目的能力不能简单堆叠。QuantLab 统一为四个平面：

```text
数据与证据平面
  -> 确定性量化/回测/风险平面
  -> LLM 研究与探索平面
  -> 人工决策、手工下单与事后学习平面
```

- 数据与证据平面决定“系统实际知道什么”。
- 确定性平面负责指标、成本、回测、仓位和硬约束。
- LLM 平面负责解释、反证、问题发现和概率软信号。
- 最终外部副作用仍由用户确认；圆桌、Chat、通知和 MCP 不能获得放宽权限。

## 横向能力矩阵

| 能力 | 主要参考 | QuantLab 当前状态 | 优先级 | 下一验收条件 |
|---|---|---|---|---|
| 专家圆桌 | TradingAgents、AI Berkshire、daily_stock_analysis | 已完成第一版 | P0 | 真实模型固定样本回放；角色观点变化和证据引用质量评估 |
| 版本化 AnalysisContextPack | daily_stock_analysis | 证据系统已有大部分原料，缺统一块级契约 | P0 | quote/K线/技术/基本面/新闻/持仓逐块标状态、来源、时间和缺失原因 |
| 后台任务与 Run Flow | QuantDinger、daily_stock_analysis | `daily-cycle` 有步骤状态，长任务仍多为同步 | P0 | 研究/擂台/回放统一 job_id、进度、幂等、取消与拓扑快照 |
| 决策 profile 预览 | daily_stock_analysis | 风险画像影响组合；不能对同一报告快速重评 | P0 | 保守/平衡/进取只重跑确定性决策映射，不重抓数据、不改原报告 |
| 预测/推荐成绩单 | daily_stock_analysis、FinGPT、CorTex | 5/20 日预测和擂台已结算 | P0 | 增加按市场状态、数据质量、Agent、模型和候选风格分组统计 |
| 通知中心 | daily_stock_analysis、QuantDinger | 待实现 | P1 | 本地/邮件/飞书至少两种；去重、冷却、静默时段、失败审计 |
| QuantLab Skill | khquant-skill、QuantDinger MCP | 待实现 | P1 | 查询自动、修改确认、危险操作显式授权；版本预检和 `doctor` |
| MCP Agent Gateway | QuantDinger | 待实现 | P1 | 只读 R 与回测 B 先行；Token 范围、限流、幂等、审计；无自动实盘 |
| 通用策略实验室 | QuantDinger、khQuant、Qlib | 当前仅内置策略 | P1 | 策略/配置分离；信号契约；未来函数校验；next-open；walk-forward 认证 |
| 宏观/资金流/行业持续性 | IronQ、daily_stock_analysis | 部分完成 | P1 | 数据源时间戳、fallback、历史持续性和异常变化验证 |
| 开源金融模型训练 | FinGPT、Qlib | 透明 Softmax 已上线，生成式模型未训练 | P2 | 构建无泄漏指令数据；与统计/商业 LLM 基线盲测；失败可回滚 |
| 桌面打包 | OSkhQuant、daily_stock_analysis | Streamlit Web | P2 | demo 稳定后再评估 Electron/PyInstaller，避免先做壳后补能力 |

## P0：黑客松与投资辅助最有价值的闭环

### 1. 专家圆桌（已完成第一版）

- 用户从已保存研究报告中选择冻结证据。
- 可选 2–8 位大师/专家，支持 1–3 轮。
- 同一轮并行生成，避免发言顺序偏置；下一轮读取此前全部发言。
- 每个角色输出立场、置信度、赞同、质疑、证据引用、证据缺口、追问和是否改变观点。
- 主持人输出共识、未决分歧、最强多空论点、待验证问题和下一步。
- 完整持久化逐轮发言、来源报告、模型审计和降级状态。
- `formal_decision_changed=false` 是结构化硬字段，不允许圆桌偷偷改仓位。

### 2. AnalysisContextPack

要把现有证据系统升级为版本化输入包：

```text
subject + as_of + pack_version
blocks:
  quote / daily_bars / technical / fundamentals / news / portfolio / macro
每个 block:
  status / source / timestamp / available_at / fallback_from / warnings / missing_reason
data_quality:
  overall_score / block_scores / limitations
```

验收重点不是分数好看，而是任何 Agent 都能明确知道：哪些是可用事实、哪些是 fallback、哪些已陈旧。

### 3. Job 与 Run Flow

- 研究、批量会诊、候选擂台、历史盲测、训练统一任务状态机。
- 相同稳定输入支持幂等重放，避免重复花费 LLM 和数据请求。
- 任务事件覆盖数据源、上下文、Agent、Reviewer、保存和通知。
- 前端显示耗时、失败尝试、fallback 次数、模型、瓶颈节点和最终状态。
- 单进程第一版可用有界线程池；进入多人部署前必须迁移到数据库 claim 或独立 worker。

### 4. 决策 profile 预览

同一份冻结研究允许切换：

- 保守：更高证据门槛、更低仓位上限、冲突优先观望；
- 平衡：默认风险预算；
- 进取：只在硬风控和策略准入允许范围内提高软预算。

profile 只能改变确定性决策映射，不能改写原始 Agent 发言、预测概率和源报告。

## P1：平台化能力

### QuantLab Skill

借鉴 khquant-skill 的最小上下文路由：

| 等级 | QuantLab 操作 |
|---|---|
| 自动执行 | health、状态、历史报告、自选池、预测成绩单、数据诊断 |
| 确认后执行 | 抓取数据、运行研究、回测、训练、生成报告、发送通知 |
| 必须明确授权 | 删除历史、重建数据库、覆盖配置；第一版不存在自动实盘授权 |

### MCP Gateway

先开放：

- R：系统状态、市场雷达、自选池、报告、证据、成绩单；
- B：提交回测/历史盲测并查询 job；
- W：自选池和研究备注，默认需要确认。

不开放 T 实盘域。即便未来增加，也必须与手工下单产品边界重新评审。

### 通用策略实验室

策略必须声明：

- 输入数据和频率；
- 信号字段；
- 退出负责人；
- 信号确认时刻；
- 成交时刻；
- T+0/T+1；
- 成本与滑点；
- 参数搜索空间；
- walk-forward 和敏感性结果。

建议认证等级：L1 静态安全、L2 回测语义对齐、L3 样本外与模拟盘稳定。

## P2：训练与产品扩展

开源生成模型训练必须晚于数据治理。最低准入：

1. 时间切分、`available_at` 和结果揭晓时间无泄漏；
2. 与类别频率、透明统计模型和商业 LLM 做同一盲测；
3. 同时报告 Brier、Log Loss、校准、收益、回撤和交易成本；
4. 模型版本可回滚，不能自动覆盖硬风控；
5. 训练失败或线上漂移时自动降权/停用。

## 许可证与实现纪律

- OSkhQuant 仅用于设计研究，禁止复制源码。
- 无明确许可证的参考默认不复制。
- 每个吸收项必须写明“来自哪个设计思想、由 QuantLab 如何独立实现”。
- 参考项目宣称但源码未完成的能力，不计入 QuantLab 交付依据。
- 功能完成必须同时具备实现、测试、前端或 API 入口、审计和明确边界。
