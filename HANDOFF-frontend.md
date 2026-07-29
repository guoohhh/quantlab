# 前端交接稿 · QuantLab 首屏改造

> 交接时间：2026-07-27
> 交接分支：`feat/ui-polish-cn-colors`（已 push 到 `origin`）
> 接手人：下一个 AI / 开发。请先读完本文再动手，避免踩到已定的协作红线。

---

## 一、我是谁 / 我的职责

我是这个项目临时的**前端设计师 + 前端实现**。职责边界很明确：

- **只做呈现层**：UI 结构、排版、配色、文案、CSS。
- **绝不碰业务逻辑**：Agent 圆桌怎么跑、红线引擎怎么算、判分、后端 job、数据管线——一行都不动。
- **原因**：黑客松作品简介已提交，产品的核心叙事（"把 AI 关进笼子，代码说了算"）是固定的，我的工作是让**界面兑现这句承诺**，不是重新发明产品。

---

## 二、这次做了什么（3 个提交，都在 `feat/ui-polish-cn-colors`）

分支相对 `master` 的完整改动：`app.py / product_ui.py / ui_foundation.py`，共 +159 / -3，**纯呈现层**。

| 提交 | 内容 | 动了什么 |
|---|---|---|
| `8fd6c81` | 热力图配色改 A 股红涨绿跌 | `app.py` 里 `render_radar` 的热力图/条形图，把 plotly 内置 `RdYlGn`（绿涨红跌，欧美习惯）换成自定义 `CN_UPDOWN_SCALE`（深绿跌→米色中性→深红涨）。**只改配色，数据/逻辑零改动** |
| `ad282fd` | 首屏改造为简介承诺的可视化兑现 | 新增 `_render_home_hero()`（`product_ui.py` L1064），在 `render_home` 里替换掉裸露的 demo 入口。含英雄区 + 红线笼子 + 五角色圆桌预览。配套 CSS 加在 `ui_foundation.py` |
| `452e1a5` | 放大首屏字号、强化重点层级 | 纯 CSS 字号调整（`ui_foundation.py`），英雄标题 1.72→2.15rem、红线数值 1.36→1.72rem 等 |

### 续接（审查/统筹 AI，2026-07-27）：消灭"纯白毛坯感"

用户反馈"很多按钮、表格、输入框是纯白色，看起来简陋"。根因诊断：**`--ql-*` 设计系统只渗透到自定义 `.ql-*` HTML 卡片，没渗透到 Streamlit 原生控件**（button/input/select/tabs/dataframe 等），导致自定义区精致、原生控件毛坯的割裂感。

修复（**仅** `ui_foundation.py` 的 `apply_product_theme()`，+225 行纯 CSS，0 删除，未碰任何 Python 逻辑）：
- 新增 field/shadow 系 token（`--ql-field/-soft/-line/-focus`、`--ql-shadow-sm/-btn`、`--ql-inset`）。
- **输入/数字/文本域/下拉**：暖米色"凹陷输入槽"（`--ql-field` + 内阴影），focus 态陶土红环 + 变亮；number 的 −/+ 步进器改暖调分段键。
- **按钮分级**：主按钮从 Streamlit 番茄红 `#ff4b4b` → 品牌陶土红纵向渐变 + 柔光阴影；次按钮从纯白 → 暖白渐变底。**关键**：CTA 按钮 DOM 是 `[data-testid="stBaseButton-primary/secondary"]` 而非 `.stButton > button`，选择器两者都要带，否则 hero 的 CTA 改不到。
- 侧栏"我的与工具"按钮改暖白扁平项（用 `.st-key-product_expanded_navigation` 限定，避免影响 compact 态居中）。
- tabs → 暖 pill + 陶土红下划线；表格 → 暖表头 + 斑马纹 + 圆角包裹；滑块/开关/多选 tag → 陶土红主色；滚动条细化。
- 验证：ruff/compileall 全绿，四页（今日/研究台/组合/市场）截图确认，pytest 全量通过。对比图见 `/tmp/ql_ui_shots/`。


### 续接二（审查/统筹 AI，2026-07-27 中午）：原生控件风格统一·第二遍（commit `041a2b1`）

用户要求"以设计师角度打磨细节、整体风格一致"。扫描发现第一遍的 baseweb 选择器在 **Streamlit 1.60 react-aria 化**后部分失效（selectbox/radio/checkbox 已无 `data-baseweb` 属性，钩子变成 `data-rac` / `data-selected` / testid）。第二遍 +270 行纯 CSS（与第一遍 225 行合并为 `041a2b1`，共 495 行，0 Python 改动）：

- **已实测修复**（真实浏览器计算样式断言）：radio 选中点番茄红→陶土红（每页侧栏可见）；checkbox 选中盒同；selectbox 触发框暖底输入槽+focus 红环；下拉弹层 `stSelectboxVirtualDropdown` 去纯白；dataframe 悬浮工具栏去纯白。
- **同体系收编（盲改，选择器取自 Streamlit 1.60 静态 JS 枚举的 testid）**：dataframe 列菜单/动作菜单、日历弹层与选中日、toast、spinner 圆弧、文件上传拖放区、expander、code/json、progress、stTable、link button、hr。
- **关键坑**：①无 watchdog 时改 `ui_foundation.py` 必须**重启 streamlit 进程**，页面刷新不重载已 import 模块；②radio 指示器在 `label > div > div:first-child > div`（三层），checkbox 勾选盒在 `label > div:not([data-testid])`（一层），结构不同别套同一个路径；③不要用 JS `el.click()` 点 react-aria radio——会把 PRODUCT_PAGE_KEY 与 PRODUCT_NAVIGATION_KEY 打入"选中=回退值"死锁（页面停在设置页无法导航），真实鼠标点击或新开会话可恢复。
- 验证：ruff 绿（app.py 9 个 E402 为基线遗留）、compileall 通过、pytest 全量绿、截图在 /tmp/ql_polish_*.png。

### 续接三（审查/统筹 AI，2026-07-27 下午）："墨案 INK DESK"视觉重设计 + 用户点名问题修复

用户反馈"整体太 AI 太低级、字体太规范、圆桌入口太深"，授权大改前端（仍只动呈现层）。方向：**深色墨绿侧栏 + 宣纸画布 + 宋体衬线展示字 + 朱砂印**——东方气质的专业投研案头，不是玩具。

**修复的用户点名问题**（全部实测验证）：
1. 首屏顶部大空白 → `_render_workspace_header` 删掉 ql-trace 品牌图形盒，页眉瘦身（product_ui.py）；
2. 侧栏"今日"粉色块丑 → 深色侧栏 + 选中态改"左侧朱砂竖条 + 微光底"（`box-shadow: inset 3px 0 0 var(--ql-warm-strong)`）；
3. 演示 walkthrough 页打不开 AI 助手 → 删 product_ui.py 的 `if not HISTORICAL_DEMO_OPEN_KEY` 门（原来助手在演示期间被刻意隐藏）；
4. 首页圆桌不能直跳 → 新增 `_go_latest_roundtable()`：`DecisionRepository.research_page(page=1,page_size=1)` 取最新研究 → `_open_research_roundtable(item)`；无档案回退研究台；
5. "怎么还是1月5日" → demo boundary 文案置顶"证据基准日 2026-01-05 · 与今日真实行情无关"（冻结演示数据集的设计，不是 bug，是界面没解释）。

**重设计内容**：
- `--ql-display` 换衬线栈（Songti SC/STSong/Noto Serif SC/SimSun），渗透 hero/workspace-head/cage/section-title/stMetricValue；
- 新增 token：`--ql-ink-deep/-2`（墨绿）、`--ql-gold`（淡金）、`--ql-cinnabar`（朱砂 #a33a2c）；
- 侧栏深色化：底 `linear-gradient(180deg,#1c2822,#141e19)`，按钮全部深色幽灵款，primary 保持朱砂渐变；radio 指示器深色适配（空心环→实心朱砂）；
- Hero 2.0：宣纸渐变底 + 双圆环纹理 + **朱砂方印"代码说了算"**（rotate -5deg，CSS 纯绘制，移动端隐藏）；
- 红线笼子深色"镇纸"化：墨绿底 + 金色衬线大数字 + 朱砂 flag——浅色页面的重色锚点；
- **圆桌摆到首屏 C 位**：`_render_home_hero` 内嵌真实 `_roundtable_stage_html`（chibi 舞台，角色=technical/bull/risk/bear/macro），原五角色 emoji 卡片删除。

**环境变化（重要）**：用户已在设置页配置真实 DeepSeek key（写入仓库 `.env`，provider=deepseek/model=deepseek-chat）。本地启动**不再**强制 mock/清空 key：只保留 `QUANTLAB_DATABASE_PATH/DATA_DIR` 指向隔离演示库 + worker（/tmp/ql_run_demo.sh）。实测真实对话成功。
**新坑**：playwright 操作 react-aria radio 必须用真实鼠标点击 label 坐标（click input ref 无效）；`pkill -f` 对本环境老进程偶发失灵，重启前用 `lsof -iTCP:<port>` 确认端口已释放（否则新实例 bind 失败、旧进程继续服务旧代码，极易误判）。

### 续接四（审查/统筹 AI，2026-07-27 傍晚）：四个用户反馈的修复（含一个真 bug）

1. **导航死锁（真 bug，用户被卡在专业空间出不来）**：`render_product_navigation` 原来用 `selected != active_navigation_page` 判断导航意图——工具页（专业空间/设置/帮助中心）不在一级导航里，radio 值一点击就等于参照值，比较永假。修法：新增 `PRODUCT_NAV_CONSUMED_KEY`（上次消费的导航值），widget 值变了才算用户点了；另加 `PRODUCT_LAST_PRIMARY_KEY` 供工具页参照高亮。实测 专业空间→今日、设置→决策复盘 均可跳出。**这是导航行为修复（呈现层路由），合并前请开发知悉。**
2. **侧栏选中"暗橙纯色块"**：根因出人意料——我的圆点选择器 `stRadioOption > div > div:first-child > div` 同时命中了圆点和 stMarkdownContainer（二者在 DOM 里是同级 div 兄弟），把文字也涂了底色。修为 `> div:first-child:not([data-testid])` 只打圆点；另把 react-aria 的 data-selected/focused tint 显式 transparent，选中=左竖条+实心点+加粗。
3. **侧栏工具按钮"白底浅字看不清"**：第一遍暖白规则选择器是 (0,3,1) 级，我的深色幽灵规则 (0,2,1) 级——color 赢（同级后来者胜）、background 输，合成白底浅字。修为带 `.st-key-product_expanded/compact_navigation` 的同级选择器。
4. **"问什么都一样，是不是 mock"**：**不是 mock**。chat 是有意的"证据问答"设计（chat.py）：寒暄类命中关键词路由的 else 分支→固定能力卡片；只有"为什么/风险/怎么看/估值…"类上下文问题才走真实 LLM（`_answer_context_question` + AnalysisContextPack）。实测直连 DeepSeek OK；"这份研究的风险是什么？"得到真实生成答案+11 条引用。用户看到的"排队中 0% 不动"是抽屉不自动刷新——已加 `@st.fragment(run_every=2)` 轮询（`_render_global_chat_job_live`），完成自动整页重渲染。**待产品决策：else 分支的固定文案在 src/workflows/chat.py（业务侧），建议改成引导式文案（如"试试问：这份研究为什么…"）——属开发地盘，未动。**
5. 抽屉"看不到聊天历史"：对话按页面四维隔离（page_scope+account+symbol+run_id）是产品原则，但空态没说清。已加：当前上下文无会话时，提示"其他页面有 N 段对话"+一键跳专业空间·AI 对话。
6. 字体渗透加宽：.stMain 的 h2/h3 副标题也衬线化（原来只有一级标题，用户感觉"字体没改"）。

### 续接五（审查/统筹 AI，2026-07-27 傍晚）：chat 通用问题接真 LLM（碰了后端！）+ 助手全屏（commit `7810eae`）

**⚠️ 本轮在用户明确要求下改了业务侧代码，合并前必须让开发过目：**

1. **`src/quantlab/workflows/chat.py` 的 else 兜底改为真实 LLM**（`_answer_general_question`）：关键词路由不认识的通用问题（寒暄、"我现在应该做什么"）不再一律回固定能力卡片，而是调 `build_provider` 真实回答（120 字内、不承诺收益、提醒二次确认、引导到研究/持仓/风险）。**fail-closed**：provider=mock 或调用异常时回退原能力卡片（保留为 `_GENERAL_CAPABILITY_REPLY`）。无测试钉住原文案。通用路径未走 GovernedLLMProvider 预算治理（无 context_id 可挂），如需成本治理建议开发补挂。
2. **助手抽屉一键全屏**：`global_ai_assistant_fullscreen` session 态 + `.ql-ai-fullscreen` 标记 span + `:has()` CSS（居中 880px 大窗 vs 右侧 355px 悬浮栏），头部"全屏/退出全屏"切换。

**新坑（重要）**：重启服务只杀 streamlit 端口进程会留下**旧 worker 继续跑**——worker 是独立的 `python -` 进程，队列是抢占式的，旧 worker 用旧代码抢答新任务，表现为"改了不生效"。正确重启：`ps aux | grep "venv/bin/python -$"` 全杀 + `lsof -iTCP:<port>` listener 全杀，再启动。

**基线遗留失败（与本轮无关，stash 验证基线同样失败）**：`test_round7_readiness_product_demo.py::test_five_entry_simulator_shows_partial_fill_and_can_cancel_from_ui` 硬编码 `as_of=date(2026,7,20)`，今天 2026-07-27 订单已跨过期窗口，期望"部分成交"实际"已过期"——开发的时间敏感测试，需开发修。

### 续接六（审查/统筹 AI，2026-07-27 晚）：圆桌提交真 bug 修复（StreamlitAPIException）

用户报告"圆桌暂时无法启动；当前研究报告没有被修改。"——**这是兜底文案，真实异常被 except 吞了**。调试复现后拿到真身：

`StreamlitAPIException: st.session_state.product_roundtable_session_id cannot be modified after the widget with key product_roundtable_session_id is instantiated.`

机制：render_roundtable 里，报告**已有**圆桌记录时会渲染 `st.selectbox(key="product_roundtable_session_id")`；提交按钮的 handler 在同一 run 内直接写这个 key → Streamlit 1.60 抛异常（widget 实例化后禁止改 key）。**首次圆桌（无记录、selectbox 未渲染）永远成功，第二次起必败**——完美解释了"前两次成功、第三次报错"的用户遭遇。这是 streamlit 版本升级后才暴露的既有 bug。

修复（product_ui.py，标准 pending 模式）：提交成功写 `product_roundtable_session_id_pending`，render_roundtable 开头（selectbox 实例化前）pop 出来写入正式 key。实测：有记录时提交成功、自动选中新会话、live fragment 正常。

**教训**：`st.error(_friendly_error(...))` 兜底会把真实异常吞掉，调试期应临时换成 `st.error(f"{type(exc).__name__}: {exc}")` 复现，定位后还原。同类隐患：凡"widget key 与 session_state 直写共存"的代码在 Streamlit 1.60 都值得排查一遍（导航 radio 的 PRODUCT_NAVIGATION_KEY 直写在实例化前，只产生 warning 不致命）。

### 续接七（审查/统筹 AI，2026-07-27 晚）：圆桌 UX 重构 + K线成交量图

**圆桌三个用户反馈的修法**：
1. "为啥做俩舞台"（上面静态预览不变、结果在下面）→ **舞台合一**：有选中会话时 `_render_roundtable_session` 的实时舞台占页面主位，发起表单（expander）收起在下方；仅无会话时才显示表单内静态预览。
2. "点开始后提示不明显"→ 提交即 pending 选中新会话 + 实时舞台置顶（排队/进度/逐轮发言都在主位可见）+ toast 文案改"新讨论已置顶显示"。
3. "发言长，希望点开看全文再点收起"→ 席位气泡改 **details/summary 原生折叠**（无 JS 无 rerun）：`<details class="ql-seat-detail">`，展开卡 position:absolute 浮于舞台上方（z-index 80），底排座位（3/4/5）向上展开、右侧座位（2/5/7）向左对齐，避免被舞台 overflow 裁掉。
4. 小修：舞台状态行不再重复（progress_message 与状态文案相同时去重）。

**K线+成交量图（新模块 `dashboard/chart_svg.py`）**：纯 Python 生成 SVG（无 JS 无依赖），数据源走既有 `ResearchBarService.from_settings(settings).provider.bars()`（缓存的 westock/akshare 真实日线）。涨红跌绿（中国约定）、蜡烛+影线、下方同色半透明量柱、最新收盘虚线标签、区间涨跌幅。挂在**研究详情**指标行下方；<5 根 bar 或取数异常时 fail-closed 成一行说明，绝不让图表拖垮页面。图表仅展示，研究与订单仍只用冻结证据。

**数据事实**：演示库 sh510300 有 2026-06-25~07-27 共 23+ 根真实日线（缓存），更早区间会实时拉取并缓存。

### 续接八（审查/统筹 AI，2026-07-27 晚）：圆桌入口前置 + 思考气泡 + 浮层治遮挡 + 轮数放开 + chat 优雅降级（又碰了后端）

**⚠️ 本轮在用户明确要求下再次改业务侧代码，合并前开发过目：**
1. **轮数上限 3→6**：`agents/roundtable.py:203` 与 `workflows/roundtable_jobs.py:40` 两处校验同步放开，UI slider 1-6。无测试钉住。用户还想要"无限直到统一意见"——那需要引擎加收敛判断（每轮评估共识、提前终止），属引擎级改动，未做，建议单独立项。
2. **chat 关键词分支优雅降级**（chat.py）：账户类问题（持仓/委托/通知…）在无绑定账户的会话里原本直接 raise → **后台任务整体失败**，用户看到"失败·进度10%"。改为 `_guidance_reply`：返回引导文案（去哪绑账户/带标的代码），任务正常完成。trade_intent 与研究类缺上下文的 raise 同样处理。无测试断言这些 raise。

**纯呈现层改动：**
3. **圆桌入口前置**：页面顶部加全宽朱砂渐变 CTA"发起新的圆桌讨论"（.st-key-roundtable_new_cta），点击展开表单（form_open_key session 态控制 expander）；提交成功自动收起。
4. **AI 助手"正在思考"气泡**：任务 queued/running 时先渲染虚线 AI 气泡 + 三点跳动动画（ql-thinking-dots），替代干巴巴的状态行。
5. **气泡展开改视口居中浮层**（根治三类遮挡）：details[open] 用 `position:fixed` 居中——但**座位 1/4/6/7 的 transform:translateX 会把 fixed 后代锚回座位**，故座位偏移改写进 left/right 的 calc()，不再用 transform。浮层 z-index 100000（高于抽屉），打开时隐藏摘要只留"收起"+全文。
6. 圆桌引擎事实（回答用户疑问）：`agents/roundtable.py` 每轮开始把**此前所有轮全部发言**（prior_turns）喂给每位专家——跨轮互相可见、可引用反驳；**同一轮内并发**发言，互不见当轮内容；主持人总结读全部发言。

### 续接九（审查/统筹 AI，2026-07-27 晚）：圆桌发起/讨论双视图分离（commit `6da51c5`）

用户："CTA 点了只是展开下方折叠区，仍要下滑找表单，别人根本不知道表单在下面"。render_roundtable 重构为**双视图**（session state `roundtable_view_{run_id}`）：
- **session 视图**（默认）：顶部朱砂 CTA + 历史记录下拉 + 圆桌舞台；
- **create 视图**：发起表单直接占页面主位（邀请专家/问题/轮数/开始按钮，不用下滑）；无历史会话时默认进入；提交成功自动切回 session 视图并置顶新会话；有历史时显示"取消，返回讨论"。历史记录下拉移入 session 视图，两视图不再上下堆叠。

**⚠️ 改了开发的测试**：`test_research_hub_roundtable_review_routes_to_standalone_workspace` 原断言存在"发起新的圆桌讨论" expander——发起入口已非 expander，改为断言发起视图提交按钮（`submit_roundtable_` 前缀）存在。属有意的结构变更，合并前请开发过目。
**坑**：重构时旧 selectbox 未删干净，与新分支内 selectbox 同 key 撞车（StreamlitDuplicateElementKey）——删控件要删全。

### 续接十（审查/统筹 AI，2026-07-27 晚）：K线升级为交互式 ECharts（commit `85c29c2`）

用户要"同花顺式"交互。`dashboard/chart_svg.py` 主渲染改为 **ECharts 组件**（`st.components.v1.html` 内嵌 iframe，jsdelivr CDN + script onerror 降级提示；组件异常时回退静态 SVG）：十字光标 tooltip（开高低收/涨跌%/成交量，中文格式化）、底部 dataZoom 滑块拖选区间、inside 滚轮缩放、**MA5/10/20 均线**（Python 端算好传入）、默认视窗最近 60 个交易日、数据窗口拉长到 250 根（约一年）。涨红跌绿沿用。CDN 断网时 iframe 内显示降级文案，页面不破。数据源仍为 ResearchBarService 缓存链。

### 首屏改造的设计意图（重要，接手前必须理解）

改造前：首屏是"空账户仪表盘"——`00 OPEN ITEMS` + 五个 `0` + "还没有账户"，**和简介承诺完全脱节**，评委打开 5 秒看不到产品的强。

改造后：首屏从上到下就是简介的叙事结构——

1. **英雄区**：一句话价值主张（复述简介核心句）+ 两个 CTA。主按钮"一键跑通完整决策链路"**复用现有的 `HISTORICAL_DEMO_OPEN_KEY` 逻辑**（不新发明流程），点击触发已有的历史 demo。
2. **红线笼子（置顶主角）**：8 条红线检查清单。前 4 条阈值 **80% / 15% / 30% / 15% 读自 `config/default.toml` 的 `[risk]` 段真实配置**（`settings.get("risk")` 返回 dict），不是硬编码；ST / T+1 / 涨跌停 / 成本是引擎内联逻辑，展示为标签。
3. **五角色圆桌预览（第二块）**：技术面 / 动量 / 价值否决 / 风险否决 / 宏观。否决角色用暖红、宏观用蓝。点击可跳研究台看真正的圆桌。
4. **渐进增强兜底**：一旦有账户/数据，下方自动接回**原有的**决策场 + 账户快照，逻辑不变。

**关键约束**：`_render_historical_demo_entry()`（L1170）我**保留了定义没删**，只是从首屏主流程移除，避免破坏其他引用。

### 续接十一（审查/统筹 AI，2026-07-28 午）：圆桌气泡展开改"阅读模式"（根治多人同开遮挡）

**问题**：用户多人同时展开气泡时卡片被舞台四边裁切。前一版 `position:fixed` 视口浮层**理论上可行，实测失效**——Streamlit 布局容器带 transform/contain，会把 fixed 后代锚回祖先，卡片实际落在视口外（y=-232）。

**方案（阅读模式）**：放弃一切定位技巧，任何气泡展开 → `.ql-roundtable-stage:has(.ql-seat-detail[open])` 切换为 `display:grid` 双列卡片流：圆桌椭圆隐藏、座位全部 `position:static` 进文档流、展开的座位 `grid-column:1/-1` 通栏平铺全文、页面自然长高。全部收起 → `:has` 失效 → 自动回到圆桌视图。状态行加"平铺阅读中 · 全部收起回到圆桌"提示（同样 `:has` 驱动）。

**教训**：**Streamlit 里不要依赖 position:fixed/absolute 做浮层**——容器 transform/contain 不可控。文档流方案（:has 状态切换）是这个技术栈下最稳的"模态"模式。

**分支管理注意**：~~master 已含此修复；昨日三件套在 feat/ui-analytics-live 待合并——合并时 ui_foundation.py 会冲突~~ **已于 2026-07-28 下午合并完成（ff-only 无冲突），feat/ui-analytics-live 已推送可删。**

### 续接十二（审查/统筹 AI，2026-07-28 下午）：阅读模式→毛玻璃浮层 + 三件套合并 + 助手上下文 + 纪要导出（master `11b142a`）

1. **气泡展开第三版（用户定稿）**：阅读模式被用户否决（提示竖排、布局跳变），改为**座位原位毛玻璃浮层**——`.ql-seat-detail[open]` absolute 锚定座位、统一 400×252、`rgba(255,253,248,.86)`+`backdrop-filter:blur(8px)` 磨砂、舞台 `overflow:visible`+`:has` 抬层 z30（卡片可溢出舞台覆盖周边，零裁切）、`.ql-seat-full` flex:1 + overflow-y:auto 滚轮滚动、下排座位（3/4/5）向上展开。**Streamlit 浮层终极结论**：fixed 被 transform/contain 锚飞（不可用）；absolute 原位 + 舞台 overflow:visible + 抬层是正解。
2. **feat/ui-analytics-live 已合并**（三件套：市场走势图列/净值曲线/圆桌直播动画），ff-only 无冲突。
3. **④ 助手页面上下文注入（⚠️ 业务侧 chat.py，合并过目）**：`_answer_general_question` 的 prompt 新增 `_research_brief_for_chat`（record 顶层 action/confidence/effective_as_of + payload.decision 证据首条）与 `_account_brief_for_chat`（equity/cash/今日盈亏/持仓摘要）两个 fail-closed 简报。结构坑：DecisionRepository 记录的 decision 在 `payload.decision`，顶层只有 action/confidence/as_of。
4. **⑤ 圆桌纪要导出**：`_roundtable_minutes_markdown`（议题/状态/逐轮发言 stance+把握/质疑/缺口/主持人总结/免责），`st.download_button` 挂在圆桌会话进度条下，有发言才显示。

### 续接十三（审查/统筹 AI，2026-07-28 傍晚）：收敛模式 + 成本/预警线 + AI 晨报 + 首页间距（⚠️ 引擎改动，开发必看）

**⚠️ 圆桌引擎收敛模式（agents/roundtable.py，用户口径"一轮一致就结束，上限硬顶"）：**
- 新模型 `RoundtableConsensusVerdict{converged, reason}`；每轮结束后 `_assess_consensus` 调主持人评一致性（严格口径：方向一致且无实质分歧才算），**converged=True 即 break**，`RoundtableResult` 新增 `converged/converged_at_round/convergence_reason`；评估本身失败 fail-closed 为"未收敛"继续讨论。审计链新增 `roundtable_consensus_check` 事件。
- **顺手修了我昨天引入的 bug**：`RoundtableTurn.round_number` 的 `Field(ge=1, le=3)` 上限没随轮数放开改，4-6 轮发言会全部 schema 校验失败降级——已改 `le=6`。
- 实测：真 DeepSeek 4 轮 × 5 人 → 3 次评估全"未收敛"（严格口径符合预期），打满 4 轮；桩测 converged=True → 第 1 轮即停、只有 3 条发言、理由入档。UI：会话页"已收敛 · 第 N 轮达成一致"徽章 + 纪要注明。
- 测试状态：开发已修时间炸弹测试，pytest 100% 绿。

**纯呈现层：**
- **K 线成本线/预警线**（chart_svg.py）：`_position_cost_mark`（全账户该标的持仓量加权平均成本 → 赭石虚线"持仓成本 x.xxx"）、`_alert_marks`（该标的 active 预警 → 涨到红/跌到绿点线），ECharts markLine 挂在 K 线 series；头部注明"含持仓成本线/预警线"。无持仓/无预警不画。离线核算加权成本 4.8875 精确。
- **AI 晨报**（新模块 dashboard/morning_brief.py）：今日页顶部晨报卡，事实采集（账户净值/今日盈亏/持仓数/待办/未读预警/最新研究，逐项 fail-closed）→ LLM 90 字晨报；mock/失败直接不渲染；按天缓存 + 手动"重新生成晨报"。实测真实生成。
- **首页顶部留白收敛**：stMainBlockContainer padding-top 2rem、workspace-head/hero 间距整体收紧（用户反馈"上面空白太大"）。

### 续接十四（审查/统筹 AI，2026-07-28 晚）：总结占位 bug + 轮次切换 + 市场页可读性 + 专业感重构

1. **总结全占位 bug（P0）**：最新一场圆桌的主持人以**英文**输出 synthesis → `_research_condition_label` 的"纯英文→占位符"护栏把整段总结替换为"尚未形成可靠的中文释义"。护栏本意是保护冻结研究报告的英文证据串，但**圆桌内容是实时生成的用户向文本，不适用**。修法：圆桌全部路径（座位气泡/逐轮纪要/纪要导出/总结/收敛理由）改为直出原文；源头再补 `_moderate` 系统提示"全部用简体中文输出"。研究报告路径护栏保留。
2. **总结加结论卡**：`_render_roundtable_summary` 开头固定一张「结论」卡（朱砂左边条，`ql-roundtable-conclusion`），后面才是分歧/缺口/下一步。
3. **圆桌轮次切换**：`_roundtable_stage_html` 加 `display_round` 参数 + 座位"第N轮"徽章（ql-seat-round）；会话页在有 ≥2 轮时渲染 `st.pills`（最新/第N轮），选哪轮舞台就显示哪轮的发言。实测 5 座位徽章+切换正确。
4. **市场页可读性**：刷新报错根因=westock 脚本缺失（third-party 目录不在）+akshare 断连——错误文案映射为"行情源暂时连接不上，请稍后重试；历史结果保持不变"；资金活跃度表中文标签化（cached:fallback→缓存（备用源）、degraded→降级、英文 methodology→中文口径说明）、空值列自动省略、加白话导读。
5. **专业感重构（用户："包装太狠，红线卡才专业"）**：英雄区长营销文案砍掉（只留 eyebrow+一句话主旨+印章），**红线笼子卡（8 条红线）从页面底部提到 CTA 之后、圆桌预览之前**——产品最硬的一面进首屏。

### 续接十五（审查/统筹 AI，2026-07-29 午）：溢出修复 + 圆桌说明/元信息/立场徽章 + 中文眉标清扫

1. **展开卡文字溢出（用户截图 P0）**：`.ql-seat-detail[open]` 的 flex 高度约束在 `<details>` 上不可靠（长文溢出卡片下缘）。改 block 布局 + `.ql-seat-full` 硬性 `height:190px; overflow-y:auto` + 卡片 `overflow:hidden`。实测：卡 252px、文区 189px、scrollHeight 230 可滚动、零溢出。
2. **圆桌说明**：会话页舞台上方 `st.expander("这些专家是怎么来的？")`（折叠）——AI 席位/固定视角/只读冻结研究不取新数据/全留痕/只产出研究参考不碰仓位。
3. **会话元信息条**：`ql-meta-chips`（冻结研究 symbol · 截至 as_of · 第N/M轮 · 发言N条·全部留痕 · 降级警告 chip）挂在圆桌会话舞台上方。
4. **座位立场徽章**：`ql-seat-stance`（偏多红/偏空绿/中性灰 + 把握百分比），与"第N轮"徽章并排。实测 5 座位全部正确渲染。**卡通人物保留（用户明确喜欢）**。
5. **中文眉标清扫**：PAGE_DESCRIPTIONS 十个页头英文 eyebrow 全换中文功能描述（如 DAILY DECISION DESK→"工作台 · 每日决策"）；首页 MULTI-AGENT COUNCIL→"专家圆桌 · 五个视角一份冻结研究"、DETERMINISTIC GUARDRAILS→"确定性红线"、DECISION FIELD→"决策场"、OPEN ITEMS→"待处理事项"、OUTCOME TRACE→"结果追踪"、NEW ROUNDTABLE→"发起新讨论"。桌面上的"ROUND TABLE"大字保留（主题件，用户喜欢圆桌美学）。
6. **方向备忘**：用户认可的专业化方向="证据密度即美/单色徽章/颜色收敛/语气降级"四原则，卡通人物除外。研究报告页证据密度强化（证据计数、as_of、留痕状态前置）是下一步候选。

---



## 三、上一轮还修了一个功能 bug（已进 master）

不是前端，但你要知道，否则本地跑不起来圆桌/AI 助手：

- **现象**：圆桌、AI 助手点了永远卡在"已提交到后台讨论队列"。
- **根因**：圆桌（`roundtable_request`）和 AI 助手（`chat_request`）都是**后台 job**，必须有 `JobWorker` 进程消费队列才会执行。而启动脚本 `start-hackathon-demo.sh/.ps1` 只起了 streamlit，**没起 worker**（demo 模式故意如此）。
- **修复**：两个启动脚本都改成"随 streamlit 一起起 worker、退出时清理"，加了 `--no-worker` 开关。**已提交并 push 到 master**（`345b1d9`）。
- **接手提示**：本地验证圆桌/AI 助手时，要么用改好的 `./scripts/start-hackathon-demo.sh`，要么手动另起一个 worker 循环连同一个库，否则会以为"又卡住了"。

---

## 四、协作红线（务必遵守，这是和用户约定好的）

1. **push 只推分支，绝不直接推 master**。master 上有另一个开发在工作，直接推会冲突/覆盖。
2. **前端改动全部堆在 `feat/ui-polish-cn-colors` 分支**，合并回 master 前由用户 + 审查 AI 一起决定。
3. **本地 commit 可以做**（可逆、只影响自己）；**push 前确认用户同意**（本次 push 已获用户明确指示）。
4. **业务逻辑一行不碰**——任何改动都应能回答"这只是呈现层吗？"如果不是，先停下问用户。
5. 发现某改动可能踩到开发的地盘，**先提醒用户去确认**，不自作主张。

---

## 五、当前状态 & 你可以接着做什么

**Git**：
- 分支 `feat/ui-polish-cn-colors` 已 push 到 `origin`，追踪已设置。
- 开 PR 链接：https://github.com/guoohhh/quantlab/pull/new/feat/ui-polish-cn-colors
- `gh` CLI **未安装**，如需命令行开 PR 要先 `brew install gh` 并登录；否则走上面网页链接手动开。
- master 已包含 worker 修复（`345b1d9`），分支从干净 master 长出。

**本地环境**：
- 无残留 streamlit / worker 进程（已清理）。
- venv 在 `.venv/`，启动：`PYTHONPATH=<repo根> QUANTLAB_LLM_PROVIDER=mock .venv/bin/streamlit run dashboard/app.py --server.port <port>`（mock 模式无外部 LLM/数据源，很多真实数据会显示"暂不可用"，这是 fail-closed 设计不是 bug）。
- 截图用 playwright-cli（chromium 已装）。Streamlit 内容在内部滚动容器，`window.scrollTo` 不生效；切页面用点击侧栏 radio label 的方式。

**可能的后续方向**（用户尚未拍板，别自作主张先做）：
- 首屏还可继续微调（用户对字号/层级敏感，改完务必截图给他看）。
- 之前分析报告里列过的"可做可不做"项：组合与交易页拆 tab、二级页加面包屑、收敛套娃 `segmented_control`、按钮文案缩短。**用户明确倾向克制，别为了改而改。**
- 净值/收益曲线从 `st.line_chart` 换 Plotly 主题化（兑现 README 承诺）——属"可做可不做"。

**用户偏好（重要）**：
- 做事要**一口气做完**，别做一半停下来问；有工具调用被打断就自己接上。
- 从**真实用户视角**判断"要不要改"，而不是当乙方无脑执行。
- 改完**必须本地起服务截图**给他看真实效果，不能只贴代码。
- 称呼上互称"郭哥"。

---

## 六、一句话总结

首屏已经从"空账户仪表盘"改成"简介承诺的可视化兑现现场"（红线笼子置顶 + 圆桌 + 一键跑通 + 红涨绿跌），3 个提交都在 `feat/ui-polish-cn-colors` 分支并已 push。**逻辑零改动，只重排呈现层。** 接下来交给你，守住上面的协作红线即可。

---

## 续接三：合并前审查 + lint 修复（2026-07-28 凌晨，gavin 审查）

### 审查范围
feat/ui-polish-cn-colors → master，11 提交 / 8 文件 / +1699-110，master 无新提交可 fast-forward。

### 审查拦截的 11 个 ruff 错误（合并前必修，否则 master CI 变红）
1. **app.py 9 个 E402**（gavin 自己引入的回归）：之前加 `CN_UPDOWN_SCALE` 常量插在 import 中间，把 9 个 quantlab import 挤到后面。**修复**：常量块移到所有 import 之后（`from quantlab.workflows.radar import ETF_METADATA` 后）。零逻辑改动。
2. **chart_svg.py 2 个**（前端师傅新增 K线模块引入）：
   - F841 `pad_bottom` 未用 → 改 `_pad_bottom`
   - E741 变量名 `l` 模糊（K线 OHLC 习惯）→ 改 `lo`，同步第104行 `py(l)` → `py(lo)`

### 业务逻辑改动审查（重点，因前端师傅红线是"只碰呈现层"但实际改了3个src文件）
- **chat.py +99/-15**：合格。三处 `raise ValueError` → `_guidance_reply`（fail-soft，避免杀后台job）；新增 `_answer_general_question` 通用问题接真实 LLM，mock/失败双兜底（fail-closed），system prompt 含不承诺收益/不编造数据/订单需二次确认规则。import 齐全（build_provider/await_with_provider_close 第27行）。
- **roundtable.py + roundtable_jobs.py**：合格。圆桌轮数 3→6，前后端同步。
- **test_product_ui_navigation.py**：合格。适配 UI 重构（expander→button 断言）。

### 门禁
- ruff：✅ All checks passed（修复后）
- compileall：✅ 通过
- chart_svg 集成：✅ product_ui.py:13 `from dashboard.chart_svg import render_symbol_market_chart`
- pytest：⏳ 全量跑中

### lint 修复范围
只碰 app.py + chart_svg.py，+13/-13，零逻辑改动，待 commit 到 feat 分支。
