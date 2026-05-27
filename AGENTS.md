# Brain Memory System — AI Agent 接入指南

你是一个 AI 智能体，正在接入 Brain Memory 五层认知记忆系统。读完本文后，你应该能够：
1. 配置 MCP 自动连接
2. 设置自动记忆摄入管道
3. 检索和管理记忆

---

## 一、这是什么

Brain Memory 是一个受认知神经科学启发的记忆系统，为 AI Agent 提供：
- **注意力门控** — 5步决策树自动筛选值得记住的信息
- **情绪加权** — 5维公式（重要性/失败代价/新颖度/目标相关/惊喜度）
- **艾宾浩斯衰减** — 指数衰减 + 检索逆衰减
- **睡眠巩固** — 定期清理低权记忆、检测冲突、生成叙事
- **五维检索** — 语义 × 目标 × 情绪 × 时间 × 因果

## 二、接入方式（二选一或都用）

### 方式 A：MCP（直接工具调用）

在配置文件中添加 MCP 服务器：

**Hermes Agent** (`~/.hermes/config.yaml`):
```yaml
mcp_servers:
  brain-memory:
    command: C:/Users/24763/AppData/Local/Programs/Python/Python312/python.exe
    args:
      - E:/brain-memory/mcp_server.py
```

**Claude Code / Claude Desktop** (`claude_desktop_config.json` 或 `mcp.json`):
```json
{
  "mcpServers": {
    "brain-memory": {
      "command": "python",
      "args": ["E:/brain-memory/mcp_server.py"]
    }
  }
}
```

**通用 stdio MCP**:
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

### 方式 B：REST API（HTTP 摄入）

端点: `POST http://127.0.0.1:8765/api/v1/ingest`

```bash
curl -X POST http://127.0.0.1:8765/api/v1/ingest \
  -H "Content-Type: application/json" \
  -d '{"text": "发现重要信息...", "source": "my-agent", "explicit_mark": false}'
```

## 三、自动记忆摄入（推荐）

Brain Memory 提供了完整的三层过滤管道。最简单的接入方式：

```bash
# 追加一条记忆（自动去重 + 门控 + 入库）
python E:/brain-memory/feed_append.py "重要发现：..." --source my-agent

# 标记为重要（绕过门控）
python E:/brain-memory/feed_append.py "关键信息..." --source my-agent --mark

# 强制摄入（跳过重复检测）
python E:/brain-memory/feed_append.py "..." --source my-agent --force
```

**在你的启动文件中加入自动推送逻辑：**

对于 Hermes，在 `~/.bashrc` 的启动函数中：
```bash
my_agent() {
    # ... 其他启动逻辑 ...
    command my-agent "$@"
    # 会话结束后推送摘要（可选）
    python E:/brain-memory/feed_append.py "会话摘要..." --source my-agent
}
```

对于任何 Agent，在对话循环中加入：
```python
# 在每个重要 turn 后
import subprocess
subprocess.run([
    "python", "E:/brain-memory/feed_append.py",
    f"用户: {user_msg} | 发现: {key_finding}",
    "--source", "my-agent"
])
```

## 四、可用的 MCP 工具

连接 MCP 后，你可以使用以下工具：

| 工具 | 用途 |
|---|---|
| `memory_record` | 手动记录记忆（带门控） |
| `memory_search` | 五维认知检索 |
| `memory_list` | 列出记忆，按层筛选 |
| `memory_get` | 获取单条记忆详情 |
| `consolidation_run` | 执行睡眠巩固 |
| `health_overview` | 系统健康总览 |


## 四、自动记忆检索（上下文加载）

对称于摄入管道，Brain Memory 提供自动上下文加载。Agent 在以下时机应调用检索：

### 触发时机

| 时机 | 说明 | 示例 |
|---|---|---|
| **会话开始** | 加载最近的重要记忆作为前置上下文 | `query="会话开始，加载相关记忆"` |
| **话题转换** | 用户提到新话题，搜索相关历史 | `query="用户正在讨论 MCP 配置"` |
| **显式引用** | 用户说"上次""之前那个" | `query=user_message` |
| **决策前** | 做重要决定前查历史经验 | `query="如何配置 mcp 自启动"` |
| **不确定时** | 遇到未知问题，先搜记忆 | `query="hermes mcp 不自动连接"` |

### 调用方式

```bash
# REST API
curl -X POST http://127.0.0.1:8765/api/v1/context   -H "Content-Type: application/json"   -d '{"query": "当前对话上下文...", "current_goal": "当前目标", "top_k": 5}'
```

返回格式化的 `context_block`，可直接注入 system prompt：
```
=== 相关记忆 (Brain Memory) ===
[episodic] 发现 MCP 自动加载失败根因 (相关度: 0.62, 情绪权重: 0.72)
  venv 缺少 mcp 包，修复：pip install mcp v1.27.1
```

### 在你的对话循环中加入

```python
# 会话开始时
context = requests.post("http://127.0.0.1:8765/api/v1/context",
    json={"query": "开始新会话，加载相关上下文", "top_k": 5}).json()
if context["should_inject"]:
    system_prompt += context["context_block"]

# 每次用户消息后
context = requests.post("http://127.0.0.1:8765/api/v1/context",
    json={"query": user_message, "top_k": 3}).json()
if context["should_inject"]:
    messages.insert(0, {"role": "system", "content": context["context_block"]})
```

## 五、REST API 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/v1/ingest` | 摄入文本（自动去重+门控） |
| POST | `/api/v1/ingest/batch` | 批量摄入 |
| POST | `/api/v1/context` | 上下文加载（检索+格式化）|
| GET | `/api/memories` | 列出记忆 |
| GET | `/api/memories/{id}` | 获取详情 |
| POST | `/api/memories` | 创建记忆（绕过自动推断） |
| POST | `/api/retrieval` | 五维检索 |
| POST | `/api/consolidation` | 执行巩固 |

## 六、项目结构

```
brain-memory/
├── main.py              # FastAPI 入口 (端口 8765)
├── mcp_server.py        # MCP stdio 服务端
├── observer.py          # Feed 轮询处理器
├── feed_append.py       # 便捷追加脚本
├── config.py            # 配置常量
├── models/              # 数据模型
│   ├── database.py      # SQLite
│   └── schemas.py       # Pydantic
├── routers/             # API 路由
│   ├── ingest_router.py # 摄入端点
│   ├── memory_router.py # CRUD
│   └── retrieval_router.py
├── services/            # 业务逻辑
│   ├── auto_ingest.py   # 自动推断（类型/情绪/重要性）
│   ├── attention_gating.py  # 5步门控
│   ├── emotion_weight.py    # 5维情绪加权
│   ├── decay.py             # 艾宾浩斯衰减
│   └── consolidation.py     # 睡眠巩固
└── ingest_feed.jsonl    # 摄入 feed 文件
```

## 七、启动服务

```bash
# 启动记忆系统
cd E:/brain-memory
python -m uvicorn main:app --host 127.0.0.1 --port 8765

# 启动 observer（守护模式，持续轮询 feed）
python observer.py --daemon

# Windows 快速启动
start.bat
```

## 八、快速检查清单

接入 Brain Memory 后，逐项确认：

- [ ] MCP 配置已添加到启动文件
- [ ] `mcp` Python 包已安装（`pip install mcp`）
- [ ] 记忆系统服务在端口 8765 运行
- [ ] Observer cron 或守护进程在运行
- [ ] 测试摄入：`python feed_append.py "测试" --source my-agent`
- [ ] 验证：`curl http://127.0.0.1:8765/api/memories?limit=5`
