# QuantLab 手册导航

最终验收从 `../AI_HANDOVER.md` 开始；日常文档入口以 `../../README.md`、
`../../PROJECT_HANDBOOK.md` 和 `../README.md` 为准。
本目录中的长篇文档形成于不同开发阶段：概念仍可参考，具体页面数、测试数、日期和运行状态必须
重新核验。

## 推荐阅读

| 文档 | 适合什么时候看 |
|---|---|
| [项目总览](01_PROJECT_OVERVIEW.md) | 理解产品目标、五入口和证据边界 |
| [量化金融入门](02_BEGINNER_GUIDE.md) | 理解 ETF、回测、回撤、IC、Brier 等概念 |
| [架构与数据流](03_ARCHITECTURE_AND_DATA_FLOW.md) | 理解当前分层、Runtime、持久化和证据隔离 |
| [功能与用户流程](04_FEATURES_AND_USER_WORKFLOWS.md) | 当前 5 个一级决策工作区、工具入口和二级路由 |
| [策略与证据](05_STRATEGIES_AND_EVIDENCE.md) | 理解证据门槛；具体成绩从最新报告核验 |
| [多 Agent 与学习](06_MULTI_AGENT_LLM_AND_LEARNING.md) | 理解 LLM 权限和学习闭环 |
| [限制与动态验收](07_STATUS_LIMITATIONS_AND_ROADMAP.md) | 稳定限制、声明边界与动态检查方法 |
| [运行与代码地图](08_RUN_VERIFY_AND_CODE_MAP.md) | 当前稳定入口；命令参数仍需对照 CLI `--help` |

## 当前操作文档

- [AI 验收接管](../AI_HANDOVER.md)
- [文档状态清单](../DOCUMENT_STATUS.md)
- [部署与运行](../DEPLOYMENT.md)
- [Windows 自启](../WINDOWS_AUTOSTART.md)
- [持续运行状态核验](../CONTINUOUS_RUNTIME_STATUS.md)
- [数据源状态核验](../DATA_SOURCE_STATUS.md)
- [数据源许可](../DATA_SOURCE_LICENSES.md)
- [黑客松隔离演示](../HACKATHON_DEMO.md)
- [宽样本研究边界](../WIDE_FORWARD_RESEARCH.md)

`../BACKEND_ROUND*.md` 是历史交付记录。它们只能回答“当时做了什么”，不能回答“现在是否可用”。
