"""Agent Tools — v5.0 内置工具集。

每个工具文件在模块顶层调用 registry.register(ToolDef(...))。
通过 agent.tool_registry.discover_tools() 自动发现。

工具列表:
  - web_search: 网络搜索
  - memory_search: 搜索记忆库
  - send_message: 发送消息到平台
  - file_read: 读取文件
"""

import json
import logging
from agent.tool_registry import registry, ToolDef

logger = logging.getLogger("brain-v5.tool.web-search")


# ── web_search ──

async def _web_search(args: dict, context: dict) -> str:
    """执行网络搜索。"""
    query = args.get("query", "")
    limit = min(int(args.get("limit", 5)), 10)
    if not query:
        return json.dumps({"error": "query required"}, ensure_ascii=False)
    # 实际搜索通过 Hermes 的 web_tools 或直接 HTTP 调用
    try:
        import urllib.request
        import urllib.parse
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        # 简化实现 — 实际集成时使用 Hermes web_tools 或 Brave API
        return json.dumps({
            "query": query,
            "results": [{"title": f"Search: {query}", "url": url}],
            "note": "搜索功能已注册，需配置搜索引擎 API Key 实现完整功能",
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _web_search_prompt(context: dict) -> str:
    return "搜索互联网获取最新信息。适用场景：查找资料、验证事实、获取实时数据。"


registry.register(ToolDef(
    name="web_search",
    description="搜索互联网获取最新信息。输入 query（搜索关键词）和可选的 limit（结果数量）。",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "limit": {"type": "integer", "description": "结果数量，默认5", "default": 5},
        },
        "required": ["query"],
    },
    call=_web_search,
    is_read_only=True,
    concurrency_safe=True,
    emoji="🔍",
    prompt_fn=_web_search_prompt,
))


# ── memory_search ──

async def _memory_search(args: dict, context: dict) -> str:
    """搜索大脑记忆库。"""
    query = args.get("query", "")
    limit = min(int(args.get("limit", 10)), 30)
    if not query:
        return json.dumps({"error": "query required"}, ensure_ascii=False)
    # 通过大脑 API 搜索（agent_bridge 会注入 brain_client）
    brain = context.get("brain_client")
    if brain:
        try:
            import urllib.request
            url = f"http://127.0.0.1:8001/api/v4/memory/search?q={urllib.parse.quote(query)}&limit={limit}"
            r = urllib.request.urlopen(url, timeout=5)
            data = json.loads(r.read())
            return json.dumps({
                "count": data.get("count", 0),
                "memories": data.get("memories", []),
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"memory API error: {e}"}, ensure_ascii=False)
    return json.dumps({"count": 0, "memories": []}, ensure_ascii=False)


async def _memory_search_prompt(context: dict) -> str:
    return "搜索大脑的记忆库。适用场景：回忆过去的经历、查找已知信息、确认历史决策。"


registry.register(ToolDef(
    name="memory_search",
    description="搜索大脑记忆库。输入 query（搜索关键词）和可选的 limit。",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "limit": {"type": "integer", "description": "结果数量，默认10", "default": 10},
        },
        "required": ["query"],
    },
    call=_memory_search,
    is_read_only=True,
    concurrency_safe=True,
    emoji="🧠",
    prompt_fn=_memory_search_prompt,
))


# ── send_message ──

async def _send_message(args: dict, context: dict) -> str:
    """发送消息到平台。"""
    target = args.get("target", "user")
    text = args.get("text", "")
    if not text:
        return json.dumps({"error": "text required"}, ensure_ascii=False)
    # 通过 agent_bridge 的 platform_adapter 发送
    adapter = context.get("platform_adapter")
    if adapter:
        try:
            await adapter.send(target, text)
            return json.dumps({"success": True, "target": target}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
    return json.dumps({"success": False, "error": "no platform adapter"}, ensure_ascii=False)


async def _send_message_prompt(context: dict) -> str:
    return "发送消息到用户或群组。适用场景：回复用户、通知进展、提问澄清。"


registry.register(ToolDef(
    name="send_message",
    description="发送消息到聊天平台。输入 target（目标ID）和 text（消息内容）。",
    schema={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "目标，默认 user", "default": "user"},
            "text": {"type": "string", "description": "消息内容"},
        },
        "required": ["text"],
    },
    call=_send_message,
    is_read_only=False,
    concurrency_safe=False,
    emoji="💬",
    prompt_fn=_send_message_prompt,
))


# ── file_read ──

async def _file_read(args: dict, context: dict) -> str:
    """读取文件内容。"""
    path = args.get("path", "")
    if not path:
        return json.dumps({"error": "path required"}, ensure_ascii=False)
    try:
        import os
        if not os.path.exists(path):
            return json.dumps({"error": f"file not found: {path}"}, ensure_ascii=False)
        # 安全检查
        if os.path.getsize(path) > 1024 * 1024:  # 1MB limit
            return json.dumps({"error": "file too large (>1MB)"}, ensure_ascii=False)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()[:50000]
        return json.dumps({
            "path": path,
            "size": len(content),
            "content": content[:10000],  # truncate for LLM
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _file_read_prompt(context: dict) -> str:
    return "读取本地文件内容。适用场景：查看代码、读配置、审阅文档。"


registry.register(ToolDef(
    name="file_read",
    description="读取本地文件内容。输入 path（文件路径）。",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
        },
        "required": ["path"],
    },
    call=_file_read,
    is_read_only=True,
    concurrency_safe=True,
    emoji="📄",
    prompt_fn=_file_read_prompt,
))
