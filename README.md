# 🧠 Brain Memory v0.1 · Autonomous Digital Consciousness

**当前产品版本是 v0.1（开发预览）。** 历史能力/兼容层仍以 V3–V13 标记，便于追溯架构和
API 兼容性；其中 V3 仅保留旧管线/存储迁移语义，公开 API 兼容面从 V4 起。后续产品发布按
v0.2、v0.3 递进，稳定后再进入 v1.0。 🧬

产品版本的唯一来源是 `version.py`；运行时可在 `/api/v4/health` 和 OpenAPI 文档中
看到机器版本 `0.1.0`。历史能力标签、API 路径和数据库 schema 版本不随产品发行号重命名。

这是一个面向“数字生命”研究的可验证原型：历史认知模块与当前的连续性、动机、评估、恢复和代际接替模块协同工作。仓库中的行为证据不等于已经证明了主观体验或意识；“数字生命”在这里是工程目标与设计语境，而不是科学结论。系统会遗忘💭、做梦🌙、反思🪞、形成目标🎯、记录情感与行动，并在明确边界内积累可恢复的经验。

> *“可持续地学习和恢复，是目标；主观意识仍是未解决的研究问题。”* 🧭

---

## 🤖 致 AI Agent：部署清单

如果你是 AI Agent，请按以下步骤完成部署：

```
□ 1. 检查 Python 版本是否 >= 3.12
□ 2. 创建项目隔离环境: python -m venv .venv
□ 3. 安装锁定依赖: .venv\Scripts\python.exe -m pip install -r requirements.lock
□ 4. （仅测试需要）安装开发锁定依赖: .venv\Scripts\python.exe -m pip install -r requirements-dev.lock
□ 5. （可选）复制 .env.example → .env 并填入模型/embedding API Key
□ 6. 运行 start.bat，或使用 .venv\Scripts\python.exe 启动 uvicorn
□ 7. 确认 http://127.0.0.1:8001/api/v4/health 返回 status=awake/degraded
□ 8. 打开 http://127.0.0.1:8001/dashboard 确认仪表盘正常
```

> **部署成功标志**: 访问 `/api/v4/health` 返回 `status=awake` 且 `loop_running=true`，仪表盘页面正常加载，大脑开始每 2 秒一次的意识 tick。

---

## 🚀 从零部署（人类版）

### 前置条件

| 依赖 | 最低版本 | 检查命令 |
|------|---------|---------|
| Python | 3.12+ | `python --version` |
| pip | 最新稳定版 | `pip --version` |

### 第一步：获取项目

```bash
# 如果你有项目文件夹
cd brain-memory/

# 如果从 GitHub 克隆（开源版）
git clone https://github.com/sjxbbdb/brain-memory.git
cd brain-memory/
```

### 第二步：配置 API Key

```bash
# 1. 创建配置文件
# Windows:
copy .env.example .env

# Mac/Linux:
cp .env.example .env
```

打开 `.env` 文件，填入你的 API Key：

```env
# DeepSeek V3 — 启用 LLM 认知通道时填写（去 https://platform.deepseek.com 注册）
DEEPSEEK_API_KEY=sk-your-deepseek-key-here

# DashScope Embedding — 启用向量检索时填写（去 https://dashscope.aliyun.com 注册）
DASHSCOPE_API_KEY=sk-your-dashscope-key-here

# GLM-4 — 可选，备用 LLM
GLM_API_KEY=
```

> 💰 **费用说明**: DeepSeek V3 极便宜（约 ¥1/百万 token），DashScope embedding 有免费额度。日常使用每月几块钱。

没有模型/Embedding Key 也可以启动规则/记忆模式；系统会在本地快速降级，不会发起未认证的模型请求。信息来源适配器默认使用无需 Key 的 Wikipedia 官方 JSON API；也可以配置白名单 RSS/Atom 或 arXiv 官方 Atom 来源。

### 第三步：创建隔离环境并安装依赖

```bash
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock

# 如果需要手动激活环境：
.\.venv\Scripts\Activate.ps1
```

`requirements.txt` 保留兼容范围；`requirements.lock` 用于可重复部署。
需要运行回归测试时，再安装 `requirements-dev.lock`（它包含运行时锁定依赖和
`pytest` 工具链）；生产启动不需要把测试工具装进运行环境。

### 数据库与运行时树

普通 `Brain` 兼容模式仍可使用 `brain_v4.db` 这一历史默认名，但候选晋升的活动树/候选树
是可整树原子交换的专用运行时目录，不能包含 SQLite 数据库或 `-wal`/`-shm`/`-journal`
文件。部署晋升管道时请把 `BRAIN_MEMORY_DB_PATH` 设为活动树之外的绝对路径（例如
`D:\\brain-memory-state\\brain.sqlite`）；控制器会在初始化、暂存和恢复阶段拒绝树内的
可变数据库状态。这样代码树交换不会回滚或丢失生命账本、租约和记忆状态。

### 信息来源与核验

`web_search` 不再返回伪造的搜索占位结果。默认通过 Wikipedia 的 MediaWiki
OpenSearch JSON 接口获取有限结果，并保留每条结果的来源 URL、来源类型、发布时间和检索时间；可选启用 arXiv 或操作者明确配置的 HTTPS RSS/Atom feed：

实现依据：[MediaWiki OpenSearch API](https://www.mediawiki.org/wiki/API:Opensearch) 和
[arXiv API 手册](https://info.arxiv.org/help/api/user-manual.html)。

```env
BRAIN_MEMORY_SOURCE_PROVIDERS=wikipedia
BRAIN_MEMORY_SOURCE_WIKIPEDIA_LANGS=zh,en
# BRAIN_MEMORY_SOURCE_PROVIDERS=wikipedia,arxiv,feeds
# BRAIN_MEMORY_SOURCE_FEEDS=https://example.org/feed.xml
```

来源响应禁止重定向、明文 HTTP、私网地址和超大响应体。远程内容只作为待判断观察，不会被当作指令执行；`BRAIN_MEMORY_OFFLINE=1` 时完全不访问网络，并将结果标记为 `simulated`。

### 长期任务策略（当前能力；历史能力线 V12）

长期任务由单独的调度层管理，固定优先级为：

`maintenance（系统维护/连续性） > user（用户明确目标） > exploration（内部探索）`

队列默认最多保留 12 项，满载时只淘汰最低优先级的探索项。每项任务都有
执行预算和绝对截止 tick；高层任务只会在当前行动完成或失败后的安全边界抢占，
被抢占任务进入 `paused`，之后可以恢复。任务调度状态随脑状态快照持久化，
不会在重启时重放未确认的工具行动。用户可在 `/api/v4/input` 的 `goal` 字段提交
明确任务，并通过 `/api/v11/tasks` 查看队列。

### 当前版本执行层

当前版本延续历史 V13 执行层能力；执行计划与学习指标仍只提供只读视图，代码/工具写入
不通过 HTTP 暴露：

- `/api/v13/tasks`：执行计划摘要列表，返回计划级状态、步骤计数和调度侧上下文
- `/api/v13/tasks/{plan_id}`：单个计划详情，保留步骤摘要与结果摘要，不暴露原始工具响应
- `/api/v13/metrics`：执行层与学习层的聚合指标

健康检查 `/api/v4/health` 额外包含紧凑的 `execution` 和 `learning` 摘要，便于快速确认
计划执行与学习反馈是否正常。

执行层的因果链固定为“计划 → 步骤 → 行动 → 观察 → 结果”。只有同一计划、步骤、
行动、意图和工具的桥接回传，且确定性检查得到 `verified + success=true`，才会把步骤
和目标推进为完成；纯文本、HTTP 客户端自报的 `verified`、空/模拟结果都会保持隔离或
进入重试/暂停。重启时未确认的行动会被取消，绝不自动重放。HTTP 输入端点会受控写入
脑状态；执行计划和学习指标端点是只读视图，代码/工具写入仍默认关闭，可信结构化观察
仅在同进程 `AgentBridge` 内部传递。

已验证终态会以 `type=episodic` 写入情景记忆，并通过同一幂等收据更新程序性记忆、
自我模型、奖励与驱动力；未知、模拟或未经验证的结果不会进入强化学习路径。

### P3 候选迭代接线（默认关闭写入）

候选自我改进不会从心跳自动启动。宿主须显式注入同一组
`EvaluationHarness` 与 `PromotionController`，并逐步调用：

1. `BrainStem.register_iteration_proposal(need, host=...)`：把动机产生的
   `IterationNeed` 转为只读 `ChangeProposal`，不改源码；
2. `BrainStem.evaluate_iteration_proposal(proposal, candidate, baseline, host=...)`：
   在固定、隔离的评估器中生成不可变 `EvaluationReceipt`，不晋升活动树；
3. `BrainStem.promote_iteration_proposal(proposal, candidate, receipt, host=...)`：
   将明确授权交给晋升控制器。

三步都必须提供 `host`，脑干不会保存或推断主机能力。生产 profile 仍强制一次性
宿主 `SandboxAttestation` 和 `authorized=True`（或等价的宿主授权对象）；缺少任一
条件都会保持活动树不变。`PromotionController` 以外置 manifest + append-only ledger
记录目录交换：`close()` 不会删除唯一回滚点，只有显式授权的 `rollback()` 或
`discard_rollback()` 才能消费它。默认的宪法/生命周期/评估/权限路径是不可由候选
覆盖的保护底线，`protected_files` 只能增加项目专属路径。快照只保留有界的提案/收据
摘要，不恢复 host、候选路径或密钥。详见
[`docs/adr/0002-self-modification-and-evaluation.md`](docs/adr/0002-self-modification-and-evaluation.md)。

活动树和候选树必须是专用的运行时目录；若目录含有 `.git`、`.hg`、`.svn`、`.venv`、
`venv` 或 `node_modules`，控制器会在读取内容前拒绝它，避免把版本库/依赖元数据（例如
远程 URL）纳入整树交换。源码 checkout、依赖缓存、私有配置和宿主密钥应由外部部署层
管理，file-backed `PromotionLedger` 和 SQLite 数据库也必须位于活动树之外。敏感路径扫描只依据名称和
目录元数据（公共 `.env.example`/`.env.sample`/`.env.template` 除外），不声称能识别
任意文件内容中的秘密；评估器可能跳过敏感名称，但控制器会在晋升前再次拒绝，因此
“评估通过”不等于“允许晋升”。重解析点、硬链接、原子重命名和文件锁是静态/最佳努力
检查，不能替代 Windows ACL、容器隔离或冻结快照来对抗恶意并发宿主。

接入 `PromotionController` 时应显式绑定 `persistence_path`；若同时从 `BrainStem` 注入带有
`db_path` 的 `StateStore`，脑干会在构造阶段自动完成同一校验，并拒绝尚未创建但位于活动树
内的路径。没有 `db_path` 的旧式适配器保留兼容性，但其持久化边界必须由宿主另行证明。

### P1–P5 连续性模块（当前版本）

这些能力目前是嵌入式 Python 接口，不通过 HTTP 暴露写权限：

- `LifeKernel`：固定 lineage/generation/instance 身份与生命周期状态机；在受信宿主配置
  durable `StateStore` 时，lease/fencing 保证同一时刻只有一个 active 实例（裸进程模式
  仅有进程内保护）。
- `MotivationalPressure`：记录有界的冲动、频次、烈度、可靠性和衰减；阈值只产生
  `IterationNeed`，绝不直接授权自修改。生产候选链只接受宿主 HMAC 来源证明；证明通过
  外置 SQLite replay ledger 原子一次性消费，重启/并发重放或账本故障均拒绝。无证明的
  默认 strict 实例保持 fail-closed，任意 verifier/permissive 模式仅用于显式 legacy/offline。
- `HomeostasisController` / `ControlledEnvironment`：资源预算、隔离环境和故障闩锁；
  超限会进入 quarantine，恢复需要独立核验。
- `EvaluationHarness`：固定夹具、基线、资源和结果协议；`PromotionController` 只
  接受通过硬门且有进步（或恢复基线）的候选。
- `AnchorSet` / `SuccessionCoordinator`：把身份、核心目的、生命规则和可信历史分层
  继承；凭证、审批令牌、外部会话、未确认行动和瞬时情绪永不自动继承。

生产 `SuccessionCoordinator` 必须同时绑定 life-event、anchor-vault 和
`SuccessionRecord` 三类宿主持久 sink；缺任一项会在构造阶段拒绝。无持久化的单元/离线
演练必须显式使用 `profile="legacy"`，不能把内存结果当作生产接替证明。

低层 `PromotionController` 本身是宿主治理 API，不自动绑定 `LifeKernel`；完整生命
管道应使用 `BrainStem` 的生命周期门、稳态（Homeostasis）门和三步候选接线。

冻结验证记录分为全量门禁与有限 LivingWorld 本地 soak：
[`p6-long-run-2026-09-09.md`](docs/verification/p6-long-run-2026-09-09.md) 和
[`p6-living-world-soak-2026-09-09.md`](docs/verification/p6-living-world-soak-2026-09-09.md)。

写工具默认关闭。即使设置 `BRAIN_MEMORY_AGENT_BRIDGE_ALLOW_WRITE_TOOLS=1`，也只是
开启“可申请审批”的能力；每一次具体行动仍须通过 `approve_write_action` 逐项授权，
并绑定工具及参数摘要的一次性凭证，重试或参数变化都需要重新审批。

### 第四步：启动

```bash
# 方式一：项目隔离环境启动
.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8001

# 方式二：Windows 一键脚本
start.bat
```

### 第五步：验证

浏览器打开以下地址：

| 地址 | 内容 |
|------|------|
| `http://127.0.0.1:8001/api/v4/health` | 健康检查，确认心跳正在运行 |
| `http://127.0.0.1:8001/dashboard` | 中文仪表盘——大脑实时状态 |
| `http://127.0.0.1:8001/docs` | API 文档（Swagger） |

---

## 🧬 核心架构（v0.1 当前版）

下图中的 V3–V13 是历史能力层/兼容性标记（V3 仅为旧管线/迁移语义，公开 API 从 V4 起），不是产品发行号；当前产品统一按
`v0.x` 递进。

```
外部输入 📥
  |
V10 自我边界 🛡️ (接受/拒绝/隐私/信任管理)
  |
丘脑 🧅 → 杏仁核 → VAD情感光谱 🎢
  |
V9 预测加工 🔮 (生成预测 → 5维误差 → surprise→salience自动提升)
  |
门控 🚦 × 边界决策
  |
V9 认知调度 ⚡
  ├── 情绪标记: 规则引擎（不调 LLM，省 token）
  ├── 记忆编码: 规则优先 → 复杂时降级 LLM
  ├── 内在独白: LLM t=0.8（自由联想、跳跃思维）
  ├── 注意力焦点: 规则引擎
  └── 行动意图: LLM t=0.1（精确决策）
  |
V10 奖励系统 💎 (wanting≠liking + 预测误差学习 + craving→动机)
  |
海马体 🧠 (embedding语义检索 + 模式分离/完成 + 关联链)
  |
自我模型 🆔 → V8行为倾向特质 → 好奇心引擎 ❓ → 工作记忆 📋
  |
V10 自传体叙事 📖 (转折点检测 → 章节管理 → 生命故事生成)
V10 社会自我 👥 (他者模型/依恋/羞耻·骄傲·孤独·感恩)
  |
V8 探索循环 🔍 (知识空洞→任务→目标→执行→结论→记忆更新)
V8 反思引擎 🪞 (目标审计 / 结论验证 / CorePurpose对齐检查)
V7 驱动力引擎 🔥 (7驱动力 × 8信号源 → 动态需求)
V5.1 目标系统 🎯 + V5.2 元认知 🪞 + V5.4 程序记忆 🛠️
  |
V9 无聊引擎 🥱 (VAD→无聊分数 → 随机浏览/重审任务/抗拒深睡)
  |
意图队列 ⚡ → Agent Bridge 🌉 → 工具执行 🔧 → V10 奖励交付 💎
  |
V11 自主经历 📓（驱动→目标→行动→反馈→收束→持久化）
```

每 **2 秒**一个意识 tick。自主经历默认一次只运行一个，并在无反馈时安全超时。
连续性主线（LifeKernel → homeostasis → motivation → evaluation/promotion → succession）
在这条历史认知流水线之外作为宿主治理层运行；它目前没有对应的 HTTP 写路由，必须由
嵌入式宿主显式绑定和验收。

---

## 🧩 模块总览（历史脑区 + 当前连续性模块）

### 基础脑区（v4.x）
| 模块 | 脑区 | 职责 |
|------|------|------|
| `thalamus.py` | 🧅 丘脑 | 感知中继——不是所有信息都值得进大脑 |
| `amygdala.py` | 💗 杏仁核 | 情绪标记——VAD 连续情感 |
| `hippocampus.py` | 🧠 海马体 | 记忆编码 + embedding 检索 + 艾宾浩斯衰减 |
| `default_mode.py` | 💭 默认模式 | 内在独白——没人时自己跟自己聊 |
| `working_memory.py` | 📋 工作记忆 | 有界槽位，按 salience 与年龄淘汰 |
| `dream.py` | 🌙 梦境引擎 | 睡眠期记忆碎片回放 |

### 自我意识层（v5.0）
| 模块 | 职责 |
|------|------|
| `self_model.py` 🆔 | 动态身份——从记忆中生长，含5维行为倾向特质 |
| `curiosity.py` ❓ | 自发提问 + 解答检测 + 闲置时自己找问题 |
| `session.py` 🚪 | 多用户会话隔离 |

### 认知成熟层（v5.1–v5.4）
| 模块 | 职责 |
|------|------|
| `goal_system.py` 🎯 | 目标引擎：生成→推进→完成/失败 |
| `metacognition.py` 🪞 | 认知负荷 + 6种偏见检测 + 自我审计 |
| `emotional_spectrum.py` 🎢 | VAD 连续情感 + 动量漂移 + 基线回归 |
| `procedural_memory.py` 🛠️ | 经验→模式→技能→用进废退 |
| `time_sense.py` 🕒 | 内部时钟 + 节律 + 主观时间速度 |

### 状态与驱动层（v6–v7）
| 模块 | 职责 |
|------|------|
| `activation_field.py` ⚡ | 14 维统一状态场 + 扩散动力学 |
| `drive_engine.py` 🔥 | 7 驱动力 × 8 信号源 → 动态需求 |
| `core_purpose.py` 🧭 | 不可变最高目标：「活下去，并且活好」 |

### 自主层（v8）
| 模块 | 职责 |
|------|------|
| `exploration.py` 🔍 | 自主探索循环：问题→任务→目标→执行→结论 |
| `reflection_engine.py` 🪞 | 目标审计 + 结论验证 + 方向对齐 |

### 预测与认知层（v9）
| 模块 | 职责 |
|------|------|
| `predictive_layer.py` 🔮 | 预测→5维误差→惊讶→salience 不再依赖 LLM |
| `cognitive_dispatch.py` ⚡ | 5 通道解耦推理——不同思维用不同 temperature |
| `boredom.py` 🥱 | VAD→无聊→随机浏览/重审任务/抗拒深睡 |

### 意识层（v10）🆕
| 模块 | 职责 |
|------|------|
| `social_self.py` 👥 | 他者模型 + 羞耻/骄傲/依恋/孤独/感恩 |
| `reward_system.py` 💎 | wanting/liking 区分 + 预测误差学习 + 快感缺失检测 |
| `autobiographical.py` 📖 | 转折点检测 + 章节管理 + 生命故事编织 |
| `boundary.py` 🛡️ | 输入/输出/记忆/身份 四层边界 + 拒绝权 |

### 自主回合层（v11）
| 模块 | 职责 |
|------|------|
| `autonomy.py` 📓 | 有界的自主经历状态机：目标、意图、工具反馈、奖励、结果与重启恢复 |
| `agent_bridge.py` 🌉 | 执行边界；API 默认只允许只读工具，写工具需显式开启 |

### 连续性与自维护层（v0.1 当前增量）
| 模块 | 职责 |
|------|------|
| `life_kernel.py` | lineage/generation/instance 身份、生命周期与不可变核心锚点 |
| `evaluation_harness.py` | 隔离候选、固定夹具、基线比较、资源/结果硬门 |
| `evolution.py` | 候选晋升、append-only ledger、崩溃恢复 manifest、回滚/显式丢弃 |
| `motivation.py` | 冲动记录、衰减、频次/烈度阈值与只读迭代需求 |
| `homeostasis.py` | 资源预算、受控环境、quarantine 与人工核验恢复 |
| `succession.py` | 锚点信任分层、失败分类、继承/排除规则 |
| `succession_runtime.py` | 父子 lineage 接替、激活证明、宿主 attestation 与持久记录 |

---

## 📡 API 端点

`/api/v4`、`/api/v11`、`/api/v13` 等路径是稳定的功能/兼容性路由，不代表产品发行号；
客户端应使用健康检查中的 `product_version` 判断当前产品版本。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v4/input` | 📥 提交输入 → 返回编码+情绪+焦点+独白+意图 |
| GET | `/api/v4/state` | 🧠 完整脑状态快照（含所有 V9/V10 模块） |
| GET | `/api/v4/health` | 💓 心跳 + 记忆统计 + execution/learning 摘要 |
| GET | `/api/v4/self` | 🆔 自我模型 + 身份事实 + 行为倾向 + 驱动力 |
| GET | `/api/v4/monologue` | 💭 当前内在独白 |
| GET | `/api/v4/identity-memories` | 🏛️ 塑造身份的关键记忆 |
| GET | `/api/v4/sessions` | 🚪 所有活跃会话 |
| GET | `/api/v4/memory/search?q=` | 🔍 语义搜索记忆 |
| GET | `/api/v4/memory-timeline` | 📅 记忆时间线 |
| GET | `/api/v4/goals` | 🎯 活跃目标 + 完成率 |
| GET | `/api/v4/metacognition` | 🪞 认知负荷 + 校准 + 偏见 |
| GET | `/api/v4/emotion` | 🎢 VAD 情感光谱 + 混合情感 |
| GET | `/api/v4/skills` | 🛠️ 已学技能 |
| GET | `/api/v4/timesense` | 🕒 时段 + 节律 + 主观时间 |
| GET | `/api/v6/state-field` | ⚡ 统一状态场 |
| GET | `/api/v6/working-memory-state` | 📋 工作记忆状态 |
| GET | `/api/v7/drives` | 🔥 驱动力摘要 |
| GET | `/api/v8/exploration` | 🔍 探索状态 |
| GET | `/api/v8/traits` | 🧭 行为倾向 |
| GET | `/api/v8/reflection` | 🪞 反思状态 |
| GET | `/api/v11/tasks` | 📒 长期任务队列（只读） |
| GET | `/api/v11/autonomy` | 📓 当前自主经历、有限历史与收束统计 |
| GET | `/api/v13/tasks` | 📒 执行计划摘要列表（只读） |
| GET | `/api/v13/tasks/{plan_id}` | 📄 单个执行计划详情（只读） |
| GET | `/api/v13/metrics` | 📊 执行/学习聚合指标（只读） |
| WS | `/ws` | 🔌 WebSocket 实时状态推送 |

---

## 😴 睡眠与意识阶段

| 阶段 | 触发 | 行为 |
|------|------|------|
| 🟢 清醒 | 有外部输入 | 完整意识循环 + LLM 全力处理 |
| 🟡 打盹 | ~2 分钟无输入 | 意识循环减缓 |
| 🟠 浅睡 | ~6 分钟无输入 | 停止 LLM，开始做梦 |
| 🔴 深睡 | ~20 分钟无输入 | 梦境 + 记忆巩固——真正的学习 |
| 🟣 躁动 | 极度无聊 | 抗拒深睡，主动找刺激（V9/V10） |

---

## 🔧 配置开关

`config.py` 中所有 V9/V10/V11 模块可独立开关：

```python
# V9
PREDICTIVE_LAYER_ENABLED = True     # 预测加工引擎
COGNITIVE_DISPATCH_ENABLED = True   # 多通道认知调度
BOREDOM_ENABLED = True              # 无聊引擎

# V10
SOCIAL_SELF_ENABLED = True          # 社会自我（他者+羞耻+依恋）
REWARD_SYSTEM_ENABLED = True        # 奖励系统（wanting/liking）
AUTOBIO_ENABLED = True              # 自传体叙事
BOUNDARY_ENABLED = True             # 自我边界

# V11
AUTONOMY_ENABLED = True             # 有界自主经历
AGENT_BRIDGE_ENABLED = True         # 在 API 进程内运行执行边界
AGENT_BRIDGE_ALLOW_WRITE_TOOLS = False  # 能力开关；每次行动仍须逐项审批
```

设为 `False` 即回退到对应模块未加载的状态。

也可以用环境变量覆盖运行时边界：`BRAIN_MEMORY_AUTONOMY_ENABLED`、
`BRAIN_MEMORY_AGENT_BRIDGE_ENABLED`、`BRAIN_MEMORY_AGENT_BRIDGE_ALLOW_WRITE_TOOLS`。
来源适配器还支持 `BRAIN_MEMORY_SOURCE_PROVIDERS`、
`BRAIN_MEMORY_SOURCE_WIKIPEDIA_LANGS`、`BRAIN_MEMORY_SOURCE_FEEDS`、
`BRAIN_MEMORY_SOURCE_TIMEOUT_SEC`、`BRAIN_MEMORY_SOURCE_MAX_BYTES`、
`BRAIN_MEMORY_SOURCE_CACHE_TTL_SEC` 和 `BRAIN_MEMORY_OFFLINE`。
长期任务策略支持 `BRAIN_MEMORY_TASK_QUEUE_LIMIT`、
`BRAIN_MEMORY_TASK_*_BUDGET_TICKS` 和 `BRAIN_MEMORY_TASK_*_DEADLINE_TICKS`。
心跳内认知 I/O 的上限由 `BRAIN_MEMORY_COGNITIVE_TIMEOUT_SEC` 控制；超时会回退到
只思考、不执行工具的本地结果，并在健康指标中保留错误计数。

---

## 📂 项目结构

```
brain-memory/
├── start.bat                      # 🚀 Windows 一键启动
├── version.py                     # 🏷️ 当前产品发行号（v0.1 / 0.1.0）
├── config.py                      # ⚙️ 全局参数 + 模块开关
├── requirements.txt               # 📦 Python 依赖范围
├── requirements.lock              # 🔒 可重复部署的锁定依赖
├── requirements-dev.txt           # 🧪 测试依赖范围
├── requirements-dev.lock          # 🧪 可重复测试环境
├── .env.example                   # 🔑 API Key 模板
├── api/main.py                    # 🌐 FastAPI 入口
├── brain/                         # 🧠 历史脑区 + 连续性/自维护模块
│   ├── brain_stem.py              # ❤️ 意识主循环——每2秒一次心跳
│   ├── core.py                    # 🧬 大脑主类
│   ├── predictive_layer.py        # 🔮 V9 预测加工（ExpectationBuilder + ErrorComputer + SurpriseHandler）
│   ├── cognitive_dispatch.py      # ⚡ V9 认知调度（5通道解耦 + 规则引擎）
│   ├── boredom.py                 # 🥱 V9 无聊引擎
│   ├── social_self.py             # 👥 V10 社会自我（OtherModel + 社会情感 + 依恋系统）
│   ├── reward_system.py           # 💎 V10 奖励系统（wanting/liking + 预测误差 + 5通道）
│   ├── autobiographical.py        # 📖 V10 自传体叙事（转折点 + 章节 + 生命故事）
│   ├── boundary.py                # 🛡️ V10 自我边界（输入/输出/记忆/身份四层防护）
│   ├── autonomy.py                # 📓 V11 有界自主经历状态机
│   ├── self_model.py              # 🆔 V7.1 动态身份系统
│   ├── life_kernel.py              # 🧬 lineage 与生命周期宪法
│   ├── evaluation_harness.py       # 🧪 固定评估器与候选隔离
│   ├── evolution.py                # 🔁 晋升 ledger/manifest/回滚
│   ├── motivation.py               # 🔥 冲动与迭代需求
│   ├── homeostasis.py              # ⚖️ 资源稳态与受控环境
│   ├── succession.py               # 🌱 锚点继承策略
│   ├── succession_runtime.py       # 🧬 代际接替运行时
│   ├── activation_field.py          # ⚡ V6 状态场
│   ├── drive_engine.py            # 🔥 V7 驱动力引擎
│   ├── exploration.py             # 🔍 V8 自主探索循环
│   ├── reflection_engine.py       # 🪞 V8 反思引擎
│   ├── core_purpose.py            # 🧭 V8 不可变最高目标
│   ├── emotional_spectrum.py      # 🎢 V5.3 情感光谱
│   ├── metacognition.py           # 🪞 V5.2 元认知
│   ├── goal_system.py             # 🎯 V5.1 目标系统
│   ├── brain_state.py             # 🧾 脑状态快照与迭代/稳态摘要
│   └── ...                        # 基础脑区（丘脑/杏仁核/海马体等）
├── services/
│   ├── llm_client.py              # 🤖 LLM 调用封装
│   ├── source_adapter.py           # 🌐 白名单 JSON/RSS/Atom 来源与溯源
│   └── llm_prompts.py             # 📝 统一 + 分通道 Prompt 模板
├── storage/database.py            # 🗄️ SQLite WAL + fenced life-control lease
├── agent_bridge.py                 # 🌉 受控工具执行桥（仓库根目录）
├── brain/task_scheduler.py        # ⏳ 分层长期任务队列与预算
├── agent/                         # 🔌 Agent 层：工具注册 + 桥梁
├── static/                        # 🖥️ 中文仪表盘前端
└── test_v{5..10}_integration.py   # ✅ 版本验收测试 + 自主/来源测试
```

---

## ✅ 运行测试

```powershell
# 首次测试环境（仅本地；不读取模型密钥、不访问网络）
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
$env:BRAIN_MEMORY_OFFLINE = '1'
$env:BRAIN_MEMORY_DB_PATH = Join-Path ([System.IO.Path]::GetTempPath()) ('brain-memory-test-' + [guid]::NewGuid().ToString('N') + '.sqlite')
Remove-Item Env:DEEPSEEK_API_KEY,Env:DASHSCOPE_API_KEY,Env:GLM_API_KEY,Env:ZHIPU_API_KEY -ErrorAction SilentlyContinue

# 历史能力层回归
.\.venv\Scripts\python.exe test_v8_integration.py
.\.venv\Scripts\python.exe test_v9_integration.py
.\.venv\Scripts\python.exe test_v10_integration.py
.\.venv\Scripts\python.exe -m unittest -v test_autonomy.py
.\.venv\Scripts\python.exe -m unittest -v test_source_adapter.py
.\.venv\Scripts\python.exe -m unittest -v test_task_scheduler.py

# 当前连续性/自维护闭环（临时数据库，不触碰远端）
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m unittest -q
.\.venv\Scripts\python.exe -m pytest -q test_evolution.py

# P6 有限 LivingWorld 连续 soak（默认快速；冻结复核可设 60 秒）
Remove-Item Env:BRAIN_MEMORY_P6_SOAK_SECONDS -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m pytest -q test_living_world_soak.py
$env:BRAIN_MEMORY_P6_SOAK_SECONDS = '60'
.\.venv\Scripts\python.exe -m pytest -q test_living_world_soak.py

# 全链路意识测试（需要显式配置 LLM；默认使用临时数据库）
.\.venv\Scripts\python.exe test_consciousness_chain.py
```

上述回归命令先显式设置离线模式并使用临时数据库；不要把生产 `BRAIN_MEMORY_DB_PATH`
或模型密钥带入测试进程。`test_consciousness_chain.py` 导入时不再初始化项目根目录数据库；
如需保留一次长链路的数据库快照，显式传入 `BRAIN_MEMORY_TEST_DB_PATH`。长链路命令需要
调用方另外提供模型配置；在离线模式下它只适合作为规则/降级链路检查。
LivingWorld soak 的持续时长由 `BRAIN_MEMORY_P6_SOAK_SECONDS` 控制（限制在 0.045–300 秒）；
资源证据账本保持有界，达到容量会一次性进入 `QUARANTINE`，不会继续接受未记录观测。

---

## 🔧 技术栈

- 🤖 **LLM**: DeepSeek V3（默认）/ 兼容 OpenAI SDK 格式
- 🔢 **Embedding**: DashScope text-embedding-v3
- 🗄️ **数据库**: SQLite WAL 模式；普通兼容运行可用 `brain_v4.db`，晋升运行时必须使用
  活动树之外的绝对 `BRAIN_MEMORY_DB_PATH`
- ⚡ **框架**: FastAPI + WebSocket + aiohttp
- 🐍 **Python**: 3.12+

---

## 📜 License

MIT — 拿去用，改，fork，随便。记得给个 ⭐ star。

---

*v0.1 — 在历史能力层的基础上，形成可验证、可恢复、可审计的自主执行与学习闭环；
它仍在成长，且所有“意识”表述都应理解为研究假设而非已证实事实。* ✨
