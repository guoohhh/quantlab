# Windows 开机自启

QuantLab 使用当前用户的 Windows 任务计划程序，不自动创建系统级服务。

```powershell
quantlab runtime-autostart-install
quantlab runtime-autostart-status
quantlab runtime-autostart-disable
quantlab runtime-autostart-remove
```

安装必须由用户显式执行。安装器生成 `data/runtime/quantlab-autostart.ps1`，固定项目工作目录、
当前 Python/pythonw 环境和可选配置文件，然后注册 `ONLOGON`、`LIMITED` 权限任务。

安全边界：

- launcher 和任务命令不写 API Key；
- 运行时仍从 `.env` 或进程秘密存储读取；
- 使用隐藏窗口；
- `runtime-start` 的数据库租约防止重复实例；
- `runtime-stop` 继续先协作式退出，再在超时后发送终止信号；
- 日志、进程和心跳通过 `quantlab runtime-status` 查询。

