# LLM 多模型路由与容错

QuantLab 支持 OpenAI、DeepSeek、本地开源模型和 OpenAI-compatible 接口。默认 `auto` 模式会自动发现本地 Key 与本地模型端点，没有可用端点时才回退到 Mock。真实 Key 只从环境变量或被忽略的本地 `.env` 读取，不写入配置示例、数据库、日志或 API 响应。

## 当前验证状态

截至 2026-07-13，当前开发环境使用 GPT + DeepSeek 混合路由：

- `openai_enabled = true`
- `deepseek_enabled = true`
- OpenAI-compatible Base URL 为 `https://code-plan.site/v1`，已验证 `gpt-5.6-luna`、`gpt-5.6-terra` 和 `gpt-5.6-sol`。
- GPT smoke 回放 2/2 成功，委员会回放 4/4 成功；风险否决和 Reviewer 拒绝用例均通过。
- DeepSeek 已完成固定委员会回放和真实 ETF 全链路验证，继续作为技术分析主力和 GPT 故障备用。
- 真实混合 ETF 链路为 11/11 成功：Quant、Technical、Momentum、Macro 共 4 次走 DeepSeek，Value Veto、Risk、Bull/Bear、两个 Forecast 和 Reviewer 共 7 次走 GPT。
- Reviewer 会收到确定性的综合分公式、分项权重、冲突度和证据覆盖率轨迹，不能把系统计算结果误判为 LLM 编造数字。
- 审计记录每次实际模型、reasoning effort、reasoning tokens、延迟和总 token。网关支持 `none/low/medium/high/xhigh`；官方 `reasoning.mode=pro` 在该网关返回不支持，因此未启用。
- 所有调用审计均未保存 Key、授权头或完整 Prompt。

该 Base URL 是用户指定的第三方兼容网关，不等同于 OpenAI 官方域名。模型名称和能力以网关实际回放结果为准，不能仅凭名称推断。

## 多 Key 配置

```powershell
$env:QUANTLAB_LLM_PROVIDER="auto"
$env:OPENAI_API_KEYS="key-1,key-2"
$env:DEEPSEEK_API_KEYS="key-3;key-4"
```

单 Key 环境变量 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY` 仍然兼容。复数环境变量存在时优先使用复数 Key 池，并自动去重。

模型和兼容端点可独立覆盖：`QUANTLAB_OPENAI_MODEL`、`QUANTLAB_DEEPSEEK_MODEL`、`QUANTLAB_OPENAI_BASE_URL`、`QUANTLAB_DEEPSEEK_BASE_URL`。不要仅凭模型名称猜测账号权限，先运行固定回放确认。

## 角色路由

默认偏好：

- Forecast、Reviewer、风险/估值否决、基本面、投资大师和 Bull/Bear：OpenAI 优先，DeepSeek 备用。
- Technical、Momentum、News：DeepSeek 优先，OpenAI 备用。
- 未配置角色：在健康端点间轮换。

角色偏好只影响调用顺序，不取消故障切换。

当前混合模式下，Forecast、Reviewer、风险/估值否决、基本面、投资大师和 Bull/Bear 默认走 GPT；Technical、Momentum、News、Quant 和 Macro 默认走 DeepSeek。首选端点熔断后会自动切到另一 Provider。

GPT 内部继续按职责分层：

- Forecast、Reviewer、Risk、Value Veto：`gpt-5.6-sol + high`
- Fundamental、Buffett、Graham、Fisher、Lynch、Bull/Bear：`gpt-5.6-terra + medium`
- 技术类角色在 DeepSeek 故障时：`gpt-5.6-luna + low`

Streamlit 侧边栏提供“速度优先 / 平衡 / 质量优先”预设，也可以展开高级配置，为每个 Agent 单独选择模型和 `none/low/medium/high/xhigh/max`。API Key 仍只从 `.env` 读取，前端不显示或保存密钥。修改后可直接在“LLM 评估”页运行 smoke 或 committee 回放，再决定是否用于正式分析。

## 隔离验证兼容网关

变更 Key、Base URL 或模型后，先隔离验证，不要直接切换生产路由：

```powershell
$env:QUANTLAB_OPENAI_ENABLED="true"
$env:QUANTLAB_DEEPSEEK_ENABLED="false"
$env:QUANTLAB_OPENAI_BASE_URL="https://code-plan.site/v1"
$env:QUANTLAB_OPENAI_MODEL="gpt-5.6-terra"
$env:QUANTLAB_OPENAI_REASONING_EFFORT="medium"

quantlab llm-replay --suite smoke --runs 1 --no-save
quantlab llm-replay --suite committee --runs 1 --no-save
```

只有 smoke 达到 2/2、committee 达到 4/4 且产生正常 token 审计后，才重新启用 DeepSeek 并进入混合路由。若返回 `model_not_found`，应先查询网关模型列表；若返回 `401 invalid_api_key`，应更换对应网关的 Key。

## 熔断

每个 Key 对应独立端点状态。连续失败达到阈值后，该端点进入冷却期；其他端点继续提供服务。冷却结束后自动允许重试。

调用审计只保存：

- 端点编号，例如 `openai-2`
- Provider、模型、结构化输出类型、角色路由键
- 成功/失败、延迟、错误类型

不保存 API Key、请求授权头或完整敏感提示内容。

使用 `quantlab llm-status` 查看配置数量和熔断状态。输出只包含 Key 数量，不包含 Key 内容。

使用 `quantlab llm-replay --suite smoke` 进行两例低成本在线回放；`--suite committee` 会增加风险否决和最终审核测试。完整治理见 [PROMPT_GOVERNANCE.md](PROMPT_GOVERNANCE.md)。
