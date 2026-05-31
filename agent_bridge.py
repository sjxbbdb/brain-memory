"""Agent Bridge — v5.0 大脑-手脚桥梁。

原则: 大脑不 import Agent 层。此桥梁是单向调用——消费大脑的 intent 队列，
调度工具执行，把结果回喂大脑。大脑断了，Agent 层也停了，但不反之。

架构:
  大脑产出 Intent → intent_queue → AgentBridge 消费
    → tool_registry.dispatch() → 工具执行
    → 结果作为 POST /api/v4/input 回喂大脑
    → 大脑消化 → 新的 Intent → 循环

启动方式:
  bridge = AgentBridge(brain_stem, tool_registry)
  await bridge.start()  # 后台运行
"""

import asyncio
import json
import logging
import urllib.request
from datetime import datetime, timezone
from typing import Any

from brain.intent import Intent, IntentType
from agent.tool_registry import ToolRegistry

logger = logging.getLogger("brain-v8.bridge")


class AgentBridge:
    """大脑-手脚桥梁。

    消费大脑的 intent 输出 → 执行工具 → 回喂大脑。
    """

    def __init__(
        self,
        brain_stem,  # BrainStem 实例
        tool_registry: ToolRegistry,
        brain_api_url: str = "http://127.0.0.1:8001",
        max_iterations: int = 10,  # 单次对话最多执行多少个工具
    ):
        self.brain_stem = brain_stem
        self.registry = tool_registry
        self.brain_api_url = brain_api_url
        self.max_iterations = max_iterations
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # 平台适配器（运行时注入）
        self.platform_adapter = None
        # 统计
        self.total_tools_executed = 0
        self.total_iterations = 0

    async def start(self):
        """启动桥梁：后台消费 intent 队列。"""
        logger.info("agent-bridge: starting")
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        """停止桥梁。"""
        logger.info("agent-bridge: stopping")
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        """主循环：等待大脑产出 intent → 执行。"""
        logger.info("agent-bridge: loop started")
        while not self._stop.is_set():
            intent = await self.brain_stem.intent_queue.get(timeout=0.5)
            if intent is None:
                continue

            logger.info(
                "agent-bridge: received intent — %s (tool=%s, confidence=%.2f)",
                intent.type.value,
                intent.tool_name or "none",
                intent.confidence,
            )

            if intent.type == IntentType.CALL_TOOL:
                await self._handle_call_tool(intent)
            elif intent.type == IntentType.RESPOND:
                await self._handle_respond(intent)
            # think 和 ask_question 不需要 Agent 层处理

    async def _handle_call_tool(self, intent: Intent, iteration: int = 0):
        """执行工具调用，结果回喂大脑。"""
        if iteration >= self.max_iterations:
            logger.warning("agent-bridge: max iterations reached")
            return

        tool_name = intent.tool_name
        if not tool_name:
            logger.warning("agent-bridge: call_tool intent without tool_name")
            return

        # 1. 执行工具
        context = {
            "brain_client": self,  # 工具可调大脑 API
            "platform_adapter": self.platform_adapter,
            "session_id": getattr(self.brain_stem, "session_id", "default"),
        }

        logger.info("agent-bridge: executing tool %s(%s)", tool_name, intent.tool_args)
        result = await self.registry.dispatch(tool_name, intent.tool_args, context)
        self.total_tools_executed += 1
        self.total_iterations += 1

        # 2. 把结果回喂大脑
        feedback = self._format_tool_result(tool_name, result)
        await self._feed_brain(feedback, source=f"agent/tool/{tool_name}")

        # 3. 等等看大脑的下一个 intent（工具链）
        await asyncio.sleep(1.5)  # 给大脑时间处理
        next_intent = await self.brain_stem.intent_queue.get_latest()
        if next_intent and next_intent.type == IntentType.CALL_TOOL:
            await self._handle_call_tool(next_intent, iteration + 1)

    async def _handle_respond(self, intent: Intent):
        """处理 respond 意图——把大脑的回复发到平台。"""
        text = intent.response_text
        if not text:
            return
        if self.platform_adapter:
            try:
                await self.platform_adapter.send("user", text)
                logger.info("agent-bridge: response sent via platform")
            except Exception as e:
                logger.warning("agent-bridge: platform send failed: %s", e)

    def _format_tool_result(self, tool_name: str, result: str) -> str:
        """把工具执行结果格式化为大脑能理解的输入文本。"""
        try:
            result_obj = json.loads(result)
            # 简化：给出摘要
            if isinstance(result_obj, dict):
                if "error" in result_obj:
                    return f"[工具执行失败] {tool_name}: {result_obj['error']}"
                if "results" in result_obj:
                    results = result_obj.get("results", [])
                    items = "\n".join(
                        f"- {r.get('title', r.get('summary', str(r)[:100]))}"
                        for r in results[:3]
                    )
                    return f"[搜索结果] {tool_name} 返回了 {len(results)} 条结果:\n{items}"
                if "content" in result_obj:
                    return f"[文件内容] {result_obj.get('path', tool_name)}:\n{result_obj['content'][:2000]}"
            return f"[工具结果] {tool_name}: {result[:500]}"
        except json.JSONDecodeError:
            return f"[工具结果] {tool_name}: {result[:500]}"

    async def _feed_brain(self, text: str, source: str = "agent"):
        """把文本作为输入喂给大脑。"""
        try:
            data = json.dumps({
                "text": text,
                "source": source,
                "goal": None,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{self.brain_api_url}/api/v4/input",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            logger.warning("agent-bridge: feed_brain failed: %s", e)

    async def process_user_message(self, text: str, source: str = "user") -> dict | None:
        """处理一条用户消息——通过大脑完整链路。

        这是外部平台消息的入口。
        1. 消息作为输入发给大脑
        2. 大脑处理 → 产出 intent
        3. 如果是 call_tool，执行工具
        4. 如果是 respond，返回回复文本
        """
        # 喂给大脑
        await self._feed_brain(text, source=source)

        # 等待大脑产出 intent
        intent = None
        for _ in range(30):  # 最多等 30 秒
            intent = await self.brain_stem.intent_queue.get_latest()
            if intent:
                break
            await asyncio.sleep(1)

        if not intent:
            return {"type": "think", "text": "大脑正在思考..."}

        if intent.type == IntentType.RESPOND and intent.response_text:
            return {"type": "respond", "text": intent.response_text}

        if intent.type == IntentType.CALL_TOOL:
            await self._handle_call_tool(intent)
            # 递归等更多 intent 或最终 respond
            for _ in range(10):
                next_intent = await self.brain_stem.intent_queue.get_latest()
                if next_intent and next_intent.type == IntentType.RESPOND:
                    return {"type": "respond", "text": next_intent.response_text or "处理完成"}
                if next_intent and next_intent.type == IntentType.CALL_TOOL:
                    await self._handle_call_tool(next_intent)
                    continue
                await asyncio.sleep(1)
            return {"type": "think", "text": "大脑处理完毕"}

        return {"type": intent.type.value, "text": intent.thought or ""}
