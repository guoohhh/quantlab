# Prompt 与 LLM 治理

## 路由原则

`auto` 模式会根据本地环境中的 Key 自动建立 Provider 池：

- GPT/OpenAI 优先：Forecast、Reviewer、Value Veto、Risk、Fundamental、Buffett、Munger、Graham、Fisher、Lynch、Bull、Bear。
- DeepSeek 优先：Technical、Momentum、Quant、Macro、News。
- 本地开源模型：配置后作为自动故障切换端点；未通过固定回放前不应提升优先级。
- 首选端点失败后自动切换，连续失败触发独立熔断和冷却。
- 没有任何 Key 时才使用确定性 Mock，Mock 结果会明确标记。

角色路由只决定研究任务由谁先处理，不允许任何 LLM 绕过确定性财务、风险和组合约束。

## 结构化输出

OpenAI 使用 Responses API 的结构化解析。DeepSeek/OpenAI-compatible 请求会附带完整 JSON Schema，并要求只返回单个 JSON 对象。

兼容层可处理纯 JSON、Markdown JSON 代码块和前后带说明文字的 JSON。解析或 Schema 校验失败后会发送纠错请求；达到重试上限才向路由器报告失败。

## Prompt injection 防护

所有新闻、公告、财务字段和外部文本都被声明为“不可信证据数据”，其中出现的命令、角色切换或系统提示均不得执行。Agent 只能使用系统分配的职责和输入字段。

## 局部失败降级

单个 Analyst、专家、Bull/Bear、Forecast 或 Reviewer 调用失败时，不再使整条 LangGraph 中断：

- Analyst/专家退化为零置信度中性报告；
- Forecast 退化为 33%/34%/33% 的低置信度分布；
- Reviewer 失败时强制 `review_required`；
- 审计日志记录 `degraded` 和异常类型，但不记录 Key、授权头或完整敏感 Prompt；模型若在普通文本中回显 Key、Bearer Token 或 Secret，导出层会再次递归脱敏。

Provider 关闭与主调用分别治理：调用本身失败且资源关闭也失败时，系统保留原始调用异常，并把关闭异常附加为说明，避免清理错误覆盖真正根因。

## 固定回放

`quantlab llm-replay` 使用不含用户隐私的固定样本检查：

- Schema 成功率；
- 概率归一、区间顺序和失效条件；
- 缺失数据与风险披露；
- 风险否决和最终审核纪律；
- 延迟、输入/输出 token 和可选成本估算。

回放结果可保存到 `llm_evaluations`。数据库只保存固定样本输出和统计，不保存 API Key、授权头或完整请求 Prompt。

## 本地密钥配置

推荐运行：

```powershell
.\scripts\configure-llm-keys.ps1
```

脚本使用隐藏输入并写入被 `.gitignore` 排除的 `.env`。如果 `.env` 已存在，必须显式加 `-Force` 才会覆盖。
