# 持续运行状态核验说明

本文件不再记录某次短时 Soak 的进程数、心跳百分比或累计时长。那些值只对当时数据库快照有效，
不能证明当前 Runtime 正在运行或已经连续稳定运行多日。

## 当前状态从哪里看

```powershell
quantlab runtime-status
quantlab runtime-soak-observe
quantlab runtime-soak-report
quantlab runtime-autostart-status
```

验收时应同时检查：

- API、Worker、Scheduler、Notification Worker 的唯一实例、心跳和停止时间；
- Scheduler Run 与 Job 的来源、终态、attempt、幂等键和失败原因；
- Provider 选择、点时池、字段覆盖与 readiness；
- Primary 是否唯一、样本是否自然注册、七影子账户是否隔离；
- 通知 Outbox、重试、死信、备份和数据库/WAL 增长；
- 重启电脑或进程后是否依赖数据库恢复，而不是内存状态。

`runtime-start` 是本机进程主管，不等于 Windows Service。开机自启需显式安装并通过
`runtime-autostart-status` 验证。任何短时运行只能证明该观察窗口内正常，不能外推为长期 SLA。
