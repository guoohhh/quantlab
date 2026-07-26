# QuantLab vNext 生产前端收口记录

> 日期：2026-07-21  
> 范围：`dashboard/`、`design/quantlab-vnext/` 与前端测试。  
> 隔离：未修改策略、Provider、Runtime 调度、readiness 门槛或 `data/quantlab.db`。

## 本轮完成

1. 组合页金融指标改为可换行指标网格；金额使用 tabular numbers，禁止省略号。1440、900、390 均完整显示大金额。
2. “专业空间”保留为产品内的专业运营层；工程审计不再是并列导航，而由专业空间显式进入，并可返回投资工作区。
3. “今日”升级为决策中心，统一汇总账户、任务、论文复评、待处理订单、通知与数据状态。
4. 研究台增加最近研究、活跃论文、待复核对象和后台任务；手动标的与日期输入保留在可展开区域。
5. 生产化保留 Evidence Trace、Decision Field 和 Quiet Signal 三个标志性图形；动效只表达状态，遵守 reduced-motion。
6. 空、加载、降级、不可用、失败和成功使用统一状态卡；所有状态提供解释，关键失败可安全重试。
7. Historical Demo 从首页移到专业空间，避免演示能力干扰日常决策中心。
8. 未增加登录、强制教学或风险画像。目标用户已有投资经验，首次使用支持继续由帮助中心承接。

## 关键设计判断

- 金融数字优先于卡片密度：900px 下宁可纵向展开，也不截断金额。
- 专业感来自真实账户、研究身份、订单事件和证据状态，不来自暴露更多工程术语。
- 工程审计是专业空间的下钻视图，不是第二套产品导航。
- 研究台默认展示“已经发生的研究工作”，手动生成是能力之一，不再是整个页面。
- UI 只提交意图；价格、费用、仓位、可卖数量、订单状态和确认权限继续由后端决定。

## 浏览器端到端证据

隔离数据库：`data/frontend-e2e-20260721/quantlab-frontend-e2e.db`。

路径：

```text
发现 → 研究 → Chat → 交易前检查 → 用户确认
→ pending / 成交 → 持仓 → 复盘
```

测试数据由真实 simulator workflow 创建，包含：

- 11 个持仓；
- 10 个 filled、1 个 partially_filled、1 个 pending、1 个 cancelled 订单；
- 123,456,789.12 元初始资金；
- 长研究证据与长复盘文本；
- StoredTestQuoteProvider 明确标识的 test-only 行情。

结果：

- 1440 / 900 / 390 横向溢出：0；
- 金融指标截断：0；
- Streamlit 页面异常：0；
- 浏览器控制台错误：0；
- 页面错误事件：0；
- 完整路径：通过。

机器报告：`data/frontend-e2e-20260721/screenshots/production-frontend-e2e.json`。

## 前端测试

```text
ruff dashboard + frontend tests: passed
compileall dashboard/e2e harness: passed
34 targeted Streamlit/product tests: passed
Node syntax check: passed
production browser e2e: passed
```

## 仍需真实环境验证

- 正式交易日、正式服务器行情下的交易前检查与成交状态；
- 正式研究 Provider 的真实延迟、超时和长报告密度；
- 正式前瞻样本自然到期后的论文变化与复盘信息量。

这些事项不能用隔离测试结果替代，也不影响本轮前端工程验收。
