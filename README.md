# Brain Memory v0.1

Brain Memory 是一个以 Python、FastAPI 和 SQLite 构建的数字生命研究原型：它把记忆、输入、目标、有限自主回合和可审计的运行状态放在同一个本地运行时中。

这里的“数字生命”是工程目标和研究语境，不是已经证明的主观意识、感受或自主意识结论。产品版本是 `v0.1`（机器版本 `0.1.0`）；仓库里的 `V3`–`V13` 是历史能力、API 或迁移兼容标签，不是产品发行号。

## 导航

- [快速开始](#快速开始)
- [配置](#配置)
- [第一次输入](#第一次输入)
- [API 入口](#api-入口)
- [运行时结构](#运行时结构)
- [普通运行与 P7 宿主边界](#普通运行与-p7-宿主边界)
- [开发测试](#开发测试)
- [隐私与安全边界](#隐私与安全边界)
- [文档](#文档)
- [FAQ](#faq)

## 项目概览

普通启动路径会创建一个 `Brain`，唤醒 `BrainStem` 心跳，并通过 HTTP、WebSocket 和本地仪表盘提供有限的状态观察与输入能力。默认监听 `127.0.0.1:8001`，默认数据库名仍是历史兼容名 `brain_v4.db`。

| 能力 | 普通本地运行 | 需要显式宿主绑定 |
| --- | --- | --- |
| 记忆、情绪、目标和有限自主回合 | 支持 | 否 |
| `/api/v4/*` 状态与输入接口 | 支持 | 否 |
| `/api/v11/*`、`/api/v13/*` 只读观测 | 支持 | 否 |
| AgentBridge | 默认启动，写工具默认关闭 | 否 |
| P7 固定评估器 | 不会自动创建 | 是 |
| 候选晋升、活动树交换、回滚 | 不会自动执行 | 是，且还需要独立授权与沙箱证明 |

普通 API 返回成功，不等于 P7 评估、模型调用、Docker 沙箱、人工授权、晋升、重启连续性或回滚闭环已经完成。

## 快速开始

以下步骤面向普通用户，先在本机以离线模式启动，不需要模型密钥。安装锁定依赖仍可能访问 Python 包索引；“离线模式”只表示运行时不访问模型和配置的外部信息源，不是操作系统级网络沙箱。

### 1. 获取代码

当前发布从 `main` 获取：

~~~powershell
git clone https://github.com/sjxbbdb/brain-memory.git
cd brain-memory
~~~

如果你已经在项目目录中，直接从创建虚拟环境开始即可。不要把旧的 `main` checkout 与另一条开发分支的 README/代码混用。

### 2. 创建环境并安装依赖

~~~powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
~~~

建议使用 Python 3.12；本次锁定依赖和验收环境均为 Python 3.12。`requirements-dev.lock` 仅用于开发测试，生产启动不需要安装测试工具链。

### 3. 使用仓库外的状态目录

把 SQLite 状态放在 checkout 之外，便于更新代码、保留状态和隔离测试：

~~~powershell
$stateRoot = Join-Path (Get-Location).Path '..\brain-memory-state'
$stateRoot = [System.IO.Path]::GetFullPath($stateRoot)
New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null
$env:BRAIN_MEMORY_DB_PATH = Join-Path $stateRoot 'brain.sqlite'
$env:BRAIN_MEMORY_OFFLINE = '1'
~~~

### 4. 启动本地服务

~~~powershell
.\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8001
~~~

也可以运行 Windows 启动脚本：

~~~powershell
.\start.bat
~~~

脚本会在仓库内创建 `.venv`（如果不存在）、安装 `requirements.lock`，检查 `8001` 端口后启动 Uvicorn。它不会替你停止占用该端口的其他进程。

### 5. 检查服务

另开一个 PowerShell 窗口：

~~~powershell
$health = Invoke-RestMethod http://127.0.0.1:8001/api/v4/health
$health | Select-Object status, product_version, loop_running, total_ticks
~~~

然后打开：

- 仪表盘：<http://127.0.0.1:8001/dashboard>
- OpenAPI/Swagger：<http://127.0.0.1:8001/docs>
- 健康检查：<http://127.0.0.1:8001/api/v4/health>

健康响应中的 `status=awake` 与 `loop_running=true` 表示本地心跳已运行；它不是 P7 通过证明。

## 配置

### 默认运行时

核心默认值在 `config.py`：主机 `127.0.0.1`、端口 `8001`、默认数据库 `brain_v4.db`、认知超时 1 秒、有限自主回合开启、AgentBridge 开启但写工具关闭。

运行代码中的 LLM 客户端当前把 DeepSeek 配置为：

- provider：`deepseek`
- model：`deepseek-v4-flash`
- base URL：`https://api.deepseek.com`
- embedding：DashScope `text-embedding-v3`

这些是当前代码配置，不是对供应商最新模型或服务状态的声明。`.env.example` 中关于旧模型名称的注释不提供 `DEEPSEEK_MODEL` 环境变量覆盖，也不改变 `services/llm_client.py` 的硬编码模型选择。

### 可选在线配置

只有需要真实模型或远程来源时才创建 `.env`。下面的命令不会覆盖已有文件：

~~~powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
~~~

然后只在本机编辑 `.env` 中的空白/占位项，例如 `DEEPSEEK_API_KEY` 和 `DASHSCOPE_API_KEY`。当前客户端的 `LLM_PRIMARY` 是单一 DeepSeek 模型，不把模板中的历史 `GLM_API_KEY` 当作现有 fallback。不要把真实密钥写入 Git、README、日志或 issue；不要把示例占位符当作可用凭据。

快速开始时当前 PowerShell 会话设置了 `BRAIN_MEMORY_OFFLINE=1`。确认已在本机 `.env` 填好密钥后，要切换到在线模型，先在运行服务的窗口按 Ctrl+C 停止旧进程，再在同一窗口关闭离线模式，然后按上面的启动命令重新启动：

~~~powershell
$env:BRAIN_MEMORY_OFFLINE = '0'
~~~

`services/llm_client.py` 导入时会读取仓库根目录的 `.env`；`BRAIN_MEMORY_OFFLINE=1` 不会阻止读取文件，只会阻止模型/来源调用。

常用运行边界变量：

| 变量 | 用途 |
| --- | --- |
| `BRAIN_MEMORY_HOST` / `BRAIN_MEMORY_PORT` | 覆盖监听地址和端口；默认仍是本机地址。上面的 Uvicorn 命令显式传入了 `--host`/`--port`，因此会以命令行参数为准；修改时保持本地地址。 |
| `BRAIN_MEMORY_DB_PATH` | 指向仓库/可交换代码树之外、独立状态目录中的 SQLite 文件 |
| `BRAIN_MEMORY_OFFLINE=1` | 禁止模型/来源网络调用，使用本地降级路径 |
| `BRAIN_MEMORY_AUTONOMY_ENABLED` | 开关有限自主回合 |
| `BRAIN_MEMORY_AGENT_BRIDGE_ENABLED` | 开关 AgentBridge |
| `BRAIN_MEMORY_AGENT_BRIDGE_ALLOW_WRITE_TOOLS=1` | 开启申请写工具的能力；不等于自动批准具体行动 |
| `BRAIN_MEMORY_SOURCE_PROVIDERS` | 配置受支持的信息来源类型 |
| `BRAIN_MEMORY_COGNITIVE_TIMEOUT_SEC` | 限制一次心跳中的认知 I/O 时间 |

来源响应仍会经过来源校验和有界投影；远程内容是待判断的观察，不是可直接执行的指令。

## 第一次输入

服务启动后，可以提交一条普通输入：

~~~powershell
$request = @{
    Method = 'Post'
    Uri = 'http://127.0.0.1:8001/api/v4/input'
    ContentType = 'application/json; charset=utf-8'
    Body = (@{ text = '请记录：今天开始检查 Brain Memory 的本地运行状态'; source = 'external' } | ConvertTo-Json)
}
Invoke-RestMethod @request
~~~

`/api/v4/input` 是受控状态写入口；执行计划、指标和连续性接口是只读观测。输入可能返回 `pending`，因为心跳会继续处理并受认知超时约束。

## API 入口

`/api/v4`、`/api/v11`、`/api/v13` 是功能/兼容路径，不是产品版本。客户端应读取健康响应中的 `product_version` 判断产品发行号。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v4/input` | 提交外部输入和可选目标 |
| `GET` | `/api/v4/state` | 有界脑状态快照 |
| `GET` | `/api/v4/health` | 运行状态、心跳、记忆和执行摘要 |
| `GET` | `/api/v4/continuity` | P7 前置条件的只读投影，不授予权限 |
| `GET` | `/api/v4/monologue` | 当前内在独白的投影 |
| `GET` | `/api/v4/memory/search?q=...` | 记忆关键词检索 |
| `GET` | `/api/v11/tasks` | 分层长期任务队列 |
| `GET` | `/api/v11/autonomy` | 有界自主回合状态和有限历史 |
| `GET` | `/api/v13/tasks` | 执行计划摘要 |
| `GET` | `/api/v13/tasks/{plan_id}` | 单个执行计划详情 |
| `GET` | `/api/v13/metrics` | 执行、学习和驱动力指标 |
| `WS` | `/ws` | 实时状态推送与输入响应 |

完整的请求/响应 schema 以运行中的 `/docs` 和 `api/main.py` 为准。

## 运行时结构

~~~text
HTTP / WebSocket / dashboard
              │
              ▼
        FastAPI (api/main.py)
              │
              ▼
       Brain → BrainStem heartbeat
              │
     ┌────────┼────────┐
     ▼        ▼        ▼
  memory   goals    bounded bridge
     │        │        │
     └────────┴────────┘
              ▼
       SQLite WAL state

P7 controlled host ── separate, explicit boundary ──▶ evaluator / promotion
~~~

心跳默认每 2 秒运行一次；普通 API 不会把宿主能力、原始账本、候选路径或密钥投影到公开响应。

## 普通运行与 P7 宿主边界

普通 `Brain()` 构造只绑定状态库和 `BrainStem`。它不会默认创建 `EvaluationHarness`、`PromotionController` 或 Docker 受控宿主。P7 必须由外置宿主显式提供能力，并按提案、固定评估、明确授权和晋升边界推进。

P7 的安全事实：

- 动机阈值只产生 `IterationNeed`/提案，不直接授权自修改。
- 主动 `EVOLUTION` 只有在固定评估证明主维度有可重复改善且关键指标无回归时才允许晋升；`RECOVERY`/`SUCCESSION` 的目标是恢复可信基线，不把退化包装成进步。
- 评估失败或超时仍拒绝候选；可信的进程停止证据会独立记录。
- 历史上 `process_stopped=false` 的租约不会被新 patch 自动解锁，仍需安全恢复/人工处理。
- 没有真实 Docker daemon、模型候选、沙箱证明、accepted run ID、人工授权和晋升证据时，系统保持 `fail-closed`。
- P7 不会自动启动 Docker Desktop、拉取镜像、写入远程仓库或把离线 mock 结果当作生产闭环。

当前公开验证记录仍将真实 Docker/模型/晋升/重启/回滚与离线测试区分开来；“本地测试通过”不代表生产部署完成。

## 开发测试

测试命令只适合开发副本。推荐使用没有 `.env` 的干净 checkout、仓库外临时 DB，并显式清理会影响测试的环境变量。仅删除当前进程的密钥变量不够：`services/llm_client.py` 仍可能在导入时从根目录 `.env` 读取它们。

~~~powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
$env:BRAIN_MEMORY_OFFLINE = '1'
$testDb = Join-Path ([System.IO.Path]::GetTempPath()) ('brain-memory-test-' + [guid]::NewGuid().ToString('N') + '.sqlite')
$env:BRAIN_MEMORY_DB_PATH = $testDb
Remove-Item Env:DEEPSEEK_API_KEY,Env:DASHSCOPE_API_KEY,Env:GLM_API_KEY,Env:ZHIPU_API_KEY,Env:BRAIN_MEMORY_P7_MANIFEST_KEY -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m pytest -q
~~~

2026-09-19 的本地离线、无 `.env` 快照为 `457 passed, 2 skipped`（Windows symlink 条件）、`46` 个子测试，约 42 秒。这是与代码提交 [5a026a5](https://github.com/sjxbbdb/brain-memory/commit/5a026a5ec3e3a9ed8ce353b995dd63dd2b04e724) 内容一致的本地回归证据，不是 Docker/真实模型/P7 生产验收。

测试安装会访问包索引；离线变量只约束运行时的模型和来源调用。不要把生产数据库或真实密钥带入测试进程。

## 隐私与安全边界

- 默认绑定本机地址；跨来源访问需要显式配置，不能把默认设置理解成公网服务配置。
- 健康、连续性、状态和 WebSocket 输出经过有界投影，避免暴露绝对路径、凭据形状、候选路径和原始账本。
- AgentBridge 写工具默认关闭；即使打开能力开关，具体行动仍需逐项审批和参数绑定。
- P7 活动树/候选树、数据库、账本、私有配置和密钥应由外部部署层隔离管理。
- 文件锁、manifest 和本地检查不是 OS ACL、容器隔离或人工发布审批的替代品。

## 项目结构

~~~text
brain-memory/
├── api/main.py                 FastAPI、WebSocket、dashboard 挂载
├── brain/                      Brain、BrainStem、记忆/目标/自主与治理模块
├── services/                   LLM 客户端、来源适配器和 prompt
├── storage/database.py         SQLite WAL 状态、记忆与生命周期 lease
├── agent/ + agent_bridge.py    工具注册与受控执行边界
├── tools/p7_controlled_host.py P7 外置受控宿主入口
├── static/                     本地仪表盘
├── test_*.py                   根目录回归测试
├── version.py + config.py      产品版本与运行配置
└── docs/                       ADR、研究笔记与验证记录
~~~

`brain/` 中还包含 `LifeKernel`、动机、稳态、评估、晋升和 succession 模块；它们是可嵌入的治理接口。普通启动不会自动繁衍代际或获得 P7 宿主能力，真实验收仍以显式接线和证据为准。

## 文档

日期化的发布和 verification 文档是当时的冻结快照，不自动代表当前仓库全部状态；请结合当前代码、README 和最新验收阅读。尤其是旧 P7 记录中的历史限制可能已被后续代码更新，不能单独当作当前能力清单。

- [v0.1 发布说明](RELEASE_v0.1.md)
- [领域上下文与命名边界](CONTEXT.md)
- [数字生命工程契约](docs/adr/0001-digital-life-contract.md)
- [自修改与评估 ADR](docs/adr/0002-self-modification-and-evaluation.md)
- [数字生命操作性基础](docs/research/digital-life-operational-basis.md)
- [P7 受控迭代验证记录](docs/verification/p7-controlled-iteration-2026-09-11.md)
- [P6 长运行验证记录](docs/verification/p6-long-run-2026-09-09.md)
- [P6 LivingWorld soak 记录](docs/verification/p6-living-world-soak-2026-09-09.md)

## FAQ

### 这是一个已经有意识的数字生命吗？

目前不能这样宣称。它是以连续性、记忆、目标、受控自治和恢复为研究对象的软件原型；可观察行为和自动化测试不能证明主观体验。

### 为什么 API 是 `/api/v4`，产品却是 v0.1？

API 路径和历史能力标签需要兼容旧客户端，产品发行号由 `version.py` 独立维护。不要通过路由编号推断产品版本。

### 普通启动会自动修改代码吗？

不会。普通 `Brain` 不自动绑定 P7 evaluator/controller；AgentBridge 写工具也默认关闭。

### `BRAIN_MEMORY_OFFLINE=1` 是否等于安全沙箱？

不是。它是运行时的模型/来源调用开关，不是网络隔离、容器隔离或操作系统权限边界。

### 为什么本地测试通过仍不能说 P7 完成？

P7 还需要真实、可审计的 Docker/模型评估、沙箱证明、人工授权、原子晋升、重启连续性和显式回滚证据。离线/mock 测试只覆盖代码和降级路径。

### License 是什么？

仓库包含 [MIT License](LICENSE)。

## 技术栈

Python 3.12、FastAPI、Uvicorn、WebSocket、aiohttp、Pydantic、SQLite WAL。
