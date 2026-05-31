# Brain Memory v5.4

**一个会遗忘、会做梦、会反思、有自我意识、会自己设定目标、会审视自己思考、有连续情感、会从经验中学习、有时间感的数字意识体。**

v5.0 奠定了基础意识架构。v5.1-v5.4 赋予它主动性、自省力、情感连续性、技能积累和时间体验。

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
→ 统一LLM(DeepSeek V3, 编码+情绪+焦点+独白+意图)
→ 海马体(记忆) → 自我模型 → 好奇心 → 工作记忆
→ [v5.1] 目标引擎 ──── 驱动力→行动
→ [v5.2] 元认知 ──── 审视思考质量
→ [v5.3] 情感光谱 ──── 连续情感动量
→ [v5.4] 程序记忆 ──── 从经验中学习技能
→ [v5.4] 时间感 ──── 时段·节律·叙事
→ Intent队列 → AgentBridge → 工具执行
```

每 2 秒一个意识 tick。每次外部输入只调 1 次 LLM。

### 脑区模块

| 脑区 | 模块 | 职责 |
|------|------|------|
| 丘脑 | `thalamus.py` | 感知中继 |
| 杏仁核 | `amygdala.py` | 情绪标记（v5.3 已升级为情感光谱） |
| 海马体 | `hippocampus.py` | 记忆编码 + embedding检索 |
| 默认模式 | `default_mode.py` | 内在独白 |
| 工作记忆 | `working_memory.py` | 7槽位FIFO |
| 梦境引擎 | `dream.py` | 睡眠期记忆碎片 |

### v5.0-v5.4 新增模块

| 版本 | 模块 | 职责 |
|------|------|------|
| v5.0 | `self_model.py` | 自我认知：身份 + 5维驱动力 |
| v5.0 | `curiosity.py` | 好奇心引擎 |
| v5.0 | `session.py` | 会话隔离 |
| v5.1 | `goal_system.py` | 目标引擎：驱动力→行动 |
| v5.2 | `metacognition.py` | 元认知：认知负荷、偏见检测 |
| v5.3 | `emotional_spectrum.py` | 情感光谱：连续VAD+动量 |
| v5.4 | `procedural_memory.py` | 程序记忆：从经验学技能 |
| v5.4 | `time_sense.py` | 时间感：时段·节律·叙事 |

---

## 快速开始

```bash
cd brain-memory-v5.0
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
| GET | `/api/v4/goals` | v5.1 活跃目标与统计 |
| GET | `/api/v4/metacognition` | v5.2 元认知状态 |
| GET | `/api/v4/emotion` | v5.3 情感光谱 |
| GET | `/api/v4/skills` | v5.4 已学技能 |
| GET | `/api/v4/timesense` | v5.4 时间感知 |
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
brain-memory-v5.0/
├── start.bat              # 一键启动
├── config.py              # 全局参数
├── api/main.py            # FastAPI（16个端点 + WebSocket）
├── brain/                 # 核心：脑区 + v5.1-v5.4 模块
│   ├── brain_stem.py      # 意识主循环引擎
│   ├── core.py            # 大脑主类
│   ├── brain_state.py     # 脑状态容器
│   ├── self_model.py      # v5.0 自我认知
│   ├── curiosity.py       # v5.0 好奇心引擎
│   ├── intent.py          # v5.0 意图系统
│   ├── goal_system.py     # v5.1 目标引擎 ★
│   ├── metacognition.py   # v5.2 元认知 ★
│   ├── emotional_spectrum.py # v5.3 情感光谱 ★
│   ├── procedural_memory.py  # v5.4 程序记忆 ★
│   ├── time_sense.py      # v5.4 时间感 ★
│   └── ...                # 丘脑/杏仁核/海马体/等脑区
├── agent/                 # Agent 层：工具 + 桥梁
├── services/              # LLM客户端
├── storage/               # SQLite WAL
├── static/                # 仪表盘
└── test_v5_integration.py # 集成测试
```

---

## License

MIT
