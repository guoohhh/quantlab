# QuantLab 部署与运维契约

本文描述当前代码支持的稳定部署形态和操作边界，不保存某次运行的进程、数据、测试或 readiness
结论。每次部署和验收都必须重新运行命令，并以当前配置、生产数据库和匹配当前源码指纹的机器报告
为准。

## 1. 支持的部署形态

当前生产形态是 Windows 单机、单用户部署：

- API、Worker、Scheduler 和 Notification Worker 由 `runtime-start` 启动；
- 四个进程共享本机 SQLite WAL 数据库，并通过数据库租约和心跳防止重复实例；
- Streamlit 前端是独立进程，不由 `runtime-start` 启动；
- 数据库、WAL、备份和运行时目录必须位于本机磁盘；
- 多主机 Worker、高可用数据库、多租户 SaaS 和券商自动交易不在支持范围内。

SQLite 文件不得放在网络共享、同步盘或多个主机同时写入的位置。需要多主机或高可用时，应先迁移
持久化和租约机制，而不是直接共享现有数据库文件。

## 2. 安装

项目要求 Python 3.11 或更高版本。下面使用 Python 3.12：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[agents,data,api,ui]"
Copy-Item .env.example .env
quantlab doctor
quantlab database-migrate
```

需要在部署机运行工程质量门禁时，再安装 `dev` 依赖：

```powershell
python -m pip install -e ".[dev,agents,data,api,ui]"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\quality-gate.ps1
```

`database-migrate` 会初始化新数据库，或按注册顺序升级现有数据库。不要让旧 API 与 Worker 保持运行
的同时执行升级。

## 3. 配置与秘密

`.env` 只保存本机秘密和环境差异，例如 LLM Key、可选 API Token、数据库路径和通知凭证。不要把
真实 Key 写入 TOML、数据库 payload、报告、启动参数或版本控制。

生产覆盖配置可使用单独 TOML；所有运维命令必须传入同一个 `--config`，避免 API、Worker、
Scheduler 和备份命令指向不同数据库或数据目录。例如：

```toml
[system]
database_path = "D:/quantlab/data/quantlab.db"
data_dir = "D:/quantlab/data"

[runtime]
api_host = "127.0.0.1"
api_port = 8000
backup_directory = "D:/quantlab/backups"
trusted_calendar_path = "D:/quantlab/input/calendar.csv"
trusted_security_master_path = "D:/quantlab/input/security-master.csv"
trusted_industry_path = "D:/quantlab/input/industry.csv"
trusted_pit_pool_path = "D:/quantlab/input/a-share-pool.json"
trusted_data_auto_refresh_enabled = false
trusted_data_default_level = "server_observed"
trusted_data_license_status = "unverified_no_sla"
```

示例默认保守地标记为 `server_observed/unverified_no_sla`。只有在来源、授权、字段语义和 SLA 都有
可审计依据时，才能提高可信等级；不能通过修改标签把免费或未核验数据变成 licensed 数据。

当受控文件路径为空且 `trusted_data_auto_refresh_enabled=true` 时，服务器会尝试内置免费 Provider。
这些结果仍是 `server_observed/unverified_no_sla`。Provider 超时、空结果、字段漂移或覆盖不足必须
落为 `partial`、`unavailable` 或 `failed`，并保持 readiness 关闭。

API 默认只监听 `127.0.0.1:8000`。本项目当前不是公网多用户服务；不要直接把端口暴露到公网。
`QUANTLAB_API_TOKEN` 设置后，除 `/api/health` 外的 `/api/` 请求需携带
`X-QuantLab-Token`，但 Token 本身不等于完整的公网认证、授权和传输安全方案。

## 4. 启动、检查与停止

使用生产配置启动四个后台进程：

```powershell
quantlab runtime-start --config .\config\production.toml
quantlab runtime-status --config .\config\production.toml
```

`runtime-start` 启动 API、Worker、Scheduler 和 Notification Worker。每个组件记录 PID、实例身份、
心跳和停止状态；健康实例存在时，重复启动应被拒绝。Scheduler 的重复 tick 和任务幂等键用于防止
重复提交，但业务成功仍需检查任务终态和结果 payload。

前端需另开终端启动：

```powershell
streamlit run dashboard/app.py --server.port 8510
```

本机检查入口：

```text
GET http://127.0.0.1:8000/api/health
GET http://127.0.0.1:8000/api/runtime/status
GET http://127.0.0.1:8000/api/runtime/readiness
```

停止时先请求协作式退出，超时后才由主管进程发送终止信号：

```powershell
quantlab runtime-stop --grace-seconds 15 --config .\config\production.toml
```

`status=completed` 只证明 Worker 已返回，不证明数据被正式接纳、样本已注册、账户已完成标记或通知
已送达。运维验收必须继续核对 schedule run、attempt、来源、结果 payload 和业务表。

## 5. Trusted Data 与 Readiness

正式链路只接纳服务器控制的 production 数据，不能从用户上传、Demo、research 或 test namespace
补写。至少要审计：

- 信号日与服务器市场日期一致；
- 当前源码存在新鲜且指纹匹配的质量门禁报告；
- production 交易日历覆盖信号日和所需的后续交易日；
- 信号日存在精确日期的 production 点时池；
- security master、industry、必需字段覆盖和 eligible 数量满足冻结协议；
- 每个入池字段的 `available_at` 不晚于相应 cutoff；
- 配置了真实 LLM Provider，Worker 和 Scheduler 心跳健康。

检查命令：

```powershell
quantlab runtime-status --config .\config\production.toml
```

也可读取 `GET /api/runtime/readiness`。`start_allowed=true` 只允许开始不可变的前瞻证据收集；它不
代表策略已准入、存在 Alpha、可以盈利或运行时具有 SLA。休市日即使其他条件满足，也不能注册当日
正式样本。

`forward_preflight` 是只读检查路径：它可以探测并报告 blocker，但不得创建点时池、正式实验、
样本、订单或影子账户。数据刷新恢复也不能改写早先自然窗口的失败结果。

## 6. Scheduler、手工 tick 与 Backfill

持续 Scheduler 只对服务器本地当天执行正常 tick，并按依赖图提交到期任务。一次诊断性 tick 可用：

```powershell
quantlab scheduler-run --config .\config\production.toml
```

单次 Worker 诊断可用：

```powershell
quantlab worker --worker-id diagnostic-worker --once --maximum-jobs 1 --config .\config\production.toml
```

不要在正常 Runtime Worker 仍健康时随意启动额外 Worker；先确认数据库租约、队列和并发键。

历史补跑必须显式声明日期和 `--backfill`：

```powershell
quantlab scheduler-run --run-date 2026-07-17 --backfill --config .\config\production.toml
```

Backfill 可用于允许补跑的数据、结算、报告和运维任务，但会跳过 Primary 和宽样本的正式前瞻注册。
它不能创建或修复自然前瞻证据，不能把 recovery 重新标记为首次自然窗口成功，也不能降低 readiness
门槛。正式注册只能来自当天 Scheduler 拥有、非 backfill 且满足事件时间检查的 schedule run。

## 7. 备份、校验与恢复

在线备份使用 SQLite backup API，并生成 SHA-256 manifest：

```powershell
quantlab database-backup --label before-upgrade --config .\config\production.toml
```

记录命令输出中的 backup path 和 SHA-256。恢复源必须位于配置的 `backup_directory` 内。恢复前先做
只读校验和隔离演练：

```powershell
quantlab database-backup-verify "D:\quantlab\backups\<backup>.db" --expected-sha256 "<sha256>" --config .\config\production.toml
quantlab database-restore-dry-run "D:\quantlab\backups\<backup>.db" --expected-sha256 "<sha256>" --config .\config\production.toml
```

`database-backup-verify` 只校验 checksum 和 SQLite integrity。`database-restore-dry-run` 在一次性副本
上执行恢复、迁移和完整性检查，并应报告生产数据库未修改；它不能替代正式恢复时的停机和维护模式。

正式恢复：

```powershell
quantlab runtime-stop --config .\config\production.toml
quantlab database-restore "D:\quantlab\backups\<backup>.db" "<sha256>" --confirm --config .\config\production.toml
quantlab database-migrate --config .\config\production.toml
quantlab runtime-start --config .\config\production.toml
quantlab runtime-status --config .\config\production.toml
```

恢复命令要求显式 `--confirm`，校验 checksum，并在维护模式下拒绝活动 Worker。恢复后必须重新检查
数据库完整性、迁移版本、运行时心跳、readiness 和最新任务，而不能只看进程是否启动。

## 8. 升级与回滚顺序

稳定升级顺序：

1. 记录当前配置路径、源码 revision/指纹和 `runtime-status`；
2. 执行在线备份、backup verify 和 restore dry-run；
3. 停止 Runtime 与独立 Streamlit 进程；
4. 安装新代码和依赖；
5. 执行 `database-migrate`；
6. 运行当前源码质量门禁；
7. 启动 Runtime，再启动 Streamlit；
8. 检查 health、runtime status、readiness、Scheduler 和通知；
9. 失败时停止新 Runtime，使用已验证备份恢复，不用手工 SQL 回写状态。

迁移是注册、按序且应幂等的。不要删除或改写研究、决策、Reflection、任务失败和审计记录来改善
成绩单。

## 9. 开机自启与持续运行

`runtime-start` 是本机进程主管，不是 Windows Service。开机自启必须由用户显式安装：

```powershell
quantlab runtime-autostart-install --config .\config\production.toml
quantlab runtime-autostart-status --config .\config\production.toml
quantlab runtime-autostart-disable --config .\config\production.toml
quantlab runtime-autostart-remove --config .\config\production.toml
```

安装器优先使用当前用户的 Windows Task Scheduler，并在不可用时使用用户 Startup Folder fallback。
生成的 launcher 固定工作目录、解释器和配置路径，但不包含 API Key。

持续运行证据必须来自真实存储的观察区间：

```powershell
quantlab runtime-soak-observe --config .\config\production.toml
quantlab runtime-soak-report --config .\config\production.toml
```

短时健康不能外推为长期 SLA。应监控进程心跳、Scheduler 延迟、任务重试/死信、通知 outbox、备份
年龄、磁盘空间、SQLite/WAL 增长、LLM 失败率和费用。保留期由配置项控制，调整前先确认审计和研究
保留要求。

## 10. 不可越过的运行边界

- 数据缺失、过期、来源不符、字段晚于 cutoff 或质量指纹不匹配时必须 fail closed；
- Demo、test、research、用户模拟账户和外部手工账本不能进入正式前瞻成绩；
- backfill、手工 SQL、复制历史结果或放宽阈值不能制造正式证据；
- LLM 负责解释、反证和概率，不能绕过确定性数据、成本、交易和风控规则；
- 系统只生成建议、模拟订单和可审计草稿，用户必须显式确认；
- 真实交易由用户在外部券商人工执行，并可手工登记成交；
- 不连接券商自动下单，不承诺收益，不把工程通过或 readiness 当成 Alpha。

相关文档：`WINDOWS_AUTOSTART.md`、`CONTINUOUS_RUNTIME_STATUS.md`、
`handbook/07_STATUS_LIMITATIONS_AND_ROADMAP.md` 和 `HACKATHON_DEMO.md`。
