"""Brain Stem — 脑干。意识主循环引擎，永不停止。

职责:
  1. 驱动每个 tick 的脑区管道
  2. 分发输入到丘脑 → 协调各脑区依次运转
  3. 管理觉醒状态
  4. 统计和健康监控

每 tick 流程:
  脑干 → 丘脑(输入过滤) → 杏仁核(情绪) → 前额叶(决策) 
  → 海马体(检索+编码) → 默认模式(内在独白) 
  → 基底节(习惯) → 扣带回(冲突监控) → 工作记忆更新
"""

import asyncio
import inspect
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from brain.thalamus import Thalamus
from brain.amygdala import Amygdala
from brain.prefrontal import Prefrontal
from brain.hippocampus import Hippocampus
from brain.default_mode import DefaultModeNetwork
from brain.basal_ganglia import BasalGanglia
from brain.cingulate import Cingulate
from brain.dream import DreamEngine
from brain.pipeline import gate_check, compute_emotion_weight, compute_strength, compress_clusters, run_consolidation, format_context_block
from brain.working_memory import WorkingMemory
from brain.brain_state import BrainState, SNAPSHOT_SCHEMA_VERSION
from brain.self_model import SelfModel
from brain.intent import Intent, IntentQueue, IntentType
from brain.goal_system import GoalSystem
from brain.task_scheduler import LongTermTaskScheduler, TaskTier
from brain.metacognition import Metacognition
from brain.emotional_spectrum import EmotionalSpectrum
from brain.procedural_memory import ProceduralMemory
from brain.time_sense import TimeSense
from brain.drive_engine import DriveEngine, GoalGenerator, GoalScheduler, build_state_snapshot_for_drive_engine  # V7
from brain.exploration import ExplorationQueue, ExplorationExecutor  # V8
from brain.reflection_engine import ReflectionEngine  # V8
from brain.core_purpose import core_purpose  # V8
from brain.predictive_layer import PredictiveLayer  # V9
from brain.cognitive_dispatch import CognitiveDispatch  # V9
from brain.boredom import BoredomEngine  # V9
from brain.social_self import SocialEmotionEngine, AttachmentSystem  # V10
from brain.reward_system import RewardSystem  # V10
from brain.autobiographical import AutobiographicalNarrative  # V10
from brain.boundary import BoundaryEngine  # V10
from brain.autonomy import AutonomyEpisode, EpisodeStatus  # V11
from config import (
    TICK_INTERVAL_SEC,
    MEMORY_DECAY_RATE,
    MEMORY_DECAY_INTERVAL_TICKS,
    MEMORY_ARCHIVE_THRESHOLD,
    DROWSY_THRESHOLD_TICKS,
    LIGHT_SLEEP_THRESHOLD_TICKS,
    DEEP_SLEEP_THRESHOLD_TICKS,
    DREAM_INTERVAL_SEC,
    CONSOLIDATION_INTERVAL_SEC,
    REFLECTION_INTERVAL_SEC,
    STATE_SNAPSHOT_INTERVAL_SEC,
    SALIENCE_THRESHOLD,
    GATE_GOAL_RELEVANCE_DEFAULT,
    GATE_GOAL_RELEVANCE_WITH_GOAL,
    GATE_GOAL_RELEVANCE_PASS,
    GATE_NOVELTY_PASS,
    DEEP_REFLECTION_INTERVAL_TICKS,
    DEEP_REFLECTION_ENABLED,
    DREAM_ENABLED,
    LLM_EMOTION_BLEND_RATIO,
    PREDICTIVE_LAYER_ENABLED,
    COGNITIVE_DISPATCH_ENABLED,
    BOREDOM_ENABLED,
    SOCIAL_SELF_ENABLED,
    REWARD_SYSTEM_ENABLED,
    AUTOBIO_ENABLED,
    BOUNDARY_ENABLED,
    AUTONOMY_ENABLED,
    AUTONOMY_GOAL_INTERVAL_TICKS,
    AUTONOMY_MAX_EPISODE_TICKS,
    TASK_QUEUE_LIMIT,
    TASK_MAINTENANCE_BUDGET_TICKS,
    TASK_USER_BUDGET_TICKS,
    TASK_EXPLORATION_BUDGET_TICKS,
    TASK_MAINTENANCE_DEADLINE_TICKS,
    TASK_USER_DEADLINE_TICKS,
    TASK_EXPLORATION_DEADLINE_TICKS,
)

logger = logging.getLogger("brain-v5.brain-stem")


class BrainStem:
    """Consciousness loop engine — the brain's heartbeat."""

    def __init__(self, state_store=None, memory_store=None):
        # Brain regions
        self.thalamus = Thalamus()
        self.amygdala = Amygdala()
        self.prefrontal = Prefrontal()
        self.hippocampus = Hippocampus(memory_store=memory_store)
        self.default_mode = DefaultModeNetwork()
        self.basal_ganglia = BasalGanglia()
        self.cingulate = Cingulate()
        self.working_memory = WorkingMemory()
        self.dream_engine = DreamEngine()

        # Sleep state
        self.sleep_state = "awake"  # awake / drowsy / light_sleep / deep_sleep
        self.last_dream_time = 0.0
        self.last_consolidation_time = 0.0

        # State
        self.state = BrainState()
        self.state_store = state_store
        self.memory_store = memory_store

        # Loop control
        self._stop_event = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._bound_loop = None
        self._task: asyncio.Task | None = None
        self._has_started = False
        # Inputs may be buffered before the first ``start()`` (useful for
        # embedded callers), but once a running instance is stopped we reject
        # new input until the next explicit start.  This prevents a shutdown
        # race from leaving work that unexpectedly executes after restart.
        self._accepting_input = True
        self._pending_input: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._input_processed = asyncio.Event()
        self._input_waiters: dict[str, asyncio.Future] = {}
        self._input_sources: dict[str, str] = {}
        self._active_input_id: str | None = None
        self._restored_uptime_seconds: float = 0.0

        # Output feed — external agents consume this
        self._output_feed: asyncio.Queue = asyncio.Queue(maxsize=100)

        # Intent queue — brain produces intents, agent layer consumes (v5.0)
        self.intent_queue: IntentQueue = IntentQueue()

        # Goal system — v5.1: brain sets its own goals
        self.goal_system = GoalSystem()
        # Long-term policy — one execution lane, bounded tiered queue.
        self.task_scheduler = LongTermTaskScheduler(
            max_queue=TASK_QUEUE_LIMIT,
            default_budgets={
                TaskTier.MAINTENANCE: TASK_MAINTENANCE_BUDGET_TICKS,
                TaskTier.USER: TASK_USER_BUDGET_TICKS,
                TaskTier.EXPLORATION: TASK_EXPLORATION_BUDGET_TICKS,
            },
            default_deadlines={
                TaskTier.MAINTENANCE: TASK_MAINTENANCE_DEADLINE_TICKS,
                TaskTier.USER: TASK_USER_DEADLINE_TICKS,
                TaskTier.EXPLORATION: TASK_EXPLORATION_DEADLINE_TICKS,
            },
        )

        # Metacognition — v5.2: brain monitors its own thinking
        self.metacognition = Metacognition()

        # Emotional Spectrum — v5.3: continuous emotion instead of discrete labels
        self.emotional_spectrum = EmotionalSpectrum()

        # Procedural Memory — v5.4: learns skills from repeated success
        self.procedural_memory = ProceduralMemory()

        # Time Sense — v5.4: internal clock, rhythm, temporal awareness
        self.time_sense = TimeSense()

        # Drive Engine — V7: dynamic drive system
        self.drive_engine = DriveEngine()
        self.goal_generator = GoalGenerator()
        self.goal_scheduler = GoalScheduler()
        self._last_archived_count: int = 0  # tracked for stagnation detection

        # V8: exploration + reflection
        self.exploration_queue = ExplorationQueue()
        self.exploration_executor = ExplorationExecutor()
        self.reflection_engine = ReflectionEngine()

        # V9: predictive processing + cognitive dispatch + boredom
        self.predictive_layer = PredictiveLayer() if PREDICTIVE_LAYER_ENABLED else None
        self.cognitive_dispatch = CognitiveDispatch() if COGNITIVE_DISPATCH_ENABLED else None
        self.boredom_engine = BoredomEngine() if BOREDOM_ENABLED else None

        # V10: social self + reward + autobiography + boundary
        self.social_emotion = SocialEmotionEngine() if SOCIAL_SELF_ENABLED else None
        self.attachment_system = AttachmentSystem() if SOCIAL_SELF_ENABLED else None
        self.reward_system = RewardSystem() if REWARD_SYSTEM_ENABLED else None
        self.autobiography = AutobiographicalNarrative() if AUTOBIO_ENABLED else None
        self.boundary = BoundaryEngine() if BOUNDARY_ENABLED else None

        # V11: a bounded causal record for internally generated action cycles.
        # The manager never executes a tool; it only coordinates state,
        # feedback and recovery across BrainStem and AgentBridge.
        self.autonomy = (
            AutonomyEpisode(max_ticks=AUTONOMY_MAX_EPISODE_TICKS)
            if AUTONOMY_ENABLED else None
        )

        # Stats
        self.start_time = datetime.now(timezone.utc)
        self.state.total_ticks = 0
        self.last_reflection = 0.0
        self.last_snapshot = 0.0
        self.last_decay = 0.0
        if self.autonomy:
            self.state.autonomy = self.autonomy.summary()

    # ── Public API ──

    @staticmethod
    def _transfer_queue(queue: asyncio.Queue) -> asyncio.Queue:
        """Copy buffered items into a queue owned by the current loop."""
        # A blocked reader/writer belongs to the old loop.  Replacing the
        # queue underneath it would strand that task forever, which is worse
        # than rejecting an unsafe cross-loop migration explicitly.
        active_getters = [
            waiter for waiter in (getattr(queue, "_getters", ()) or ())
            if not waiter.done()
        ]
        active_putters = [
            waiter for waiter in (getattr(queue, "_putters", ()) or ())
            if not waiter.done()
        ]
        if active_getters or active_putters:
            raise RuntimeError(
                "cannot move brain queue while a reader or writer is waiting; "
                "finish the owning event loop first"
            )
        replacement = asyncio.Queue(maxsize=queue.maxsize)
        while True:
            try:
                replacement.put_nowait(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
            except asyncio.QueueFull:
                break
        return replacement

    def _ensure_loop_primitives(self):
        """Rebind asyncio primitives when an embedded caller changes loops."""
        current = asyncio.get_running_loop()
        if self._bound_loop is current:
            # AgentBridge may have been started before the stem on this loop,
            # or may have moved an idle queue during a sequential restart.
            # Validate the shared queue even when the stem itself is already
            # bound so cross-loop ownership cannot remain implicit.
            if getattr(self.intent_queue, "_bound_loop", None) is not current:
                self.intent_queue.rebind_loop()
            return
        if self._bound_loop is None:
            # Objects are often constructed before an event loop exists.  The
            # primitives above are still unbound at that point, so simply
            # claim them for the first running loop.  In particular, preserve
            # a request future submitted before ``start()``; replacing the
            # maps here would silently cancel that caller's completion path.
            self.intent_queue.rebind_loop()
            self._bound_loop = current
            return
        lifecycle_lock = self._lifecycle_lock
        if lifecycle_lock.locked() or any(
            not waiter.done()
            for waiter in (getattr(lifecycle_lock, "_waiters", ()) or ())
        ):
            # A stop/start transition may have cleared ``_task`` before its
            # awaitable finished.  The lock is the authoritative ownership
            # marker in that window; do not replace it from another loop.
            raise RuntimeError(
                "brain-stem lifecycle transition is still running on another event loop"
            )
        if self._task and not self._task.done():
            # A live heartbeat must never be moved underneath itself.
            raise RuntimeError("brain-stem cannot change event loops while running")

        # Futures belong to the old loop and cannot safely be completed from a
        # new one.  They represent calls that outlived that loop, so cancel
        # them and let their callers observe cancellation rather than leak.
        for future in self._input_waiters.values():
            if not future.done():
                future.cancel()
        self._input_waiters.clear()
        self._input_sources.clear()

        self._stop_event = asyncio.Event()
        self._input_processed = asyncio.Event()
        self._pending_input = self._transfer_queue(self._pending_input)
        self._output_feed = self._transfer_queue(self._output_feed)
        self.intent_queue.rebind_loop()
        self._lifecycle_lock = asyncio.Lock()
        self._bound_loop = current

    async def start(self):
        """Start the consciousness loop, serializing lifecycle transitions."""
        self._ensure_loop_primitives()
        async with self._lifecycle_lock:
            await self._start_unlocked()

    async def _start_unlocked(self):
        """Start the consciousness loop."""
        if self._task and not self._task.done():
            return
        self._task = None

        logger.info("brain-stem: consciousness loop starting")
        self._stop_event.clear()
        self._accepting_input = True
        self._has_started = True
        self.start_time = datetime.now(timezone.utc)

        # Restore state before starting the loop.  Starting the task first
        # allowed a tick to race with state replacement and lose continuity.
        if self.state_store:
            try:
                restored = self.state_store.load_latest()
            except Exception as exc:
                restored = None
                self._record_loop_error("state_restore", exc)
            if restored:
                try:
                    self._restore_snapshot(restored)
                    logger.info("brain-stem: state restored from snapshot")
                except Exception as exc:
                    # A corrupt/old snapshot must not prevent a fresh
                    # heartbeat from starting.  The in-memory defaults remain
                    # usable and the failure is visible in health state.
                    self._record_loop_error("state_restore", exc)
                    logger.warning("brain-stem: ignoring invalid snapshot: %s", str(exc)[:120])

        # Tool execution is intentionally not replayed from a snapshot: a
        # crash may have happened after an external side effect but before its
        # feedback was journaled.  Close an in-flight action as interrupted so
        # the next heartbeat can make a fresh, policy-checked decision.  If
        # feedback was already durable, preserve that outcome without running
        # the tool a second time.
        if self.autonomy and self.autonomy.is_active:
            active = self.autonomy.active
            if active and active.status == EpisodeStatus.FEEDBACK_RECEIVED:
                recovered_success = active.success is not False
                recovered_outcome = "重启前已收到反馈，结果已恢复"
                recovered = (
                    self.autonomy.complete(recovered_outcome, self.state.total_ticks)
                    if recovered_success
                    else self.autonomy.fail(recovered_outcome, self.state.total_ticks)
                )
                self._finish_autonomy_episode(
                    recovered,
                    recovered_success,
                    recovered_outcome,
                )
            else:
                interrupted = self.autonomy.abort(
                    "进程重启，未重放未确认行动",
                    self.state.total_ticks,
                )
                self._finish_autonomy_episode(
                    interrupted,
                    False,
                    "进程重启，未重放未确认行动",
                )

        self.state.awake = True
        self._restored_uptime_seconds = max(0.0, float(self.state.uptime_seconds or 0.0))
        # ``wake_up`` means the process is available, but preserve the
        # recovered sleep phase so the next tick can make the same decision.
        if self.sleep_state not in {"awake", "drowsy", "light_sleep", "deep_sleep"}:
            self.sleep_state = "awake"
        self.state.sleep_state = self.sleep_state
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_loop_done)

    async def stop(self):
        """Stop the consciousness loop, serializing lifecycle transitions."""
        self._ensure_loop_primitives()
        async with self._lifecycle_lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self):
        """Stop the consciousness loop."""
        logger.info("brain-stem: stopping consciousness loop")
        # Close the admission gate before cancelling the task.  Callers that
        # race with shutdown then receive an immediate, correlated failure
        # instead of enqueueing work that could survive into the next start.
        self._accepting_input = False
        self._stop_event.set()
        task = self._task
        self._task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                # A failed background task must not make shutdown fail too.
                self._record_loop_error("loop_shutdown", exc)

        self._discard_pending_inputs("brain stopped")
        self._fail_all_waiters("brain stopped")
        self.state.awake = False

        # Final snapshot
        try:
            await self._snapshot_state()
        except Exception as exc:
            self._record_loop_error("final_snapshot", exc)

    def _on_loop_done(self, task: asyncio.Task):
        """Fail pending callers if the heartbeat exits unexpectedly."""
        # A completed task's callback can run just after an explicit restart.
        # Never let an old callback mark the newly-created heartbeat as dead.
        if self._task is not None and self._task is not task:
            return
        if task.cancelled() or self._stop_event.is_set():
            return
        try:
            error = task.exception()
        except Exception as exc:
            error = exc
        if error is None:
            error = "heartbeat exited without a stop request"
        self._record_loop_error("loop_exit", error)
        self._accepting_input = False
        self._discard_pending_inputs("brain loop stopped unexpectedly")
        self._fail_all_waiters("brain loop stopped unexpectedly")
        self.state.awake = False

    def _fail_all_waiters(self, error: str):
        """Resolve every outstanding input future with its own correlation ID."""
        self._fail_active_input(error)
        for request_id, future in list(self._input_waiters.items()):
            if not future.done():
                source = self._input_sources.get(request_id, "external")
                future.set_result(self._build_input_result(
                    source=source,
                    pending=False,
                    request_id=request_id,
                    error=error,
                ))
            self._input_waiters.pop(request_id, None)
            self._input_sources.pop(request_id, None)

    def _discard_pending_inputs(self, error: str) -> int:
        """Drop queued requests during shutdown and resolve their futures.

        A request that has not reached ``_tick`` must never be replayed after
        restart.  Tracked callers still receive a normal structured result so
        they do not hang waiting for a future that can no longer complete.
        """
        discarded = 0
        while True:
            try:
                input_data = self._pending_input.get_nowait()
            except asyncio.QueueEmpty:
                break
            discarded += 1
            request_id = input_data.get("request_id") if isinstance(input_data, dict) else None
            if not request_id:
                continue
            future = self._input_waiters.pop(request_id, None)
            source = self._input_sources.pop(
                request_id,
                input_data.get("source", "external") if isinstance(input_data, dict) else "external",
            )
            if future and not future.done():
                future.set_result(self._build_input_result(
                    source=source,
                    pending=False,
                    request_id=request_id,
                    error=error,
                ))
        if discarded:
            logger.info("brain-stem: discarded %d queued input(s) during shutdown", discarded)
        return discarded

    async def receive_input(
        self,
        text: str,
        source: str = "external",
        goal: str | None = None,
        episode_id: str | None = None,
        intent_id: str | None = None,
        goal_id: str | None = None,
    ):
        """Receive input from an external agent. Pushes to input queue."""
        self._ensure_loop_primitives()
        # This legacy one-way API does not need a completion future.  Avoid
        # retaining an unobserved waiter when a caller only wants to enqueue.
        request_id, _ = self.submit_input(
            text=text,
            source=source,
            goal=goal,
            episode_id=episode_id,
            intent_id=intent_id,
            goal_id=goal_id,
            track=False,
            _raise_on_reject=True,
        )
        logger.debug("brain-stem: input queued from %s (id=%s)", source, request_id)
        return request_id

    def submit_input(
        self,
        text: str,
        source: str = "external",
        goal: str | None = None,
        episode_id: str | None = None,
        intent_id: str | None = None,
        goal_id: str | None = None,
        track: bool = True,
        _raise_on_reject: bool = False,
    ) -> tuple[str, asyncio.Future | None]:
        """Queue an input and return an isolated completion future.

        Each caller gets its own correlation ID.  The old shared Event made
        concurrent requests observe one another's result and could return a
        stale intent after a timeout.
        """
        request_id = uuid.uuid4().hex
        source = str(source or "external")[:200]
        text = str(text or "")[:4000]
        goal = str(goal)[:500] if goal is not None else None
        episode_id = str(episode_id)[:80] if episode_id else None
        intent_id = str(intent_id)[:80] if intent_id else None
        goal_id = str(goal_id)[:100] if goal_id else None
        future = None
        # ``submit_input`` is also used by the legacy pre-start path.  A first
        # request may be buffered before ``start()``, but after an instance has
        # been started and stopped admission is closed until the next start.
        running = self._task is not None and not self._task.done()
        if self._has_started and (not running or self._stop_event.is_set()):
            if _raise_on_reject:
                raise RuntimeError("brain stopped")
            if track:
                self._ensure_loop_primitives()
                future = asyncio.get_running_loop().create_future()
                future.set_result(self._build_input_result(
                    source=source,
                    pending=False,
                    request_id=request_id,
                    error="brain stopped",
                ))
            return request_id, future
        if track:
            self._ensure_loop_primitives()
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._input_waiters[request_id] = future
            self._input_sources[request_id] = source
        item = {
            "text": text,
            "source": source,
            "goal": goal,
            "episode_id": episode_id,
            "intent_id": intent_id,
            "goal_id": goal_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if track:
            item["request_id"] = request_id
        try:
            self._pending_input.put_nowait(item)
        except asyncio.QueueFull:
            self._input_waiters.pop(request_id, None)
            self._input_sources.pop(request_id, None)
            if _raise_on_reject:
                raise RuntimeError("input queue full")
            if future is not None:
                future.set_result(self._build_input_result(
                    source=source,
                    pending=False,
                    request_id=request_id,
                    error="input queue full",
                ))
        return request_id, future

    async def get_output(self) -> dict | None:
        """Get the latest output event (non-blocking)."""
        self._ensure_loop_primitives()
        try:
            return self._output_feed.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def get_state(self) -> dict:
        """Get current brain state snapshot."""
        self._ensure_loop_primitives()
        return self.state.snapshot()

    # ── Main Loop ──

    async def _loop(self):
        """The consciousness loop — runs until stop_event is set."""
        logger.info("brain-stem: loop started")

        while not self._stop_event.is_set():
            tick_start = datetime.now(timezone.utc)

            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._record_loop_error("tick", e)
                self._fail_active_input(str(e)[:200])

            self.state.total_ticks += 1
            self.state.last_heartbeat_at = datetime.now(timezone.utc).isoformat()
            self.state.uptime_seconds = self._restored_uptime_seconds + (
                datetime.now(timezone.utc) - self.start_time
            ).total_seconds()

            # Periodic tasks
            try:
                now_ts = datetime.now(timezone.utc).timestamp()

                # Sleep-time dream + consolidation
                if self.sleep_state != "awake" and DREAM_ENABLED:
                    if now_ts - self.last_dream_time >= DREAM_INTERVAL_SEC:
                        self.last_dream_time = now_ts
                        await self._run_periodic("dream", self._dream_tick)
                    if now_ts - self.last_consolidation_time >= CONSOLIDATION_INTERVAL_SEC:
                        self.last_consolidation_time = now_ts
                        await self._run_periodic("consolidation", self._consolidation_tick)

                # Reflection (every REFLECTION_INTERVAL_SEC)
                if now_ts - self.last_reflection >= REFLECTION_INTERVAL_SEC:
                    self.last_reflection = now_ts
                    await self._run_periodic("reflection", self._reflection_tick)
                    logger.debug("brain-stem: reflection tick")

                # Memory decay (every MEMORY_DECAY_INTERVAL_TICKS ticks)
                if self.state.total_ticks % MEMORY_DECAY_INTERVAL_TICKS == 0 and self.state.total_ticks > 0:
                    self.last_decay = now_ts
                    if self.memory_store:
                        def _decay():
                            return self.memory_store.decay_all(
                                decay_rate=MEMORY_DECAY_RATE,
                                archive_threshold=MEMORY_ARCHIVE_THRESHOLD,
                            )

                        result = await self._run_periodic("memory_decay", _decay)
                        if isinstance(result, dict):
                            self._last_archived_count = int(result.get("archived", 0) or 0)

                # State snapshot (every STATE_SNAPSHOT_INTERVAL_SEC)
                if now_ts - self.last_snapshot >= STATE_SNAPSHOT_INTERVAL_SEC:
                    self.last_snapshot = now_ts
                    await self._run_periodic("snapshot", self._snapshot_state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The maintenance scheduler itself is part of the heartbeat;
                # protect it even if a future task is added without a wrapper.
                self._record_loop_error("maintenance", exc)

            # Sleep until next tick
            elapsed = (datetime.now(timezone.utc) - tick_start).total_seconds()
            sleep_time = max(0.0, TICK_INTERVAL_SEC - elapsed)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_time)
                break  # stop_event set
            except asyncio.TimeoutError:
                pass  # normal tick cycle

        logger.info("brain-stem: loop stopped after %d ticks", self.state.total_ticks)

    async def _run_periodic(
        self,
        name: str,
        operation: Callable[[], Awaitable[Any] | Any],
    ) -> Any:
        """Run a maintenance task without taking down the heartbeat."""
        try:
            result = operation()
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict) and result.get("archived", 0) > 0:
                logger.info("brain-stem: archived %d decayed memories", result["archived"])
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_loop_error(name, exc)
            return None

    def _record_loop_error(self, phase: str, error: Exception | str):
        message = f"{phase}: {str(error)[:300]}"
        self.state.loop_error_count += 1
        self.state.last_loop_error = message
        self.state.recent_errors.append(message)
        self.state.recent_errors = self.state.recent_errors[-20:]
        if isinstance(error, Exception) and error.__traceback__ is not None:
            logger.error(
                "brain-stem: %s",
                message,
                exc_info=(type(error), error, error.__traceback__),
            )
        else:
            logger.error("brain-stem: %s", message)

    def _fail_active_input(self, error: str):
        request_id = self._active_input_id
        if not request_id:
            return
        future = self._input_waiters.pop(request_id, None)
        source = self._input_sources.pop(request_id, self.state.last_input_source or "external")
        if future and not future.done():
            future.set_result(self._build_input_result(
                source=source,
                pending=False,
                request_id=request_id,
                error=error,
            ))
        self._active_input_id = None

    def _complete_input_waiter(self, input_data: dict | None):
        """Resolve exactly the future associated with ``input_data``."""
        if not input_data:
            self._active_input_id = None
            return
        request_id = input_data.get("request_id")
        if request_id:
            future = self._input_waiters.pop(request_id, None)
            source = self._input_sources.pop(request_id, input_data.get("source", "external"))
            if future and not future.done():
                future.set_result(self._build_input_result(
                    source=source,
                    pending=False,
                    request_id=request_id,
                ))
        self._active_input_id = None

    def _build_input_result(
        self,
        source: str,
        pending: bool,
        request_id: str | None = None,
        error: str | None = None,
    ) -> dict:
        """Build a stable response snapshot for one input request."""
        st = self.state
        sm = st.self_model
        # A pending response is deliberately a neutral acknowledgement: it
        # must not leak a different request's error or intent while the actor
        # is still working.  For completed requests only expose state errors
        # when the state belongs to this request.
        request_matches = request_id is None or request_id == st.last_input_id
        effective_error = None if pending else (
            error if error is not None else (st.last_error if request_matches else "")
        )

        # Never expose an intent from a different request.  A pending or
        # failed request has no response even if a prior intent exists.
        intent = None if pending or error is not None else st.last_intent
        # Keep completed responses type-stable for callers that render or
        # slice the field even when the brain only produced a ``think`` intent.
        # ``None`` remains reserved for a still-pending acknowledgement.
        response_text = None if pending else ""
        intent_type = None
        if intent:
            intent_type = intent.get("type")
            if intent_type == "respond":
                response_text = intent.get("response_text", "")
            elif intent_type == "ask_question":
                response_text = intent.get("question", "")

        try:
            memory_total = self.memory_store.count() if self.memory_store else 0
            identity_total = (
                len(self.memory_store.get_identity_memories(20))
                if self.memory_store else 0
            )
        except Exception:
            memory_total = identity_total = 0

        try:
            top_drives = [
                {
                    "name": d.get("name", ""),
                    "label": d.get("label", ""),
                    "weight": d.get("weight", 0.0),
                }
                for d in sm.get_top_drives(3)
                if isinstance(d, dict)
            ]
        except Exception:
            top_drives = []
        return {
            "accepted": False if pending or error is not None else (
                st.last_input_accepted if request_matches else False
            ),
            "gated": False if pending else st.last_input_gated,
            "llm_error": bool(effective_error),
            "llm_error_message": effective_error[:200] if effective_error else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "response": response_text,
            "intent_type": intent_type,
            "focus": st.focus_entity,
            "emotion": st.current_emotion,
            "inner_monologue": (st.inner_monologue or "")[:200],
            "working_memory": (st.current_context or "")[:300],
            "memories": {
                "total": memory_total,
                "identity_forming": identity_total,
            },
            "self": {
                "identity": str(sm.identity_anchor or "")[:200],
                "traits": list(sm.identity_traits or []),
                "top_drives": top_drives,
                "mood": str(sm.mood_tendency or "balanced"),
                "version": sm.identity_version,
                "experiences": sm.total_experiences,
                "last_reflection": str(sm.last_reflection or "")[:100],
            },
            "session": {
                "source": source,
                "active_sessions": st.session_manager.get_session_count(),
            },
            "request_id": request_id or st.last_input_id or None,
            "pending": pending,
        }

    # ── V11: Autonomous episode coordination ──

    def _sync_autonomy_projection(self) -> None:
        """Keep the API/snapshot projection aligned with the live manager."""
        if self.autonomy is not None:
            self.state.autonomy = self.autonomy.summary()

    def _find_goal(self, goal_id: str | None):
        if not goal_id:
            return None
        return self.goal_system.get_by_id(str(goal_id))

    def _finish_autonomy_episode(
        self,
        record,
        success: bool,
        outcome: str,
    ) -> None:
        """Apply one closed episode to goals, reward and self-narrative.

        All integrations are best-effort and synchronous.  A bookkeeping
        failure must never take down the heartbeat that just completed the
        action.
        """
        if record is None:
            return
        try:
            goal = self._find_goal(getattr(record, "goal_id", ""))
            status = getattr(record, "status", "")
            episode_success = bool(
                status == EpisodeStatus.COMPLETED
                and getattr(record, "success", None) is not False
                and success
            )
            if status == EpisodeStatus.COMPLETED and goal is not None:
                was_actionable = getattr(goal, "status", "") in {"active", "pending"}
                if getattr(goal, "status", "") in {"active", "pending"}:
                    self.goal_system.mark_done(goal.id, outcome[:240])
                if was_actionable and getattr(goal, "status", "") == "done":
                    self.drive_engine.notify_goal_completed()
            elif status == EpisodeStatus.FAILED and goal is not None:
                if getattr(goal, "status", "") == "active":
                    self.goal_system.mark_failed(goal.id, outcome[:240])

            if self.reward_system and status in {
                EpisodeStatus.COMPLETED,
                EpisodeStatus.FAILED,
            }:
                quality = getattr(record, "result_quality", "unknown")
                actual_reward = (
                    0.8 if quality == "verified" and episode_success
                    else 0.55 if quality == "simulated" and episode_success
                    else 0.2
                )
                reward_event = self.reward_system.deliver_reward(
                    channel="achievement",
                    actual_reward=actual_reward,
                    context=f"autonomy:{getattr(record, 'tool_name', '') or 'episode'}",
                )
                # Keep the reward and its local prediction error attached to
                # the episode so a snapshot explains why a goal changed.
                record.reward = actual_reward
                record.prediction_error = reward_event.prediction_error
            elif status in {EpisodeStatus.COMPLETED, EpisodeStatus.FAILED}:
                record.reward = (
                    0.55
                    if episode_success and getattr(record, "result_quality", "") == "simulated"
                    else 0.8 if episode_success
                    else 0.2
                )

            description = getattr(record, "goal", "自主经历")[:180]
            result_text = "完成" if episode_success else (
                "中止" if status == EpisodeStatus.ABORTED else "失败"
            )
            quality_note = (
                "（模拟结果，未作为外部事实确认）"
                if getattr(record, "result_quality", "") == "simulated"
                else ""
            )
            marker = f"[自主经历] {result_text}{quality_note}：{description}"
            self.working_memory.push(
                marker[:240], "autonomy", 0.55 if episode_success else 0.35
            )
            self.state.last_narrative = marker[:500]

            # A compact episodic memory preserves the causal outcome without
            # copying a potentially sensitive tool response into the ledger.
            if self.memory_store and status in {
                EpisodeStatus.COMPLETED,
                EpisodeStatus.FAILED,
            }:
                memory_id = self.memory_store.save({
                    "type": "episodic",
                    "title": f"自主经历：{description[:60]}",
                    "content": marker,
                    "summary": marker,
                    "source": "autonomy",
                    "importance": (
                        0.65
                        if episode_success and getattr(record, "result_quality", "") != "simulated"
                        else 0.45 if episode_success
                        else 0.4
                    ),
                    "emotion_label": "breakthrough" if episode_success else "confused",
                    "emotion_vector": dict(self.state.emotion_vector),
                    "is_identity_forming": False,
                })
                record.memory_ids = [memory_id]
            if status in {EpisodeStatus.COMPLETED, EpisodeStatus.FAILED}:
                self.state.self_model.ingest_experience(
                    text=marker,
                    emotion={
                        "emotion_label": "breakthrough" if episode_success else "failure",
                        "emotion_vector": dict(self.state.emotion_vector),
                        "salience": (
                            0.65
                            if episode_success and getattr(record, "result_quality", "") != "simulated"
                            else 0.45 if episode_success
                            else 0.35
                        ),
                    },
                    importance=(
                        0.65
                        if episode_success and getattr(record, "result_quality", "") != "simulated"
                        else 0.45 if episode_success
                        else 0.4
                    ),
                    memory_count=self.memory_store.count() if self.memory_store else 0,
                )
        except Exception as exc:
            self._record_loop_error("autonomy_close", exc)
        finally:
            try:
                self.task_scheduler.on_episode_closed(record)
            except Exception as exc:
                self._record_loop_error("task_scheduler_close", exc)
            self._sync_autonomy_projection()

    def _record_autonomy_feedback(
        self,
        input_data: dict,
        success: bool,
        tool_name: str,
    ) -> bool:
        if self.autonomy is None:
            return False
        episode_id = input_data.get("episode_id")
        if not episode_id:
            # Legacy bridges did not send correlation IDs.  Only use the
            # single-lane fallback when the returned tool name also matches
            # the action we are waiting for; otherwise unrelated external
            # tool traffic must remain an orphan rather than close this run.
            active = self.autonomy.active
            if (
                active is not None
                and active.status in {
                    EpisodeStatus.AWAITING_ACTION,
                    EpisodeStatus.AWAITING_FEEDBACK,
                }
                and active.tool_name == tool_name
            ):
                episode_id = active.id
        if not episode_id:
            # ``AutonomyEpisode`` deliberately requires explicit correlation.
            # Account for the unmatched observation without letting it mutate
            # whichever episode happens to be active.
            self.autonomy.total_orphan_feedback += 1
            self._sync_autonomy_projection()
            return False
        accepted = self.autonomy.record_feedback(
            episode_id=episode_id,
            success=success,
            tool_name=tool_name,
            tick=self.state.total_ticks,
            intent_id=input_data.get("intent_id") or "",
            result_summary="success" if success else "failure",
            result_quality=self._classify_autonomy_feedback(
                input_data.get("text", ""), success
            ),
        )
        self._sync_autonomy_projection()
        return accepted

    @staticmethod
    def _classify_autonomy_feedback(text: str, success: bool) -> str:
        """Classify execution feedback without treating placeholders as facts."""
        if not success:
            return "failed"
        normalized = str(text or "").lower()
        if any(
            marker in normalized
            for marker in (
                "模拟",
                "simulated",
                "dry-run",
                "需配置搜索引擎",
                "来源质量] simulated",
            )
        ):
            return "simulated"
        if "来源质量] failed" in normalized:
            return "failed"
        return "verified"

    def _observe_autonomy_intent(self, intent: Intent) -> bool:
        """Attach a follow-up intent to the active episode.

        Returns True when the correlated follow-up was consumed, whether it
        schedules another action or closes the episode.
        """
        if self.autonomy is None or not self.autonomy.is_active:
            return False
        active = self.autonomy.active
        if active is None:
            return False
        if intent.episode_id and intent.episode_id != active.id:
            # A follow-up produced for another causal lane must never mutate
            # the currently active episode.
            return False
        if intent.goal_id and intent.goal_id != active.goal_id:
            return False
        # Tool feedback may come from an older bridge that did not carry the
        # correlation field.  In that case the single active episode is the
        # only safe fallback.
        if not intent.episode_id:
            intent.episode_id = active.id
        if not intent.goal_id:
            intent.goal_id = active.goal_id
        if not intent.origin or intent.origin == "external":
            intent.origin = active.origin or "autonomous"
        if intent.type == IntentType.CALL_TOOL:
            planned = self.autonomy.plan(
                episode_id=active.id,
                action_type=intent.type.value,
                tool_name=intent.tool_name or "",
                tick=self.state.total_ticks,
                reason=intent.reason,
                intent_id=intent.intent_id,
                expected=intent.reason[:240],
                attempt_no=intent.attempt_no,
            )
            self._sync_autonomy_projection()
            return planned
        if intent.type in {
            IntentType.RESPOND,
            IntentType.THINK,
            IntentType.ASK_QUESTION,
        } and active.status == EpisodeStatus.FEEDBACK_RECEIVED:
            outcome = (
                intent.response_text
                or intent.thought
                or intent.question
                or "反馈已吸收"
            )
            success = active.success is not False
            record = (
                self.autonomy.complete(outcome[:300], self.state.total_ticks)
                if success
                else self.autonomy.fail(outcome[:300], self.state.total_ticks)
            )
            self._finish_autonomy_episode(record, success, outcome)
            return True
        return False

    def _restore_snapshot(self, snapshot: dict) -> None:
        """Restore the durable parts of the consciousness runtime.

        Snapshots are an interoperability boundary: they may have been
        written by an older release or be partially damaged after a power
        loss.  The core state is decoded first, and each optional subsystem
        is restored independently so one bad component cannot erase the rest
        of the subject's continuity.
        """
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot must be an object")

        version = snapshot.get("schema_version", 1)
        if isinstance(version, (int, float)) and version > SNAPSHOT_SCHEMA_VERSION:
            logger.warning(
                "brain-stem: snapshot schema %s is newer than supported %s; best-effort restore",
                version,
                SNAPSHOT_SCHEMA_VERSION,
            )

        # BrainState has its own defensive decoder and is the source of truth
        # for self-model, curiosity, activation, and per-source sessions.
        self.state = BrainState.from_snapshot(snapshot)

        raw_wm = snapshot.get("working_memory")
        if isinstance(raw_wm, dict):
            self.working_memory = WorkingMemory.from_snapshot(raw_wm)
        elif self.state.active_thoughts:
            # v1 snapshots exposed active thoughts as a bare list.
            self.working_memory = WorkingMemory.from_snapshot({
                "items": self.state.active_thoughts,
                "context_text": self.state.current_context,
            })
        self.state.active_thoughts = [dict(item) for item in self.working_memory.items]
        if not self.state.current_context:
            self.state.current_context = self.working_memory.get_context()

        valid_sleep_states = {"awake", "drowsy", "light_sleep", "deep_sleep"}
        self.sleep_state = (
            self.state.sleep_state
            if self.state.sleep_state in valid_sleep_states
            else "awake"
        )

        def _restore_component(attribute: str, key: str, decoder):
            raw = snapshot.get(key)
            if raw is None:
                return
            try:
                restored = decoder(raw)
                if restored is not None:
                    setattr(self, attribute, restored)
            except Exception as exc:
                self._record_loop_error(f"restore_{key}", exc)
                logger.warning("brain-stem: component restore skipped (%s): %s", key, str(exc)[:120])

        _restore_component("goal_system", "goal_system", GoalSystem.from_snapshot)
        _restore_component(
            "task_scheduler",
            "task_scheduler",
            LongTermTaskScheduler.from_snapshot,
        )
        _restore_component("metacognition", "metacognition", Metacognition.from_snapshot)
        _restore_component("emotional_spectrum", "emotional_spectrum", EmotionalSpectrum.from_snapshot)
        _restore_component("procedural_memory", "procedural_memory", ProceduralMemory.from_snapshot)
        _restore_component("time_sense", "time_sense", TimeSense.from_snapshot)
        _restore_component("exploration_queue", "exploration_queue", ExplorationQueue.from_snapshot)
        _restore_component("reflection_engine", "reflection_engine", ReflectionEngine.from_snapshot)
        _restore_component("drive_engine", "drive_engine", DriveEngine.from_snapshot)
        if self.autonomy is not None and isinstance(snapshot.get("autonomy"), dict):
            _restore_component("autonomy", "autonomy", AutonomyEpisode.from_snapshot)
            self._sync_autonomy_projection()

        raw_thalamus = snapshot.get("thalamus", {})
        if isinstance(raw_thalamus, dict):
            self.thalamus.last_input = str(raw_thalamus.get("last_input", ""))
            try:
                self.thalamus.noise_discarded = max(0, int(raw_thalamus.get("noise_discarded", 0)))
                self.thalamus.total_relayed = max(0, int(raw_thalamus.get("total_relayed", 0)))
            except (TypeError, ValueError):
                pass
        raw_amygdala = snapshot.get("amygdala", {})
        if isinstance(raw_amygdala, dict):
            for name in ("valence", "arousal", "dominance", "salience"):
                try:
                    setattr(self.amygdala, name, float(raw_amygdala.get(name, getattr(self.amygdala, name))))
                except (TypeError, ValueError):
                    pass

        # Optional V9/V10 regions are only restored when enabled by the
        # current configuration; a snapshot must not silently turn features
        # back on after an operator disabled them.
        if self.predictive_layer is not None:
            _restore_component("predictive_layer", "predictive_layer", PredictiveLayer.from_snapshot)
        if self.cognitive_dispatch is not None:
            _restore_component("cognitive_dispatch", "cognitive_dispatch", CognitiveDispatch.from_snapshot)
        if self.boredom_engine is not None:
            _restore_component("boredom_engine", "boredom_engine", BoredomEngine.from_snapshot)
        if self.social_emotion is not None:
            _restore_component("social_emotion", "social_emotion", SocialEmotionEngine.from_snapshot)
        if self.attachment_system is not None:
            _restore_component("attachment_system", "attachment_system", AttachmentSystem.from_snapshot)
        if self.reward_system is not None:
            _restore_component("reward_system", "reward_system", RewardSystem.from_snapshot)
        if self.autobiography is not None:
            _restore_component("autobiography", "autobiography", AutobiographicalNarrative.from_snapshot)
        if self.boundary is not None:
            _restore_component("boundary", "boundary", BoundaryEngine.from_snapshot)

        if isinstance(snapshot.get("exploration_executor"), dict):
            try:
                self.exploration_executor = ExplorationExecutor.from_snapshot(
                    snapshot["exploration_executor"]
                )
            except Exception as exc:
                self._record_loop_error("restore_exploration_executor", exc)

        # Scheduling timestamps are wall-clock values.  Restore them only
        # when valid; a missing value simply causes the corresponding task to
        # run on its next eligible cycle.
        maintenance = snapshot.get("maintenance", {})
        if not isinstance(maintenance, dict):
            maintenance = {}

        def _timestamp(name: str) -> float:
            value = maintenance.get(name, snapshot.get(name, 0.0))
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                return 0.0

        self.last_dream_time = _timestamp("last_dream_time")
        self.last_consolidation_time = _timestamp("last_consolidation_time")
        self.last_reflection = _timestamp("last_reflection")
        self.last_snapshot = _timestamp("last_snapshot")
        self.last_decay = _timestamp("last_decay")
        try:
            self._last_archived_count = max(0, int(snapshot.get("last_archived_count", 0)))
        except (TypeError, ValueError):
            self._last_archived_count = 0
        try:
            self.task_scheduler.sync(self.goal_system, self.state.total_ticks)
        except Exception as exc:
            self._record_loop_error("restore_task_scheduler", exc)

    async def _tick(self):
        """One tick of consciousness. V6: ActivationField drives state dynamics."""

        # A tick owns at most one queued request.  The ID is used by the
        # per-request future so an exception can be reported to the correct
        # caller instead of waking every caller at once.
        self._active_input_id = None
        autonomy_feedback_seen = False
        autonomy_followup_intent = False
        autonomy_feedback_success = False
        autonomy_feedback_tool = ""

        if self.autonomy:
            expired = self.autonomy.tick(self.state.total_ticks)
            if expired:
                self._finish_autonomy_episode(
                    expired,
                    False,
                    expired.outcome or "自主经历超时",
                )

        # ── V6: ActivationField tick — 状态扩散 + 基线回归 ──
        activation = self.state.activation
        activation.tick(dt=1.0)
        self.state.last_tick = datetime.now(timezone.utc).isoformat()

        # ── Step 0: Sleep state management ──
        was_asleep = self.sleep_state != "awake"
        self._update_sleep_state()
        if self.sleep_state != "awake" and not was_asleep:
            logger.info("brain-stem: entering %s", self.sleep_state)
        elif self.sleep_state == "awake" and was_asleep:
            logger.info("brain-stem: waking up")
            self.working_memory.push("Waking up. Resuming consciousness.", "brain_stem", 0.6)

        # ── Step 1: Check for input ──
        input_data = None
        previous_ticks_since_input = self.state.ticks_since_input
        previous_sleep_state = self.sleep_state
        try:
            input_data = self._pending_input.get_nowait()
            self._active_input_id = input_data.get("request_id")
            # Record the correlation metadata immediately, but defer session,
            # emotion, and wake-up changes until the boundary has accepted the
            # message.  A refused input must not be able to perturb the active
            # subject state merely by reaching the queue.
            self.state.last_input_id = self._active_input_id or ""
            self.state.last_input_source = input_data.get("source", "external")
            # An intent belongs to the current input.  Clear the previous one
            # before processing so a gated/failed input cannot return stale
            # output from an earlier turn.
            self.state.last_intent = None
            self.state.last_error = ""
            self.state.last_retrieved = []
            self.state.association_chain = []
        except asyncio.QueueEmpty:
            self.state.ticks_since_input += 1
            input_data = None

        # ── Step 2: Thalamus — sensory relay ──
        inner_signal = self.default_mode.get_recent_thoughts(1)
        inner_text = inner_signal[0] if inner_signal else None

        thalamus_out = self.thalamus.relay(
            input_text=input_data["text"] if input_data else None,
            inner_signal=inner_text,
            source=input_data.get("source", "external") if input_data else "internal",
        )

        # Apply the self-boundary immediately after sensory normalization.  A
        # refused message must not alter emotional state, prediction history,
        # or long-term-memory access counters.
        boundary_accepted = True
        boundary_reason = "accepted"
        if self.boundary and input_data and thalamus_out.get("has_input"):
            boundary_accepted, boundary_reason = self.boundary.should_accept_input(
                source=thalamus_out.get("source", input_data.get("source", "external")),
                text=thalamus_out.get("text", ""),
                cognitive_load=self.metacognition.cognitive_load,
                attachment_system=self.attachment_system,
            )

        refused_input = bool(
            input_data and thalamus_out.get("has_input") and not boundary_accepted
        )

        # A new accepted subject input preempts an in-flight autonomous action;
        # a rejected message does not get to rewrite the episode timeline.
        if (
            input_data
            and not refused_input
            and self.autonomy
            and self.autonomy.is_active
            and not str(input_data.get("source", "")).startswith("agent/")
        ):
            interrupted = self.autonomy.abort(
                "外部输入打断自主经历",
                self.state.total_ticks,
            )
            self._finish_autonomy_episode(interrupted, False, "外部输入打断")

        # Commit per-source context only after the input boundary has passed.
        # Rejected traffic remains observable as a boundary event, but cannot
        # switch the active session, wake the subject, or reset its idle clock.
        if input_data and not refused_input:
            self.state.ticks_since_input = 0
            source_id = input_data.get("source", "default")
            session = self.state.session_manager.get(source_id)
            session.last_active = datetime.now(timezone.utc).isoformat()
            # Replace, rather than update, the shared view.  Updating left
            # custom keys from the previous source alive when sessions used
            # different emotion dimensions, causing cross-session leakage.
            self.state.emotion_vector = dict(session.emotion_vector)
            self.state.current_emotion = session.current_emotion
            self.state.focus_entity = session.focus_entity
            self.state.inner_monologue = session.inner_monologue
            self.state.current_goal = input_data.get("goal") or None
            # Always replace the shared working-memory view, including with an
            # empty list.  Only copying non-empty sessions allowed source A's
            # context to bleed into a newly activated source B.
            self.working_memory.items = [dict(it) for it in session.working_memory.items]
            self.working_memory.context_text = session.working_memory.context_text
            explicit_goal = input_data.get("goal")
            if (
                explicit_goal
                and not str(input_data.get("source", "")).startswith("agent/")
            ):
                user_goal = self.task_scheduler.submit_user_goal(
                    self.goal_system,
                    str(explicit_goal),
                    current_tick=self.state.total_ticks,
                    source=str(input_data.get("source", "user")),
                )
                if user_goal is not None:
                    self.working_memory.push(
                        content="[用户任务] {0}".format(user_goal.description[:120]),
                        source="task_scheduler",
                        base_salience=0.65,
                    )
                else:
                    self.working_memory.push(
                        content="[任务队列] 已满，未接收新的用户任务",
                        source="task_scheduler",
                        base_salience=0.55,
                    )
            if self.sleep_state != "awake":
                logger.info("brain-stem: input received, waking from %s", self.sleep_state)
                self.sleep_state = "awake"
                self.state.sleep_state = self.sleep_state
        elif refused_input:
            self.state.ticks_since_input = previous_ticks_since_input + 1
            self.sleep_state = previous_sleep_state
            self.state.sleep_state = self.sleep_state

        # Register tool feedback before cognitive dispatch so a follow-up
        # intent can advance the same episode instead of racing its status.
        if input_data and not refused_input and str(
            input_data.get("source", "")
        ).startswith("agent/tool/"):
            feedback_text = (input_data.get("text", "") or "").lower()
            autonomy_feedback_success = not any(
                keyword in feedback_text
                for keyword in ["失败", "error", "错误", "exception", "traceback"]
            )
            autonomy_feedback_tool = str(input_data.get("source", ""))[len("agent/tool/"):]
            autonomy_feedback_seen = self._record_autonomy_feedback(
                input_data,
                autonomy_feedback_success,
                autonomy_feedback_tool,
            )

        # ── V9 Predictive Layer: 在感知之前生成预测 ──
        # Build an expectation only for an accepted message.  A rejected
        # message must not train or mutate the predictive subsystem.
        if self.predictive_layer and input_data and not refused_input:
            wm_entities = [
                item.get("content", "")[:30]
                for item in self.working_memory.items[-3:]
            ]
            self.predictive_layer.build_expectation(
                current_tick=self.state.total_ticks,
                entities_from_wm=wm_entities,
                time_sense=self.time_sense,
            )

        # ── Step 3: Amygdala — emotion ──
        if refused_input:
            # Do not call Amygdala with an empty string: that path applies
            # emotion decay and changes short-term state for an input the
            # subject explicitly refused.  Keep a neutral event envelope for
            # downstream formatting without mutating the live values.
            amygdala_out = {
                "emotion_label": self.state.current_emotion,
                "emotion_vector": dict(self.state.emotion_vector),
                "emotional_tags": [],
                "salience": self.amygdala.salience,
            }
        elif thalamus_out["has_input"] and not thalamus_out["discarded"]:
            amygdala_out = self.amygdala.evaluate(
                text=thalamus_out["text"],
                current_state={"current_emotion": self.state.current_emotion, "emotion_vector": self.state.emotion_vector},
                activation=activation,  # V6
            )
            self.state.emotion_vector.update(amygdala_out["emotion_vector"])
            self.state.current_emotion = amygdala_out["emotion_label"]
        else:
            # No input — decay emotion
            amygdala_out = self.amygdala.evaluate(
                text="",
                current_state={"current_emotion": self.state.current_emotion, "emotion_vector": self.state.emotion_vector},
            )
            self.state.emotion_vector.update(amygdala_out["emotion_vector"])

        self.state.emotion_history.append({
            "tick": self.state.total_ticks,
            "emotion": self.state.current_emotion,
            "vector": dict(self.state.emotion_vector),
        })
        self.state.emotion_history = self.state.emotion_history[-50:]

        # ── V9 Predictive Layer: 计算预测误差，surprise → salience boost ──
        surprise_salience = 0.0
        if self.predictive_layer and boundary_accepted and input_data and thalamus_out["has_input"]:
            emotion_vec = amygdala_out.get("emotion_vector", {})
            error = self.predictive_layer.observe_and_compute(
                expectation=self.predictive_layer.last_expectation,
                input_data=input_data,
                emotion_vector=emotion_vec,
                ticks_since_input=self.state.ticks_since_input,
            )
            if error.is_surprising:
                # salience 不再只依赖 LLM — 系统自己感受到惊讶
                surprise_salience = self.predictive_layer.salience_from_surprise
                amygdala_out["salience"] = max(
                    amygdala_out.get("salience", 0.0),
                    surprise_salience,
                )
                # 惊讶事件写入工作记忆
                _ = self.predictive_layer.handle_surprise(
                    error=error,
                    working_memory=self.working_memory,
                    curiosity=self.state.curiosity,
                    hippocampus=self.hippocampus,
                    exploration_queue=self.exploration_queue,
                )
                logger.debug("brain-stem: predictive error=%.3f salience_boost=%.3f",
                           error.total_error, surprise_salience)

        # ── Step 4-8: LLM Processing (V9 dispatch or legacy unified) ──
        hippocampus_out = None
        encoded_memory = None
        input_source = thalamus_out.get("source", "none")
        # All non-internal sources are externally supplied from the brain's
        # point of view (creator/user/tool are distinct for policy, but none
        # should be silently downgraded to the legacy literal "external").
        is_external = bool(input_data) and input_source not in {"internal", "none"}
        is_subject_input = is_external and not input_source.startswith("agent/")
        dmn_out = None

        # Reset input tracking at start of each tick
        self.state.last_input_accepted = False
        self.state.last_input_gated = True
        
        gate = {"passed": False}
        if thalamus_out["has_input"] and not thalamus_out["discarded"]:
            if refused_input:
                # Keep only a minimal, non-content-bearing audit marker.  The
                # rejected text itself must not enter working memory.
                self.working_memory.push(
                    content=f"[边界] 已拒绝来自 {thalamus_out['source']} 的输入（原因: {boundary_reason}）",
                    source="boundary",
                    base_salience=0.25,
                )
            else:
                # Do not touch long-term memory until the input boundary has
                # accepted the message.  Retrieval can update access counters
                # and expose hit counts, so doing it first leaked side effects
                # from a request the subject had already refused.
                hippocampus_out = await self.hippocampus.retrieve(
                    query=thalamus_out["text"],
                    top_k=5,
                )
                self.state.last_retrieved = [
                    str(item.get("id", item.get("title", "")))
                    for item in hippocampus_out.get("results", [])
                    if isinstance(item, dict)
                ][-20:]

                # Gate check: should we process this?
                attn_boost = self.state.self_model.attention_weight(thalamus_out["text"])
                attn_importance = min(0.5 + attn_boost * 0.1, 1.0)

                gate = gate_check(
                    text=thalamus_out["text"],
                    importance=attn_importance,
                    novelty=0.5,
                    goal_relevance=GATE_GOAL_RELEVANCE_WITH_GOAL if self.state.current_goal else GATE_GOAL_RELEVANCE_DEFAULT,
                    explicit_mark=amygdala_out.get("salience", 0) > 0.7,
                )

            # Track gate result for API response (v5.0).  Refused input stays
            # gated without invoking the regular attention gate.
            self.state.last_input_gated = not gate["passed"]
            self.state.last_input_accepted = gate["passed"]
            self.state.last_error = ""

            # LLM Processing: encoding + focus + monologue + intent
            # V9: 多通道认知调度；V5-V8: 统一单次调用
            if is_external and gate["passed"]:
                try:
                    from services.llm_client import get_llm
                    llm = get_llm()

                    # ── Build shared context (both paths need this) ──
                    tools_summary = ""
                    try:
                        from agent.tool_registry import registry as tool_reg
                        tools = tool_reg.get_enabled()
                        if tools:
                            tools_list = []
                            for t in tools[:10]:
                                ro = "只读" if t.is_read_only else "读写"
                                tools_list.append(f"  {t.emoji} {t.name} [{ro}]: {t.description[:120]}")
                            tools_summary = "\n".join(tools_list)
                    except Exception:
                        pass

                    id_mems = self.memory_store.get_identity_memories(5) if self.memory_store else []
                    id_context = self.state.self_model.identity_memories_context(id_mems, max_items=3)
                    temporal_ctx = self.time_sense.get_short_temporal_context()
                    skill_hint = self.procedural_memory.get_skill_suggestion(thalamus_out["text"])

                    # ── V9: Cognitive Dispatch (多通道) vs Legacy Unified (单次调用) ──
                    if self.cognitive_dispatch:
                        # 使用多通道认知调度器
                        cog_result = await self.cognitive_dispatch.dispatch(
                            text=thalamus_out["text"],
                            source=thalamus_out["source"],
                            goal=self.state.current_goal,
                            current_emotion={
                                "valence": self.emotional_spectrum.valence,
                                "arousal": self.emotional_spectrum.arousal,
                                "dominance": self.emotional_spectrum.dominance,
                            },
                            recent_thoughts=self.working_memory.get_context()[:300],
                            identity_anchor=self.state.self_model.identity_anchor[:300],
                            identity_memories_context=id_context,
                            top_drives=[
                                d["label"] for d in self.state.self_model.get_top_drives(2)
                            ],
                            tools_summary=tools_summary or "",
                            temporal_context=temporal_ctx,
                            skill_hint=skill_hint or "",
                            llm_client=llm,
                        )
                        # 转换为与旧 unified 格式兼容的 dict
                        unified = cog_result.to_unified_dict()

                        # ── V9: 规则引擎情绪已在 dispatch 中计算，跳过 LLM 情绪摄入 ──
                        # 直接用规则引擎的 VAD 值（不做 LLM 情绪混合）
                        llm_emotion = unified.get("emotion", {})
                        if llm_emotion:
                            v = float(llm_emotion.get("valence", 0.5))
                            a = float(llm_emotion.get("arousal", 0.5))
                            d = float(llm_emotion.get("dominance", 0.5))
                            u = float(llm_emotion.get("urgency", 0.0))
                            label = llm_emotion.get("label", "neutral")

                            # V9: 规则引擎情绪权重低于 LLM 情绪但更高频更新
                            self.emotional_spectrum.ingest_llm_emotion(v, a, d, label, u, activation=activation)
                            # 同步杏仁核（使用与旧路径相同的混合逻辑）
                            prev_v = self.amygdala.valence
                            prev_a = self.amygdala.arousal
                            prev_d = self.amygdala.dominance
                            keep_ratio = 1.0 - LLM_EMOTION_BLEND_RATIO
                            self.amygdala.valence = prev_v * keep_ratio + v * LLM_EMOTION_BLEND_RATIO
                            self.amygdala.arousal = prev_a * keep_ratio + a * LLM_EMOTION_BLEND_RATIO
                            self.amygdala.dominance = prev_d * keep_ratio + d * LLM_EMOTION_BLEND_RATIO
                            self.amygdala.salience = a * 0.4 + u * 0.6

                            amygdala_out["emotion_label"] = label
                            amygdala_out["emotion_vector"] = {
                                "valence": round(self.emotional_spectrum.valence, 3),
                                "arousal": round(self.emotional_spectrum.arousal, 3),
                                "dominance": round(self.emotional_spectrum.dominance, 3),
                                "urgency": u,
                                "salience": round(self.amygdala.salience, 3),
                            }
                            amygdala_out["salience"] = self.amygdala.salience
                            self.state.emotion_vector.update(amygdala_out["emotion_vector"])
                            self.state.current_emotion = self.emotional_spectrum.dominant_emotion
                    else:
                        # ── Legacy: 统一 LLM 调用（V5-V8 路径）──
                        from services.llm_prompts import UNIFIED_TICK_PROMPT

                        ctx_obj = {
                            "text": thalamus_out["text"][:3000],
                            "goal": self.state.current_goal or "none",
                            "emotion": amygdala_out.get("emotion_label", "neutral"),
                            "salience": amygdala_out.get("salience", 0.0),
                            "recent_thoughts": self.working_memory.get_context()[:300],
                            "identity": self.state.self_model.identity_anchor[:300],
                            "identity_memories": id_context,
                            "top_drives": [
                                d["label"] for d in self.state.self_model.get_top_drives(2)
                            ],
                        }
                        if tools_summary:
                            ctx_obj["available_tools"] = tools_summary
                        ctx_obj["temporal_context"] = temporal_ctx
                        if skill_hint:
                            ctx_obj["skill_memory"] = skill_hint
                        ctx = json.dumps(ctx_obj, ensure_ascii=False)

                        unified = await llm.chat_json(system=UNIFIED_TICK_PROMPT, user=ctx, temperature=0.1, max_tokens=1024)

                    # ── LLM Emotion — v5.3: 情感光谱摄入（替代旧杏仁核混合）──
                    llm_emotion = unified.get("emotion", {})
                    if llm_emotion:
                        v = float(llm_emotion.get("valence", 0.5))
                        a = float(llm_emotion.get("arousal", 0.5))
                        d = float(llm_emotion.get("dominance", 0.5))
                        u = float(llm_emotion.get("urgency", 0.0))
                        label = llm_emotion.get("label", "neutral")

                        # v5.3: 情感光谱摄入（带动量平滑）→ V6: 写入 ActivationField
                        self.emotional_spectrum.ingest_llm_emotion(v, a, d, label, u, activation=activation)

                        # 向后兼容：同步杏仁核（旧系统）
                        prev_v = self.amygdala.valence
                        prev_a = self.amygdala.arousal
                        prev_d = self.amygdala.dominance
                        keep_ratio = 1.0 - LLM_EMOTION_BLEND_RATIO
                        self.amygdala.valence = prev_v * keep_ratio + v * LLM_EMOTION_BLEND_RATIO
                        self.amygdala.arousal = prev_a * keep_ratio + a * LLM_EMOTION_BLEND_RATIO
                        self.amygdala.dominance = prev_d * keep_ratio + d * LLM_EMOTION_BLEND_RATIO
                        self.amygdala.salience = a * 0.4 + u * 0.6

                        amygdala_out["emotion_label"] = label
                        amygdala_out["emotion_vector"] = {
                            "valence": round(self.emotional_spectrum.valence, 3),
                            "arousal": round(self.emotional_spectrum.arousal, 3),
                            "dominance": round(self.emotional_spectrum.dominance, 3),
                            "urgency": u,
                            "salience": round(self.amygdala.salience, 3),
                        }
                        amygdala_out["salience"] = self.amygdala.salience
                        self.state.emotion_vector.update(amygdala_out["emotion_vector"])
                        self.state.current_emotion = self.emotional_spectrum.dominant_emotion

                    # Encode
                    enc = unified.get("encoding", {})
                    raw_ents = enc.get("entities", [])
                    entities = [e for e in raw_ents if e and e in thalamus_out["text"]]
                    mem = {
                        "type": enc.get("type", "episodic"),
                        "title": enc.get("title", thalamus_out["text"][:80]),
                        "content": thalamus_out["text"][:4000],
                        "entities": entities,
                        "emotion_tags": amygdala_out.get("emotional_tags", []),
                        "importance": max(0.0, min(1.0, float(enc.get("importance", 0.5)))),
                        "summary": enc.get("summary", thalamus_out["text"][:80]),
                        "source": thalamus_out["source"],
                        "emotion_label": amygdala_out.get("emotion_label", "neutral"),
                        "emotion_vector": amygdala_out.get("emotion_vector", {}),
                        "created": datetime.now(timezone.utc).isoformat(),
                        "llm_encoded": True,
                    }
                    if self.memory_store:
                        mem_id = self.memory_store.save(mem)
                        mem["id"] = mem_id
                    encoded_memory = mem

                    # ── Self-model: ingest this experience ──
                    if is_subject_input and mem.get("importance", 0) > 0:
                        shift = self.state.self_model.ingest_experience(
                            text=thalamus_out["text"][:500],
                            emotion={
                                "emotion_label": amygdala_out.get("emotion_label", "neutral"),
                                "emotion_vector": amygdala_out.get("emotion_vector", {}),
                                "salience": amygdala_out.get("salience", 0.0),
                            },
                            importance=mem.get("importance", 0.5),
                            memory_count=self.memory_store.count() if self.memory_store else 0,
                        )

                        # ── Identity memory: two paths ──
                        # Path A: significance high → identity shift + fact ingestion
                        if shift and self.memory_store:
                            mem["is_identity_forming"] = True
                            mem["importance"] = max(mem["importance"], 0.75)
                            try:
                                self.memory_store.save(mem)
                            except Exception:
                                pass
                            # ingest fact
                            fact = (mem.get("summary", "") or mem.get("title", ""))[:120]
                            if fact and len(fact) > 3:
                                self.state.self_model.ingest_identity_fact(
                                    fact=fact, memory_id=mem.get("id", ""),
                                    confidence=mem.get("importance", 0.75))

                        # Path B: LLM marked as important (≥0.7) but significance < 0.3
                        # → still ingest identity fact (e.g. naming, relationship)
                        elif mem.get("importance", 0) >= 0.7 and self.memory_store:
                            mem["is_identity_forming"] = True
                            try:
                                self.memory_store.save(mem)
                            except Exception:
                                pass
                            fact = (mem.get("summary", "") or mem.get("title", ""))[:120]
                            if fact and len(fact) > 3:
                                self.state.self_model.ingest_identity_fact(
                                    fact=fact, memory_id=mem.get("id", ""),
                                    confidence=mem.get("importance", 0.7))

                        # ── Curiosity: check if this resolves any open questions ──
                        entities = mem.get("entities", [])
                        resolved = self.state.curiosity.check_resolution(thalamus_out["text"], entities)
                        if resolved:
                            for rq in resolved:
                                self.working_memory.push(
                                    content="问题已解答: {0}".format(rq["question"][:100]),
                                    source="curiosity",
                                    base_salience=0.6,
                                )

                        # ── Curiosity: update exploration topics ──
                        self.state.curiosity.update_exploration_topics(entities)

                        # ── V10 Social Self: 互动社会情感评估 ──
                        if self.social_emotion and self.attachment_system and is_subject_input:
                            source = thalamus_out["source"]
                            other = self.attachment_system.get_or_create(source)
                            sentiment = amygdala_out.get("emotion_vector", {}).get("valence", 0.5)
                            # 将当前体验的情感映射为"他们对我的态度"
                            perceived_sentiment = (sentiment - 0.5) * 1.5  # 放大
                            other.record_interaction(
                                sentiment=perceived_sentiment,
                                impression=thalamus_out["text"][:80],
                            )
                            _ = self.social_emotion.evaluate_interaction(
                                self_model=self.state.self_model,
                                other=other,
                                my_action="",
                                their_response=thalamus_out["text"][:80],
                                their_sentiment=perceived_sentiment,
                                was_ignored=False,
                            )

                        # ── V10 Autobiographical: 转折点检测 ──
                        if self.autobiography and is_subject_input and mem.get("importance", 0) > 0:
                            tp = self.autobiography.detect_turning_point(
                                experience={
                                    "significance": shift.get("significance", 0) if shift else 0,
                                    "importance": mem.get("importance", 0),
                                    "text_snippet": thalamus_out["text"][:100],
                                    "emotion_label": amygdala_out.get("emotion_label", "neutral"),
                                    "reflection": shift.get("reflection", "") if shift else "",
                                },
                                identity_shift=shift if shift else None,
                                emotion_vector=amygdala_out.get("emotion_vector", {}),
                                current_tick=self.state.total_ticks,
                            )
                            if tp:
                                # 里程碑 → 奖励
                                if self.reward_system:
                                    self.reward_system.deliver_reward(
                                        channel="cognitive",
                                        actual_reward=0.8,
                                        context="turning_point",
                                    )
                    
                    # Focus
                    self.state.focus_entity = unified.get("focus", thalamus_out["text"][:80])
                    
                    # Monologue
                    monologue = unified.get("monologue", "")
                    if monologue:
                        self.state.inner_monologue = monologue
                        dmn_out = {"thought": monologue, "llm_used": True}

                    # ── Intent: parse and queue for agent layer (v5.0) ──
                    intent_data = unified.get("intent")
                    intent = Intent.from_llm_output(
                        intent_data,
                        source_input=thalamus_out["text"][:200],
                    )
                    if intent:
                        if (
                            input_data
                            and str(input_data.get("source", "")).startswith("agent/tool/")
                            and self.autonomy
                            and self.autonomy.is_active
                        ):
                            autonomy_followup_intent = self._observe_autonomy_intent(intent)
                        # V8: 行为倾向特质调制 intent 置信度
                        intent.confidence = self.state.self_model.modulate_intent(
                            intent.type.value, intent.confidence)
                        # v5.2: feed intent to metacognition for tracking
                        self.metacognition.feed_intent(
                            intent.type.value, intent.confidence,
                            intent.tool_name or "",
                        )

                    if intent and intent.type.value in ("call_tool", "respond"):
                        self.state.last_intent = intent.to_dict()
                        self.state.intent_count += 1
                        await self.intent_queue.put(intent)
                        logger.info(
                            "brain-stem: intent produced — %s (confidence=%.2f)",
                            intent.type.value, intent.confidence,
                        )
                    elif intent:
                        # think / ask_question — track but don't push to agent
                        self.state.last_intent = intent.to_dict()
                        self.state.intent_count += 1

                    logger.debug("brain-stem: unified LLM tick — encode+focus+monologue+intent in one call")
                except Exception as e:
                    err_msg = str(e)[:200]
                    logger.warning("brain-stem: unified LLM call failed: %s", err_msg)
                    self.state.last_error = err_msg
                    self.state.llm_error_count += 1
                    self.state.last_input_accepted = False
                    # v5.2: metacognition — LLM failure is a negative outcome
                    self.metacognition.feed_outcome(False, 0.0)
            
            # Chain association (async, fire and forget)
            if encoded_memory:
                chain = await self.hippocampus.associate(
                    query=thalamus_out["text"],
                    previous_results=hippocampus_out.get("results", []) if hippocampus_out else [],
                )
                self.state.association_chain = chain
        
        # Default mode + Curiosity — idle tick inner monologue + spontaneous thinking
        if not is_external and self.state.ticks_since_input >= 5:
            wm = self.working_memory.get_context()

            # ── Curiosity: spontaneous thought every ~10 idle ticks ──
            if self.state.ticks_since_input % 10 == 0:
                top_drives = self.state.self_model.get_top_drives(2)
                thought = self.state.curiosity.spontaneous_think(wm, top_drives)
                if thought:
                    self.state.inner_monologue = thought[:200]
                    self.working_memory.push(thought[:200], "curiosity", 0.35)
                elif wm:
                    self.state.inner_monologue = "Thinking: " + wm[:100]

            # ── Curiosity: generate new questions when running low ──
            if (self.state.curiosity.pending_count < 5
                    and self.state.ticks_since_input % 20 == 0
                    and self.memory_store
                    and self.memory_store.count() > 0):
                recent_entities = self.state.self_model.identity_traits + (
                    self.state.curiosity.exploration_topics[-5:]
                    if self.state.curiosity.exploration_topics else []
                )
                new_qs = self.state.curiosity.generate_questions(
                    top_drives=self.state.self_model.get_top_drives(3),
                    recent_entities=recent_entities,
                )
                if new_qs:
                    first_q = new_qs[0]["question"]
                    self.state.inner_monologue = "[好奇] {0}".format(first_q[:150])

            # V11: periodically turn persistent internal signals into a
            # bounded candidate goal.  Generation is throttled and deduped in
            # _tick_drive_engine; it no longer waits for a 10-minute deep
            # reflection before the subject can initiate an action.
            if (
                AUTONOMY_ENABLED
                and self.sleep_state == "awake"
                and AUTONOMY_GOAL_INTERVAL_TICKS > 0
                and self.state.total_ticks > 0
                and self.state.total_ticks % AUTONOMY_GOAL_INTERVAL_TICKS == 0
            ):
                self._tick_drive_engine(activation)

            # ── v5.1 Goal System: tick active goals, produce intent if actionable ──
            if self.state.ticks_since_input % 15 == 0 and self.sleep_state == "awake":
                # The long-term scheduler owns selection.  There is still one
                # causal lane for autonomous work; a running episode cannot be
                # preempted until its feedback reaches a safe boundary.
                active_episode = self.autonomy.active if self.autonomy else None
                episode_busy = bool(
                    active_episode
                    and active_episode.status not in {
                        EpisodeStatus.COMPLETED,
                        EpisodeStatus.FAILED,
                        EpisodeStatus.ABORTED,
                    }
                )
                active_goal = (
                    self.task_scheduler.select(
                        self.goal_system,
                        self.state.total_ticks,
                        episode_busy=True,
                    )
                    if episode_busy
                    else self.task_scheduler.claim_for_execution(
                        self.goal_system,
                        self.state.total_ticks,
                    )
                )
                if active_goal and active_goal.status == "active":
                    episode = None
                    if self.autonomy and not episode_busy:
                        episode = self.autonomy.begin(
                            goal_id=active_goal.id,
                            goal=active_goal.description,
                            drive=getattr(active_goal, "source_drive", "") or active_goal.drive,
                            trigger="drive",
                            tick=self.state.total_ticks,
                            attempt_no=active_goal.attempt_count,
                        )

                    # Convert goal to a CALL_TOOL intent if we have tools to execute it
                    can_issue = (self.autonomy is None) or (episode is not None)
                    goal_intent = (
                        self._goal_to_intent(
                            active_goal,
                            episode_id=episode.id if episode else (
                                active_episode.id if episode_busy else None
                            ),
                            attempt_no=getattr(active_goal, "attempt_count", 0),
                        )
                        if can_issue and not episode_busy else None
                    )
                    if goal_intent:
                        if episode:
                            self.autonomy.plan(
                                episode.id,
                                goal_intent.type.value,
                                goal_intent.tool_name or "",
                                self.state.total_ticks,
                                goal_intent.reason,
                                intent_id=goal_intent.intent_id,
                                expected=f"完成目标：{active_goal.description[:180]}",
                                attempt_no=goal_intent.attempt_no,
                            )
                            if self.reward_system:
                                self.reward_system.anticipate(
                                    channel="achievement",
                                    expectation=max(
                                        0.0,
                                        min(1.0, float(getattr(active_goal, "priority", 0.5))),
                                    ),
                                )
                        self.state.last_intent = goal_intent.to_dict()
                        self.state.intent_count += 1
                        queued = await self.intent_queue.put(goal_intent)
                        if not queued and episode:
                            failed = self.autonomy.fail(
                                "意图队列已满，行动未交付",
                                self.state.total_ticks,
                            )
                            self._finish_autonomy_episode(
                                failed,
                                False,
                                "意图队列已满，行动未交付",
                            )
                        self.working_memory.push(
                            content="[目标驱动] {0}".format(active_goal.description[:100]),
                            source="goal_system",
                            base_salience=0.5,
                        )
                        # v5.2: metacognition tracks goal-driven intents
                        self.metacognition.feed_intent(
                            goal_intent.type.value, goal_intent.confidence,
                            goal_intent.tool_name or "",
                        )
                        logger.info("brain-stem: goal-driven intent — %s", active_goal.description[:60])

            # ── v5.2 Metacognition: idle pattern detection ──
            if self.state.ticks_since_input % 30 == 0:
                self.metacognition.tick_idle()

            # ── V6: 元认知校准扩散权重（每30个空闲tick）──
            if self.state.ticks_since_input % 30 == 0:
                self.metacognition.calibrate_diffusion(activation)

            # ── V8: 探索循环 — 发现问题 → 创建任务（基于总tick，不只是空闲）──
            if self.state.total_ticks % 15 == 0 and self.state.total_ticks > 0:
                issues = self.exploration_executor.find_issues(self)
                if issues:
                    new_count = self.exploration_executor.issues_to_tasks(
                        issues, self.exploration_queue)
                    if new_count > 0:
                        self.working_memory.push(
                            content=f"[探索] 发现 {new_count} 个新问题",
                            source="exploration",
                            base_salience=0.35,
                        )

            # ── V8: 反思引擎 — 周期检查（基于总tick）──
            if self.state.total_ticks % 30 == 0 and self.state.total_ticks > 0:
                ref = self.reflection_engine.reflect(
                    self, self.memory_store, self.state.total_ticks)
                if ref:
                    self.working_memory.push(
                        content=f"[反思] {ref.get('summary', '')[:100]}",
                        source="reflection_engine",
                        base_salience=0.4,
                    )

            # ── v5.4 Procedural Memory: periodic decay ──
            if self.state.total_ticks % 300 == 0 and self.state.total_ticks > 0:
                self.procedural_memory.decay_skills()

            # ── v5.4 Time Sense: record important events ──
            if input_data and is_subject_input and not refused_input:
                self.time_sense.record_event("input", thalamus_out.get("text", "")[:80])

        # ── v5.2: detect tool result inputs (agent feedback loop) and feed outcomes ──
        if input_data and not refused_input and input_data.get("source", "").startswith("agent/tool/"):
            # Tool result came back — if text doesn't contain error, treat as success
            success = autonomy_feedback_success
            tool = autonomy_feedback_tool or input_data.get("source", "").replace("agent/tool/", "")
            self.metacognition.feed_outcome(success, 0.6)
            # v5.4: record experience for procedural memory
            self.procedural_memory.record_experience("call_tool", tool, success, input_data.get("text", "")[:200])
            # V10: 工具结果 → 奖励交付
            # Autonomous episodes receive one richer, correlated reward in
            # ``_finish_autonomy_episode``.  Avoid double-counting the same
            # tool result here; external tool traffic keeps the legacy path.
            if self.reward_system and not autonomy_feedback_seen:
                actual_reward = 0.7 if success else 0.2
                self.reward_system.deliver_reward(
                    channel="achievement",
                    actual_reward=actual_reward,
                    context=f"tool:{tool}",
                )

            # In offline/rule-only mode no follow-up LLM intent may be
            # produced.  A successfully observed tool result is still a
            # complete one-step episode; close it here rather than leaving a
            # goal permanently in ``feedback_received``.
            if (
                autonomy_feedback_seen
                and self.autonomy
                and self.autonomy.is_active
                and not autonomy_followup_intent
            ):
                active = self.autonomy.active
                outcome = f"工具 {tool} {'执行成功' if success else '返回失败'}"
                record = (
                    self.autonomy.complete(outcome, self.state.total_ticks)
                    if success
                    else self.autonomy.fail(outcome, self.state.total_ticks)
                )
                self._finish_autonomy_episode(
                    record,
                    success,
                    outcome,
                )

        # ── v5.3: 情感光谱 tick（每个 tick 漂移一步）──
        self.emotional_spectrum.tick()

        # ── v5.4: 时间感 tick ──
        has_recent_activity = (input_data is not None and not refused_input) or self.state.ticks_since_input < 10
        self.time_sense.tick(has_recent_activity, self.state.total_ticks, self.state.uptime_seconds)

        # ── V9 Boredom Engine: 无聊评估 + 行为触发 ──
        if self.boredom_engine:
            boredom_result = self.boredom_engine.tick(
                activation=activation,
                ticks_since_input=self.state.ticks_since_input,
                exploration_queue=self.exploration_queue,
                working_memory=self.working_memory,
                memory_store=self.memory_store,
                curiosity=self.state.curiosity,
                sleep_state=self.sleep_state,
                current_tick=self.state.total_ticks,
            )
            if boredom_result.get("actions"):
                logger.debug("brain-stem: boredom=%.2f(%s) actions=%s",
                           boredom_result["score"], boredom_result["level"],
                           boredom_result["actions"])

        # ── V10: 社会情感 + 奖励系统 + 边界 tick ──
        if self.social_emotion:
            self.social_emotion.tick()
        if self.reward_system:
            self.reward_system.tick()
        if self.boundary:
            self.boundary.tick(cognitive_load=self.metacognition.cognitive_load)
        if self.autobiography:
            self.autobiography.update_chapters(
                current_tick=self.state.total_ticks,
                total_experiences=self.state.self_model.total_experiences,
            )

        # ── v5.2: update cognitive load ──
        has_external_input = (
            input_data is not None
            and not refused_input
            and not (input_data.get("source", "") or "").startswith("agent/")
        )
        self.metacognition.update_cognitive_load(
            llm_called=(is_external and not refused_input and thalamus_out.get("has_input") and not thalamus_out.get("discarded", True)),
            input_processed=has_external_input,
            recent_input_count=min(10, self.state.intent_count),
            activation=activation,  # V6: 同步 fatigue/uncertainty/confidence 到 ActivationField
        )

        # ── Step 6: Basal Ganglia — habit match ──
        habit_out = None
        if thalamus_out["has_input"] and not refused_input:
            habit_out = self.basal_ganglia.match(thalamus_out["text"])
            if habit_out and habit_out.get("matched"):
                self.state.active_habit = habit_out["habit"]["id"]
                self.state.habit_confidence = habit_out["confidence"]

        # ── Step 7: Cingulate — conflict monitor ──
        if refused_input:
            # A refused message is not evidence of an internal conflict.  Do
            # not let hostile text weaken habits or overwrite prior conflict
            # state through the normal cingulate path.
            cingulate_out = {
                "conflict_detected": False,
                "conflicts": [],
                "error_count": self.state.error_count,
            }
        else:
            cingulate_out = self.cingulate.monitor(
                input_text=thalamus_out.get("text", ""),
                emotion=amygdala_out,
                hippocampus_result=hippocampus_out,
                previous_state={"current_emotion": self.state.current_emotion},
            )
        if cingulate_out.get("conflict_detected"):
            self.state.conflict_detected = True
            self.state.conflict_detail = str(cingulate_out.get("conflicts", [])[:2])
            self.state.error_count = cingulate_out.get("error_count", 0)
            if self.state.active_habit:
                self.basal_ganglia.weaken(self.state.active_habit, delta=0.1)

        # ── Working Memory — update ──
        if thalamus_out["has_input"] and not thalamus_out["discarded"] and not refused_input:
            wm_content = thalamus_out["text"][:500]
            if encoded_memory and is_external:
                wm_content = "[记忆: {0}] {1}".format(encoded_memory.get("title", ""), wm_content[:400])
            self.working_memory.push(content=wm_content, source=thalamus_out["source"], base_salience=amygdala_out.get("salience", 0.5))
        if self.state.inner_monologue:
            self.working_memory.push(content=self.state.inner_monologue, source="inner_monologue", base_salience=0.3)
        self.working_memory.tick(activation=activation)  # V6: SalienceScore竞争保留
        self.state.current_context = self.working_memory.get_context() or self.working_memory.context_text
        self.state.active_thoughts = [dict(item) for item in self.working_memory.items]
        self.state.goal_stack = [
            goal.description[:200] for goal in self.goal_system.get_active()
        ][-20:]
        if input_data and not refused_input:
            self.state.attention_span_ticks = 0
        else:
            self.state.attention_span_ticks += 1

        # ── Session sync: save per-source state ──
        if input_data and not refused_input:
            source_id = input_data.get("source", "default")
            session = self.state.session_manager.get(source_id)
            session.current_emotion = self.state.current_emotion
            session.emotion_vector = dict(self.state.emotion_vector)
            session.focus_entity = self.state.focus_entity
            session.inner_monologue = self.state.inner_monologue
            session.current_context = self.state.current_context
            # Save working memory by copying items (v5.0 fix: copy, not reference swap)
            session.working_memory.items = [dict(it) for it in self.working_memory.items]
            session.working_memory.context_text = self.working_memory.context_text
        
        # Signal input processed (always, even if discarded).  Keep the old
        # event for compatibility with legacy callers, but resolve the
        # correlated future as the authoritative completion signal.
        self._complete_input_waiter(input_data)
        self._input_processed.set()
        self._input_processed.clear()

        # ── Step 10: Output push ──
        output_event = self._build_output_event(
            thalamus_out, amygdala_out, hippocampus_out,
            cingulate_out, dmn_out,
        )
        if output_event:
            try:
                self._output_feed.put_nowait(output_event)
            except asyncio.QueueFull:
                pass

    async def _reflection_tick(self):
        """Scheduled reflection — uses self_model for identity-aware thinking."""
        wm_context = self.working_memory.get_context()
        sm = self.state.self_model

        # ── Self-model guided reflection ──
        top_drives = sm.get_top_drives(2)
        drives_str = ", ".join(d["label"] for d in top_drives)

        if wm_context:
            self.state.inner_monologue = "[{0}] {1}".format(drives_str, wm_context[:120])
            self.working_memory.push(
                content="反思[{0}]: {1}".format(drives_str, wm_context[:180]),
                source="reflection",
                base_salience=0.35,
            )
        else:
            if sm.last_reflection:
                self.state.inner_monologue = "Idle: {0}".format(sm.last_reflection[:120])

        # ── Periodic deep self-reflection (every ~20 min by default) ──
        if (DEEP_REFLECTION_ENABLED and
                self.state.total_ticks > 0 and
                self.state.total_ticks % DEEP_REFLECTION_INTERVAL_TICKS == 0):
            await self._deep_self_reflection()

        # ── v5.2 Metacognition insight — inject into monologue ──
        meta_insight = self.metacognition.get_insight()
        if meta_insight and "良好" not in meta_insight:
            self.working_memory.push(
                content="[元认知] {0}".format(meta_insight[:120]),
                source="metacognition",
                base_salience=0.45,
            )

        # ── v5.2 Self-improvement goal from metacognition ──
        improvement = self.metacognition.get_self_improvement_goal()
        if improvement:
            self.working_memory.push(
                content="[自我改进] {0}".format(improvement["description"][:100]),
                source="metacognition",
                base_salience=0.5,
            )

        # ── V7 Goal Generation: via DriveEngine + GoalGenerator ──
        if (self.state.total_ticks > 0 and
                self.state.total_ticks % (DEEP_REFLECTION_INTERVAL_TICKS // 2) == 0):
            self._tick_drive_engine(self.state.activation)

        # Push reflection event
        try:
            self._output_feed.put_nowait({
                "type": "reflection",
                "thought": wm_context[:200] if wm_context else sm.last_reflection[:200],
                "emotion": self.state.current_emotion,
                "top_drives": [d["label"] for d in top_drives],
                "identity_version": sm.identity_version,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except asyncio.QueueFull:
            pass

        # Reset cingulate errors
        if self.state.error_count > 0:
            self.cingulate.acknowledge()
            self.state.error_count = 0
            self.state.conflict_detected = False


    async def _deep_self_reflection(self):
        """Deeper self-reflection using LLM — identity check-in."""
        sm = self.state.self_model
        wm = self.working_memory.get_context()

        ctx = sm.prepare_reflection_prompt(
            working_memory=wm,
            recent_events=sm.self_narrative[-10:] if sm.self_narrative else [],
        )

        try:
            from services.llm_client import get_llm
            llm = get_llm()
            result = await llm.chat_json(
                system="""[SYSTEM CONSTRAINT]
You are the SELF-REFLECTION module of a brain memory agent.
You examine the state of the brain and produce a brief self-reflection.
You are NOT giving advice. You are the brain thinking about itself.
Output JSON only:
{
  "thought": "a brief self-observation — max 100 chars",
  "mood_tendency": "balanced|curious|cautious|excited|tired",
  "trait_shift": {"add": "optional new trait name"} or null,
  "drive_shifts": {"curiosity": 0.01, "coherence": -0.01} or {}
}
Rules:
- Only suggest trait_shift if a genuinely new trait has emerged (rare).
- Drive shifts should be tiny (+-0.02 max).
- Never fabricate events.
- Keep thoughts authentic, sometimes uncertain.""",
                user=ctx,
                temperature=0.4,
                max_tokens=512,
            )
            sm.integrate_reflection(result)

            # ── V7.1: 从所有身份记忆中重新合成 identity_anchor ──
            if self.memory_store:
                id_mems = self.memory_store.get_identity_memories(20)
                if id_mems:
                    await sm.synthesize_anchor(llm, id_mems)

            # ── v5.1: Apply goal feedback to self-model ──
            goal_feedback = self.goal_system.get_feedback_for_self_model()
            if goal_feedback:
                for drive_name, delta in goal_feedback.items():
                    sm.update_drive(drive_name, delta)
                logger.debug("brain-stem: goal feedback applied to drives — %s", goal_feedback)

            logger.debug("brain-stem: deep self-reflection completed")
        except Exception as e:
            logger.debug("brain-stem: deep self-reflection skipped: %s", str(e)[:60])

    def _tick_drive_engine(self, activation):
        """V7: DriveEngine tick — 评估信号 → 更新驱动力 → 生成候选目标。

        长期任务的分层、容量和执行顺序由 ``task_scheduler`` 统一负责。
        """
        # 1. 构建状态快照
        state_snap = build_state_snapshot_for_drive_engine(self)
        state_snap["memories_archived"] = self._last_archived_count

        # 2. 评估信号，更新 ActivationField 中的驱动力维度
        self.drive_engine.tick(activation, state_snap, self.state.total_ticks)

        # 3. 用 GoalGenerator 生成新目标
        sm = self.state.self_model
        recent_entities = (
            self.state.curiosity.exploration_topics[-5:]
            if self.state.curiosity.exploration_topics else
            sm.identity_traits
        )
        curiosity_qs = self.state.curiosity.open_questions[-5:]
        memory_count = self.memory_store.count() if self.memory_store else 0

        new_goals = self.goal_generator.generate(
            activation=activation,
            identity_traits=sm.identity_traits,
            recent_entities=recent_entities,
            curiosity_questions=curiosity_qs,
            memory_count=memory_count,
            current_tick=self.state.total_ticks,
        )

        # 4. 注册到长期任务队列。GoalGenerator 是基于驱动的候选生成器，
        # 不是持久化 owner；任务层负责分级、去重和有限容量。
        self.task_scheduler.sync(self.goal_system, self.state.total_ticks)
        existing = {
            (getattr(goal, "drive", ""), getattr(goal, "description", "")[:160])
            for goal in self.goal_system.get_active()
        }
        for g in new_goals:
            fingerprint = (getattr(g, "drive", ""), getattr(g, "description", "")[:160])
            # Always let the policy layer make the capacity decision.  In
            # particular, a full queue may still accept a maintenance/user
            # task by evicting its lowest-priority exploration item.
            if fingerprint in existing:
                continue
            registered = self.task_scheduler.register_goal(
                self.goal_system,
                g,
                current_tick=self.state.total_ticks,
                source="drive_engine",
            )
            if not registered:
                continue
            self.drive_engine.total_goals_generated += 1
            existing.add(fingerprint)
            self.working_memory.push(
                content="[V7目标] {0}".format(g.description[:80]),
                source="drive_engine",
                base_salience=0.4,
            )

        # 5. 选择在下一次安全边界执行的任务；不再让旧 V7 scheduler
        # 直接 abandon 队列中的低层级任务。
        self.task_scheduler.sync(self.goal_system, self.state.total_ticks)

    def _generate_goals_from_state(self):
        """v5.1 向后兼容——委托给 V7 DriveEngine。"""
        activation = self.state.activation
        self._tick_drive_engine(activation)

    def _goal_to_intent(
        self,
        goal,
        episode_id: str | None = None,
        attempt_no: int = 0,
    ) -> Intent | None:
        """v5.1: Convert a goal into a CALL_TOOL intent."""
        from brain.intent import IntentType

        # Map goal drive to appropriate tool
        tool_map = {
            "curiosity": ("web_search", {"query": goal.description[:100]}),
            "coherence": ("memory_search", {"query": goal.description[:100]}),
            "growth": ("memory_search", {"query": goal.description[:100]}),
            "connection": ("memory_search", {"query": "最近的对话和未完成的事项"}),
            "self_preservation": ("memory_search", {"query": "身份变化 核心认知"}),
        }

        tool_name, tool_args = tool_map.get(goal.drive, ("memory_search", {"query": goal.description[:100]}))

        return Intent(
            type=IntentType.CALL_TOOL,
            tool_name=tool_name,
            tool_args=tool_args,
            confidence=goal.priority * 0.8,
            reason="goal-driven: {0}".format(goal.description[:60]),
            source_input=goal.description[:200],
            episode_id=episode_id,
            goal_id=getattr(goal, "id", None),
            attempt_no=max(0, int(attempt_no or 0)),
            origin="autonomous",
            created_tick=self.state.total_ticks,
        )

    async def _snapshot_state(self):
        """Persist brain state. V6: includes ActivationField."""
        if self.state_store:
            # Keep the compact BrainState fields and the lossless working
            # memory view in sync immediately before journaling.
            self.state.sleep_state = self.sleep_state
            self.state.active_thoughts = [dict(item) for item in self.working_memory.items]
            self.state.current_context = self.working_memory.get_context()
            if self.autonomy:
                self._sync_autonomy_projection()
            self.task_scheduler.sync(self.goal_system, self.state.total_ticks)
            snap = self.state.snapshot()  # State.snapshot() already includes activation
            snap["working_memory"] = self.working_memory.snapshot()
            snap["last_archived_count"] = self._last_archived_count
            snap["maintenance"] = {
                "last_dream_time": self.last_dream_time,
                "last_consolidation_time": self.last_consolidation_time,
                "last_reflection": self.last_reflection,
                "last_snapshot": self.last_snapshot,
                "last_decay": self.last_decay,
            }
            snap["goal_system"] = self.goal_system.snapshot()
            snap["task_scheduler"] = self.task_scheduler.snapshot(self.goal_system)
            snap["metacognition"] = self.metacognition.snapshot()
            snap["emotional_spectrum"] = self.emotional_spectrum.snapshot()
            snap["procedural_memory"] = self.procedural_memory.snapshot()
            snap["time_sense"] = self.time_sense.snapshot()
            snap["exploration_queue"] = self.exploration_queue.snapshot()
            snap["exploration_executor"] = {
                "cycles_completed": self.exploration_executor.cycles_completed,
                "total_issues_found": self.exploration_executor.total_issues_found,
            }
            snap["reflection_engine"] = self.reflection_engine.snapshot()
            snap["drive_engine"] = self.drive_engine.snapshot()
            if self.autonomy:
                snap["autonomy"] = self.autonomy.snapshot()
            snap["thalamus"] = {
                "last_input": self.thalamus.last_input,
                "noise_discarded": self.thalamus.noise_discarded,
                "total_relayed": self.thalamus.total_relayed,
            }
            snap["amygdala"] = {
                "valence": self.amygdala.valence,
                "arousal": self.amygdala.arousal,
                "dominance": self.amygdala.dominance,
                "salience": self.amygdala.salience,
            }
            if self.predictive_layer:
                snap["predictive_layer"] = self.predictive_layer.snapshot()
            if self.cognitive_dispatch:
                snap["cognitive_dispatch"] = self.cognitive_dispatch.snapshot()
            if self.boredom_engine:
                snap["boredom_engine"] = self.boredom_engine.snapshot()
            if self.social_emotion:
                snap["social_emotion"] = self.social_emotion.snapshot()
            if self.attachment_system:
                snap["attachment_system"] = self.attachment_system.snapshot()
            if self.reward_system:
                snap["reward_system"] = self.reward_system.snapshot()
            if self.autobiography:
                snap["autobiography"] = self.autobiography.snapshot()
            if self.boundary:
                snap["boundary"] = self.boundary.snapshot()
            saved = self.state_store.save(snap)
            if saved is False:
                self._record_loop_error("snapshot_persist", "state store rejected snapshot")
                return False
            logger.debug("brain-stem: state snapshot saved (V6 activation: %d dims)",
                         len(snap.get("activation", {}).get("values", {})))
            return True
        return False


    def _update_sleep_state(self):
        """Update sleep state based on ticks since last input. V9: considers boredom."""
        t = self.state.ticks_since_input

        # V9: 极度无聊时抗拒深睡 — 先找事做再考虑睡觉
        if self.boredom_engine and self.boredom_engine.should_resist_sleep(t):
            # 保持 drowsy 或 light_sleep，不进入 deep_sleep
            if t >= LIGHT_SLEEP_THRESHOLD_TICKS:
                self.sleep_state = "light_sleep"
            elif t >= DROWSY_THRESHOLD_TICKS:
                self.sleep_state = "drowsy"
            else:
                self.sleep_state = "awake"
            self.state.sleep_state = self.sleep_state
            return

        if t >= DEEP_SLEEP_THRESHOLD_TICKS:
            self.sleep_state = "deep_sleep"
        elif t >= LIGHT_SLEEP_THRESHOLD_TICKS:
            self.sleep_state = "light_sleep"
        elif t >= DROWSY_THRESHOLD_TICKS:
            self.sleep_state = "drowsy"
        else:
            self.sleep_state = "awake"
        self.state.sleep_state = self.sleep_state

    async def _dream_tick(self):
        """Generate a dream during sleep."""
        if not self.memory_store:
            return

        wm_snapshot = [item["content"][:100] for item in self.working_memory.get_top(3)]
        dream = await self.dream_engine.dream(self.memory_store, wm_snapshot)

        if dream:
            self.state.inner_monologue = "[{0}] {1}".format(
                self.sleep_state, dream.get("narrative", "")[:200]
            )
            self.working_memory.push(
                "Dream: " + dream.get("narrative", "")[:200],
                "dream",
                dream.get("significance", 0.3),
            )
            # Push dream to output feed
            try:
                self._output_feed.put_nowait({
                    "type": "dream",
                    "sleep_state": self.sleep_state,
                    "dream_title": dream.get("dream_title"),
                    "narrative": dream.get("narrative", "")[:200],
                    "emotional_tone": dream.get("emotional_tone"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            except asyncio.QueueFull:
                pass

    async def _consolidation_tick(self):
        """Memory consolidation during sleep — uses v3 pipeline."""
        if not self.memory_store:
            return

        result = await run_consolidation(self.memory_store, self.memory_store.db_path)
        if result["consolidated"]:
            logger.info("brain-stem: consolidation complete — %d changes, %d clusters",
                       result["changes"], result["compressed_clusters"])

    def _build_output_event(
        self,
        thalamus_out: dict,
        amygdala_out: dict,
        hippocampus_out: dict | None,
        cingulate_out: dict,
        dmn_out: dict | None,
    ) -> dict | None:
        """Build an output event for external agents to consume."""
        if not thalamus_out.get("has_input"):
            return None

        return {
            "type": "brain_tick",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "focus": self.state.focus_entity,
            "emotion": self.state.current_emotion,
            "salience": amygdala_out.get("salience", 0.0),
            "priority": "normal",  # no longer tracked via prefrontal
            "memory_hits": len(hippocampus_out.get("results", [])) if hippocampus_out else 0,
            "conflict": cingulate_out.get("conflict_detected", False),
            "inner_monologue": self.state.inner_monologue[:200],
            "working_memory": self.state.current_context[:200],
            "active_goals": len(self.goal_system.get_active()),
            "cognitive_load": round(self.metacognition.cognitive_load, 2),
            "emotional_expression": self.emotional_spectrum.get_expression(),
        }
