<p align="center">
  <img src="https://img.shields.io/badge/version-3.0.0-blue" alt="version">
  <img src="https://img.shields.io/badge/python-3.10+-green" alt="python">
  <img src="https://img.shields.io/badge/license-MIT-orange" alt="license">
  <img src="https://img.shields.io/badge/forgets%20things-on%20purpose-red" alt="forgets">
</p>

<h1 align="center">🧠 Brain Memory</h1>

<p align="center">
  <strong>你的 AI Agent 记性太差了。修一下。</strong>
</p>

<p align="center">
  <em>不是又一个大模型 wrapper。不是又一个 vector DB。<br>
  是一个真的会忘、会做梦、会反思的记忆系统。</em>
</p>

---

> **场景：** 你跟 AI 说"上次那个 bug 怎么修来着？" — 它说"什么 bug？"
>
> **场景：** 你的 Agent 第三次踩同一个坑。你开始怀疑它是不是装的。
>
> **场景：** AI 说"我不记得我们讨论过这个"——明明 10 分钟前才说过。

**问题不是模型不够大。问题是它没有记忆。**

Brain Memory 把这件事修好。真正的人脑式记忆——有注意力（不什么都记）、有遗忘曲线（不用的慢慢消失）、有睡眠巩固（定期整理修正）、甚至会自己产生"这段时间发生了什么"的叙事。

**全自动。零手动。启动即闭环。**

---

## 一句话

```python
# 你的 Agent 记一条记忆
python feed_append.py "端口从8765改到8000，因为8765被占了" --source my-agent

# 30 秒后，它自动被：
#   ✅ 去重检查（说过类似的话吗？）
#   ✅ 注意力门控（值得记吗？）
#   ✅ 情绪加权（重要吗？）
#   ✅ 脑区管线处理（怎么存？覆盖还是合并？）
#   ✅ 持久化（写入了）

# 下次 Agent 启动，MCP session_start 自动检索相关记忆。
# 你的 Agent 终于看起来不像金鱼了。
```

---

## v3.0：它现在会自己动了

v2 还需要你手动跑 observer、手动触发巩固——像个需要你伺候的半成品。

v3 启动后：

```
    你的 Agent                    Brain Memory v3.0
    ─────────                    ─────────────────
    写一条记忆  ──────────────►  30s 后自动消费
    什么也不做  ──────────────►  每 30m 自我体检（"我漏了什么？有什么冲突？"）
    什么也不做  ──────────────►  每 1h 轻量巩固（整理笔记）
    什么也不做  ──────────────►  每 2h 完整巩固（深度整理 + 修正矛盾）
    什么也不做  ──────────────►  每 6h 压缩 + 叙事（"过去6小时发生了什么故事？"）
    
    读取输出  ◄──────────────  output_feed.jsonl（告警/叙事/上下文）
```

**你只负责"记"和"查"。中间的全自动。**

---

## 它到底在模拟什么

| 人脑干的 | Brain Memory 怎么干的 |
|---|---|
| 🎯 **注意力** — 不是什么都进脑子 | 5 步门控决策树：显式标记 → 情绪信号 → 新颖度 → 目标相关 → 丢弃 |
| 😤 **情绪加成** — 踩坑记得比成功牢 | 5 维加权：重要性×失败代价×新颖度×目标相关×惊喜度 |
| 📉 **遗忘曲线** — 用不到的慢慢忘 | 艾宾浩斯指数衰减，检索一次衰减-15% |
| 😴 **睡眠巩固** — 睡觉时整理记忆 | Phase 0→3 自动定时：权重自适应→评分→分诊→冲突仲裁→聚合→修正 |
| 🗜️ **抽象提炼** — 从碎片经历总结规律 | 压缩引擎：情景簇 → 实体聚类 → 语义摘要 |
| 📖 **自我叙事** — "这段时间发生了什么" | 叙事生成器：情景簇 → 模式识别 → 因果推断 → 连贯故事 |
| 🔍 **元认知** — 知道自己不知道什么 | 自我感知：检索日志扫描 → 知识缺口告警 |
| 🧠 **海马体** — 新信息和旧记忆是覆盖还是合并？ | 5 种编码决策：new / overwrite / merge / append / conflict |

> *说是"模拟"其实有点夸张——更像是"受到了启发然后自己瞎搞了一套"。但它确实能用。*

---

## 五层记忆

```
  你说话 ──► [注意力门控：值得记吗？]
                        │
          ┌─────┬──────┼──────┬─────┐
          ▼     ▼      ▼      ▼     ▼
       情景   语义    程序   叙事   全局
      "那次   "端口    "启动  "今天  "系统
      端口改  要配     流程"  修了3  配置"
      8000"  MCP"           个bug"
          
                   压缩引擎 ──► 情景 → 语义摘要
                   叙事生成 ──► 碎片 → 连贯故事
```

| 层 | 是什么 | 实际例子 |
|---|---|---|
| **episodic** | 带时间地点的事件 | "5月28日下午把端口从8765改到8000" |
| **semantic** | 从事件提炼的知识 | "Brain Memory 默认端口是8000" |
| **procedural** | 可复用的操作步骤 | "部署流程：改config→重启→验证" |
| **narrative** | 自动编的故事 | "今天围绕 venv 发生了3件事..." |
| **global** | 跨领域通用规则 | 系统配置、约定 |

---

## 接入：三种方式，挑顺手的

### MCP（推荐，功能最全）

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

7 个工具：`session_start` `memory_record` `memory_search` `memory_list` `memory_get` `consolidation_run` `health_overview`

### Feed 追加（最简单）

```bash
python feed_append.py "发现了一个坑" --source my-agent          # 正常
python feed_append.py "这个很重要" --source my-agent --mark     # 绕过门控
```

丢进去就不用管了。调度器 30 秒后自动消费。

### REST API（最灵活）

```bash
# 摄入
curl -X POST http://127.0.0.1:8000/api/v1/ingest \
  -H "Content-Type: application/json" \
  -d '{"text": "...", "source": "my-agent"}'

# 检索
curl -X POST http://127.0.0.1:8000/api/v1/context/auto-trigger \
  -H "Content-Type: application/json" \
  -d '{"query": "端口怎么改的来着？", "top_k": 5}'
```

---

## 10 秒跑起来

```bash
pip install fastapi uvicorn mcp aiosqlite pydantic

cd brain-memory
python -m uvicorn main:app --host 127.0.0.1 --port 8000
# 打开 http://127.0.0.1:8000 有 Dashboard

# 试试
python feed_append.py "第一次见到 Brain Memory" --source test --mark
curl http://127.0.0.1:8000/api/memories?limit=3
```

Windows 直接双击 `restart.bat`。

---

## 它跟别的东西有什么区别

| | 向量数据库 | LangChain Memory | Brain Memory |
|---|---|---|---|
| 会忘吗 | ❌ 永远记得 | ❌ 永远记得 | ✅ 不用的慢慢衰减 |
| 会过滤噪音吗 | ❌ 全存 | ❌ 全存 | ✅ 5步注意力门控 |
| 会自动整理吗 | ❌ | ❌ | ✅ 定时巩固+压缩 |
| 会讲故事吗 | ❌ | ❌ | ✅ 6h自动叙事 |
| 知道自己漏了什么吗 | ❌ | ❌ | ✅ 自我感知 |
| 有输出管道吗 | ❌ | ❌ | ✅ alert/narrative/context |

> *向量数据库是仓库。Brain Memory 是大脑。仓库不会自己整理，大脑会。*

---

## 设计哲学

> *"人不是在记住过去，而是一直在重新解释过去。"*

- **不全记。** 注意力门控过滤噪音。不值得记的，直接丢。
- **会遗忘。** 艾宾浩斯衰减让不重要的事自然消失。记忆不是越多越好。
- **会做梦。** 睡眠巩固自动整理、清理、修正。碎片变知识。
- **会反思。** 自我感知知道自己的盲区。"我好像不太懂这个。"
- **会讲故事。** 叙事生成器把碎片经历编织成连贯的自我叙事。

**不是又一个数据库。是一个会忘的、会做梦的、会反思的——记忆系统。**

---

## 项目结构

```
brain-memory/
├── main.py              # FastAPI 入口 (内置全自动调度器)
├── mcp_server.py        # MCP stdio 服务 (7 tools)
├── feed_append.py       # 一行命令追加记忆
├── config.py            # 所有参数集中配置
├── AGENTS.md            # AI Agent 通用接入指南
│
├── services/brain/      # ★ 脑区管线
│   ├── input_zone.py    # 输入预处理
│   ├── prefrontal.py    # 注意力门控
│   ├── hippocampus.py   # 模式分离 + 编码决策
│   ├── storage_zone.py  # 持久化
│   └── output_zone.py   # 检索 + 格式化
│
├── services/
│   ├── scheduler.py     # 全自动调度器 (30s→6h)
│   ├── consolidation.py # 睡眠巩固 Phase 0→3
│   ├── compression.py   # 压缩引擎 (情景→语义)
│   ├── narrative.py     # 叙事生成器
│   ├── self_awareness.py# 自我感知
│   ├── retrieval.py     # 五维认知检索
│   ├── decay.py         # 艾宾浩斯衰减
│   ├── overwrite.py     # 覆写权值引擎
│   └── ...              # 更多
│
├── routers/             # 8 个 API 路由
├── static/index.html    # Web Dashboard
└── scripts/             # 工具脚本
```

---

## 配置

`config.py` —— 所有旋钮都在这里：

```python
# 情绪加权系数
EMOTION_WEIGHTS = {"importance": 0.30, "failure_cost": 0.25, ...}
# 衰减参数
DECAY_RATE_DEFAULT = 0.05    # 每次衰减5%
HALF_LIFE_DEFAULT = 30       # 半衰期30天
STRENGTH_ENDANGERED = 0.15   # 低于此值标记濒危
# 压缩触发
COMPRESSION_CLUSTER_MIN = 5  # 同一实体5条以上触发压缩
# 覆写阈值
SIMILARITY_OVERWRITE_THRESHOLD = 0.45  # 实体重叠>45%检测覆写
```

---

## License

MIT © [sjxbbdb](https://github.com/sjxbbdb)

---

<p align="center">
  <sub>made with ☕ and questionable neuroscience</sub>
</p>
