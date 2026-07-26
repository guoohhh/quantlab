# QuantLab vNext 前端设计交接

> 更新时间：2026-07-20  
> 状态：vNext 产品壳层已生产化接入 Streamlit；真实工作流、隔离账本与高级审计能力继续保留。  
> 范围：生产实现位于 `dashboard/`，产品使用事件兼容位于 `src/quantlab/workflows/product.py`，测试与浏览器验收脚本已同步更新。

2026-07-21 最后一轮前端收口与浏览器闭环证据见 `PRODUCTION_CLOSURE_2026-07-21.md`。

## 1. 当前结论

vNext 已采用渐进式生产化：保留 Streamlit 与既有 workflow/repository 绑定，把普通模式改造为侧栏工作区，并新增独立复盘和帮助中心。未建立第二套前端金融规则，也未引入会与后端漂移的订单计算。

当前没有已知的 P0/P1 视觉或交互问题。后续视觉调整应由以下情况触发：

- 真实数据接入后出现异常长度、密度或状态组合；
- 生产技术栈对布局、字体或动效有明确限制；
- 用户在实际任务中暴露新的理解或操作问题；
- 无障碍、性能或浏览器兼容性测试发现问题。

## 2. 主干 AI 必读顺序与权威边界

1. `PRODUCT_STRATEGY.md`：产品定位与业务边界；
2. `design/quantlab-vnext/FRONTEND_HANDOFF.md`：本文件，当前生产接手摘要；
3. `design/quantlab-vnext/PRODUCT_DESIGN_VNEXT.md`：体验审计、信息架构、页面和状态设计；
4. `dashboard/ui_foundation.py`：当前导航、产品上下文、研究缓存与视觉 token；
5. `dashboard/product_ui.py`：当前真实工作流编排；
6. 后端 workflow、repository、domain 与机器测试：价格、订单、仓位、费用、证据身份、权限与隔离的最终权威。

本交接是 `my-system/` 内自包含文档，不依赖仓库外的 `HANDOVER.md` 或 `docs/decisions.md`。历史文档只用于追溯；当前行为以本目录产品文档、代码、测试和机器报告为准。

## 3. 当前信息架构

桌面端采用稳定侧边工作区，不保留旧版五入口假设：

- 今日：现在发生了什么、是否有需要立即确认的操作；
- 市场与发现：市场环境、行业与标的线索；
- 研究台：活跃论文、证据覆盖、后台运行和研究输出；
- 组合与交易：用户模拟账户、持仓、盈亏、约束、订单与账户隔离；
- 决策复盘：原判断、用户选择、结果与到期复核；
- 专业空间：数据、Agent、实验、审计和运行状态；
- 帮助中心：快速开始、功能教程、AI 报告阅读、模拟交易、FAQ 与能力边界。

窄屏使用 Streamlit 可折叠侧栏，主内容按单列重排；同一信息架构不复制第二套移动端业务逻辑。

## 4. 核心产品链路

```text
发现机会
→ AI 研究
→ 用户理解与 Chat 追问
→ 交易前检查
→ 用户确认模拟订单
→ 持仓与盈亏
→ 决策复盘
```

AI 只能研究、解释、提出反证和生成草稿，不能替用户确认订单。模拟交易不连接券商。

## 5. 最终视觉与字体基线

- 品牌方向：Evidence Trace / Quiet Signal（证据有迹 / 安静信号）；
- 气质：温暖、克制、自然、低疲劳，但不是 Claude 复制品；
- UI 字体：Noto Sans SC / Microsoft YaHei UI 优先；
- 投资论文与判断：Noto Serif SC；
- 金额和比例：Bahnschrift 与 tabular numbers；
- 桌面正文基线：16px；
- 连续阅读内容：约 14px；
- 关键状态与操作信息：约 12–13.5px；
- 主要页面标题：500 字重；
- 小于 11.5px 的文本只应用于原型顶栏、角标、编号或图形刻度；
- 不强制 `geometricPrecision`，不在文字上叠加噪点；
- 弱文字对比度已经提高；
- 动效只表达页面进入、证据关系、运行状态和用户确认，不做庆祝或收益暗示。
- 宽桌面（`min-width: 1261px`）使用工作台尺度：侧栏 270px、顶部栏 78px、首页标题最高 46px、卡片与图表增加高度、主内容左右留白收紧；900px 和手机继续使用独立响应式密度。

## 6. 关键文件

- `prototype/index.html`：全部页面与内容结构；
- `prototype/styles.css`：视觉系统、字体、响应式、图表和动效；
- `prototype/app.js`：导航、搜索、Chat、订单、状态演示和交互；
- `audit/prototype_qa.mjs`：1440 / 900 / 390 视觉与溢出检查；
- `audit/prototype_interaction_qa.mjs`：订单、搜索、自选和 reduced-motion 检查；
- `audit/typography_qa.mjs`：DPR 2 字体与首屏验证；
- `PRODUCT_DESIGN_VNEXT.md`：完整设计决策；
- `README.md`：体验和运行说明。
- `../../dashboard/ui_foundation.py`：生产导航、上下文、研究缓存和视觉系统；
- `../../dashboard/product_ui.py`：7 个生产工作区与真实工作流；
- `audit/streamlit_walkthrough.mjs`：生产 Streamlit 1440 / 900 / 390 浏览器验收。

### 6.1 版本控制状态

截至本次交接，`my-system` 仓库中的 `design/quantlab-vnext/` 仍显示为未跟踪目录：

```text
?? design/quantlab-vnext/
```

主干 AI 在确认纳入范围后，需要显式 `git add` / 提交这些文件；不要假设高保真原型已经进入当前分支，也不要在清理未跟踪文件时误删。

## 7. 最新生产验证结果

2026-07-20 使用隔离数据库 `data/vnext-acceptance/quantlab-vnext-acceptance.db` 和备用端口 8513 验证：

- 1440 × 1000：7 个生产工作区逐页走查，均无横向溢出；
- 900 × 900：今日、组合与交易、帮助中心无横向溢出；
- 390 × 844：今日、组合与交易、帮助中心无横向溢出；
- 浏览器控制台错误：0；
- 页面异常：0；
- 页面错误事件：0；
- 1440 实测正文 17px、侧栏约 16px、页面标题约 45px；390 页面标题 32px；
- 帮助中心教程展开、页面切换和隔离模拟账户读取通过。
- 完整 `scripts/quality-gate.ps1`：通过；总覆盖率 79.5%，7 个普通工作区异常 0，19 个高级/审计标签保留，所有关键模块覆盖率阈值通过。

最新生产 QA 输出位于：

```text
data/vnext-acceptance/screenshots/browser-acceptance.json
data/vnext-acceptance/screenshots/*.png
```

本机默认 Node.js 可能仍是 16.x，而 Playwright 需要 Node.js 18+。运行 QA 时应先通过 Codex workspace dependencies 获取捆绑 Node.js，再执行脚本。

## 8. 原型边界

- 所有行情、持仓、金额和研究内容都是 `2026-07-20 08:20` 的固定演示快照；
- 原型不调用真实 API、LLM、SQLite 或券商；
- 只有贵州茅台接入完整演示报告，其他标的会 fail closed，不能复用错误对象上下文；
- 用户模拟、正式七影子、旧系统影子、外部记录和 Historical Demo 必须继续隔离；
- 数据缺失、过期、降级和 Provider 未配置必须原样呈现；
- 模型结构值、历史回测和演示结果不能包装成正式前瞻证据或收益承诺。

## 9. 当前实现与后续顺序

已完成：设计 token、应用壳层、7 工作区导航、今日、市场、研究、Chat、组合与模拟交易、复盘、专业空间、帮助中心、状态样式、三档浏览器验收和 AppTest 更新。

后续只在真实环境证据出现后继续：

1. 用当前正式数据源复核长文本、极端表格和高持仓数量；
2. 在真实可操作交易日验证交易前检查到用户确认的完整页面状态；
3. 补充自动视觉基线差异阈值，而不把截图像素相等当作金融正确性；
4. 保持完整质量门禁通过后再合并。

生产实现不能把原型中的固定数字硬编码进业务页面，也不能为了视觉一致性绕过后端权威状态和审计关系。

## 10. 查看原型

在 `design/quantlab-vnext/prototype/` 运行：

```powershell
python -m http.server 8123 --bind 127.0.0.1
```

然后访问 `http://127.0.0.1:8123`。
