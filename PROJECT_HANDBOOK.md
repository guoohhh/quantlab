# QuantLab 项目手册

这是一份面向项目所有者的中文总入口。默认读者没有量化金融背景，目标是让你能回答下面这些问题：

- 这个系统究竟想解决什么问题？
- 用户从选股票到拿到结论，中间发生了什么？
- 多 Agent、LLM、量化策略、风控和回测分别负责什么？
- 哪些能力已经实现，哪些只是研究候选？
- 目前有什么真实证据，哪些数字不能被当成赚钱保证？
- 如果继续开发，最应该先补什么？

本手册记录的是 **2026-07-15 暂停开发时的项目快照**，对应代码基线：

- 项目版本：`0.9.0`
- Git 提交：`839d0a9 Harden strategy production consistency and evidence gates`
- 最近质量门禁：238 项测试通过，总覆盖率 72.49%，Streamlit AppTest 0 异常

## 推荐阅读顺序

1. [手册导航](docs/handbook/00_INDEX.md)
2. [项目总览与当前进度](docs/handbook/01_PROJECT_OVERVIEW.md)
3. [量化金融小白入门](docs/handbook/02_BEGINNER_GUIDE.md)
4. [整体架构与数据流](docs/handbook/03_ARCHITECTURE_AND_DATA_FLOW.md)
5. [功能与用户使用流程](docs/handbook/04_FEATURES_AND_USER_WORKFLOWS.md)
6. [策略逻辑、状态与盈利证据](docs/handbook/05_STRATEGIES_AND_EVIDENCE.md)
7. [多 Agent、LLM 与学习闭环](docs/handbook/06_MULTI_AGENT_LLM_AND_LEARNING.md)
8. [现状、不足、评分与路线图](docs/handbook/07_STATUS_LIMITATIONS_AND_ROADMAP.md)
9. [运行、验证与代码地图](docs/handbook/08_RUN_VERIFY_AND_CODE_MAP.md)

## 一句话结论

QuantLab 已经是一个可运行、可演示、可审计的个人投资研究系统第一版，不是只有 Prompt 的概念项目；但它目前最强的是 **工程闭环、风险约束和研究透明度**，不是已经被证明能持续创造超额收益。

当前唯一进入默认组合的策略，是冻结六只 ETF、总暴露 45% 的半年等权核心。主动 ETF、A 股主动策略、可转债策略、LLM 交易增益和统计模型都没有获得足够证据，因此仍被限制在研究、复核或影子账户中。

> 本项目只辅助研究和手工下单，不连接券商自动交易，不构成投资建议，也不承诺未来盈利。
