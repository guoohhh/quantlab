# QuantLab 文档地图

本文是 `docs/` 的当前入口。静态文档负责说明稳定规则，动态状态必须从代码、数据库和机器报告读取。

## 当前使用

- `AI_HANDOVER.md`：最终验收 AI 的首要入口、阅读顺序和动态验收清单；
- `DOCUMENT_STATUS.md`：文档权威等级、维护状态和历史资料分类；
- `../PRODUCT_STRATEGY.md`：产品定位、非目标和合规边界；
- `../PROJECT_HANDBOOK.md`：面向用户的功能、流程与证据边界；
- `DEPLOYMENT.md`：本机部署、Runtime、Worker、Scheduler、备份；
- `WINDOWS_AUTOSTART.md`：Windows 自启；
- `HACKATHON_DEMO.md`：隔离黑客松演示；
- `FIVE_ENTRY_USER_FLOW.md`：当前 5 个一级决策工作区、工具入口和二级路由；
- `WIDE_FORWARD_RESEARCH.md`：宽样本研究的独立证据边界；
- `DATA_SOURCE_LICENSES.md`：数据许可和再分发边界。

## 概念与研究

`handbook/` 中的入门、架构、策略和 LLM 文档保留背景解释。除入门概念外，其中带有具体页面数、
测试数、数据库数量或日期的内容可能是历史快照，不能作为当前状态证明。

根目录的 `*_PROTOCOL.md`、`*_POSTMORTEM.md` 和证据政策属于研究审计资料。协议只有在对应实验
明确绑定该版本时才具有约束力；复盘记录不能被当成当前策略已经上线。

根目录 `ARCHITECTURE.md`、`VALIDATION.md`、`LEARNING_SYSTEM.md`、`EVIDENCE_SYSTEM.md`、
`LLM_ROUTING.md` 与 `PORTFOLIO_EVIDENCE_POLICY.md` 保留设计和研究形成过程，其中的日期、数量、
Provider、模型和实验成绩均按历史快照处理。当前入口见 `DOCUMENT_STATUS.md`。

## 历史交付

`BACKEND_ROUND1.md` 至 `BACKEND_ROUND9.md` 和 `BACKEND_ROUNDS_1_4_AUDIT.md` 是阶段交付记录。
它们保留是为了追溯 Schema、迁移和信任边界的形成过程，不是当前功能清单、路线图或质量报告。

以下结论必须动态核验，禁止从历史 Markdown 复制：

- 当前 Provider 是否可用及字段覆盖率；
- Runtime 是否在运行；
- Primary、正式样本或影子账户是否已自然创建；
- 最新测试数、覆盖率和数据库表数；
- 系统是否产生 Alpha 或 LLM 是否提高收益。
