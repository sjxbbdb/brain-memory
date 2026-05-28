"""Brain Memory MCP Server — 将脑记忆系统暴露为 MCP tools，供所有 Agent 统一接入。

启动方式:
  python mcp_server.py    # stdio 模式
  所有主流 Agent 均支持 stdio MCP 接入。
"""
import asyncio
import json
import os
import sys

from mcp.server import Server
from mcp.server.models import InitializationOptions, ServerCapabilities
from mcp.server.stdio import stdio_server
import mcp.types as types

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from services import memory_service as svc
from services import retrieval
from services import consolidation
from services.attention_gating import check_gate
from models.schemas import MemoryCreate, RetrievalQuery
from models.database import init_db

server = Server("brain-memory")
_db_ready = False


async def _ensure_db():
    global _db_ready
    if not _db_ready:
        await init_db()
        _db_ready = True


# ── 工具列表 ──
@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="memory_record",
            description="记录一条记忆，自动经过注意力门控筛查过滤低价值信息。记忆类型: episodic=情景事件, semantic=语义知识, procedural=程序流程, narrative=自我叙事, global=全局共享。",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "记忆标题"},
                    "content": {"type": "string", "description": "记忆内容", "default": ""},
                    "type": {"type": "string", "enum": ["episodic", "semantic", "procedural", "narrative", "global"], "default": "episodic"},
                    "importance": {"type": "number", "description": "重要性 0-1", "default": 0.5},
                    "failure_cost": {"type": "number", "description": "失败代价 0-1", "default": 0.5},
                    "novelty": {"type": "number", "description": "新颖度 0-1", "default": 0.5},
                    "goal_relevance": {"type": "number", "description": "目标相关度 0-1", "default": 0.5},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "标签"},
                    "entities": {"type": "array", "items": {"type": "string"}, "description": "实体"},
                    "explicit_mark": {"type": "boolean", "description": "标记为重要绕过门控", "default": False},
                },
                "required": ["title"],
            },
        ),
        types.Tool(
            name="memory_search",
            description="五维认知检索——综合语义、目标、情绪、时间、因果五维度智能搜索已有记忆。返回 log_id 可用 consolidation_run 的 feedback 端点标记有用/无用。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索查询"},
                    "current_goal": {"type": "string", "description": "当前目标（可选）"},
                    "current_risk": {"type": "string", "description": "当前风险（可选）"},
                    "top_k": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="memory_list",
            description="列出记忆，支持按层级筛选和排序。",
            inputSchema={
                "type": "object",
                "properties": {
                    "layer": {"type": "string", "enum": ["episodic", "semantic", "procedural", "narrative", "global"]},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="memory_get",
            description="获取单条记忆完整详情，含实时衰减强度和全部元数据。",
            inputSchema={
                "type": "object",
                "properties": {"memory_id": {"type": "string"}},
                "required": ["memory_id"],
            },
        ),
        types.Tool(
            name="consolidation_run",
            description="执行睡眠巩固——Phase 0权重自适应, Phase 1评分刷新, Phase 2分诊清理, Phase 2.5冲突仲裁, Phase 2.8话题聚合, Phase 3记忆修正。类比人脑睡眠时的记忆整理过程。",
            inputSchema={
                "type": "object",
                "properties": {
                    "phases": {"type": "array", "items": {"type": "integer"}, "description": "阶段: 0=权重自适应,1=评分刷新,2=分诊清理,25=冲突仲裁,28=话题聚合,3=记忆修正"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="health_overview",
            description="记忆系统健康总览——总数、各层分布、平均强度、濒危记忆、冲突等。",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="session_start",
            description="会话开始时调用，自动检索相关记忆并返回上下文摘要。Agent 接入后首个调用的工具，无需手动指定查询即可获得当前最相关的记忆上下文。请每轮对话结束后调用 session_append 写入对话。",
            inputSchema={
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "当前会话目标（可选，增强检索精度）"},
                    "top_k": {"type": "integer", "default": 5},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="session_append",
            description="每轮对话结束后调用，将对话内容写入记忆系统的 ingest feed。内容会自动经过门控和脑区管线处理。调用后无需等待，系统后台异步处理。",
            inputSchema={
                "type": "object",
                "properties": {
                    "speaker": {"type": "string", "description": "说话方: user / agent / system"},
                    "text": {"type": "string", "description": "对话内容"},
                    "goal": {"type": "string", "description": "当前会话目标（可选）"},
                },
                "required": ["speaker", "text"],
            },
        )
    ]


# ── 工具调用 ──
@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    await _ensure_db()
    try:
        handlers = {
            "memory_record": _record,
            "memory_search": _search,
            "memory_list": _list,
            "memory_get": _get,
            "consolidation_run": _consolidate,
            "health_overview": _health,
            "session_start": _session_start,
            "session_append": _session_append,
        }
        h = handlers.get(name)
        if h:
            return await h(arguments)
        return [types.TextContent(type="text", text=f"未知工具: {name}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"错误 {type(e).__name__}: {e}")]



def _consume_output_feed():
    """Read new entries from output_feed.jsonl since last session_start.
    Returns (alerts, narratives) with dedup via cursor file.
    """
    import json as _json
    from pathlib import Path

    feed = Path(__file__).parent / "output_feed.jsonl"
    cursor_file = Path(__file__).parent / ".output_cursor"

    if not feed.exists():
        return [], []

    # Read cursor
    cursor = 0
    if cursor_file.exists():
        try:
            cursor = int(cursor_file.read_text().strip())
        except (ValueError, OSError):
            cursor = 0

    # Read new lines
    try:
        with open(feed, "r", encoding="utf-8") as f:
            f.seek(cursor)
            new_content = f.read()
    except Exception:
        return [], []

    # Update cursor immediately
    new_size = feed.stat().st_size
    cursor_file.write_text(str(new_size))

    if not new_content.strip():
        return [], []

    alerts = []
    narratives = []
    for line in new_content.strip().split(chr(10)):
        line = line.strip()
        if not line:
            continue
        try:
            entry = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        etype = entry.get("type", "")
        edata = entry.get("data", {})
        if etype == "alert":
            alerts.append({
                "severity": edata.get("severity", "medium"),
                "title": edata.get("title", ""),
                "detail": edata.get("detail", ""),
                "timestamp": entry.get("timestamp", ""),
            })
        elif etype == "narrative":
            narratives.append({
                "narrative_id": edata.get("narrative_id", ""),
                "title": edata.get("title", ""),
                "summary": edata.get("summary", ""),
                "timestamp": entry.get("timestamp", ""),
            })

    return alerts, narratives


async def _session_start(a: dict) -> list[types.TextContent]:
    """会话开始：检索记忆 + 消费未读告警 + 返回最新叙事。"""
    from services.context_load import context_load

    goal = a.get("goal")
    query = goal or "当前任务 最近活动 系统状态"
    top_k = a.get("top_k", 5)

    ctx = await context_load(query=query, current_goal=goal, top_k=top_k)
    mems = ctx.get("high_relevance", [])

    # Consume unread output feed
    alerts, narratives = _consume_output_feed()

    lines = []

    # ── 告警置顶 ──
    if alerts:
        lines.append("=== 系统告警 ===")
        for a_item in alerts:
            sev_icon = {"high": "[HIGH]", "medium": "[MED]", "low": "[LOW]"}.get(a_item["severity"], "")
            lines.append(f"  {sev_icon} {a_item['title']}")
            if a_item.get("detail"):
                lines.append(f"       {a_item['detail'][:120]}")
        lines.append("")

    # ── 记忆检索 ──
    if mems:
        lines.append(f"[记忆检索] 基于 '{query[:40]}' 检索到 {len(mems)} 条相关记忆：")
        for i, m in enumerate(mems):
            lines.append(
                f"  #{i+1} [{m['type']}] {m['title']} "
                f"(相关度={m['score']:.2f}, 情绪权重={m['emotion_weight']:.2f})"
            )
    else:
        lines.append("[记忆检索] 暂无高相关记忆。")

    if ctx.get("context_block"):
        lines.append("")
        lines.append("--- 上下文摘要 ---")
        lines.append(ctx["context_block"])

    # ── 最近叙事 ──
    if narratives:
        lines.append("")
        lines.append("=== 最近叙事 ===")
        for n_item in narratives:
            lines.append(f"  [{n_item['narrative_id']}] {n_item['title']}")
            lines.append(f"  {n_item['summary'][:150]}")

    if not lines:
        lines.append("[会话启动] 系统健康，等待新任务。")

    return [types.TextContent(type="text", text=chr(10).join(lines))]

async def _session_append(a: dict) -> list[types.TextContent]:
    """会话追加：将对话内容写入 ingest feed。"""
    import json as _json
    from datetime import datetime, timezone
    from pathlib import Path

    feed_path = Path(__file__).parent / "ingest_feed.jsonl"
    speaker = a["speaker"]
    text = a["text"]
    goal = a.get("goal")

    emotional = [
        "bug", "error", "fail", "crash",
        "fixed", "solved",
        "conflict", "contradict",
    ]
    has_emotional = any(kw in text.lower() for kw in emotional)

    entry = {
        "text": f"[{speaker}]: {text}",
        "source": f"session:{speaker}",
        "explicit_mark": has_emotional,
        "metadata": {"goal": goal} if goal else {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        with open(feed_path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(entry, ensure_ascii=False) + chr(10))
        return [types.TextContent(
            type="text",
            text=f"[会话已记录] {speaker}: {text[:60]}..."
            if len(text) > 60
            else f"[会话已记录] {speaker}: {text}"
        )]
    except Exception as e:
        return [types.TextContent(type="text", text=f"[会话记录失败] {e}")]

async def _record(a: dict) -> list[types.TextContent]:
    data = MemoryCreate(
        title=a["title"], content=a.get("content", ""), type=a.get("type", "episodic"),
        importance=a.get("importance", 0.5), failure_cost=a.get("failure_cost", 0.5),
        novelty=a.get("novelty", 0.5), goal_relevance=a.get("goal_relevance", 0.5),
        tags=a.get("tags", []), entities=a.get("entities", []),
        explicit_mark=a.get("explicit_mark", False),
    )
    gate = check_gate(data)
    if not gate.passed:
        return [types.TextContent(type="text", text=f"[门控拦截] {gate.reason} — 未写入记忆")]
    if "importance" in gate.adjusted_fields:
        data.importance = gate.adjusted_fields["importance"]
    m = await svc.create_memory(data)
    return [types.TextContent(type="text", text=f"[已记录] {m.id}\n{m.title}\n情绪权重={m.emotion_weight:.4f} 强度={m.current_strength:.4f} 门控={gate.reason}")]


async def _search(a: dict) -> list[types.TextContent]:
    search_result = await retrieval.cognitive_search(RetrievalQuery(
        query=a["query"], current_goal=a.get("current_goal"),
        current_risk=a.get("current_risk"), top_k=a.get("top_k", 10),
    ))
    results = search_result["results"]
    if not results:
        return [types.TextContent(type="text", text="[检索] 无匹配记忆")]
    lines = [f"[检索] {a['query']} — {len(results)} 条"]
    for i, r in enumerate(results):
        b = r.score_breakdown
        lines.append(
            f"#{i+1} 得分={r.score:.3f} | {r.memory.id} | {r.memory.title[:60]}\n"
            f"    语义={b['semantic_similarity']:.2f} 目标={b['goal_relevance']:.2f} 情绪={b['emotion_weight']:.2f} 时间={b['temporal_proximity']:.2f} 因果={b['causal_relevance']:.2f}"
        )
    return [types.TextContent(type="text", text="\n".join(lines))]


async def _list(a: dict) -> list[types.TextContent]:
    mems = await svc.list_memories(layer=a.get("layer"), limit=a.get("limit", 20))
    if not mems:
        return [types.TextContent(type="text", text="[列表] 暂无记忆")]
    tn = {"episodic": "情景", "semantic": "语义", "procedural": "程序", "narrative": "叙事", "global": "全局"}
    lines = [f"[列表] {len(mems)} 条"]
    for m in mems:
        s = m.current_strength or m.emotion_weight
        lines.append(f"  {m.id} | {tn.get(m.type, m.type)} | {m.title[:50]} | 强度={s:.3f}")
    return [types.TextContent(type="text", text="\n".join(lines))]


async def _get(a: dict) -> list[types.TextContent]:
    m = await svc.get_memory(a["memory_id"])
    if not m:
        return [types.TextContent(type="text", text=f"记忆 {a['memory_id']} 不存在")]
    s = m.current_strength or m.emotion_weight
    return [types.TextContent(type="text", text=(
        f"ID: {m.id}\n标题: {m.title}\n类型: {m.type} | {m.layer}\n"
        f"创建: {m.created} | 归档: {'是' if m.archived else '否'}\n"
        f"内容:\n{m.content or '(无)'}\n"
        f"强度={s:.4f} 情绪权重={m.emotion_weight:.4f} 重要性={m.importance:.2f}\n"
        f"新颖度={m.novelty:.2f} 失败代价={m.failure_cost:.2f} 目标相关度={m.goal_relevance:.2f}\n"
        f"置信度={m.confidence:.2f} 衰减率={m.decay_rate} 访问={m.access_count}\n"
        f"标签: {', '.join(m.tags) if m.tags else '无'}\n"
        f"实体: {', '.join(m.entities) if m.entities else '无'}"
    ))]


async def _consolidate(a: dict) -> list[types.TextContent]:
    phases = a.get("phases", [0, 1, 2, 25, 3])
    r = await consolidation.run_consolidation(phases)
    pn = {"phase0": "权重自适应", "phase1": "评分刷新", "phase2": "分诊清理", "phase25": "冲突仲裁", "phase3": "记忆修正"}
    lines = [f"[巩固] {r.timestamp}", f"阶段: {' → '.join(pn.get(p, p) for p in r.phases_executed)}"]
    for phase, results in r.phase_results.items():
        lines.append(f"  {pn.get(phase, phase)}: {', '.join(f'{x.action}x{x.affected_count}' for x in results)}")
    lines.append(f"总变更: {r.memory_changes}")
    return [types.TextContent(type="text", text="\n".join(lines))]


async def _health(a: dict) -> list[types.TextContent]:
    from models.database import get_db
    from services.decay import compute_strength, is_endangered

    db = await get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) FROM memories WHERE archived = 0")
        total = (await cur.fetchone())[0]
        cur = await db.execute("SELECT layer, COUNT(*) FROM memories WHERE archived = 0 GROUP BY layer")
        layers = {row[0]: row[1] for row in await cur.fetchall()}
        cur = await db.execute("SELECT * FROM memories WHERE archived = 0")
        strengths, endangered, low_conf, conflicts = [], 0, 0, 0
        for row in await cur.fetchall():
            r = dict(row)
            s = compute_strength(r["emotion_weight"], r["decay_rate"], r["last_accessed"], r["created"])
            strengths.append(s)
            if is_endangered(s): endangered += 1
            if r["confidence"] < 0.5: low_conf += 1
            if json.loads(r["contradicted_by"] or "[]"): conflicts += 1
        avg = round(sum(strengths) / len(strengths), 4) if strengths else 0
        cur = await db.execute("SELECT COUNT(*) FROM memories WHERE archived = 1")
        archived = (await cur.fetchone())[0]
        return [types.TextContent(type="text", text=(
            f"[健康] 总计={total} 归档={archived} 各层={layers} 平均强度={avg} "
            f"濒危={endangered} 低置信={low_conf} 冲突={conflicts}"
        ))]
    finally:
        await db.close()


# ── 启动 ──
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
            InitializationOptions(server_name="brain-memory", server_version="1.0.0", capabilities=ServerCapabilities()),
        )


if __name__ == "__main__":
    asyncio.run(main())
