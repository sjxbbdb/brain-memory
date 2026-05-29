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
    LLM_EMOTION_BLEND_RATIO,
)

logger = logging.getLogger("brain-v4.brain-stem")


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
            if self.sleep_state != "awake":
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
        """One tick of consciousness."""

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
            # Set goal from input (v4.1 fix: was never extracted before)
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

            # Track gate result for API response (v4.1)
            self.state.last_input_gated = not gate["passed"]
            self.state.last_input_accepted = gate["passed"]
            self.state.last_error = ""

            # Unified LLM call: encoding + focus + monologue (only for external input, only if gate passed)
            if is_external and gate["passed"]:
                try:
                    from services.llm_client import get_llm
                    from services.llm_prompts import UNIFIED_TICK_PROMPT
                    llm = get_llm()
                    ctx = json.dumps({
                        "text": thalamus_out["text"][:3000],
                        "goal": self.state.current_goal or "none",
                        "emotion": amygdala_out.get("emotion_label", "neutral"),
                        "salience": amygdala_out.get("salience", 0.0),
                        "recent_thoughts": self.working_memory.get_context()[:300],
                    }, ensure_ascii=False)
                    unified = await llm.chat_json(system=UNIFIED_TICK_PROMPT, user=ctx, temperature=0.1, max_tokens=1024)

                    # ── LLM Emotion (replaces keyword-based amygdala for this tick) ──
                    llm_emotion = unified.get("emotion", {})
                    if llm_emotion:
                        # Blend LLM emotion with existing state (smooth transition, no jump)
                        v = float(llm_emotion.get("valence", 0.5))
                        a = float(llm_emotion.get("arousal", 0.5))
                        d = float(llm_emotion.get("dominance", 0.5))
                        u = float(llm_emotion.get("urgency", 0.0))
                        label = llm_emotion.get("label", "neutral")

                        # Update amygdala internals (so next tick doesn"t revert to keywords)
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
                            "valence": round(self.amygdala.valence, 3),
                            "arousal": round(self.amygdala.arousal, 3),
                            "dominance": round(self.amygdala.dominance, 3),
                            "urgency": u,
                            "salience": round(self.amygdala.salience, 3),
                        }
                        amygdala_out["salience"] = self.amygdala.salience
                        self.state.emotion_vector.update(amygdala_out["emotion_vector"])
                        self.state.current_emotion = label

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

                        # ── Identity memory: mark if this triggered a self-model shift ──
                        if shift and self.memory_store:
                            mem["is_identity_forming"] = True
                            mem["importance"] = max(mem["importance"], 0.75)
                            # Update the stored memory
                            try:
                                self.memory_store.save(mem)  # re-save with identity flag
                                logger.debug("brain-stem: identity-forming memory marked")
                            except Exception:
                                pass

                        # ── Curiosity: check if this resolves any open questions ──
                        entities = mem.get("entities", [])
                        resolved = self.state.curiosity.check_resolution(thalamus_out["text"], entities)
                        if resolved:
                            for rq in resolved:
                                self.working_memory.push(
                                    content="问题已解答: {0}".format(rq["question"][:100]),
                                    source="curiosity",
                                    salience=0.6,
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
                    
                    logger.debug("brain-stem: unified LLM tick — encode+focus+monologue in one call")
                except Exception as e:
                    err_msg = str(e)[:200]
                    logger.warning("brain-stem: unified LLM call failed: %s", err_msg)
                    self.state.last_error = err_msg
                    self.state.llm_error_count += 1
                    self.state.last_input_accepted = False
            
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
            self.working_memory.push(content=wm_content, source=thalamus_out["source"], salience=amygdala_out.get("salience", 0.5))
        if self.state.inner_monologue:
            self.working_memory.push(content=self.state.inner_monologue, source="inner_monologue", salience=0.3)
        self.working_memory.tick()
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
            # Save working memory by copying items (v4.1 fix: copy, not reference swap)
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
                salience=0.35,
            )
        else:
            if sm.last_reflection:
                self.state.inner_monologue = "Idle: {0}".format(sm.last_reflection[:120])

        # ── Periodic deep self-reflection (every ~5 min) ──
        if self.state.total_ticks % DEEP_REFLECTION_INTERVAL_TICKS == 0:
            await self._deep_self_reflection()

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
            logger.debug("brain-stem: deep self-reflection completed")
        except Exception as e:
            logger.debug("brain-stem: deep self-reflection skipped: %s", str(e)[:60])

    async def _snapshot_state(self):
        """Persist brain state."""
        if self.state_store:
            self.state_store.save(self.state.snapshot())
            logger.debug("brain-stem: state snapshot saved")


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
        }
