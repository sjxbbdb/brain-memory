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
import json
import logging
from datetime import datetime, timezone
from typing import Any

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
from brain.brain_state import BrainState
from brain.self_model import SelfModel
from brain.intent import Intent, IntentQueue
from brain.goal_system import GoalSystem
from brain.metacognition import Metacognition
from brain.emotional_spectrum import EmotionalSpectrum
from brain.procedural_memory import ProceduralMemory
from brain.time_sense import TimeSense
from brain.drive_engine import DriveEngine, GoalGenerator, GoalScheduler, build_state_snapshot_for_drive_engine  # V7
from brain.exploration import ExplorationQueue, ExplorationExecutor  # V8
from brain.reflection_engine import ReflectionEngine  # V8
from brain.core_purpose import core_purpose  # V8
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
)

logger = logging.getLogger("brain-v8.brain-stem")


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
        self._task: asyncio.Task | None = None
        self._pending_input: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._input_processed = asyncio.Event()

        # Output feed — external agents consume this
        self._output_feed: asyncio.Queue = asyncio.Queue(maxsize=100)

        # Intent queue — brain produces intents, agent layer consumes (v5.0)
        self.intent_queue: IntentQueue = IntentQueue()

        # Goal system — v5.1: brain sets its own goals
        self.goal_system = GoalSystem()

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

        # Stats
        self.start_time = datetime.now(timezone.utc)
        self.state.total_ticks = 0
        self.last_reflection = 0.0
        self.last_snapshot = 0.0
        self.last_decay = 0.0

    # ── Public API ──

    async def start(self):
        """Start the consciousness loop."""
        logger.info("brain-stem: consciousness loop starting")
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop())
        self.start_time = datetime.now(timezone.utc)

        # Restore state if available
        if self.state_store:
            restored = self.state_store.load_latest()
            if restored:
                self.state = BrainState.from_snapshot(restored)
                logger.info("brain-stem: state restored from snapshot")

    async def stop(self):
        """Stop the consciousness loop."""
        logger.info("brain-stem: stopping consciousness loop")
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        # Final snapshot
        await self._snapshot_state()

    async def receive_input(self, text: str, source: str = "external", goal: str | None = None):
        """Receive input from an external agent. Pushes to input queue."""
        try:
            await self._pending_input.put({
                "text": text,
                "source": source,
                "goal": goal,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            logger.debug("brain-stem: input queued from %s", source)
        except asyncio.QueueFull:
            logger.warning("brain-stem: input queue full, dropping input")

    async def get_output(self) -> dict | None:
        """Get the latest output event (non-blocking)."""
        try:
            return self._output_feed.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def get_state(self) -> dict:
        """Get current brain state snapshot."""
        return self.state.snapshot()

    # ── Main Loop ──

    async def _loop(self):
        """The consciousness loop — runs until stop_event is set."""
        logger.info("brain-stem: loop started")

        while not self._stop_event.is_set():
            tick_start = datetime.now(timezone.utc)

            try:
                await self._tick()
            except Exception as e:
                logger.exception("brain-stem: tick error: %s", str(e)[:120])

            self.state.total_ticks += 1
            self.state.uptime_seconds = (datetime.now(timezone.utc) - self.start_time).total_seconds()

            # Periodic tasks
            now_ts = datetime.now(timezone.utc).timestamp()

            # Sleep-time dream + consolidation
            if self.sleep_state != "awake" and DREAM_ENABLED:
                if now_ts - self.last_dream_time >= DREAM_INTERVAL_SEC:
                    self.last_dream_time = now_ts
                    await self._dream_tick()
                if now_ts - self.last_consolidation_time >= CONSOLIDATION_INTERVAL_SEC:
                    self.last_consolidation_time = now_ts
                    await self._consolidation_tick()

            # Reflection (every REFLECTION_INTERVAL_SEC)
            if now_ts - self.last_reflection >= REFLECTION_INTERVAL_SEC:
                self.last_reflection = now_ts
                await self._reflection_tick()
                logger.debug("brain-stem: reflection tick")

            # Memory decay (every MEMORY_DECAY_INTERVAL_TICKS ticks)
            if self.state.total_ticks % MEMORY_DECAY_INTERVAL_TICKS == 0 and self.state.total_ticks > 0:
                self.last_decay = now_ts
                if self.memory_store:
                    result = self.memory_store.decay_all(
                        decay_rate=MEMORY_DECAY_RATE,
                        archive_threshold=MEMORY_ARCHIVE_THRESHOLD,
                    )
                    if result.get("archived", 0) > 0:
                        logger.info("brain-stem: archived %d decayed memories", result["archived"])

            # State snapshot (every STATE_SNAPSHOT_INTERVAL_SEC)
            if now_ts - self.last_snapshot >= STATE_SNAPSHOT_INTERVAL_SEC:
                self.last_snapshot = now_ts
                await self._snapshot_state()

            # Sleep until next tick
            elapsed = (datetime.now(timezone.utc) - tick_start).total_seconds()
            sleep_time = max(0.0, TICK_INTERVAL_SEC - elapsed)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_time)
                break  # stop_event set
            except asyncio.TimeoutError:
                pass  # normal tick cycle

        logger.info("brain-stem: loop stopped after %d ticks", self.state.total_ticks)

    async def _tick(self):
        """One tick of consciousness. V6: ActivationField drives state dynamics."""

        # ── V6: ActivationField tick — 状态扩散 + 基线回归 ──
        activation = self.state.activation
        activation.tick(dt=1.0)

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
        try:
            input_data = self._pending_input.get_nowait()
            self.state.ticks_since_input = 0
            # ── Session: switch to this source's session ──
            source_id = input_data.get("source", "default")
            session = self.state.session_manager.get(source_id)
            from datetime import datetime as _dt, timezone
            session.last_active = _dt.now(timezone.utc).isoformat()
            # Restore per-source state as the active context
            self.state.emotion_vector.update(session.emotion_vector)
            self.state.current_emotion = session.current_emotion
            self.state.focus_entity = session.focus_entity
            self.state.inner_monologue = session.inner_monologue
            # Set goal from input (v5.0 fix: was never extracted before)
            self.state.current_goal = input_data.get("goal") or None
            # Restore working memory from session (copy contents, don't replace reference)
            if session.working_memory.get_context():
                self.working_memory.items = [dict(it) for it in session.working_memory.items]
            # Wake on input
            if self.sleep_state != "awake":
                logger.info("brain-stem: input received, waking from %s", self.sleep_state)
                self.sleep_state = "awake"
        except asyncio.QueueEmpty:
            self.state.ticks_since_input += 1
            input_data = None

        # ── Step 2: Thalamus — sensory relay ──
        inner_signal = self.default_mode.get_recent_thoughts(1)
        inner_text = inner_signal[0] if inner_signal else None

        thalamus_out = self.thalamus.relay(
            input_text=input_data["text"] if input_data else None,
            inner_signal=inner_text,
        )

        # ── Step 3: Amygdala — emotion ──
        if thalamus_out["has_input"] and not thalamus_out["discarded"]:
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

        # ── Step 4-8: Unified LLM Tick (encode + focus + monologue in ONE call) ──
        hippocampus_out = None
        encoded_memory = None
        is_external = thalamus_out.get("source") == "external"
        dmn_out = None

        # Reset input tracking at start of each tick
        self.state.last_input_accepted = False
        self.state.last_input_gated = True
        
        if thalamus_out["has_input"] and not thalamus_out["discarded"]:
            # Hippocampus retrieval (embedding — doesn't use LLM chat)
            hippocampus_out = await self.hippocampus.retrieve(
                query=thalamus_out["text"],
                top_k=5,
            )
            
            # Gate check: should we process this?
            # ── Apply self-model attention bias ──
            attn_boost = self.state.self_model.attention_weight(thalamus_out["text"])
            attn_importance = min(0.5 + attn_boost * 0.1, 1.0)

            gate = gate_check(
                text=thalamus_out["text"],
                importance=attn_importance,
                novelty=0.5,
                goal_relevance=GATE_GOAL_RELEVANCE_WITH_GOAL if self.state.current_goal else GATE_GOAL_RELEVANCE_DEFAULT,
                explicit_mark=amygdala_out.get("salience", 0) > 0.7,
            )

            # Track gate result for API response (v5.0)
            self.state.last_input_gated = not gate["passed"]
            self.state.last_input_accepted = gate["passed"]
            self.state.last_error = ""

            # Unified LLM call: encoding + focus + monologue (only for external input, only if gate passed)
            if is_external and gate["passed"]:
                try:
                    from services.llm_client import get_llm
                    from services.llm_prompts import UNIFIED_TICK_PROMPT
                    llm = get_llm()

                    # ── Build tool context so the brain KNOWS its capabilities (v5.0) ──
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
                        pass  # agent layer not loaded, tools unavailable

                    # ── Build embodiment-aware context ──
                    # ── V7.1: 注入身份记忆到 LLM 上下文 ──
                    id_mems = self.memory_store.get_identity_memories(5) if self.memory_store else []
                    id_context = self.state.self_model.identity_memories_context(id_mems, max_items=3)

                    ctx_obj = {
                        "text": thalamus_out["text"][:3000],
                        "goal": self.state.current_goal or "none",
                        "emotion": amygdala_out.get("emotion_label", "neutral"),
                        "salience": amygdala_out.get("salience", 0.0),
                        "recent_thoughts": self.working_memory.get_context()[:300],
                        "identity": self.state.self_model.identity_anchor[:300],
                        "identity_memories": id_context,  # V7.1: 身份记忆
                        "top_drives": [
                            d["label"] for d in self.state.self_model.get_top_drives(2)
                        ],
                    }
                    if tools_summary:
                        ctx_obj["available_tools"] = tools_summary
                    # v5.4: inject temporal context + procedural memory hints
                    ctx_obj["temporal_context"] = self.time_sense.get_short_temporal_context()
                    skill_hint = self.procedural_memory.get_skill_suggestion(thalamus_out["text"])
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
                    if is_external and mem.get("importance", 0) > 0:
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

            # ── v5.1 Goal System: tick active goals, produce intent if actionable ──
            if self.state.ticks_since_input % 15 == 0 and self.sleep_state == "awake":
                active_goal = self.goal_system.tick_goals(self.state.total_ticks)
                if active_goal and active_goal.status == "active":
                    # Convert goal to a CALL_TOOL intent if we have tools to execute it
                    goal_intent = self._goal_to_intent(active_goal)
                    if goal_intent:
                        self.state.last_intent = goal_intent.to_dict()
                        self.state.intent_count += 1
                        await self.intent_queue.put(goal_intent)
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
            if input_data and is_external:
                self.time_sense.record_event("input", thalamus_out.get("text", "")[:80])

        # ── v5.2: detect tool result inputs (agent feedback loop) and feed outcomes ──
        if input_data and input_data.get("source", "").startswith("agent/tool/"):
            # Tool result came back — if text doesn't contain error, treat as success
            text_lower = (input_data.get("text", "") or "").lower()
            is_error = any(kw in text_lower for kw in ["失败", "error", "错误", "exception", "traceback"])
            success = not is_error
            self.metacognition.feed_outcome(success, 0.6)
            # v5.4: record experience for procedural memory
            tool = input_data.get("source", "").replace("agent/tool/", "")
            self.procedural_memory.record_experience("call_tool", tool, success, input_data.get("text", "")[:200])

        # ── v5.3: 情感光谱 tick（每个 tick 漂移一步）──
        self.emotional_spectrum.tick()

        # ── v5.4: 时间感 tick ──
        has_recent_activity = input_data is not None or self.state.ticks_since_input < 10
        self.time_sense.tick(has_recent_activity, self.state.total_ticks, self.state.uptime_seconds)

        # ── v5.2: update cognitive load ──
        has_external_input = input_data is not None and not (input_data.get("source", "") or "").startswith("agent/")
        self.metacognition.update_cognitive_load(
            llm_called=(is_external and thalamus_out.get("has_input") and not thalamus_out.get("discarded", True)),
            input_processed=has_external_input,
            recent_input_count=min(10, self.state.intent_count),
            activation=activation,  # V6: 同步 fatigue/uncertainty/confidence 到 ActivationField
        )

        # ── Step 6: Basal Ganglia — habit match ──
        habit_out = None
        if thalamus_out["has_input"]:
            habit_out = self.basal_ganglia.match(thalamus_out["text"])
            if habit_out and habit_out.get("matched"):
                self.state.active_habit = habit_out["habit"]["id"]
                self.state.habit_confidence = habit_out["confidence"]

        # ── Step 7: Cingulate — conflict monitor ──
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
        if thalamus_out["has_input"] and not thalamus_out["discarded"]:
            wm_content = thalamus_out["text"][:500]
            if encoded_memory and is_external:
                wm_content = "[记忆: {0}] {1}".format(encoded_memory.get("title", ""), wm_content[:400])
            self.working_memory.push(content=wm_content, source=thalamus_out["source"], base_salience=amygdala_out.get("salience", 0.5))
        if self.state.inner_monologue:
            self.working_memory.push(content=self.state.inner_monologue, source="inner_monologue", base_salience=0.3)
        self.working_memory.tick(activation=activation)  # V6: SalienceScore竞争保留
        self.state.current_context = self.working_memory.get_context()

        # ── Session sync: save per-source state ──
        if input_data:
            source_id = input_data.get("source", "default")
            session = self.state.session_manager.get(source_id)
            session.current_emotion = self.state.current_emotion
            session.emotion_vector = dict(self.state.emotion_vector)
            session.focus_entity = self.state.focus_entity
            session.inner_monologue = self.state.inner_monologue
            session.current_context = self.state.current_context
            # Save working memory by copying items (v5.0 fix: copy, not reference swap)
            session.working_memory.items = [dict(it) for it in self.working_memory.items]
        
        # Signal input processed (always, even if discarded)
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

        # ── V7 Goal Generation: via DriveEngine + GoalGenerator + GoalScheduler ──
        if (self.state.total_ticks > 0 and
                self.state.total_ticks % (DEEP_REFLECTION_INTERVAL_TICKS // 2) == 0):
            self._tick_drive_engine(activation)

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
        """V7: DriveEngine tick — 评估信号 → 更新驱动力 → 生成目标 → 调度排序。"""
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

        # 4. 注册到 goal_system 并调度排序
        for g in new_goals:
            self.goal_system._goals.append(g)
            self.goal_system.total_generated += 1
            self.drive_engine.total_goals_generated += 1
            self.working_memory.push(
                content="[V7目标] {0}".format(g.description[:80]),
                source="drive_engine",
                base_salience=0.4,
            )

        # 5. GoalScheduler 排序 + 自动取消低分目标
        all_active = self.goal_system.get_active()
        if all_active:
            self.goal_scheduler.schedule(all_active, activation, max_active=3)

    def _generate_goals_from_state(self):
        """v5.1 向后兼容——委托给 V7 DriveEngine。"""
        activation = self.state.activation
        self._tick_drive_engine(activation)

    def _goal_to_intent(self, goal) -> Intent | None:
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
        )

    async def _snapshot_state(self):
        """Persist brain state. V6: includes ActivationField."""
        if self.state_store:
            snap = self.state.snapshot()  # State.snapshot() already includes activation
            snap["goal_system"] = self.goal_system.snapshot()
            snap["metacognition"] = self.metacognition.snapshot()
            snap["emotional_spectrum"] = self.emotional_spectrum.snapshot()
            snap["procedural_memory"] = self.procedural_memory.snapshot()
            snap["time_sense"] = self.time_sense.snapshot()
            snap["exploration_queue"] = self.exploration_queue.snapshot()
            snap["reflection_engine"] = self.reflection_engine.snapshot()
            self.state_store.save(snap)
            logger.debug("brain-stem: state snapshot saved (V6 activation: %d dims)",
                         len(snap.get("activation", {}).get("values", {})))


    def _update_sleep_state(self):
        """Update sleep state based on ticks since last input."""
        t = self.state.ticks_since_input
        if t >= DEEP_SLEEP_THRESHOLD_TICKS:
            self.sleep_state = "deep_sleep"
        elif t >= LIGHT_SLEEP_THRESHOLD_TICKS:
            self.sleep_state = "light_sleep"
        elif t >= DROWSY_THRESHOLD_TICKS:
            self.sleep_state = "drowsy"
        else:
            self.sleep_state = "awake"

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
