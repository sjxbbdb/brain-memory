"""Acceptance tests for the bounded autonomous episode loop (V11).

The tests use a local read-only tool instead of a network provider.  They
verify the causal contract: a goal produces one correlated intent, the bridge
returns feedback, and the brain closes the episode into goal/reward/memory
state without requiring an API key.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agent.tool_registry import ToolDef, ToolRegistry
from agent_bridge import AgentBridge
from brain.autonomy import AutonomyEpisode, EpisodeStatus
from brain.brain_stem import BrainStem
from brain.goal_system import Goal, GoalStatus, GoalSystem
from brain.intent import Intent, IntentType
from storage.database import MemoryStore, StateStore, init_db


class AutonomyEpisodeTests(unittest.TestCase):
    def test_goal_feedback_is_applied_once_across_restart(self):
        goals = GoalSystem()
        completed = Goal(
            id="goal-feedback-once",
            drive="growth",
            description="只反馈一次",
            priority=0.8,
            deadline_ticks=10,
            status=GoalStatus.DONE,
        )
        goals._history.append(completed)

        self.assertEqual(goals.get_feedback_for_self_model(), {"growth": 0.02})
        self.assertIsNone(goals.get_feedback_for_self_model())
        restored = GoalSystem.from_snapshot(
            json.loads(json.dumps(goals.snapshot(), ensure_ascii=False))
        )
        self.assertIsNone(restored.get_feedback_for_self_model())

    def test_feedback_quality_distinguishes_simulation(self):
        self.assertEqual(
            BrainStem._classify_autonomy_feedback(
                "[工具说明] 搜索功能需配置搜索引擎 API Key", True
            ),
            "simulated",
        )
        self.assertEqual(
            BrainStem._classify_autonomy_feedback("[搜索结果] local", True),
            "verified",
        )
        self.assertEqual(BrainStem._classify_autonomy_feedback("error", False), "failed")

    def test_lifecycle_snapshot_and_correlation(self):
        manager = AutonomyEpisode(max_history=2, max_events=4, max_ticks=10)
        record = manager.begin(
            goal_id="goal-1",
            goal="检查本地连续性",
            drive="coherence_drive",
            tick=3,
        )
        self.assertEqual(record.status, EpisodeStatus.PLANNED)
        self.assertTrue(manager.plan(
            record.id,
            "call_tool",
            tool_name="memory_search",
            tick=3,
            intent_id="intent-1",
        ))
        self.assertTrue(manager.action_started(record.id, tick=4))
        self.assertFalse(manager.record_feedback(
            None,
            True,
            tool_name="memory_search",
            intent_id="intent-1",
            tick=5,
        ))
        self.assertFalse(manager.record_feedback(
            record.id,
            True,
            tool_name="memory_search",
            intent_id="other-intent",
            tick=5,
        ))
        self.assertFalse(manager.record_feedback(
            record.id,
            True,
            tool_name="web_search",
            intent_id="intent-1",
            tick=5,
        ))
        self.assertEqual(manager.total_orphan_feedback, 3)
        self.assertTrue(manager.record_feedback(
            record.id,
            True,
            tool_name="memory_search",
            intent_id="intent-1",
            tick=5,
            result_summary="命中一条记忆",
        ))
        closed = manager.complete("反馈已吸收", tick=6, reward=0.8)
        self.assertIsNotNone(closed)
        self.assertEqual(closed.status, EpisodeStatus.COMPLETED)
        self.assertEqual(manager.total_completed, 1)

        # Snapshot must be JSON-safe and preserve the causal history.
        restored = AutonomyEpisode.from_snapshot(json.loads(json.dumps(manager.snapshot())))
        self.assertIsNone(restored.active)
        self.assertEqual(restored.last_closed.id, record.id)
        self.assertEqual(restored.history[-1].intent_id, "intent-1")
        self.assertEqual(restored.history[-1].status, EpisodeStatus.COMPLETED)

    def test_timeout_rejects_late_feedback(self):
        manager = AutonomyEpisode(max_ticks=10)
        record = manager.begin("goal-timeout", "会超时", tick=0)
        manager.plan(record.id, "call_tool", "memory_search", tick=0, intent_id="i-timeout")
        self.assertIsNone(manager.tick(9))
        closed = manager.tick(10)
        self.assertIsNotNone(closed)
        self.assertEqual(closed.status, EpisodeStatus.FAILED)
        self.assertFalse(manager.record_feedback(
            record.id,
            True,
            tool_name="memory_search",
            intent_id="i-timeout",
            tick=11,
        ))


class BrainAutonomyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unrelated_legacy_feedback_cannot_close_active_episode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "orphan.db")
            init_db(db_path)
            state_store = StateStore(db_path)
            memory_store = MemoryStore(db_path)
            stem = BrainStem(state_store, memory_store)
            try:
                record = stem.autonomy.begin(
                    "goal-orphan",
                    "只接受匹配工具的反馈",
                    tick=1,
                )
                self.assertTrue(stem.autonomy.plan(
                    record.id,
                    "call_tool",
                    "memory_search",
                    tick=1,
                    intent_id="intent-orphan",
                ))
                self.assertTrue(stem.autonomy.action_started(record.id, tick=2))

                accepted = stem._record_autonomy_feedback(
                    {
                        "source": "agent/tool/web_search",
                        "text": "[搜索结果] unrelated",
                    },
                    success=True,
                    tool_name="web_search",
                )
                self.assertFalse(accepted)
                self.assertEqual(
                    stem.autonomy.active.status,
                    EpisodeStatus.AWAITING_FEEDBACK,
                )
                self.assertEqual(stem.autonomy.total_orphan_feedback, 1)

                # The legacy no-ID path remains compatible only when the
                # observed tool matches the action currently in flight.
                self.assertTrue(stem._record_autonomy_feedback(
                    {
                        "source": "agent/tool/memory_search",
                        "text": "[记忆搜索结果] matched",
                    },
                    success=True,
                    tool_name="memory_search",
                ))
                self.assertEqual(
                    stem.autonomy.active.status,
                    EpisodeStatus.FEEDBACK_RECEIVED,
                )
            finally:
                state_store.close()
                memory_store.close()

    async def test_followup_tool_intent_keeps_episode_open_for_next_step(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "multistep.db")
            init_db(db_path)
            state_store = StateStore(db_path)
            memory_store = MemoryStore(db_path)
            stem = BrainStem(state_store, memory_store)
            try:
                record = stem.autonomy.begin(
                    "goal-multistep",
                    "先检索记忆，再验证外部线索",
                    tick=10,
                )
                stem.autonomy.plan(
                    record.id,
                    "call_tool",
                    "memory_search",
                    tick=10,
                    intent_id="intent-step-1",
                )
                stem.autonomy.action_started(record.id, tick=11)
                stem.autonomy.record_feedback(
                    record.id,
                    True,
                    tool_name="memory_search",
                    tick=12,
                    intent_id="intent-step-1",
                )
                stem.state.total_ticks = 12
                followup = Intent(
                    type=IntentType.CALL_TOOL,
                    tool_name="web_search",
                    tool_args={"query": "continuity"},
                    reason="对记忆线索做第二步核验",
                    episode_id=record.id,
                    goal_id=record.goal_id,
                    intent_id="intent-step-2",
                    origin="autonomous",
                )

                self.assertTrue(stem._observe_autonomy_intent(followup))
                self.assertIsNotNone(stem.autonomy.active)
                self.assertEqual(
                    stem.autonomy.active.status,
                    EpisodeStatus.AWAITING_ACTION,
                )
                self.assertEqual(stem.autonomy.active.step_count, 2)
                self.assertEqual(stem.autonomy.active.tool_name, "web_search")
                self.assertEqual(stem.autonomy.active.intent_id, "intent-step-2")
            finally:
                state_store.close()
                memory_store.close()

    async def test_bridge_fails_closed_when_action_claim_cannot_be_recorded(self):
        stem = BrainStem()
        record = stem.autonomy.begin(
            "goal-claim-error",
            "无法记账时不得执行工具",
            tick=1,
        )
        stem.autonomy.plan(
            record.id,
            "call_tool",
            "safe_probe",
            tick=1,
            intent_id="intent-claim-error",
        )
        executions = 0
        tools = ToolRegistry()

        async def safe_probe(args, context):
            nonlocal executions
            executions += 1
            return "ok"

        tools.register(ToolDef(
            name="safe_probe",
            description="must not run when the episode claim fails",
            schema={},
            call=safe_probe,
            is_read_only=True,
        ))
        bridge = AgentBridge(stem, tools)
        original_action_started = stem.autonomy.action_started

        def fail_claim(*args, **kwargs):
            raise RuntimeError("claim ledger unavailable")

        stem.autonomy.action_started = fail_claim
        try:
            await bridge._handle_call_tool(Intent(
                type=IntentType.CALL_TOOL,
                tool_name="safe_probe",
                episode_id=record.id,
                goal_id=record.goal_id,
                intent_id="intent-claim-error",
                origin="autonomous",
            ))
        finally:
            stem.autonomy.action_started = original_action_started

        self.assertEqual(executions, 0)
        self.assertIsNone(stem.autonomy.active)
        self.assertEqual(stem.autonomy.last_closed.status, EpisodeStatus.FAILED)
        self.assertIn("拒绝执行", stem.autonomy.last_closed.outcome)

    async def test_bridge_closes_episode_and_persists_outcome(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "autonomy.db")
            init_db(db_path)
            state_store = StateStore(db_path)
            memory_store = MemoryStore(db_path)
            stem = BrainStem(state_store, memory_store)
            goal = Goal(
                id="goal-integration",
                drive="coherence",
                description="核对本地连续性",
                priority=0.9,
                deadline_ticks=150,
                status=GoalStatus.ACTIVE,
            )
            stem.goal_system._goals.append(goal)

            tools = ToolRegistry()

            async def local_memory_search(args, context):
                return json.dumps({
                    "results": [{"title": "连续性记录"}],
                }, ensure_ascii=False)

            tools.register(ToolDef(
                name="memory_search",
                description="local read-only test tool",
                schema={},
                call=local_memory_search,
                is_read_only=True,
            ))
            bridge = AgentBridge(stem, tools)

            # Tick 31 with 14 idle ticks reaches the goal-action boundary but
            # avoids the periodic goal generator, keeping this test focused.
            stem.state.total_ticks = 31
            stem.state.ticks_since_input = 14
            await stem._tick()
            self.assertEqual(stem.intent_queue.qsize(), 1)
            intent = await stem.intent_queue.get(timeout=0.1)
            self.assertIsNotNone(intent)
            self.assertEqual(intent.episode_id, stem.autonomy.active_id)
            self.assertEqual(intent.goal_id, goal.id)
            self.assertEqual(stem.autonomy.active.status, EpisodeStatus.AWAITING_ACTION)

            # A second idle tick must not emit a duplicate while this episode
            # is still waiting for the bridge.
            stem.state.total_ticks = 46
            stem.state.ticks_since_input = 29
            await stem._tick()
            self.assertEqual(stem.intent_queue.qsize(), 0)
            self.assertIsNotNone(stem.autonomy.active)

            await bridge._handle_call_tool(intent)
            self.assertEqual(stem.autonomy.active.status, EpisodeStatus.AWAITING_FEEDBACK)

            stem.state.total_ticks = 47
            await stem._tick()
            self.assertIsNone(stem.autonomy.active)
            self.assertEqual(stem.autonomy.last_closed.status, EpisodeStatus.COMPLETED)
            self.assertEqual(goal.status, GoalStatus.DONE)
            self.assertGreaterEqual(memory_store.count(), 1)
            self.assertIsNotNone(stem.autonomy.last_closed.reward)
            self.assertEqual(stem.autonomy.last_closed.result_quality, "verified")
            self.assertAlmostEqual(stem.autonomy.last_closed.reward, 0.8)
            self.assertEqual(stem.reward_system.total_rewards, 1)
            self.assertEqual(stem.state.loop_error_count, 0)

            # The closed episode remains available after a durable restart.
            self.assertTrue(await stem._snapshot_state())
            restored = BrainStem(StateStore(db_path), MemoryStore(db_path))
            snapshot = state_store.load_latest()
            restored._restore_snapshot(snapshot)
            self.assertEqual(
                restored.autonomy.last_closed.id,
                stem.autonomy.last_closed.id,
            )
            self.assertEqual(restored.autonomy.total_completed, 1)

            state_store.close()
            memory_store.close()
            restored.state_store.close()
            restored.memory_store.close()

    async def test_restart_does_not_replay_unconfirmed_action(self):
        """An in-flight tool is interrupted rather than executed twice."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "restart.db")
            init_db(db_path)
            state_store = StateStore(db_path)
            memory_store = MemoryStore(db_path)
            stem = BrainStem(state_store, memory_store)
            goal = Goal(
                id="goal-restart",
                drive="coherence",
                description="不要重放未确认行动",
                priority=0.8,
                deadline_ticks=150,
                status=GoalStatus.ACTIVE,
            )
            stem.goal_system._goals.append(goal)
            record = stem.autonomy.begin(goal.id, goal.description, tick=10)
            stem.autonomy.plan(
                record.id,
                "call_tool",
                tool_name="memory_search",
                tick=10,
                intent_id="intent-restart",
            )
            stem.state.total_ticks = 10
            self.assertTrue(await stem._snapshot_state())
            state_store.close()
            memory_store.close()

            restored = BrainStem(StateStore(db_path), MemoryStore(db_path))
            await restored.start()
            self.assertIsNone(restored.autonomy.active)
            self.assertEqual(
                restored.autonomy.last_closed.status,
                EpisodeStatus.ABORTED,
            )
            self.assertIn("重启", restored.autonomy.last_closed.outcome)
            self.assertEqual(
                restored.goal_system.get_by_id(goal.id).status,
                GoalStatus.ACTIVE,
            )
            await restored.stop()
            restored.state_store.close()
            restored.memory_store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
