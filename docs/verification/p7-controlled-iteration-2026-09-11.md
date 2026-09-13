# P7 受控自迭代验证记录（2026-09-12 复核）

状态：进行中。本文记录可复现的工程证据，不把可观察行为写成已证明的主观意识，也不构成生产部署或远程发布批准。

## 范围与不变量

本阶段目标是完成一次同一 instance 的受控自迭代闭环：连续性就绪度只读投影、外置宿主、真实缺口、来源证明、模型候选、固定评估、人工授权、原子晋升、重启连续性和显式回滚。

候选只能改动连续性、可靠性、观测性、性能或冗余实现。身份锚点、核心目的、人格基线、冲动阈值、评估器、晋升规则和继承规则保持保护状态。继承系统只验证接线和 `fail-closed` 行为，不创建或激活下一代；产品版本保持 `v0.1`。

## 当前基线

- 离线全量 `pytest -q`：432 passed，2 skipped，32 个子测试通过。
- 离线全量 `python -m unittest discover -q`：390 tests，OK（skipped=2）。日志中的故障栈属于显式故障注入用例，最终退出码为 0。
- `GET /api/v4/continuity` 已提供只读、递归脱敏投影；旧 HTTP 路由和 WebSocket 状态/输入响应也经过同一有界投影；默认 API 启动路径仍不获得代码写入或晋升能力。
- 生产动机来源证明要求 `MotivationSourceAttestor` 与同一持久 `StateStore`，缺失或不可重放时在构造或就绪门处关闭。
- 外置宿主将活动树、候选树、固定 fixture、状态库和晋升账本置于仓库之外的单次运行目录；公开 `run.json` 不保存来源原证据，授权数据只在私有元数据中使用。
- Docker 协议要求本地 context、digest-pinned 镜像、`--pull=never`、断网、只读根和精确挂载、非 root、capability 清空、`no-new-privileges`、进程/内存/CPU/共享内存/ulimit 限制、可写受限 `/tmp`、现场 probe 与按容器 ID 清理。wrapper 超时后宿主只按 nonce + 精确名称/镜像/标签回收，并要求连续两次有界空查询；归属、进程终止或查询状态不确定时保持 `fail-closed`。评估 fixture、评估器、完整资源预算和沙箱契约摘要都绑定到授权前复核。

## 证据矩阵

| 要求 | 状态 | 证据 |
|---|---|---|
| 只读连续性投影 | 已实现并测试 | `GET /api/v4/continuity`；投影覆盖生命周期、租约、动机、评估、晋升、继承和外置宿主，不返回绝对路径或原始账本 |
| 宿主绑定与敏感字段隔离 | 已实现并测试 | `BrainStem` 独立校验 `controlled_host`；`AgentBridge` 不公开工作区根；公开/私有运行元数据分离 |
| 真实缺口与两份独立来源证据 | 部分完成 | 静态 AST 发现进程随机 `hash()`，跨 `PYTHONHASHSEED` 重放得到 8 个样本、2 种结果；两份摘要可重放，但尚未由真实 Docker 协议生成授权收据 |
| DeepSeek 生成真实候选 | 通道契约已验证，未形成可授权运行 | 配置锁定 `deepseek-flash`、规范基础地址、JSON mode 与受控 thinking；本轮未重新调用真实 API，未形成可授权候选，固定假候选不计完成 |
| 固定评估与可重复改善 | 代码及契约测试通过，现场阻塞 | fixture judge、资源预算和沙箱摘要已封存；Docker daemon 当前不可达，未产生真实评估收据 |
| 人工授权、原子晋升与外置账本 | 未执行 | 尚无 accepted run ID，未请求或消费单次人工授权，也未签发 `SandboxAttestation` |
| 重启连续性与显式回滚 | 未执行 | 仅验证接线和故障关闭；没有晋升可供重启/回滚演练 |
| 继承就绪度与 `fail-closed` | 已实现并测试 | 就绪投影要求生产 attestor、持久化 sinks 和无待激活后代；没有创建或激活下一代 |

## 当前现场阻塞

只读检查确认 Docker CLI 与本地 context 存在，但守护进程不可达，结果为 `docker_daemon_unavailable`。因此镜像摘要、容器配置、隔离 probe、清理、真实候选评估和授权链都保持未验证。宿主不会自动启动 Docker Desktop、不会拉取镜像，也不会用测试假候选替代真实候选。

DeepSeek 配置已更新为 V4.1 Flash 的 canonical API 标识 `deepseek-flash` 和规范基础地址 `https://api.deepseek.com`；官方更新日志说明 `deepseek-v4-flash` 仅为暂时兼容路由。结构化调用显式启用 JSON Output，并关闭默认 thinking 以保持补丁协议有界；价格信息只链接官方动态页面，不在仓库固化易变价格。本轮只验证本地请求契约，未把真实模型调用结果写入验收结论。

## 本轮复核命令

以下检查在项目虚拟环境中以显式离线模式运行，模型密钥变量未传入测试进程：

```text
.venv\Scripts\python.exe -m pytest -q
结果：432 passed, 2 skipped, 32 subtests passed

.venv\Scripts\python.exe -m unittest discover -q
结果：Ran 390 tests；OK（skipped=2）

.venv\Scripts\python.exe -m compileall -q brain storage api agent services tools
结果：通过

.venv\Scripts\python.exe -m pip check
结果：No broken requirements found.

git diff --check
结果：通过（仅报告工作树 LF/CRLF 风格提示，无空白错误）
```

宿主 CLI 在未提供 manifest key 时返回 `manifest_key_unavailable` 并以退出码 2 关闭；提供本地测试配置时，Docker 守护进程不可达则返回 `docker_daemon_unavailable`，不会启动桌面端、拉取镜像或写入远程仓库。

## 未决证据

后续必须在用户明确启动 Docker Desktop 后重新执行只读 capability probe；若本地没有获准的 digest-pinned Python 镜像，应先展示精确镜像和拉取命令并另行取得授权。只有真实候选通过固定评估并生成 accepted run ID 后，才能向用户请求针对该 run ID 的一次明确晋升授权。晋升后还必须补充候选指纹、评估收据哈希、沙箱证明摘要、重启后的租约/账本恢复、显式回滚结果和秘密/路径扫描摘要。当前没有独立 supervisor 或持久 orphan lease，因此宿主被硬杀、Docker 请求延迟落地后的即时回收和下一次启动 sweep 仍未验证；这些条件缺失时保持未完成状态。
