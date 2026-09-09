# 🧬 Brain Memory v0.1 — 可验证自主闭环

当前产品发行号从本版本起统一采用 `v0.x` 规则。本次基线为 `v0.1`（机器可读版本
`0.1.0`）；后续功能迭代按 `v0.2`、`v0.3` 递进，稳定后再进入 `v1.0`。

## 本版本重点

- 有界、可持久化的任务计划与步骤分解
- 行动—观察—结果因果账本和确定性成果验证
- 有限重试、安全重规划、暂停/恢复与重启连续性
- 未确认行动在重启后取消，不自动重放
- 已验证结果进入情景/程序性记忆、自我模型、奖励和驱动力闭环
- 只读观测 API；输入是受控状态写入口，代码/工具写默认关闭，并采用逐项、参数绑定的
  一次性审批
- `LifeKernel` 身份与生命周期边界，以及 file-backed SQLite lease/fencing 的单 active
  协调（需受信宿主配置）
- `MotivationalPressure` 的冲动频次/烈度阈值；阈值只生成 `IterationNeed`，不直接授权
  自修改
- `HomeostasisController` / `ControlledEnvironment` 的资源预算、quarantine 和显式恢复
- `EvaluationHarness` + `PromotionController` 的候选隔离、固定评估、append-only ledger、
  外置交易 manifest、崩溃恢复、回滚与显式 discard
- `AnchorSet` / `SuccessionCoordinator` 的锚点分层继承；凭证、审批令牌、外部会话和
  未确认行动不自动继承

## 版本号边界

历史文档和代码中的 `V4`–`V13` 继续保留，用于表示能力层、API 路由或兼容性代际，
不是产品发行号。数据库 schema、快照版本、身份演化计数也保持原有语义，避免破坏
已有数据和客户端。

本版本的候选迭代和代际接替均是宿主显式接线的本地能力，不通过 HTTP 暴露代码写入，
不读取或保存仓库凭据，也不自动推送远程仓库。生产恢复必须使用同一活动根目录之外的
file-backed `PromotionLedger`；仅内存账本不能跨进程证明晋升收据。文件锁和 manifest
是合作进程间协调，不替代 OS ACL、容器网络隔离或人工发布审批。

活动树/候选树必须是专用运行时树；控制器会拒绝包含 `.git`、`.hg`、`.svn`、`.venv`、
`venv` 或 `node_modules` 的 checkout。敏感路径检查只看名称和目录元数据，评估通过不
等于晋升获准；SQLite 数据库及 `-wal`/`-shm`/`-journal` 文件也必须放在活动树之外，
并通过活动树之外的绝对 `BRAIN_MEMORY_DB_PATH` 持久化。宿主仍需在冻结快照和 OS 隔离下
部署，并把私有配置、依赖和密钥放在树外。测试工具链由 `requirements-dev.lock` 单独锁定，
不会被生产启动脚本自动安装。

接入 `PromotionController` 时应显式传入活动树之外的绝对 `persistence_path`；`BrainStem` 在
同时收到带 `db_path` 的 `StateStore` 时会自动执行这项校验，即使 SQLite 文件尚未创建也会
拒绝树内路径。没有 `db_path` 的旧式适配器仅保留兼容接口，不构成已验证的持久化连续性。

代际接替的持久化边界是：若父 lineage 已有生命周期历史，父实例必须先进入终态才能写入
接替记录；仅有旧式、无生命周期行的离线审计记录保留兼容读取路径，运行时协调器不会把它
当作正常生产晋升依据。

本版本不宣称已实现主观意识；“数字生命”是工程目标和研究语境。可复现测试记录见
[`docs/verification/p6-long-run-2026-09-09.md`](docs/verification/p6-long-run-2026-09-09.md)。

历史发行记录见 [RELEASE_v10.0.md](RELEASE_v10.0.md)。
