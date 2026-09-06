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
from pathlib import Path

from brain.intent import Intent, IntentType
from agent.tool_registry import ToolRegistry

logger = logging.getLogger("brain-v5.bridge")


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
        workspace_root: str | Path | None = None,
        allow_write_tools: bool = False,
    ):
        self.brain_stem = brain_stem
        self.registry = tool_registry
        self.brain_api_url = brain_api_url
        self.max_iterations = max_iterations
        self.workspace_root = Path(
            workspace_root or Path(__file__).resolve().parent
        ).resolve()
        # Read-only tools are safe to run autonomously.  External side
        # effects (messages, file writes, future tools) require an explicit
        # operator opt-in so a malformed intent cannot damage the workspace or
        # contact a third party.
        self.allow_write_tools = bool(allow_write_tools)
        self._lifecycle_lock = asyncio.Lock()
        self._bound_loop = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_feed_error: str = ""
        self.feed_error_count: int = 0
        # 平台适配器（运行时注入）
        self.platform_adapter = None
        # 统计
        self.total_tools_executed = 0
        self.total_iterations = 0

    def _ensure_loop_primitives(self):
        """Bind bridge primitives and the shared intent queue to one loop.

        BrainStem and AgentBridge communicate through asyncio queues, so they
        must share an event loop.  Sequential ``asyncio.run`` restarts are
        supported after the bridge is idle; moving a live bridge is rejected
        explicitly instead of leaving a consumer waiting on an orphaned
        queue.
        """
        current = asyncio.get_running_loop()
        stem_task = getattr(self.brain_stem, "_task", None)
        stem_bound_loop = getattr(self.brain_stem, "_bound_loop", None)
        stem_lifecycle_lock = getattr(self.brain_stem, "_lifecycle_lock", None)
        stem_transition_active = bool(
            stem_lifecycle_lock
            and (
                stem_lifecycle_lock.locked()
                or any(
                    not waiter.done()
                    for waiter in (getattr(stem_lifecycle_lock, "_waiters", ()) or ())
                )
            )
        )
        if (
            stem_bound_loop is not None
            and stem_bound_loop is not current
            and stem_transition_active
        ):
            # BrainStem clears ``_task`` early during stop.  Its lifecycle
            # lock is the authoritative marker in that interval; without
            # this check a bridge on a second loop could rebind the shared
            # IntentQueue underneath the still-running shutdown.
            raise RuntimeError(
                "AgentBridge cannot change loops while BrainStem lifecycle "
                "transition is still running"
            )
        if stem_task is not None and not stem_task.done():
            stem_loop = stem_bound_loop
            try:
                task_loop = stem_task.get_loop()
            except Exception:
                task_loop = None
            if stem_loop is None:
                stem_loop = task_loop
            if stem_loop is not None and stem_loop is not current:
                raise RuntimeError(
                    "AgentBridge and BrainStem must run on the same event loop"
                )
        if self._bound_loop is current:
            queue = getattr(self.brain_stem, "intent_queue", None)
            if queue is not None and getattr(queue, "_bound_loop", None) is not current:
                queue.rebind_loop()
            return
        if self._bound_loop is not None and self._task and not self._task.done():
            raise RuntimeError(
                "agent-bridge cannot change event loops while running; "
                "stop it on its owning loop first"
            )
        lifecycle_lock = self._lifecycle_lock
        if self._bound_loop is not None and (
            lifecycle_lock.locked()
            or any(
                not waiter.done()
                for waiter in (getattr(lifecycle_lock, "_waiters", ()) or ())
            )
        ):
            raise RuntimeError(
                "agent-bridge lifecycle transition is still running on another event loop"
            )

        queue = getattr(self.brain_stem, "intent_queue", None)
        if queue is not None:
            # This also rejects a queue with a live waiter on another loop.
            queue.rebind_loop()
        if self._bound_loop is not None:
            self._lifecycle_lock = asyncio.Lock()
            self._stop = asyncio.Event()
        self._bound_loop = current

    async def start(self):
        """Start the bridge, serializing lifecycle transitions."""
        self._ensure_loop_primitives()
        async with self._lifecycle_lock:
            await self._start_unlocked()

    async def _start_unlocked(self):
        """启动桥梁：后台消费 intent 队列。"""
        if self._task and not self._task.done():
            return
        logger.info("agent-bridge: starting")
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_loop_done)

    async def stop(self):
        """Stop the bridge, serializing lifecycle transitions."""
        self._ensure_loop_primitives()
        async with self._lifecycle_lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self):
        """停止桥梁。"""
        logger.info("agent-bridge: stopping")
        self._stop.set()
        if self._task:
            task = self._task
            self._task = None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.exception("agent-bridge: shutdown observed loop failure: %s", exc)

    def _on_loop_done(self, task: asyncio.Task):
        """Make an unexpected bridge exit visible without crashing callers."""
        # Do not let a delayed callback from an old task poison a restarted
        # bridge that already owns a fresh consumer loop.
        if self._task is not None and self._task is not task:
            return
        if task.cancelled() or self._stop.is_set():
            return
        try:
            error = task.exception()
        except Exception as exc:
            error = exc
        if error is None:
            error = "consumer loop exited without a stop request"
        logger.error("agent-bridge: loop exited unexpectedly: %s", error)

    def _fail_correlated_episode(
        self,
        episode_id: str | None,
        reason: str = "工具反馈未送达，大脑当前不可用",
    ) -> None:
        """Close one exact autonomous action after a bridge-boundary failure.

        A stopped HTTP/process boundary must not leave an episode waiting
        forever.  This is deliberately best-effort and only touches the
        exact correlated episode; unrelated active work is left untouched.
        """
        if not episode_id:
            return
        manager = getattr(self.brain_stem, "autonomy", None)
        if manager is None or getattr(manager, "active_id", None) != episode_id:
            return
        try:
            tick = getattr(getattr(self.brain_stem, "state", None), "total_ticks", 0)
            reason = str(reason or "自主行动未能安全收束")[:300]
            record = manager.fail(reason, tick)
            finisher = getattr(self.brain_stem, "_finish_autonomy_episode", None)
            if record is not None and callable(finisher):
                finisher(record, False, reason)
        except Exception:
            logger.debug("agent-bridge: could not close lost-feedback episode", exc_info=True)

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

            try:
                if intent.type == IntentType.CALL_TOOL:
                    await self._handle_call_tool(intent)
                elif intent.type == IntentType.RESPOND:
                    await self._handle_respond(intent)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A single faulty adapter/tool must not kill the only bridge
                # between intention and action.
                logger.exception("agent-bridge: intent handling failed: %s", exc)
            # think 和 ask_question 不需要 Agent 层处理

    async def _handle_call_tool(self, intent: Intent, iteration: int = 0):
        """执行工具调用，结果回喂大脑。"""
        episode_id = getattr(intent, "episode_id", None)
        intent_id = getattr(intent, "intent_id", None)
        goal_id = getattr(intent, "goal_id", None)
        if iteration >= self.max_iterations:
            logger.warning("agent-bridge: max iterations reached")
            self._fail_correlated_episode(
                episode_id,
                "工具链达到最大步数，已拒绝继续执行",
            )
            return

        tool_name = intent.tool_name
        if not tool_name:
            logger.warning("agent-bridge: call_tool intent without tool_name")
            self._fail_correlated_episode(
                episode_id,
                "自主行动缺少工具名，已拒绝执行",
            )
            return

        async def feed_feedback(text: str, source: str):
            """Feed a result while keeping legacy bridge test doubles valid.

            Older embedders commonly replace ``_feed_brain`` with a small
            ``(text, source)`` coroutine.  Correlation metadata is only
            needed when an autonomous intent actually carries it, so avoid
            passing new keyword arguments on the legacy path.
            """
            # ``intent_id`` is auto-generated for every Intent, including
            # legacy/external ones.  Only an episode/goal correlation signals
            # that the callee understands the extended keyword contract.
            if episode_id or goal_id:
                fed = await self._feed_brain(
                    text,
                    source=source,
                    episode_id=episode_id,
                    intent_id=intent_id,
                    goal_id=goal_id,
                )
            else:
                fed = await self._feed_brain(text, source=source)
            if fed is False:
                self._fail_correlated_episode(episode_id)
            return fed

        if episode_id and hasattr(self.brain_stem, "autonomy"):
            try:
                claimed = self.brain_stem.autonomy.action_started(
                    episode_id, getattr(self.brain_stem.state, "total_ticks", 0)
                )
                sync_projection = getattr(
                    self.brain_stem, "_sync_autonomy_projection", None
                )
                if callable(sync_projection):
                    sync_projection()
                if not claimed:
                    # The intent may have been queued before an external
                    # input aborted the episode, or may be a duplicate.  Do
                    # not execute a stale action across that boundary.
                    logger.warning(
                        "agent-bridge: refusing stale autonomous intent %s",
                        str(intent.intent_id)[:80],
                    )
                    return
            except Exception as exc:
                # Correlation is a safety boundary, not optional telemetry.
                # Never execute an autonomous action if its single-delivery
                # claim cannot be recorded first.
                logger.warning(
                    "agent-bridge: refusing unclaimed autonomous intent: %s",
                    str(exc)[:120],
                )
                self._fail_correlated_episode(
                    episode_id,
                    "无法确认自主行动归属，已拒绝执行",
                )
                return

        tool = self.registry.get(tool_name)
        if tool is None:
            logger.warning("agent-bridge: unknown tool requested: %s", tool_name)
            await feed_feedback(
                f"[工具执行失败] {tool_name}: 未注册的工具",
                source=f"agent/tool/{tool_name}",
            )
            return
        if not tool.is_enabled():
            logger.warning("agent-bridge: tool unavailable: %s", tool_name)
            await feed_feedback(
                f"[工具执行失败] {tool_name}: 工具当前不可用",
                source=f"agent/tool/{tool_name}",
            )
            return
        if not tool.is_read_only and not self.allow_write_tools:
            logger.warning("agent-bridge: blocked write tool by policy: %s", tool_name)
            await feed_feedback(
                f"[工具执行失败] {tool_name}: 写操作需要操作者显式授权",
                source=f"agent/tool/{tool_name}",
            )
            return

        # 1. 执行工具
        context = {
            "brain_client": self,  # 工具可调大脑 API
            "platform_adapter": self.platform_adapter,
            "session_id": getattr(self.brain_stem, "session_id", "default"),
            "workspace_root": str(self.workspace_root),
        }

        logger.info("agent-bridge: executing tool %s(%s)", tool_name, intent.tool_args)
        result = await self.registry.dispatch(tool_name, intent.tool_args, context)
        self.total_tools_executed += 1
        self.total_iterations += 1

        # 2. 把结果回喂大脑
        feedback = self._format_tool_result(tool_name, result)
        fed = await feed_feedback(
            feedback,
            source=f"agent/tool/{tool_name}",
        )
        if fed is False:
            # Do not wait for a follow-up intent when the feedback never
            # reached the brain.  Without this guard a stopped/unreachable
            # stem caused the bridge to sleep and poll for up to 15 seconds.
            logger.warning("agent-bridge: skipping tool-chain continuation; feedback was not delivered")
            return

        # Autonomous intents are correlated by episode and are consumed by
        # the bridge's main loop one at a time.  Do not use ``get_latest``
        # here: draining the queue could swallow an unrelated external
        # intent that arrived while the tool was running.  A follow-up action
        # for this episode will be picked up by the normal loop after the
        # brain processes the feedback.
        if episode_id:
            return

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
                    note = str(result_obj.get("note", ""))[:180]
                    suffix = f"\n[工具说明] {note}" if note else ""
                    return f"[搜索结果] {tool_name} 返回了 {len(results)} 条结果:\n{items}{suffix}"
                if "content" in result_obj:
                    return f"[文件内容] {result_obj.get('path', tool_name)}:\n{result_obj['content'][:2000]}"
            return f"[工具结果] {tool_name}: {result[:500]}"
        except json.JSONDecodeError:
            return f"[工具结果] {tool_name}: {result[:500]}"

    async def _feed_brain(
        self,
        text: str,
        source: str = "agent",
        episode_id: str | None = None,
        intent_id: str | None = None,
        goal_id: str | None = None,
    ):
        """把文本作为输入喂给大脑。"""
        try:
            # The bridge normally lives in the same process as BrainStem.
            # Direct enqueue avoids a self-HTTP call that could deadlock the
            # event loop and preserves the original source for boundary rules.
            if self.brain_stem is not None and hasattr(self.brain_stem, "receive_input"):
                await self.brain_stem.receive_input(
                    text,
                    source=source,
                    episode_id=episode_id,
                    intent_id=intent_id,
                    goal_id=goal_id,
                )
                self.last_feed_error = ""
                return True

            data = json.dumps({
                "text": text,
                "source": source,
                "goal": None,
                "episode_id": episode_id,
                "intent_id": intent_id,
                "goal_id": goal_id,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{self.brain_api_url}/api/v4/input",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            def _post():
                with urllib.request.urlopen(req, timeout=10) as response:
                    response.read(1)

            await asyncio.to_thread(_post)
            self.last_feed_error = ""
            return True
        except Exception as e:
            self.feed_error_count += 1
            self.last_feed_error = str(e)[:300]
            logger.warning("agent-bridge: feed_brain failed: %s", e)
            return False

    def snapshot(self) -> dict:
        """Expose bridge health without exposing tool arguments or secrets."""
        task = self._task
        return {
            "running": bool(task and not task.done()),
            "bound_loop": bool(self._bound_loop),
            "total_tools_executed": self.total_tools_executed,
            "total_iterations": self.total_iterations,
            "feed_error_count": self.feed_error_count,
            "last_feed_error": self.last_feed_error,
            "allow_write_tools": self.allow_write_tools,
            "workspace_root": str(self.workspace_root),
        }

    async def process_user_message(self, text: str, source: str = "user") -> dict | None:
        """处理一条用户消息——通过大脑完整链路。

        这是外部平台消息的入口。
        1. 消息作为输入发给大脑
        2. 大脑处理 → 产出 intent
        3. 如果是 call_tool，执行工具
        4. 如果是 respond，返回回复文本
        """
        # 喂给大脑
        fed = await self._feed_brain(text, source=source)
        if fed is False:
            return {
                "type": "error",
                "text": self.last_feed_error or "大脑当前不可用",
            }

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
