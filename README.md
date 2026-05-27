# 🧠 Brain Memory System

> 一个受认知神经科学启发的**五层记忆系统**——为 AI Agent 提供人类的记忆机理：注意力门控、情绪加权、艾宾浩斯衰减、睡眠巩固、五维认知检索。

**A five-layer cognitive memory system for AI agents** — implementing human-like memory mechanisms: attention gating, emotion weighting, Ebbinghaus decay, sleep consolidation, and five-dimensional cognitive retrieval.

---

## 目录

- [核心理念](#核心理念)
- [五层记忆架构](#五层记忆架构)
- [核心机制](#核心机制)
- [接入方式](#接入方式)
- [快速开始](#快速开始)
- [API 参考](#api-参考)
- [项目结构](#项目结构)
- [配置参数](#配置参数)

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
Step 2: goal_relevance > 0.5? → YES → 通过
Step 3: 高情绪信号？→ YES → emotion_weight=0.85，通过
Step 4: novelty > 0.7？→ YES → 通过
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
  λ     = 衰减速率（默认 0.05，每次检索后 × 0.85）
  β     = 半衰期因子（默认 1/30，每次检索后 × 1.15）
  t     = 距创建/上次检索的天数
```

每次检索自动强化：衰减变慢、半衰延长、重要性微增——模拟"提取练习效应"。

### 4. 睡眠巩固 (Sleep Consolidation)

四阶段引擎：

```
Phase 0: 衰减更新     — 对所有记忆应用当前衰减
Phase 1: 濒危扫描     — 识别 R(t) < 0.15 的记忆
Phase 2: 冲突检测     — 检测互为矛盾的记忆对
Phase 3: 反思叙事     — ≥30 条情景记忆时生成叙事整合
```

结果：低价值记忆自动清理，冲突标记待审查，情景记忆凝练为叙事。

### 5. 五维认知检索 (Cognitive Retrieval)

```python
score = semantic_similarity × 0.25
      + goal_relevance      × 0.25
      + emotion_weight      × 0.20
      + temporal_proximity  × 0.15
      + causal_relevance    × 0.15
```

区别于纯语义搜索——当前目标和情绪状态会**改变**检索结果，就像人类的"状态依赖记忆"。

---

## 接入方式

### 方式一：MCP Server（推荐）

任何支持 MCP (Model Context Protocol) 的 Agent 可直接接入：

```json
{
  "mcpServers": {
    "brain-memory": {
      "command": "python",
      "args": ["path/to/brain-memory/mcp_server.py"]
    }
  }
}
```

提供 6 个 MCP Tools：

| Tool | 说明 |
|------|------|
| `memory_record` | 记录记忆（自动门控） |
| `memory_search` | 五维认知检索 |
| `memory_list` | 列出/筛选记忆 |
| `memory_get` | 获取单条记忆详情 |
| `consolidation_run` | 手动触发睡眠巩固 |
| `health_overview` | 健康概览（濒危数、冲突数等） |

### 方式二：REST API

```
POST /api/memories              — 创建记忆
GET  /api/memories              — 列出记忆
GET  /api/memories/{id}         — 获取详情
PUT  /api/memories/{id}         — 更新记忆
DELETE /api/memories/{id}       — 删除记忆
POST /api/retrieval/search      — 认知检索
POST /api/consolidation/run     — 运行巩固
GET  /api/health/overview       — 健康概览
GET  /api/dashboard/stats       — 统计数据
```

### 方式三：Web Dashboard

启动后访问 `http://127.0.0.1:8766`，提供完整的可视化操作界面：

- 记忆记录（支持 5 种类型）
- 认知检索
- 巩固报告
- 健康面板（濒危记忆 / 衰减曲线 / 时间线）

---

## 快速开始

### 环境要求

- Python 3.10+
- 依赖：`fastapi`, `uvicorn`, `mcp`, `aiosqlite`

### 安装

```bash
git clone https://github.com/sjxbbdb/brain-memory.git
cd brain-memory
pip install -r requirements.txt
```

### 启动 Web 服务

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8766
```

打开 http://127.0.0.1:8766 查看 Dashboard。

### 接入 MCP

在 Hermes / Claude Desktop / 其他 MCP 客户端的配置中添加：

```yaml
mcp_servers:
  brain-memory:
    command: python
    args:
      - /path/to/brain-memory/mcp_server.py
```

---

## API 参考

### 创建记忆

```http
POST /api/memories
Content-Type: application/json

{
  "title": "STM32时钟树结构",
  "content": "HSE 8MHz → PLL ×9 = 72MHz SYSCLK → AHB / APB1 / APB2",
  "type": "semantic",
  "importance": 0.7,
  "failure_cost": 0.5,
  "novelty": 0.6,
  "goal_relevance": 0.8,
  "tags": ["stm32", "clock"],
  "entities": ["STM32F103"],
  "explicit_mark": false
}
```

响应（通过门控时）：

```json
{
  "id": "semantic-2026-05-27-001",
  "type": "semantic",
  "emotion_weight": 0.655,
  "gated": true,
  "gate_reason": "goal_relevance"
}
```

### 认知检索

```http
POST /api/retrieval/search
Content-Type: application/json

{
  "query": "时钟配置",
  "current_goal": "调试STM32定时器中断",
  "current_risk": "时钟频率不对",
  "top_k": 5
}
```

---

## 项目结构

```
brain-memory/
├── main.py                     # FastAPI 入口 + CORS + 路由注册
├── mcp_server.py               # MCP stdio server（6 个 tools）
├── config.py                   # 所有可调参数集中管理
│
├── models/
│   ├── database.py             # SQLite 建表 / 初始化
│   └── schemas.py              # Pydantic 请求/响应模型
│
├── services/
│   ├── memory_service.py       # 记忆 CRUD 核心
│   ├── retrieval.py            # 5 维认知检索引擎
│   ├── consolidation.py        # 睡眠巩固 Phase 0→3
│   ├── attention_gating.py     # 5 步决策树门控
│   ├── emotion_weight.py       # 5 维情绪权重计算
│   └── decay.py                # 艾宾浩斯衰减 + 检索强化
│
├── routers/
│   ├── memory_router.py        # /api/memories
│   ├── retrieval_router.py     # /api/retrieval
│   ├── consolidation_router.py # /api/consolidation
│   ├── health_router.py        # /api/health
│   └── dashboard_router.py     # /api/dashboard
│
└── static/
    └── index.html              # Web Dashboard（纯 HTML/JS，无框架）
```

---

## 配置参数

所有可调参数集中在 `config.py`。关键参数：

| 参数 | 默认值 | 说明 |
|-----|--------|------|
| `DECAY_RATE_DEFAULT` | 0.05 | 衰减速率 λ |
| `HALF_LIFE_DEFAULT` | 30 | 半衰期（天） |
| `EVICTION_THRESHOLD` | 0.10 | 低于此 emotion_weight 触发清理 |
| `STRENGTH_ENDANGERED` | 0.15 | R(t) 低于此值为濒危 |
| `EMOTION_WEIGHTS` | 30/25/20/15/10 | 情绪公式五维系数 |
| `RETRIEVAL_WEIGHTS` | 25/25/20/15/15 | 检索公式五维系数 |

---

## 设计哲学

> "记忆不是记录，是重构。" — 认知神经科学共识

Brain Memory 不试图记住一切。它模拟人脑的选择性——**重要的事留下，重复的事强化，无意义的事遗忘**。这不是 bug，是 feature。

---

## License

MIT © 2026 [sjxbbdb](https://github.com/sjxbbdb)
