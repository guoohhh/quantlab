# 数据源状态核验说明

本文件不再保存某一天的 Provider 行数、覆盖率或成功状态。免费源可达性、字段和交易日会变化，
静态数字很快过期，也不能证明当前正式实验 readiness。

## 当前状态从哪里看

1. 运行 `quantlab runtime-status` 查看最新数据、任务和 readiness 摘要；
2. 在生产数据库中核对本交易日的 refresh、`provider_refresh_selections`、manifest、点时池和成员；
3. 核对 `provider_market_date`、字段 `available_at`、来源版本和原始响应指纹；
4. 对照 `docs/DATA_SOURCE_LICENSES.md` 检查许可与再分发边界。

## 永久边界

- 免费数据源一律视为 `server_observed/unverified_no_sla`，不是交易所授权 SLA；
- 价格、成交额、换手率、市值、ST、停牌和行业等必需字段不能用猜测或旧缓存补造；
- 单个 Provider 健康不等于它被本轮实际选中；
- Job `completed` 不等于点时池完整或 `readiness.start_allowed=true`；
- 非交易日、过期行情、字段不全和时间戳越界必须 fail closed；
- 北向、ETF 份额、融资余额和资金流若无可审计来源，应显示 `unavailable` 或代理口径。

历史探针与验收报告可用于诊断，但不得改写为当前数据状态。
