# 开源模型训练路线

> **远期研究设想，未承诺实施**：本文不是当前模型配置或产品路线。当前系统保留 DeepSeek、OpenAI 和兼容端点选配，是否训练开放权重模型需另行立项与验收。

## 先区分三种学习

1. **金融概率模型训练**：当前 Softmax 模型使用冻结因子和真实 5/20 日收益训练，完全本地、可解释、可回滚。
2. **知识更新/RAG**：把公告、财报、研究笔记检索给 LLM，不修改模型权重，更新最快、风险最低。
3. **开源 LLM 微调**：对开放权重模型做 LoRA/QLoRA，使其学习 QuantLab 的结构化输出、证据纪律和投资角色。第一版不做从零预训练。

## 推荐演进

### 阶段 1：教师模型和评测集

- GPT 优先生成高难度 Forecast、Reviewer、风险否决和投资大师输出。
- DeepSeek 处理技术、动量、新闻等高频任务。
- 固定回放、真实结果回填和 Reviewer 共同筛掉低质量标签。
- 不直接把单次 LLM 输出当“真值”。

### 阶段 2：训练数据治理

- 输入必须冻结在 `as_of`，结果标签使用 `evaluated_at`。
- 训练、验证、测试严格按时间划分。
- 保存原始证据 ID、数据质量、降级源和硬否决。
- 去除 API Key、授权头、个人信息和不可再分发内容。
- 对教师模型输出做一致性、事实引用和未来泄漏检查。

### 阶段 3：本地开源模型

QuantLab 已支持 `local`、`ollama`、`vllm` 或普通 `openai_compatible` Provider。启动本地 OpenAI-compatible 服务后配置：

```powershell
$env:QUANTLAB_LOCAL_MODEL="your-local-model"
$env:QUANTLAB_LOCAL_BASE_URL="http://127.0.0.1:8001/v1"
$env:QUANTLAB_LOCAL_API_KEY="EMPTY"
quantlab llm-status
quantlab llm-replay --suite committee
```

本地模型只有在固定回放和历史决策回放达到门槛后，才进入正式路由。初期放在 DeepSeek 之后、GPT 之前作为备用；能力稳定后再逐步承担高频 Agent。

### 阶段 4：LoRA/QLoRA

- 优先微调结构化输出、证据引用、缺失数据披露和角色边界。
- Forecast 的方向准确率不能作为唯一目标，同时优化 Brier Score 和 Log Loss。
- Reviewer 与 Risk 数据集应提高错误样本和反例比例。
- 每个微调版本必须登记基础模型、数据版本、时间切分、超参数和评测结果。

## 不采用

- 不用未来新闻或结果解释回填预测日输入。
- 不把 GPT/DeepSeek 的自信措辞当正确标签。
- 不在没有独立时间测试集时上线微调模型。
- 不为了“有训练功能”从零训练大模型或降低现有风险门槛。
