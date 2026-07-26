# QuantLab 第一轮后端改造交付说明

> **历史快照**：本文只记录第一轮当时的交付，不是当前功能清单、运行状态或质量报告。当前事实请从 `../README.md`、`../PROJECT_HANDBOOK.md`、代码、数据库和最新机器报告核验。

本文记录第一轮后端改造的实际边界、数据模型、API、金融规则、安全约束和第二轮可直接复用的接口。产品定位与优先级仍以根目录的 `PRODUCT_STRATEGY.md` 为最高依据。

## 1. 本轮交付结果

第一轮已经形成一条完整的用户模拟交易主链路：

```text
创建用户模拟账户
→ 服务器 QuoteService 获取带时间戳的原始行情
→ 运行确定性交易前检查
→ 读取已有研究报告作为软建议
→ 用户确认并创建委托
→ 按交易日期和市场规则结算或部分成交
→ 更新现金、持仓、费用和盈亏
→ 每日盯市并生成净值快照
→ 触发价格、仓位和订单通知
→ 保存研究建议、用户输入和最终操作的差异
```

这里的“确定性”是指结果由代码和输入数据计算，而不是由大语言模型自由判断。例如现金是否足够、能否当日卖出、是否涨停、费用是多少，都只能由代码决定。

## 2. 三类账户为什么必须分开

| 账户类型 | 数据结构 | 用途 | 能否进入策略证据或训练 |
|---|---|---|---|
| `system_shadow` | `paper_*` | 固定策略和 Agent 的影子验证 | 可以 |
| `user_paper` | `user_paper_*` | 用户自由模拟买卖 | 不可以 |
| `manual_real_ledger` | `manual_trades` | 登记外部券商的真实手工成交 | 不可以 |

用户模拟交易可能包含追涨、临时判断或任意个人操作。如果把它混入系统策略成绩，会让策略看起来比实际更好或更差，也会污染未来模型的训练标签。因此用户模拟账户固定返回：

- `evidence_eligible=false`；
- `training_eligible=false`。

## 3. 用户模拟交易数据模型

### 3.1 核心表

- `user_paper_accounts`：账户、初始资金、现金、基准、赛季、费用、换手和版本号；
- `user_paper_orders`：委托及其完整生命周期；
- `user_paper_fills`：一笔委托可对应多笔成交；
- `user_paper_positions`：数量、冻结数量、成本、最新价和已实现盈亏；
- `user_paper_equity_snapshots`：每日现金、市值、总资产、收益、回撤和基准；
- `user_trade_decision_links`：交易前检查、研究报告、系统建议、用户请求和最终确认；
- `user_trade_reviews`：平仓或阶段性复盘；
- `user_paper_order_events`：提交、拒绝、部分成交、全部成交、撤销和过期审计轨迹。

### 3.2 委托状态

- `pending`：等待可成交日期或条件；
- `partially_filled`：只成交了部分数量；
- `filled`：全部成交；
- `cancelled`：用户撤销未完成委托；
- `rejected`：违反确定性规则，并保存具体原因；
- `expired`：超过有效期。

订单与成交不是一对一关系。这样后续可以增加成交量约束、盘口排队和分时成交，而不需要重做账本。

### 3.3 一致性和幂等

交易写入使用 SQLite `BEGIN IMMEDIATE` 事务。账户、委托和成交分别使用幂等键，重复请求不会重复扣款、重复减仓或重复下单。

“幂等”可以理解为：同一个确认请求发送两次，最终效果仍然只发生一次。

新赛季会关闭旧账户并创建继任账户；旧订单、成交、快照和复盘不会被覆盖。

## 4. 统一金融规则

`execution/rules.py` 是回测、系统影子盘和用户模拟盘共用的确定性交易规则服务。它负责：

- A 股买入整手检查；
- T+1 可卖数量；
- 停牌；
- 涨停买入和跌停卖出限制；
- 行情缺失、陈旧和数据质量；
- 现金和持仓；
- 单股、行业和总仓位上限；
- ST、上市时间和基础财务风险元数据。

费用继续复用 `execution/costs.py`：

- 佣金率和最低佣金；
- 卖出印花税；
- 过户费；
- 买卖方向滑点。

成交使用未复权原始价格。复权价格只应用于研究信号，不能直接作为真实成交价。

## 5. 交易前检查

交易前检查输出：

- 建议动作和建议数量区间；
- 行情价格、时间、来源和质量；
- 预计成交金额、交易费用和滑点；
- 操作后现金；
- 操作后单股、行业和总仓位；
- 标的下跌 10% 和 15% 时的静态账户损失；
- 支持证据、反对证据、失效条件和数据缺口；
- Reviewer 状态；
- 硬风控是否通过；
- 是否允许提交；
- 是否需要用户复核。

研究报告和 LLM 只提供软建议。即使研究报告强烈建议买入，涨停、陈旧行情、现金不足、T+1 和仓位上限仍然会拒绝订单。

研究服务或 LLM 不可用时，系统会返回“研究不可用”的降级状态，但现金、费用、持仓和交易可行性检查仍然可以完成。

## 6. 持仓、盈亏和盯市

账户概览和持仓现在提供：

- 可用现金、持仓市值和总资产；
- 数量、冻结数量和可卖数量；
- 平均成本和最新价格；
- 持仓市值、今日盈亏、未实现盈亏和持仓收益率；
- 已实现盈亏；
- 当前仓位占比；
- 累计费用、换手和成交次数；
- 每日净值、总收益、当日收益、回撤和最大回撤；
- 与沪深 300 或其他简单基准的同期比较。

盯市会同时检查：

- 单股仓位是否接近或超过上限；
- 行业仓位是否接近或超过上限；
- 总仓位是否接近或超过上限；
- 用户价格预警；
- 用户持仓比例预警。

接近上限使用 90% 阈值生成 warning，超过硬上限生成 critical。预警只产生站内通知，不会触发交易。

## 7. 受限工具型 Chat

Chat 使用显式工具注册表，不允许模型拼接任意函数名、文件路径或 URL。

只读工具包括：

- 查询模拟账户、持仓、委托、成交和绩效；
- 查询最新行情；
- 查询已有研究和 Reviewer；
- 查询组合约束；
- 查询站内通知。

受控写工具包括：

- 运行交易前检查；
- 创建需要二次确认的模拟订单草稿；
- 创建账户绑定的价格或持仓预警；
- 标记通知已读；
- 在显式允许时创建或复用研究结果。

Chat 不能自动确认订单。真正订单只能通过独立确认 API 创建，而且确认时会重新获取服务器最新行情、重新检查现金和硬风控。客户端报价不会参与正式结果；重复确认使用固定幂等键，不会产生重复订单。

会话记录保存有限摘要、工具调用、引用、数据时间、模型元数据和降级状态，不保存 API Key、认证令牌、完整系统提示词和敏感原始请求。

## 8. 站内通知与 Transactional Outbox

“Transactional Outbox”表示业务结果和待发送事件在同一个数据库事务里写入。这样可以避免订单已经成交但通知事件丢失，也能避免业务事务失败后产生虚假通知。

通知表包括：

- `notifications`；
- `notification_preferences`；
- `notification_events`；
- `notification_outbox`；
- `notification_delivery_attempts`。

支持去重、冷却合并、最低严重级别、已读、全部已读、归档和过期字段。站内投递会记录 delivery attempt。

用户不能通过普通偏好完全关闭以下关键通知：

- 订单被拒绝；
- 现金不足；
- 仓位风险；
- 行情陈旧；
- 安全事件；
- 任意 `critical` 事件。

## 9. API 清单

### 9.1 模拟账户和交易

- `POST /api/simulator/accounts`
- `GET /api/simulator/accounts`
- `GET /api/simulator/accounts/{account_id}`
- `POST /api/simulator/accounts/{account_id}/new-season`
- `POST /api/simulator/pretrade-check`
- `POST /api/simulator/orders`
- `GET /api/simulator/orders/{order_id}`
- `POST /api/simulator/orders/{order_id}/cancel`
- `DELETE /api/simulator/orders/{order_id}`
- `POST /api/simulator/orders/{order_id}/settle`
- `GET /api/simulator/accounts/{account_id}/orders`
- `GET /api/simulator/accounts/{account_id}/fills`
- `GET /api/simulator/accounts/{account_id}/positions`
- `POST /api/simulator/accounts/{account_id}/mark`
- `GET /api/simulator/accounts/{account_id}/equity-curve`
- `GET /api/simulator/accounts/{account_id}/performance`
- `POST /api/simulator/accounts/{account_id}/reviews`
- `GET /api/simulator/accounts/{account_id}/reviews`

### 9.2 Chat

- `POST /api/chat/conversations`
- `GET /api/chat/conversations`
- `GET /api/chat/conversations/{conversation_id}`
- `POST /api/chat/conversations/{conversation_id}/messages`
- `GET /api/chat/conversations/{conversation_id}/messages`
- `GET /api/chat/conversations/{conversation_id}/actions`
- `POST /api/chat/actions/{action_id}/confirm`
- `POST /api/chat/actions/{action_id}/cancel`

### 9.3 通知

- `GET /api/notifications`
- `GET /api/notifications/unread-count`
- `POST /api/notifications/{notification_id}/read`
- `POST /api/notifications/read-all`
- `POST /api/notifications/{notification_id}/archive`
- `GET /api/notifications/preferences`
- `PUT /api/notifications/preferences`

新增请求模型禁止未声明字段，避免客户端拼错字段却被静默忽略。

## 10. API 和密钥安全

- 默认 OpenAI Base URL 为空，不再隐式指向第三方代理；
- 第三方兼容端点必须由用户显式配置；
- 配置 `QUANTLAB_API_TOKEN` 后，所有 `/api/` 请求都必须提供 `X-QuantLab-Token`；
- 未配置 Token 时，写操作只允许回环地址；
- 公开错误会脱敏 API Key、Bearer Token 和本地绝对路径；
- 数据库、审计和导出使用统一敏感字段过滤；
- `.env` 不进入版本控制，质量门禁会扫描硬编码密钥。

当前服务仍是本机优先的单用户产品，不等同于具备互联网多租户认证的平台。部署到公网前必须增加真正的用户身份、会话、账户所有权和权限校验。

## 11. 数据库迁移策略

本轮使用增量、幂等初始化：

- 不删除现有 `paper_*`、`manual_trades`、研究或学习数据；
- 新表使用 `CREATE TABLE IF NOT EXISTS`；
- 旧版 `alerts` 表会通过 `ALTER TABLE ADD COLUMN` 增加账户、触发时间和触发值；
- 初始化可重复执行；
- 自动化测试会先构造旧数据库，再验证旧数据仍然存在。

生产升级前仍建议复制 SQLite 文件作为离线备份。当前还没有独立的迁移版本表和向下回滚工具，这是第二轮平台化改造项。

## 12. 测试覆盖的关键场景

- 三类账户隔离和证据污染防护；
- 创建账户、新赛季和重启一致性；
- 买入、加仓、部分成交、减仓、清仓；
- 撤单、拒绝、过期和非交易时段等待；
- 整手、T+1、停牌、涨跌停、现金和持仓；
- 费用、滑点、平均成本、已实现和未实现盈亏；
- 净值、回撤、基准、换手和费用；
- 并发确认、重复结算和 Outbox 重复消费；
- 研究失败时确定性检查继续工作；
- LLM 买入意见不能放宽硬风控；
- Chat 草稿、独立确认、跨账户拒绝和重复确认；
- 通知去重、冷却、最低严重级别和关键通知不可关闭；
- 价格预警和接近仓位上限通知；
- 旧数据库增量迁移；
- API Token、本机写限制、严格参数校验和错误脱敏。

本轮最终质量门禁结果（保留第一轮当时的历史快照；当前四轮合并门禁见
`BACKEND_ROUNDS_1_4_AUDIT.md`）：

- Ruff：通过；
- Python 编译检查：通过；
- 全量 pytest：通过；
- 总覆盖率：73.5%，高于 71% 门槛；
- 新增领域模型 `domain/trading.py`：100%；
- 统一交易规则 `execution/rules.py`：98%；
- 用户模拟账本 `persistence/simulator.py`：85%；
- Chat 持久化 `persistence/chat.py`：92%；
- 通知持久化 `persistence/notifications.py`：90%；
- API 模块：49.8%，高于 45% 关键门槛；
- Streamlit AppTest：24 个页签，0 个异常；
- 硬编码 API Key 和报告敏感字段扫描：通过。

## 13. 第二轮可直接复用的接口

前端可以直接基于以下流程开发，不需要等待账本重做：

1. 使用账户 API 构建模拟账户选择器和账户总览；
2. 使用股票搜索和研究 API 选择标的；
3. 使用 `pretrade-check` 渲染操作前检查卡；
4. 使用订单确认 API 实现明确的二次确认弹窗；
5. 使用订单、成交、持仓、净值和绩效 API 构建模拟交易页面；
6. 使用 Chat action API 实现“对话生成草稿、按钮确认”；
7. 使用通知 API 实现顶部未读数和通知抽屉；
8. 使用盯市 API 接入后台每日任务。

## 14. 仍未完成的边界

本轮没有假装完成以下平台能力：

- 真正的用户注册、登录、RBAC 和多租户账户所有权；
- 统一后台 Job、进度、取消、重试和 Worker；
- 交易所官方日历、实时盘口排队和成交量约束；
- 待成交委托的现金和持仓预冻结；
- 邮件、飞书、短信或桌面推送；
- 自动盯市调度器和真实持仓自动同步；
- 券商连接和任何真实自动下单；
- 资金流向 P1 数据产品；
- 完整前端页面改造。

这些边界不会影响本轮本机 Demo 的用户模拟交易闭环，但若要公开部署或长期实盘辅助，必须在后续迭代中补齐。
