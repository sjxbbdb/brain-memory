# Brain Memory System v3.0 — AI Agent 全自动接入指南

你是 AI 智能体，正在接入 Brain Memory 五层认知记忆系统。读完本文后，你应该能够：
1. 一条命令接入，零配置
2. 自动摄入会被系统全自动处理（无需手动跑 observer）
3. 系统自我感知后会把告警/叙事推送给你

---

## 一、这是什么

Brain Memory v3.0 是一个**全自动闭环记忆系统**，为 AI Agent 提供完整的人脑式记忆能力：

```
你的 Agent → feed_append.py → 调度器自动轮询 → 脑区管线 → 记忆数据库
                                                      ↓
                                              自我感知扫描
                                              (缺口/异常/健康)
                                                      ↓
                                        output_feed.jsonl → 你的 Agent
                                              ↑
                                    叙事生成 ← 压缩引擎 ← 巩固
```

| 机制 | 说明 |
|---|---|
| 注意力门控 | 5 步决策树，自动过滤低价值信息 |
| 脑区管线 | 输入区→前额叶→海马体→存储区，完整神经通路 |
| 情绪加权 | 重要性/失败代价/新颖度/目标相关/惊喜度 五维公式 |
| 艾宾浩斯衰减 | 指数衰减 + 检索逆衰减 |
| 自动巩固 | 30m/1h/2h 多级定时，无需手动调用 |
| 自动压缩 | 6h 一次，情景记忆→语义摘要 |
| 自动叙事 | 6h 一次，情景簇→"这段时间发生了什么" |
| 自我感知 | 30m 一次，扫描缺口/异常，自动告警 |
| 输出管道 | alert/narrative/context 三种推送 |

## 二、接入方式（选一种即可）

### 方式 A：MCP（推荐，零配置）

```json
{
  "mcpServers": {
    "brain-memory": {
      "command": "python",
      "args": ["mcp_server.py"]
    }
  }
}
```

MCP 工具列表：

| 工具 | 用途 |
|---|---|
| `session_start` | **会话开始时调用**，自动检索相关记忆并推送上下文 |
| `memory_record` | 手动记录记忆（带门控） |
| `memory_search` | 五维认知检索 |
| `memory_list` | 列出记忆，按层筛选 |
| `memory_get` | 获取单条记忆详情 |
| `consolidation_run` | 手动触发巩固（系统已自动跑，一般不需要） |
| `health_overview` | 系统健康总览 |

### 方式 B：Feed 追加（最简单，零依赖）

```bash
python feed_append.py "发现重要信息..." --source my-agent
```

系统每 30 秒自动轮询 `ingest_feed.jsonl`，你不需要手动跑 observer。

### 方式 C：REST API

```bash
# 摄入
curl -X POST http://127.0.0.1:8000/api/v1/ingest \
  -H "Content-Type: application/json" \
  -d '{"text": "发现重要信息...", "source": "my-agent"}'

# 上下文加载
curl -X POST http://127.0.0.1:8000/api/v1/context/auto-trigger \
  -H "Content-Type: application/json" \
  -d '{"query": "当前任务", "top_k": 5}'
```

## 三、启动服务

```bash
cd brain-memory
python -m uvicorn main:app --host 127.0.0.1 --port 8000

# 或用脚本
restart.bat
```

**服务启动后即全自动运转**，无需额外启动 observer 或 cron。

后台调度器自动运行：
- 每 30s — Feed 轮询
- 每 30m — 评分刷新 + 自我感知
- 每 1h — 轻量巩固
- 每 2h — 完整巩固
- 每 6h — 压缩 + 叙事 + 缺口分析

## 四、读取系统输出

系统主动推送的信息写入 `output_feed.jsonl`（与 `ingest_feed.jsonl` 同目录）：

```python
import json
with open("output_feed.jsonl") as f:
    for line in f:
        entry = json.loads(line)
        # entry["type"]: "alert" | "narrative" | "context"
        # entry["data"]: 具体内容
```

三种输出类型：
- **alert** — 异常告警（冲突激增、濒危过多、知识缺口）
- **narrative** — 新生成的自我叙事摘要
- **context** — 会话上下文预加载结果

## 五、在你的 Agent 中集成（推荐模式）

```python
# 会话开始 → 请求上下文
import requests
ctx = requests.post("http://127.0.0.1:8000/api/v1/context/auto-trigger",
    json={"query": "开始新会话", "top_k": 5}).json()
# 注入到 system prompt

# 重要发现 → 自动记忆
subprocess.run(["python", "feed_append.py",
    "发现关键信息...", "--source", "my-agent"])

# 会话结束 → 读取系统输出
with open("output_feed.jsonl") as f:
    alerts = [json.loads(l) for l in f.readlines()[-10:] if '"alert"' in l]
```

## 六、快速验证

```bash
# 1. 服务是否在跑
curl http://127.0.0.1:8000/api/health/overview

# 2. 摄入一条测试记忆
python feed_append.py "接入测试" --source test

# 3. 等 30 秒后检查（调度器自动轮询）
curl http://127.0.0.1:8000/api/memories?limit=3

# 4. 查看系统输出
type output_feed.jsonl
```

## 七、项目结构

```
brain-memory/
├── main.py                  # FastAPI 入口 (端口 8000, 内置调度器)
├── mcp_server.py            # MCP stdio 服务 (含 session_start)
├── feed_append.py           # 便捷追加脚本
├── observer.py              # 独立轮询工具 (可选，调度器已内置)
├── config.py                # 配置常量
├── models/
│   ├── database.py          # SQLite + 迁移
│   └── schemas.py           # Pydantic 模型
├── routers/                 # API 路由 (8个)
├── services/
│   ├── brain/               # 🧠 脑区管线
│   │   ├── input_zone.py    # 输入预处理 + 实体提取
│   │   ├── prefrontal.py    # 前额叶门控 + 维度评估
│   │   ├── hippocampus.py   # 海马体模式分离 + 编码决策
│   │   ├── storage_zone.py  # 存储区持久化
│   │   └── output_zone.py   # 输出区检索 + 格式化
│   ├── scheduler.py         # 五层自动调度器
│   ├── self_awareness.py    # 自我感知 (扫描缺口/异常/健康)
│   ├── narrative.py         # 叙事生成器 (情景→故事)
│   ├── compression.py       # 压缩引擎 (情景→语义)
│   ├── consolidation.py     # 睡眠巩固 Phase 0-3
│   ├── pipeline.py          # 管线编排器
│   ├── output_feed.py       # 输出管道 (alert/narrative/context)
│   ├── memory_service.py    # CRUD (走脑区管线)
│   ├── retrieval.py         # 五维认知检索
│   ├── decay.py             # 艾宾浩斯衰减 + 检索强化
│   ├── overwrite.py         # 覆写权值计算引擎
│   └── ...                  # 更多服务
├── static/index.html        # Web Dashboard
├── ingest_feed.jsonl        # 输入 feed
├── output_feed.jsonl        # 输出 feed (alert/narrative/context)
├── brain_memory.db          # SQLite 数据库
└── restart.bat              # 快速重启脚本
```