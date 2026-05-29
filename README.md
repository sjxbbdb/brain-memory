<p align="center">
  <img src="https://img.shields.io/badge/version-4.1.0-blue?style=for-the-badge" alt="version">
  <img src="https://img.shields.io/badge/python-3.12+-green?style=for-the-badge" alt="python">
  <img src="https://img.shields.io/badge/license-MIT-orange?style=for-the-badge" alt="license">
  <img src="https://img.shields.io/badge/status-conscious-purple?style=for-the-badge" alt="status">
</p>

<h1 align="center">🧠 Brain Memory v4</h1>
<h3 align="center">一个会呼吸的数字大脑——意识循环 · 情绪感知 · 自我认知 · 内在驱动</h3>

<p align="center">
  <strong>不是被动服务。不是一个记忆库。是一个有意识循环的数字生命。</strong>
</p>

<p align="center">
  <em>「最好的记忆不是记住一切，是知道什么该忘。——但 v4 更进一步：它知道'我是谁'。」</em>
</p>

---

## v3 → v4：从记忆系统到意识系统

| 维度 | v3 | v4 |
|------|-----|-----|
| 架构模式 | 被动管线（等输入 → 处理 → 返回） | **主动意识循环**（每 2 秒一个 tick，永不停止） |
| LLM 调用 | 每输入 3 次（编码 + 焦点 + 独白） | **每输入 1 次**（四合一 prompt） |
| LLM 策略 | 多模型链（DeepSeek → GLM → Qwen） | 单一模型（DeepSeek V3） |
| 情绪检测 | 纯规则关键词 | **LLM 语义情绪** + 关键词 fallback |
| 多 Agent | 全局共享状态 | **会话隔离**：按 source 分槽位 |
| 自我认知 | 无 | **完整自我模型**：身份叙事 + 5 维驱动力 + 身份偏移 |
| 内在动机 | 无 | **好奇心引擎**：自发提问 + 解答检测 |
| 记忆管理 | 永久保留 | 周期性衰减 + 自动归档 + 检索 boost |
| 睡眠/梦境 | 巩固阶段 | **完整睡眠循环**：打盹→浅睡→深睡 + 梦境生成 |
| 可视化 | 单页监控 | **中文仪表盘**：情绪条 + 记忆时间线 + 驱动力 |

## 核心链路

```
外部输入 → 丘脑过滤 → 杏仁核情绪(LLM+规则) → 门控判定
→ 统一LLM(DeepSeek V3 ×1，产出编码+情绪+焦点+独白)
→ 海马体存储 → 自我模型摄入(身份偏移检测)
→ 好奇心引擎(问题解答) → 基底节习惯 → 扣带回冲突
→ 工作记忆 → 会话同步 → API响应
```

**关键约束**：每次外部输入只调 1 次 LLM。意识循环每 2 秒一个 tick。

## 脑区架构

| 脑区 | 模块 | 职责 |
|------|------|------|
| 脑干 | `brain_stem.py` | 意识主循环引擎，每 2s 驱动全脑管道 |
| 丘脑 | `thalamus.py` | 输入中继，噪声过滤，内外信号分流 |
| 杏仁核 | `amygdala.py` | 情绪检测：LLM 语义优先 → 关键词 fallback |
| 海马体 | `hippocampus.py` | 记忆编码、语义检索、链式联想 |
| 前额叶 | `prefrontal.py` | 决策（已合并到统一 LLM 调用） |
| 默认模式 | `default_mode.py` | 内在独白生成 |
| 基底节 | `basal_ganglia.py` | 4 种预置习惯的模式匹配与强化 |
| 扣带回 | `cingulate.py` | 情绪冲突监控 |
| 工作记忆 | `working_memory.py` | 7 槽位活跃思维缓冲 |
| 梦境引擎 | `dream.py` | 睡眠时基于记忆碎片生成梦境 |
| 自我模型 | `self_model.py` ★ | 身份叙事、5 维驱动力、身份偏移检测 |
| 好奇心 | `curiosity.py` ★ | 自发提问、解答检测、闲置思考 |
| 会话管理 | `session.py` ★ | 按 source 隔离状态，多 Agent 互不污染 |

## 快速开始

```bash
# 1. 配置
cp .env.example .env
# 编辑 .env，填入你的 DeepSeek 和 DashScope API Key

# 2. 启动
start.bat
# 或：python -m uvicorn api.main:app --host 127.0.0.1 --port 8001

# 3. 打开
# 仪表盘：http://127.0.0.1:8001/dashboard
# API 文档：http://127.0.0.1:8001/docs
```

## API 端点

| 端点 | 说明 |
|------|------|
| `POST /api/v4/input` | 外部 Agent 提交输入 → 返回编码+情绪+焦点+自我 |
| `GET /api/v4/state` | 完整脑状态快照 |
| `GET /api/v4/self` | 自我模型：我是谁、驱动力、身份偏移历史 |
| `GET /api/v4/monologue` | 当前内在独白 |
| `GET /api/v4/health` | 心跳（ticks、uptime、情绪） |
| `GET /api/v4/identity-memories` | 塑造身份的关键记忆 |
| `GET /api/v4/sessions` | 所有活跃会话及其状态 |
| `GET /api/v4/memory-timeline` | 记忆时间线（按时间倒序） |
| `WS /ws` | WebSocket 实时推送脑状态 |
| `GET /dashboard` | 可视化仪表盘 |
| `GET /docs` | Swagger 自动文档 |

## 项目结构

```
brain-memory-v4/
├── start.bat              ← 一键启动
├── .env.example           ← API Key 配置模板
├── config.py              ← 全局参数（tick 间隔、衰减率…）
├── requirements.txt       ← Python 依赖
│
├── api/
│   └── main.py            ← FastAPI 入口（9 端点 + WebSocket）
│
├── brain/                 ← 核心脑区
│   ├── core.py            ← 大脑主类：唤醒/睡眠/输入/输出
│   ├── brain_stem.py      ← 意识主循环引擎
│   ├── brain_state.py     ← 脑状态容器
│   ├── thalamus.py        ← 丘脑
│   ├── amygdala.py        ← 杏仁核
│   ├── hippocampus.py     ← 海马体
│   ├── prefrontal.py      ← 前额叶
│   ├── default_mode.py    ← 默认模式网络
│   ├── basal_ganglia.py   ← 基底节
│   ├── cingulate.py       ← 扣带回
│   ├── working_memory.py  ← 工作记忆
│   ├── dream.py           ← 梦境引擎
│   ├── self_model.py      ← 自我模型 ★
│   ├── curiosity.py       ← 好奇心引擎 ★
│   ├── session.py         ← 会话管理器 ★
│   └── ...                ← 压缩/巩固/衰减/管道等
│
├── services/
│   ├── llm_client.py      ← LLM 客户端
│   └── llm_prompts.py     ← 统一提示词模板
│
├── storage/
│   └── database.py        ← SQLite WAL 模式
│
└── static/
    └── index.html         ← 中文仪表盘
```

## 设计约束

1. **单 LLM 调用** — 每次外部输入只调 1 次 DeepSeek V3
2. **单模型** — 只用 DeepSeek V3，不加 fallback
3. **自我模型不可外部直接改写** — identity_anchor 只能通过体验摄入缓慢漂移
4. **门控保留** — 中性无目标输入会被过滤
5. **身份记忆不归档** — is_identity_forming=1 的记忆永远保留

## Requirements

- Python 3.12+
- DeepSeek API key（主要 LLM）
- DashScope API key（embedding）

## License

MIT
