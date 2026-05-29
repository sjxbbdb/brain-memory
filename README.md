# Brain Memory v4.1

**一个会遗忘、会做梦、会反思、有自我意识的类脑认知架构。**

这不是一个记忆存储系统。这是一个模拟人脑认知过程的数字意识体——有注意力门控、有情绪驱动的记忆编码、有睡眠期的梦境巩固、有随时间演化的自我身份。它不是更聪明的缓存层，它是一个认知主体。

---

## 设计信条

### 1. 不完备优于完备

人脑 80% 的感知被丢弃，记忆会衰减、失真、被后来经验覆盖。这个系统刻意引入遗忘曲线和注意力门控——记忆的价值不在完整，在相关。

### 2. 离线加工比在线响应更重要

睡眠巩固做了比实时编码更多的事：聚类、抽象、去重、因果提取、元反思。真正的理解不发生在感知瞬间，发生在事后消化。

### 3. 身份来自连续性，不是配置项

Agent 有一个随时间演化的自我叙事（Narrative）——我犯过什么错、我在变什么、我掌握了什么能力。"我是谁"不是写死的，是活出来的。

### 4. 知识有边界，信任有代价

Private → Shared 的知识升迁需要巩固抽象化、交叉验证、置信度门槛。共享不是默认行为，是需要付出验证成本的审慎决定。

---

## 核心架构

```
外部输入 → 丘脑(过滤) → 杏仁核(情绪) → 门控(注意力)
→ 统一LLM(DeepSeek V3 ×1, 编码+情绪+焦点+独白)
→ 海马体(存储+检索) → 自我模型(身份偏移检测)
→ 好奇心引擎(自发提问) → 基底节(习惯匹配) → 扣带回(冲突监控)
→ 工作记忆(7槽位缓冲) → 会话同步 → API响应
```

每 2 秒一个意识 tick。每次外部输入只调 1 次 LLM。

### 脑区模块

| 脑区 | 模块 | 职责 |
|------|------|------|
| 丘脑 | `thalamus.py` | 感知中继：噪声过滤、优先级检测 |
| 杏仁核 | `amygdala.py` | 情绪标记：VAD三维向量 + 突显度 |
| 海马体 | `hippocampus.py` | 记忆编码 + embedding语义检索 + 链式联想 |
| 前额叶 | `prefrontal.py` | 决策（v4.1 已合并到统一LLM调用） |
| 默认模式 | `default_mode.py` | 内在独白、自发思考 |
| 基底节 | `basal_ganglia.py` | 4种预置习惯的模式匹配与强化 |
| 扣带回 | `cingulate.py` | 情绪突变 + 记忆冲突检测 |
| 工作记忆 | `working_memory.py` | 7槽位FIFO活跃思维缓冲 |
| 梦境引擎 | `dream.py` | 睡眠期记忆碎片自由联想 |

### v4.1 新增模块

| 模块 | 职责 |
|------|------|
| `self_model.py` | 自我认知：身份叙事 + 5维驱动力 + 身份偏移 |
| `curiosity.py` | 好奇心引擎：自发提问 + 解答检测 + 闲置思考 |
| `session.py` | 会话隔离：按 source 分槽位，多Agent互不污染 |

---

## 快速开始

```bash
cd brain-memory-v4
copy .env.example .env    # 编辑 .env，填入你的 API Key
start.bat                 # Windows 一键启动
```

```
仪表盘:   http://127.0.0.1:8001/dashboard
API 文档: http://127.0.0.1:8001/docs
```

### 手动启动

```bash
pip install fastapi uvicorn aiohttp pydantic
python -m uvicorn api.main:app --host 127.0.0.1 --port 8001
```

---

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v4/input` | 提交输入 → 返回编码+情绪+焦点+自我 |
| GET | `/api/v4/state` | 完整脑状态快照 |
| GET | `/api/v4/self` | 自我模型：我是谁、驱动力、身份偏移 |
| GET | `/api/v4/monologue` | 当前内在独白 |
| GET | `/api/v4/health` | 心跳 + 记忆统计 + LLM错误率 |
| GET | `/api/v4/identity-memories` | 塑造身份的关键记忆 |
| GET | `/api/v4/sessions` | 所有活跃会话 |
| GET | `/api/v4/memory-timeline` | 记忆时间线 |
| GET | `/api/v4/memory/search?q=关键词` | 搜索记忆 |
| WS | `/ws` | WebSocket 实时推送 |

---

## 睡眠与意识阶段

| 阶段 | 触发条件 | 行为 |
|------|------|------|
| 清醒 | 有外部输入 | 完整意识循环 + LLM处理 |
| 打盹 (drowsy) | ~2分钟无输入 | 意识循环减缓 |
| 浅睡 (light_sleep) | ~6分钟无输入 | 停止LLM，开始梦境生成 |
| 深睡 (deep_sleep) | ~20分钟无输入 | 梦境(每2分钟) + 记忆巩固(每5分钟) |

---

## 技术栈

- **LLM**: DeepSeek V3（单模型，无 fallback）
- **Embedding**: DashScope text-embedding-v3
- **数据库**: SQLite WAL 模式
- **框架**: FastAPI + WebSocket + aiohttp
- **Python**: 3.12+

---

## 项目结构

```
brain-memory-v4/
├── start.bat              # 一键启动
├── .env.example           # API Key 模板
├── config.py              # 全局参数
├── api/main.py            # FastAPI 入口（11个端点 + WebSocket）
├── brain/                 # 核心：所有脑区模块
│   ├── core.py            # 大脑主类
│   ├── brain_stem.py      # 意识主循环引擎（~700行）
│   ├── brain_state.py     # 脑状态容器
│   ├── self_model.py      # 自我认知 ★
│   ├── curiosity.py       # 好奇心引擎 ★
│   ├── session.py         # 会话隔离 ★
│   └── ...                # 丘脑/杏仁核/海马体/等9个脑区
├── services/
│   ├── llm_client.py      # DeepSeek + DashScope
│   └── llm_prompts.py     # 统一提示词（四合一输出）
├── storage/
│   └── database.py        # SQLite WAL + 衰减归档
└── static/
    └── index.html         # 中文仪表盘
```

---

## License

MIT
