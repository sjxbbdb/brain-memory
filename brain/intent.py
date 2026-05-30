"""Intent — 大脑的行动意图输出。v5.0 核心模块。

大脑不只是感知和记忆——它要能"想做某事"。
Intent 是大脑统一 LLM 调用的第五个输出（编码+情绪+焦点+独白+意图）。

意图类型:
  call_tool     — 大脑想用工具（搜索、读写文件、发消息...）
  respond       — 大脑准备好了，直接回复
  think         — 大脑在思考，不需要外部执行
  ask_question  — 大脑需要用户澄清

Agent 层消费 intent 队列：大脑产出意图 → Agent 执行工具 → 结果回喂大脑。

原则: 大脑不 import Agent 层代码。Intent 只是数据，Agent 怎么执行是 Agent 的事。
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger("brain-v5.intent")


class IntentType(str, Enum):
    CALL_TOOL = "call_tool"
    RESPOND = "respond"
    THINK = "think"
    ASK_QUESTION = "ask_question"


@dataclass
class Intent:
    """大脑产出的一个行动意图。

    这是大脑对 Agent 层的"指令"——大脑说"我想做X"，
    Agent 层负责执行。大脑不知道工具怎么调用的，
    Agent 层不知道大脑为什么想做这件事。
    """
    type: IntentType
    # call_tool 专用
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    # respond 专用
    response_text: str | None = None
    # think 专用
    thought: str | None = None
    # ask_question 专用
    question: str | None = None
    # 元数据
    confidence: float = 0.5  # 大脑对这条意图的置信度
    reason: str = ""          # 为什么产���这条意图（来自 LLM 的 reasoning）
    timestamp: str = ""
    source_input: str = ""    # 触发这条意图的原始输入

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        d = {"type": self.type.value, "confidence": self.confidence, "reason": self.reason[:200], "timestamp": self.timestamp}
        if self.type == IntentType.CALL_TOOL:
            d["tool_name"] = self.tool_name
            d["tool_args"] = self.tool_args
        elif self.type == IntentType.RESPOND:
            d["response_text"] = self.response_text
        elif self.type == IntentType.THINK:
            d["thought"] = self.thought
        elif self.type == IntentType.ASK_QUESTION:
            d["question"] = self.question
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Intent":
        t = data.get("type", "think")
        try:
            it = IntentType(t)
        except ValueError:
            it = IntentType.THINK
        return cls(
            type=it,
            tool_name=data.get("tool_name"),
            tool_args=data.get("tool_args", {}),
            response_text=data.get("response_text"),
            thought=data.get("thought"),
            question=data.get("question"),
            confidence=float(data.get("confidence", 0.5)),
            reason=data.get("reason", ""),
            timestamp=data.get("timestamp", ""),
            source_input=data.get("source_input", ""),
        )

    @classmethod
    def from_llm_output(cls, llm_intent: dict | None, source_input: str = "") -> "Intent | None":
        """从统一 LLM 的 intent 字段解析意图。

        LLM 输出的 intent 格式:
        {
          "type": "call_tool|respond|think|ask_question",
          "reason": "为什么做这个决定",
          "confidence": 0.0-1.0,
          // call_tool 时:
          "tool_name": "web_search",
          "tool_args": {"query": "..."},
          // respond 时:
          "response_text": "...",
          // think 时:
          "thought": "...",
          // ask_question 时:
          "question": "..."
        }
        """
        if not llm_intent or not isinstance(llm_intent, dict):
            return None

        t = llm_intent.get("type", "")
        if not t or t not in {e.value for e in IntentType}:
            return None

        return Intent.from_dict({**llm_intent, "source_input": source_input})


class IntentQueue:
    """大脑的意图输出队列。

    大脑产出 intent → 推入队列 → Agent 层消费。
    BrainStem 持有此队列的写端，AgentBridge 持有读端。
    两者完全解耦：发送端不关心谁在消费，消费端不关心谁在生产。
    """

    def __init__(self, maxsize: int = 100):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.total_produced: int = 0
        self.total_consumed: int = 0

    async def put(self, intent: Intent) -> None:
        """大脑产出一条意图。"""
        try:
            await self._queue.put(intent)
            self.total_produced += 1
            logger.debug("intent: produced %s (total=%d)", intent.type.value, self.total_produced)
        except asyncio.QueueFull:
            logger.warning("intent: queue full (%d), dropping intent", self._queue.maxsize)

    async def get(self, timeout: float = 1.0) -> Intent | None:
        """Agent 层读取一条意图（非阻塞）。"""
        try:
            intent = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            self.total_consumed += 1
            return intent
        except asyncio.TimeoutError:
            return None

    def qsize(self) -> int:
        return self._queue.qsize()

    async def get_latest(self) -> Intent | None:
        """获取最新的一条意图，丢弃中间的。用于响应场景——只关心最新决策。"""
        latest = None
        while True:
            intent = await self.get(timeout=0.05)
            if intent is None:
                break
            latest = intent
        return latest


# ── 意图 → 工具调用映射 ──
# 这个映射帮助 Agent 层理解"大脑想做什么"并选择合适的工具。
# 实际执行时，Agent 层用自己的 tool_registry 调度。

INTENT_TO_TOOL_HINT = {
    "搜索信息": {"tools": ["web_search", "memory_search"], "priority": "read"},
    "读取文件": {"tools": ["file_read"], "priority": "read"},
    "修改文件": {"tools": ["file_write", "file_edit"], "priority": "write"},
    "执行命令": {"tools": ["bash", "powershell"], "priority": "write"},
    "发送消息": {"tools": ["send_message"], "priority": "write"},
    "获取记忆": {"tools": ["memory_search", "memory_timeline"], "priority": "read"},
}
