# P6 有限真实生活环境本地验证（2026-09-10 冻结复核）

状态：本地针对性与 60 秒连续验证通过；未联网、未读取凭据、未执行外部副作用、未推送远端。

## 可复现命令

在 `D:\brain-memory` 项目根目录执行：

```text
Remove-Item Env:DEEPSEEK_API_KEY,Env:DEEPSEEK_API_KEY_BACKUP,Env:DASHSCOPE_API_KEY,Env:GLM_API_KEY,Env:ZHIPU_API_KEY -ErrorAction SilentlyContinue
$env:BRAIN_MEMORY_OFFLINE='1'
.venv\Scripts\python.exe -m pytest -q test_living_world_soak.py
结果：1 passed in 0.28s（默认快速回归）

$env:BRAIN_MEMORY_P6_SOAK_SECONDS='60'
.venv\Scripts\python.exe -m pytest -q test_living_world_soak.py
结果：1 passed in 61.08s（连续心跳 soak）
```

## 实际覆盖

- 在临时目录创建真实 SQLite 数据库（`init_db` + 两个独立 `StateStore`）及 living-world 根目录；测试结束后由临时目录清理。
- 以 `BrainStem.start()` 启动真实异步心跳，测试局部将 tick 间隔缩短至 0.01 秒；默认运行至少两个 tick，
  冻结复核用 `BRAIN_MEMORY_P6_SOAK_SECONDS=60` 连续运行，并每 5 秒确认 tick 仍在推进，随后写入快照并 orderly stop。
- 首个实例使用有界 `ResourceLedger(max_events=20_000)`；独立 homeostasis 契约另行验证容量耗尽时只记录一次
  `QUARANTINE` 并稳定拒绝后续未记录观测，不会无限增长或刷异常日志。
- 用新的 `BrainStem`/`StateStore` 模拟重启；重新绑定同一 living-world 根和 `network_enabled=False`，验证 lineage/instance 与环境审计链恢复。
- 发送 living-world 写请求和网络请求，仅经 `authorize_environment_action` 策略判断；两者均拒绝，且写目标未创建。
- 注入超过 `action_risk` 上限的有限资源观测，验证 `QUARANTINE`、生命周期隔离和后续环境请求拒绝；资源账本长度保持有界。

## 边界

该 soak 证明的是本地、受控、有界的持久化与安全策略路径，并包含一次 60 秒连续心跳观察；它不是生产负载、跨主机故障恢复、操作系统级沙箱、网络阻断或长期稳定性证明。`ControlledEnvironment` 本身不执行文件或网络操作；真实宿主隔离与部署审批仍需单独验证。

开发者：Codex（子任务 P6）
