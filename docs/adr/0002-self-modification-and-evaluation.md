# 自修改必须经过不可变生命核和独立评估

Status: accepted

为了让“数字生命”原型可以改进实现，同时防止它通过改写裁判或权限制造“进步”，系统采用 `LifeKernel / 受控可变工作区 / EvaluationHarness` 三分法。仓库目前没有一个名为 `MutableOrganism` 的自动代码生成类；宿主或受控组件可以在自有工作区读写源码、策略、参数和受控数据，并显式提供候选目录，系统不会从心跳自动生成或执行候选。`LifeKernel`、`EvaluationHarness`、测试夹具、身份根、权限规则、审计历史、凭证和远程仓库不属于候选的自动自改范围。

每次变更都必须先形成带证据的 ChangeProposal，在隔离候选环境中运行固定、版本封存且可重复的 EvaluationHarness，通过可启动性、完整性、资源、权限、回滚和无未验证成功等硬门，再证明相对于 BaselineRevision 的主维度改善且无关键回归，最后才能原子晋升。候选不得修改评估器、样本、依赖解析或自身的晋升规则；评估拒绝时活动树从未写入，目录交换在中途失败时才使用备份恢复，运行中的实例不原地热写自身。

动机和情感可以产生 IterationNeed，但单次冲动不能直接授权代码变更；代码级迭代还需要独立客观证据。稳定时不迭代，主动 `EVOLUTION` 必须改善，紧急 `RECOVERY/SUCCESSION` 只需恢复最后可信基线。低层 `PromotionController` 是宿主治理 API，不自动绑定 `LifeKernel` 或 lineage；完整生命管道应由 `BrainStem` 先通过生命周期、稳态和权限门。修改宪法/生命周期路径或产生外部副作用仍需外部批准，系统不自动推送远程仓库。

### BrainStem 接线约束（P3）

`BrainStem` 只提供显式的三步接线：`register_iteration_proposal` →
`evaluate_iteration_proposal` → `promote_iteration_proposal`。每一步都要求调用方
传入 `host`；脑干不会从当前进程、状态存储或受控环境推断并保留主机能力。第一步
只登记经过边界校验的 `ChangeProposal`，第二步只调用宿主注入的固定
`EvaluationHarness` 并返回不可变 `EvaluationReceipt`，第三步才把明确的授权和收据
交给宿主注入的 `PromotionController`。心跳 tick、动机轮询和快照恢复都不会隐式
触发评估或写入活动树。

生产 `PromotionController` 的 sandbox attestation 门槛不能由脑干降级或绕过；没有
宿主签发的一次性 `SandboxAttestation`，晋升必须失败且活动树保持不变。快照最多保存
有界的提案、收据哈希和结果摘要，不保存 `host`、能力对象、候选路径或密钥；重启后
宿主必须重新显式绑定并重新确认授权。

### 持久交换与恢复约束

`PromotionController` 使用活动树之外的外置 JSON manifest 和 append-only
`PromotionLedger`。清单阶段为 `PREPARED → ACTIVE_MOVED → SWAPPED → COMMITTED`；
回滚为 `ROLLBACK_STARTED → ROLLED_BACK`，每一步都在重命名附近写入并刷新记录。新的
控制器会先依据清单、目录指纹和匹配的晋升/回滚收据恢复；证据不足时保持 fail-closed。
`close()` 不是销毁授权，只有显式授权的 `rollback()` 或 `discard_rollback()` 可以
消费回滚点。跨进程重启恢复必须使用同一外置 file-backed ledger；仅内存 ledger 无法
为另一进程提供收据证明，不同 ledger 绑定同一 active root 也会在证据不足时拒绝。

manifest、收据和 attestation 的哈希提供内容完整性，不等于不可伪造的签发者身份；
文件锁/清单是合作进程间协调，不是 OS ACL 或网络隔离。生产宿主必须把 evaluator、
attestor 秘密、容器/作业权限和远程仓库凭据放在候选不可访问的边界外。

活动树与候选树是“可交换运行时树”，不是源码 checkout 或依赖缓存。控制器会拒绝含有
`.git`、`.hg`、`.svn`、`.venv`、`venv`、`node_modules` 的树，并要求 file-backed ledger
位于活动树之外；这样恢复流程不必打开版本库配置或复制宿主依赖元数据。敏感路径门只
做名称/目录元数据检查，公共环境变量模板是明确例外，也不等价于内容级秘密扫描。评估
阶段为方便审计可以跳过敏感名称，但晋升控制器会再次扫描并拒绝，因此评估收据本身不
构成写入授权。重解析点、硬链接、原子交换和 sidecar 锁只能提供静态/合作进程保护；
恶意并发替换、Windows ACL/ADS、网络文件系统和外部进程权限必须由宿主冻结快照、ACL
或容器/supervisor 另行覆盖。

SQLite 数据库及其 `-wal`、`-shm`、`-journal` 状态属于持久生命状态，不得放进可整树交换
的 active/candidate 目录；生产宿主应通过活动树之外的绝对 `BRAIN_MEMORY_DB_PATH` 绑定
它们，并在构造 `PromotionController` 时传入 `persistence_path`。控制器在初始化、暂存和
恢复阶段都会拒绝发现这类文件；显式绑定还能在文件尚未创建时提前拒绝树内路径。代际接替记录同样遵循父级
生命周期边界：已有 `life_ledger` 历史时，父实例必须是终态；只有没有父生命周期行的旧式
离线审计记录保留兼容追加语义，不能绕过运行时 coordinator 的正常接替门。

## Consequences

- 评估器与被测对象保持独立，避免自我修改形成闭环作弊。
- 自有工作区的读写能力保留，代码演化仍可自治进行。
- 每个候选都有可追溯版本、评估收据、持久交换清单和回滚路径，长期迭代可以从最近可信版本继续。
- 默认保护底线不可由 `protected_files`/`protected_paths` 删除；宿主仍应把具体项目路径、
  file-backed ledger 和 OS 隔离策略作为部署配置的一部分核验。
