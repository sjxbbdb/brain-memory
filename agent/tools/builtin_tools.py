"""Agent Tools — v5.0 内置工具集。

每个工具文件在模块顶层调用 registry.register(ToolDef(...))。
通过 agent.tool_registry.discover_tools() 自动发现。

工具列表:
  - web_search: 网络搜索
  - memory_search: 搜索记忆库
  - send_message: 发送消息到平台
  - file_read: 读取文件
"""

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path
from agent.tool_registry import registry, ToolDef
from services.source_adapter import get_source_adapter

logger = logging.getLogger("brain-v5.tool.web-search")


# ── web_search ──

async def _web_search(args: dict, context: dict) -> str:
    """执行网络搜索。"""
    query = str(args.get("query", "") or "").strip()[:300]
    try:
        limit = max(1, min(int(args.get("limit", 5)), 10))
    except (TypeError, ValueError, OverflowError):
        limit = 5
    if not query:
        return json.dumps({"error": "query required"}, ensure_ascii=False)
    try:
        result = await get_source_adapter().search(query, limit=limit)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.warning("source adapter failed: %s", str(e)[:160])
        return json.dumps({
            "error": "source adapter unavailable",
            "result_quality": "failed",
            "note": "未获得可核验来源，未生成占位事实",
        }, ensure_ascii=False)


async def _web_search_prompt(context: dict) -> str:
    return "从白名单 RSS/Atom 或官方 JSON 来源检索信息；结果会附带来源 URL、类型和时间，未核验内容不得当作事实。"


registry.register(ToolDef(
    name="web_search",
    description="从已配置的可核验信息源检索有限结果。输入 query（搜索关键词）和可选的 limit（结果数量）。",
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
        # In the normal in-process topology, query the shared store directly.
        # A self-HTTP round trip can deadlock an event loop and makes an
        # otherwise healthy autonomous episode depend on a second server.
        stem = getattr(brain, "brain_stem", None)
        memory_store = getattr(stem, "memory_store", None)
        if memory_store is not None:
            try:
                memories = memory_store.search(query, limit=limit)
                boundary = getattr(stem, "boundary", None)
                if boundary is not None:
                    memories = [
                        memory for memory in memories
                        if isinstance(memory, dict)
                        and boundary.should_share_memory(
                            str(memory.get("id", "")), "agent"
                        )
                    ]
                return json.dumps({
                    "count": len(memories),
                    "memories": memories,
                }, ensure_ascii=False)
            except Exception as e:
                logger.debug("memory search direct path failed: %s", str(e)[:120])
        try:
            base_url = getattr(brain, "brain_api_url", None) or context.get(
                "brain_api_url"
            ) or "http://127.0.0.1:8001"
            base_url = str(base_url).rstrip("/")
            url = f"{base_url}/api/v4/memory/search?q={urllib.parse.quote(query)}&limit={limit}"
            def _request_memory():
                with urllib.request.urlopen(url, timeout=5) as response:
                    return json.loads(response.read())

            data = await asyncio.to_thread(_request_memory)
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
    """Read a non-secret file from the configured workspace boundary."""
    path = args.get("path", "")
    if not path:
        return json.dumps({"error": "path required"}, ensure_ascii=False)
    try:
        workspace_root = Path(
            context.get("workspace_root") or Path(__file__).resolve().parents[2]
        ).resolve()
        requested = Path(str(path))
        target = (workspace_root / requested).resolve() if not requested.is_absolute() else requested.resolve()

        try:
            relative = target.relative_to(workspace_root)
        except ValueError:
            return json.dumps({
                "error": "path outside workspace boundary",
                "workspace_root": str(workspace_root),
            }, ensure_ascii=False)

        lowered_parts = {part.lower() for part in relative.parts}
        lowered_name = target.name.lower()
        sensitive_names = {
            ".env", ".env.local", ".env.production", ".env.development",
            "id_rsa", "id_ed25519", "credentials.json",
        }
        if (
            ".git" in lowered_parts
            or lowered_name in sensitive_names
            or "apikey" in lowered_name
            or "api_key" in lowered_name
            or target.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
        ):
            return json.dumps({"error": "sensitive file access denied"}, ensure_ascii=False)

        if not target.exists() or not target.is_file():
            return json.dumps({"error": f"file not found: {relative}"}, ensure_ascii=False)
        if target.stat().st_size > 1024 * 1024:  # 1MB limit
            return json.dumps({"error": "file too large (>1MB)"}, ensure_ascii=False)

        def _read_text():
            with target.open("r", encoding="utf-8", errors="replace") as file:
                return file.read(50000)

        content = await asyncio.to_thread(_read_text)
        return json.dumps({
            "path": str(relative),
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
