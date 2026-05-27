# 🧠 Brain Memory System v2.0

> 一个受认知神经科学启发的**五层记忆系统**——为 AI Agent 提供完整的记忆闭环：自动摄入、注意力门控、情绪加权、艾宾浩斯衰减、睡眠巩固、上下文加载、五维认知检索。

**A five-layer cognitive memory system for AI agents** — full memory loop: auto-ingest, attention gating, emotion weighting, Ebbinghaus decay, sleep consolidation, context loading, and five-dimensional cognitive retrieval.

---

## 目录

- [v2.0 新特性](#v20-新特性)
- [核心理念](#核心理念)
- [五层记忆架构](#五层记忆架构)
- [核心机制](#核心机制)
- [接入方式](#接入方式)
- [自动记忆管道](#自动记忆管道)
- [快速开始](#快速开始)
- [API 参考](#api-参考)
- [项目结构](#项目结构)
- [配置参数](#配置参数)
- [Web 控制台](#web-控制台)

---

## v2.0 新特性

### 自动摄入管道 (Auto-Ingest)

```
Agent 对话 → feed_append.py → ingest_feed.jsonl
                                    ↓
                    observer.py (事件驱动 + cron 兜底)
                                    ↓
                    POST /api/v1/ingest
                         ↓
              ① 去重检测 (指纹 + 标题重叠)
              ② 注意力门控 (5步决策树)
              ③ 情绪加权 (5维公式)
                         ↓
                    SQLite 五层存储
```

任何智能体只需向 feed 文件追加一行 JSON，或直接 POST `/api/v1/ingest`，即可触发完整的自动记忆摄入。

### 上下文加载 (Context Load)

```
Agent 启动/话题转换 → POST /api/v1/context
                            ↓
                    五维认知检索
                  (语义×目标×情绪×时间×因果)
                            ↓
                should_inject? (>0.55 → 注入)
                            ↓
                 格式化的上下文块
              (可直接注入 system prompt)
```

### 三层过滤

| 层级 | 机制 | 作用 |
|---|---|---|
| 去重检测 | 指纹哈希 + 标题重叠率 >55% | 防止重复存储 |
| 注意力门控 | 5步决策树（显式标记→情绪→新颖→目标相关→丢弃）| 过滤低价值信息 |
| 情绪加权 | importance×30% + failure_cost×25% + novelty×20% + goal_relevance×15% + surprise×10% | 决定记忆强度 |

### Web 控制台升级

- 一键摄入面板：输入文本，自动推断 → 门控 → 入库
- 上下文加载面板：输入话题，即时检索相关记忆
- 管道状态栏：摄入/上下文/Observer/MCP 实时状态

---

## 核心理念

传统的 AI 记忆系统就是一个 key-value 存储加语义搜索。但人类的记忆远比这复杂——我们**不会**记住所有事，被注意力筛选；记忆会**衰减**，需要通过睡眠巩固；检索时，当前的**目标和情绪**会影响我们能回想起什么。

Brain Memory 将记忆科学中的关键机制工程化，让 Agent 拥有：

| 人类记忆机制 | 工程实现 |
|------------|---------|
| 注意力筛选 | 5 步决策树门控（显式标记 / 目标相关 / 高情绪 / 高新颖 / 默认通道） |
| 情绪对记忆的强化 | 5 维加权公式（重要性 30% × 失败代价 25% × 新颖度 20% × 目标相关 15% × 惊喜度 10%） |
| 艾宾浩斯遗忘曲线 | 指数衰减 + 检索逆衰减 + 半衰期延长 |
| 睡眠记忆巩固 | Phase 0→3 四阶段巩固（衰减 / 濒危扫描 / 冲突检测 / 反思叙事） |
| 状态依赖检索 | 5 维认知检索（语义 × 目标 × 情绪 × 时间 × 因果） |
| 情景/语义/程序记忆分离 | 5 层记忆架构 |

---

## 五层记忆架构

```
┌──────────────────────────────────────────────────┐
│                  Attention Gate                  │
│  输入 → 显式标记？→ 目标相关？→ 高情绪？→ ... │
└────────────────┬─────────────────────────────────┘
                 ▼
┌──────────────────────────────────────────────────┐
│                   5 层记忆存储                    │
│                                                  │
│  ┌─ episodic ────┐  ┌─ semantic ────┐            │
│  │ 情景事件        │  │ 语义知识        │            │
│  │ "昨天修了bug"   │  │ "STM32时钟树"    │            │
│  └────────────────┘  └────────────────┘            │
│                                                  │
│  ┌─ procedural ──┐  ┌─ narrative ───┐            │
│  │ 程序流程        │  │ 自我叙事        │            │
│  │ "外设初始化步骤" │  │ "我作为AI的成长" │            │
│  └────────────────┘  └────────────────┘            │
│                                                  │
│  ┌─ global ───────┐                              │
│  │ 全局共享记忆     │                              │
│  └────────────────┘                              │
└──────────────────────────────────────────────────┘
```

| 层级 | 类型 | 英文 | 内容 |
|-----|------|------|------|
| 1 | 情景 | episodic | 具体事件——何人、何时、何地、做了什么 |
| 2 | 语义 | semantic | 事实/概念知识——不绑定具体时间地点 |
| 3 | 程序 | procedural | 操作流程/步骤——"怎么做" |
| 4 | 叙事 | narrative | 自我叙事——持续的身份和故事 |
| 5 | 全局 | global | 跨 Agent 共享的公共知识 |

---

## 核心机制

### 1. 注意力门控 (Attention Gating)

并非每条信息都值得进入记忆。门控通过 5 步决策树自动筛选：

```
Step 1: explicit_mark? → YES → 直接通过 (importance=0.9)
Step 2: 高情绪信号？→ YES → emotion_weight=0.85，通过
Step 3: novelty > 0.7？→ YES → 通过
Step 4: goal_relevance > 0.5？→ YES → 通过
Step 5: 默认通道 → 权重低，但依然记录
```

### 2. 情绪权重 (Emotion Weight)

```
emotion_weight = importance×0.30 + failure_cost×0.25 + novelty×0.20
               + goal_relevance×0.15 + surprise_score×0.10
```

自动检测高情绪信号——识别"失败""突破""纠正""冲突"四类关键词（中英文）。

### 3. 艾宾浩斯衰减 (Ebbinghaus Decay)

```
R(t) = S₀ × e^(-λ × t) × (t + 1)^(-β)

其中:
  R(t)  = 时刻 t 的记忆保留强度
  S₀    = 初始强度（emotion_weight）
  λ     = 衰减速率
  β     = 衰减阶数
```

每次检索会触发**逆衰减**：decay_rate ×= 0.85，half_life ×= 1.15，importance += 0.01。越常被回忆的记忆越不容易遗忘。

### 4. 睡眠巩固 (Sleep Consolidation)

每 24 小时自动或手动触发四阶段巩固：

- **Phase 0: Decay Refresh** — 全局刷新衰减值
- **Phase 1: Endangered Scan** — 识别濒危记忆（R(t) < 0.15），触发审查标记
- **Phase 2: Conflict Detection** — 检测矛盾记忆，低置信度优先
- **Phase 3: Reflection Narrative** — 从情景记忆中提炼叙事（≥30 条情景记忆时触发）

### 5. 五维认知检索 (5D Cognitive Retrieval)

```
Score = SemanticSimilarity×0.25 + GoalRelevance×0.25
      + EmotionWeight×0.20 + TemporalProximity×0.15
      + CausalRelevance×0.15
```

状态依赖检索——当前的目标和情绪状态会影响你能"想起"什么，模拟人类记忆的情境效应。

---

## 接入方式

### 方式 A：MCP 工具调用

在 Agent 配置文件中添加 MCP 服务器：

**Hermes Agent** (`~/.hermes/config.yaml`):
```yaml
mcp_servers:
  brain-memory:
    command: python
    args:
      - /path/to/brain-memory/mcp_server.py
```

**Claude Desktop** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "brain-memory": {
      "command": "python",
      "args": ["/path/to/brain-memory/mcp_server.py"]
    }
  }
}
```

MCP 工具列表：`memory_record`, `memory_search`, `memory_list`, `memory_get`, `consolidation_run`, `health_overview`

### 方式 B：REST API

所有功能通过 REST API 暴露（端口 8765）：

| 端点 | 用途 |
|---|---|
| `POST /api/v1/ingest` | 摄入文本（自动去重+门控+入库） |
| `POST /api/v1/ingest/batch` | 批量摄入 |
| `POST /api/v1/context` | 上下文加载（检索+格式化） |
| `GET/POST /api/memories` | 记忆 CRUD |
| `POST /api/retrieval/search` | 五维检索 |
| `POST /api/consolidation` | 执行巩固 |
| `GET /api/health` | 健康总览 |

### 新 Agent 接入指南

详见项目根目录 `AGENTS.md`——任何智能体 clone 本项目后读此文件即可完成全部接入。

---

## 自动记忆管道

### 摄入（写入）

```bash
# 方式 1：便捷脚本（推荐）
python feed_append.py "重要发现：..." --source hermes        # 默认（去重+门控）
python feed_append.py "关键信息..." --source hermes --mark   # 标记重要（绕过门控）
python feed_append.py "..." --source hermes --force          # 强制摄入（跳过重复检测）
python feed_append.py "..." --source hermes --no-process     # 仅追加，等 cron 兜底

# 方式 2：直接 API
curl -X POST http://127.0.0.1:8765/api/v1/ingest \
  -H "Content-Type: application/json" \
  -d '{"text": "发现重要信息...", "source": "my-agent"}'
```

### 检索（读取）

```bash
# 上下文加载（推荐）
curl -X POST http://127.0.0.1:8765/api/v1/context \
  -H "Content-Type: application/json" \
  -d '{"query": "当前对话上下文...", "current_goal": "目标", "top_k": 5}'

# 返回：
# {
#   "context_block": "=== 相关记忆 ===\n...",
#   "should_inject": true,
#   "memories": [...]
# }
```

### Observer（轮询守护）

```bash
# 守护模式（持续运行）
python observer.py --daemon

# 单次运行（配合 cron）
python observer.py --once
```

---

## 快速开始

```bash
# 1. 安装依赖
pip install fastapi uvicorn mcp aiosqlite pydantic

# 2. 启动服务
python -m uvicorn main:app --host 127.0.0.1 --port 8765

# 3. 打开 Web 控制台
# http://127.0.0.1:8765

# 4. 测试摄入
python feed_append.py "这是一条测试记忆" --source test --mark

# 5. 测试检索
curl -X POST http://127.0.0.1:8765/api/v1/context \
  -H "Content-Type: application/json" \
  -d '{"query": "测试记忆"}'
```

---

## API 参考

### POST /api/v1/ingest

摄入文本，自动推断参数 → 去重检测 → 注意力门控 → 入库。

```json
// Request
{
  "text": "发现 Hermes MCP 自动加载失败的根因",
  "source": "hermes",
  "explicit_mark": false,
  "force": false,
  "metadata": {"current_goal": "修复 MCP"}
}

// Response (accepted)
{
  "accepted": true,
  "gate_reason": "emotional_signal",
  "memory_id": "episodic-2026-05-27-012",
  "inferred_type": "episodic",
  "emotion_label": "breakthrough",
  "importance": 0.75,
  "surprise_score": 0.80
}

// Response (duplicate)
{
  "accepted": false,
  "skip_reason": "duplicate",
  "is_duplicate": true,
  "duplicate_of": "episodic-2026-05-27-005"
}
```

### POST /api/v1/context

输入当前对话上下文，返回相关记忆 + 格式化上下文块。

```json
// Request
{
  "query": "用户正在讨论 MCP 自动加载问题",
  "current_goal": "确保 MCP 服务器正常连接",
  "top_k": 5
}

// Response
{
  "context_block": "=== 相关记忆 (Brain Memory) ===\n[episodic] 发现 MCP 自动加载失败根因 (相关度: 0.72)\n  venv 缺少 mcp 包...",
  "should_inject": true,
  "memories": [...],
  "high_relevance": [...],
  "total_available": 12,
  "query_summary": "检索返回 5 条记忆, 5 条高相关"
}
```

### 检索触发时机

| 时机 | 说明 |
|---|---|
| 会话开始 | 加载最近重要记忆作为前置上下文 |
| 话题转换 | 用户提到新话题，搜索相关历史 |
| 显式引用 | 用户说"上次""之前那个" |
| 决策前 | 做重要决定前查历史经验 |
| 不确定时 | 遇到未知问题，先搜记忆 |

---

## 项目结构

```
brain-memory/
├── main.py                # FastAPI 入口 (端口 8765)
├── mcp_server.py          # MCP stdio 服务端
├── observer.py            # Feed 轮询处理器 (守护+cron)
├── feed_append.py         # 便捷摄入脚本
├── config.py              # 配置常量
├── AGENTS.md              # 智能体通用接入指南
├── README.md              # 本文件
│
├── models/
│   ├── database.py        # SQLite 数据库
│   └── schemas.py         # Pydantic 模型
│
├── routers/
│   ├── memory_router.py   # 记忆 CRUD
│   ├── retrieval_router.py # 检索
│   ├── consolidation_router.py # 巩固
│   ├── health_router.py   # 健康检查
│   ├── dashboard_router.py # 仪表盘
│   ├── ingest_router.py   # ★ 摄入端点 v2.0
│   └── context_router.py  # ★ 上下文加载 v2.0
│
├── services/
│   ├── memory_service.py  # 记忆存储
│   ├── attention_gating.py # 5步门控
│   ├── emotion_weight.py  # 5维情绪加权
│   ├── decay.py           # 艾宾浩斯衰减
│   ├── retrieval.py       # 5维检索
│   ├── consolidation.py   # 睡眠巩固
│   ├── auto_ingest.py     # ★ 自动推断+去重 v2.0
│   └── context_load.py    # ★ 上下文加载 v2.0
│
├── static/
│   └── index.html         # Web 控制台 (v2.0 升级)
│
└── ingest_feed.jsonl       # 摄入 feed 文件 (运行时)
```

---

## 配置参数

```python
# 数据库
DB_PATH = "brain_memory.db"

# 情绪权重公式系数
EMOTION_WEIGHTS = {
    "importance": 0.30,
    "failure_cost": 0.25,
    "novelty": 0.20,
    "goal_relevance": 0.15,
    "surprise_score": 0.10,
}

# 认知检索公式系数
RETRIEVAL_WEIGHTS = {
    "semantic_similarity": 0.25,
    "goal_relevance": 0.25,
    "emotion_weight": 0.20,
    "temporal_proximity": 0.15,
    "causal_relevance": 0.15,
}

# 衰减参数
DECAY_RATE_DEFAULT = 0.05
HALF_LIFE_DEFAULT = 30

# 上下文加载阈值
CONTEXT_INJECT_THRESHOLD = 0.55  # 检索得分 > 此值建议注入
```

---

## Web 控制台

启动后访问 `http://127.0.0.1:8765`：

- **记忆浏览** — 表格 + 详情面板，按层级筛选
- **手动记录** — 填写参数手动创建记忆
- **自动摄入** — ★ 输入文本，自动推断 → 门控 → 入库
- **认知检索** — 五维搜索
- **上下文加载** — ★ 输入话题即时检索相关记忆
- **睡眠巩固** — 一键触发四阶段巩固
- **健康总览** — 统计面板
- **衰减曲线** — 90天记忆强度预测
- **时间线** — 最近记忆时间轴
- **管道状态** — ★ 摄入/上下文/Observer/MCP 实时状态
