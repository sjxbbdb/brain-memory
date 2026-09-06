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
from unittest.mock import AsyncMock

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

    def test_v13_binding_is_monotonic_and_plan_lane_cannot_switch(self):
        manager = AutonomyEpisode()
        record = manager.begin(
            "goal-binding",
            "绑定不可篡改",
            task_id="plan-a",
            tick=1,
        )
        self.assertTrue(manager.plan(
            record.id,
            "call_tool",
            "memory_search",
            tick=1,
            intent_id="intent-a",
            plan_id="plan-a",
            step_id="step-a",
            action_id="action-a",
        ))
        self.assertFalse(manager.bind_action(
            record.id,
            plan_id="plan-b",
            step_id="step-b",
            action_id="action-b",
            tick=2,
        ))
        self.assertEqual(record.task_id, "plan-a")
        self.assertEqual(record.plan_id, "plan-a")
        self.assertEqual(record.step_id, "step-a")
        self.assertEqual(record.action_id, "action-a")

        self.assertTrue(manager.action_started(record.id, tick=2))
        self.assertTrue(manager.record_feedback(
            record.id,
            True,
            tool_name="memory_search",
            intent_id="intent-a",
            plan_id="plan-a",
            step_id="step-a",
            action_id="action-a",
            tick=3,
        ))
        # A sequential step may advance within the same immutable plan lane.
        self.assertTrue(manager.plan(
            record.id,
            "call_tool",
            "file_read",
            tick=4,
            intent_id="intent-b",
            plan_id="plan-a",
            step_id="step-b",
            action_id="action-b",
        ))
        self.assertEqual(record.plan_id, "plan-a")
        self.assertEqual(record.step_id, "step-b")


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

    async def test_late_feedback_after_abort_or_timeout_does_not_mutate_ledger(self):
        for close_mode in ("timeout", "abort"):
            with self.subTest(close_mode=close_mode):
                with tempfile.TemporaryDirectory() as temp_dir:
                    db_path = str(Path(temp_dir) / f"{close_mode}.db")
                    init_db(db_path)
                    state_store = StateStore(db_path)
                    memory_store = MemoryStore(db_path)
                    stem = BrainStem(state_store, memory_store)
                    try:
                        goal = Goal(
                            id=f"goal-{close_mode}",
                            drive="growth",
                            description="迟到反馈不得改账本",
                            priority=0.8,
                            deadline_ticks=100,
                            status=GoalStatus.ACTIVE,
                        )
                        stem.goal_system._goals.append(goal)
                        plan = stem.task_execution.create_plan(
                            goal.description,
                            goal_id=goal.id,
                            step_specs=[
                                {
                                    "id": f"step-{close_mode}",
                                    "description": "核对记忆",
                                    "tool_name": "memory_search",
                                    "criteria": {"any_fields": ["results"]},
                                }
                            ],
                        )
                        record = stem.autonomy.begin(
                            goal.id,
                            goal.description,
                            task_id=plan.id,
                            tick=1,
                        )
                        stem.autonomy.plan(
                            record.id,
                            "call_tool",
                            "memory_search",
                            tick=1,
                            intent_id=f"intent-{close_mode}",
                        )
                        step = plan.steps[0]
                        action = stem.task_execution.record_action(
                            plan.id,
                            step.id,
                            tool_name="memory_search",
                            tick=1,
                            episode_id=record.id,
                        )
                        stem.autonomy.action_started(record.id, tick=2)
                        before_observations = len(stem.task_execution.observations)
                        before_outcomes = len(stem.task_execution.outcomes)
                        before_status = stem.task_execution.actions[action.id].status

                        if close_mode == "timeout":
                            stem.autonomy.tick(record.last_tick + stem.autonomy.max_ticks)
                        else:
                            stem.autonomy.abort("手动中止", tick=3)

                        accepted = stem._record_autonomy_feedback(
                            {
                                "episode_id": record.id,
                                "goal_id": goal.id,
                                "plan_id": plan.id,
                                "step_id": step.id,
                                "action_id": action.id,
                                "intent_id": f"intent-{close_mode}",
                                "text": "[记忆搜索结果] late",
                                "tool_observation": {
                                    "summary": "迟到结果",
                                    "data": {"results": [{"title": "late"}]},
                                    "provenance": {"verified": True},
                                },
                            },
                            success=True,
                            tool_name="memory_search",
                        )
                        self.assertFalse(accepted)
                        self.assertEqual(
                            len(stem.task_execution.observations),
                            before_observations,
                        )
                        self.assertEqual(len(stem.task_execution.outcomes), before_outcomes)
                        self.assertEqual(
                            stem.task_execution.actions[action.id].status,
                            before_status,
                        )
                        self.assertEqual(stem.autonomy.total_orphan_feedback, 1)
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

    async def test_write_tool_requires_explicit_operator_approval(self):
        stem = BrainStem()
        tools = ToolRegistry()
        executions = 0

        async def write_note(args, context):
            nonlocal executions
            executions += 1
            return json.dumps({"ok": True}, ensure_ascii=False)

        tools.register(ToolDef(
            name="write_note",
            description="write tool that must remain gated",
            schema={},
            call=write_note,
            is_read_only=False,
        ))
        bridge = AgentBridge(stem, tools)

        goal = Goal(
            id="goal-write-approval",
            drive="growth",
            description="写操作需要审批",
            priority=0.8,
            deadline_ticks=100,
            status=GoalStatus.ACTIVE,
        )
        stem.goal_system._goals.append(goal)
        record = stem.autonomy.begin(goal.id, goal.description, tick=1)
        stem.autonomy.plan(
            record.id,
            "call_tool",
            "write_note",
            tick=1,
            intent_id="intent-write",
        )

        await bridge._handle_call_tool(Intent(
            type=IntentType.CALL_TOOL,
            tool_name="write_note",
            episode_id=record.id,
            goal_id=goal.id,
            intent_id="intent-write",
            origin="autonomous",
        ))

        self.assertEqual(executions, 0)
        self.assertEqual(bridge.total_tools_executed, 0)
        self.assertIsNotNone(stem.autonomy.active)
        self.assertEqual(stem.autonomy.active.status, EpisodeStatus.AWAITING_FEEDBACK)
        self.assertEqual(stem.autonomy.active.tool_name, "write_note")

    async def test_write_capability_flag_still_requires_one_shot_action_approval(self):
        stem = BrainStem()
        tools = ToolRegistry()
        executions = 0

        async def write_note(args, context):
            nonlocal executions
            executions += 1
            return json.dumps({"ok": True}, ensure_ascii=False)

        tools.register(ToolDef(
            name="write_note",
            description="one-shot approved write",
            schema={},
            call=write_note,
            is_read_only=False,
        ))
        bridge = AgentBridge(stem, tools, allow_write_tools=True)

        async def sink(*args, **kwargs):
            return True

        bridge._feed_brain = sink
        intent = Intent(
            type=IntentType.CALL_TOOL,
            tool_name="write_note",
            intent_id="intent-write-once",
        )

        # The global capability only enables the approval mechanism; it is
        # never blanket authorization for all writes.
        await bridge._handle_call_tool(intent)
        self.assertEqual(executions, 0)
        self.assertTrue(
            bridge.approve_write_action(intent.intent_id, "write_note")
        )
        await bridge._handle_call_tool(intent)
        self.assertEqual(executions, 1)
        self.assertEqual(bridge.snapshot()["pending_write_approvals"], 0)

        # A duplicate delivery cannot reuse the consumed capability.
        await bridge._handle_call_tool(intent)
        self.assertEqual(executions, 1)

    async def test_write_approval_is_bound_to_arguments_and_collision_cannot_evict_it(self):
        stem = BrainStem()
        tools = ToolRegistry()

        async def write_note(args, context):
            return json.dumps({"ok": True}, ensure_ascii=False)

        tools.register(ToolDef(
            name="write_note",
            description="argument-bound write",
            schema={},
            call=write_note,
            is_read_only=False,
        ))
        bridge = AgentBridge(stem, tools, allow_write_tools=True)
        bridge._max_write_approvals = 1

        self.assertTrue(
            bridge.approve_write_action(
                "fixed-action", "write_note", {"path": "a.txt"}
            )
        )
        # A reused correlation key with a different immutable request is a
        # collision.  It must not evict/replace the original capability even
        # when the bounded approval map is full.
        self.assertFalse(
            bridge.approve_write_action(
                "fixed-action", "write_note", {"path": "b.txt"}
            )
        )
        self.assertEqual(bridge.snapshot()["pending_write_approvals"], 1)

    def _make_v13_write_lane(self, *, tool_name="write_note", args=None):
        """Create one fully correlated, not-yet-dispatched V13 write action."""
        stem = BrainStem()
        tools = ToolRegistry()
        calls = []
        args = dict(args or {"value": "original"})

        async def write_tool(received_args, context):
            calls.append(dict(received_args))
            return json.dumps({"ok": True}, ensure_ascii=False)

        tools.register(ToolDef(
            name=tool_name,
            description="V13 approval test write tool",
            schema={},
            call=write_tool,
            is_read_only=False,
        ))
        bridge = AgentBridge(stem, tools, allow_write_tools=True)
        # Keep this test focused on the dispatch gate.  The strict bridge path
        # still validates all four causal identifiers before reaching it.
        bridge._feed_brain = AsyncMock(return_value=True)

        goal = Goal(
            id="goal-v13-write-approval",
            drive="growth",
            description="验证写操作逐项审批",
            priority=0.8,
            deadline_ticks=100,
            status=GoalStatus.ACTIVE,
        )
        stem.goal_system._goals.append(goal)
        plan = stem.task_execution.create_plan(
            goal.description,
            goal_id=goal.id,
            plan_id="plan-v13-write-approval",
            step_specs=[{
                "id": "step-v13-write-approval",
                "description": "执行一项受审批保护的写操作",
                "tool_name": tool_name,
            }],
        )
        episode = stem.autonomy.begin(
            goal.id,
            goal.description,
            tick=1,
            task_id=plan.id,
        )
        step = stem.task_execution.next_step(plan_id=plan.id, current_tick=1)
        action = stem.task_execution.record_action(
            plan.id,
            step.id,
            action_id="action-v13-write-approval",
            action_type="call_tool",
            tool_name=tool_name,
            args=args,
            tick=1,
            episode_id=episode.id,
        )
        intent_id = "intent-v13-write-approval"
        self.assertTrue(stem.autonomy.plan(
            episode.id,
            "call_tool",
            tool_name,
            tick=1,
            intent_id=intent_id,
            plan_id=plan.id,
            step_id=step.id,
            action_id=action.id,
        ))
        self.assertTrue(stem.autonomy.bind_action(
            episode.id,
            plan_id=plan.id,
            step_id=step.id,
            action_id=action.id,
            tick=1,
        ))
        intent = Intent(
            type=IntentType.CALL_TOOL,
            tool_name=tool_name,
            tool_args=args,
            episode_id=episode.id,
            goal_id=goal.id,
            intent_id=intent_id,
            plan_id=plan.id,
            step_id=step.id,
            action_id=action.id,
            origin="autonomous",
        )
        return stem, bridge, intent, calls, action

    async def test_write_approval_rejects_changed_tool_and_arguments_and_duplicate(self):
        """A one-shot capability is exact, consumed once, and non-replayable."""
        # Use the legacy/free-form path here so each assertion isolates the
        # approval map itself from the separate V13 causal-correlation gate.
        stem = BrainStem()
        tools = ToolRegistry()
        calls = []

        async def write_a(args, context):
            calls.append(("write_a", dict(args)))
            return json.dumps({"ok": True})

        async def write_b(args, context):
            calls.append(("write_b", dict(args)))
            return json.dumps({"ok": True})

        for name, handler in (("write_a", write_a), ("write_b", write_b)):
            tools.register(ToolDef(
                name=name,
                description="approval mutation test tool",
                schema={},
                call=handler,
                is_read_only=False,
            ))
        bridge = AgentBridge(stem, tools, allow_write_tools=True)
        bridge._feed_brain = AsyncMock(return_value=True)

        args = {"path": "safe.txt"}
        original = Intent(
            type=IntentType.CALL_TOOL,
            tool_name="write_a",
            tool_args=args,
            intent_id="intent-approval-exact",
        )
        self.assertTrue(bridge.approve_write_action(
            original.intent_id, "write_a", args=args
        ))

        changed_args = Intent(
            type=IntentType.CALL_TOOL,
            tool_name="write_a",
            tool_args={"path": "changed.txt"},
            intent_id=original.intent_id,
        )
        await bridge._handle_call_tool(changed_args)
        self.assertEqual(calls, [])
        self.assertEqual(bridge.snapshot()["pending_write_approvals"], 1)

        changed_tool = Intent(
            type=IntentType.CALL_TOOL,
            tool_name="write_b",
            tool_args=args,
            intent_id=original.intent_id,
        )
        await bridge._handle_call_tool(changed_tool)
        self.assertEqual(calls, [])
        self.assertEqual(bridge.snapshot()["pending_write_approvals"], 1)

        await bridge._handle_call_tool(original)
        self.assertEqual(calls, [("write_a", args)])
        self.assertEqual(bridge.snapshot()["pending_write_approvals"], 0)

        # The exact same intent is now a duplicate delivery and cannot reuse
        # the consumed one-shot capability.
        await bridge._handle_call_tool(original)
        self.assertEqual(calls, [("write_a", args)])

    async def test_v13_write_intent_with_any_mismatched_correlation_is_blocked(self):
        """A write never crosses the bridge when any causal ID is stale."""
        fields = (
            ("goal_id", "goal-v13-write-other"),
            ("plan_id", "plan-v13-write-other"),
            ("step_id", "step-v13-write-other"),
            ("action_id", "action-v13-write-other"),
            ("intent_id", "intent-v13-write-other"),
        )
        for field, replacement in fields:
            with self.subTest(field=field):
                stem, bridge, intent, calls, _ = self._make_v13_write_lane()
                setattr(intent, field, replacement)
                # Approval is deliberately present: correlation is an
                # independent safety boundary and must fail before dispatch.
                self.assertTrue(bridge.approve_write_action(
                    intent.action_id, intent.tool_name, args=intent.tool_args
                ))
                await bridge._handle_call_tool(intent)
                self.assertEqual(calls, [])
                self.assertEqual(bridge.total_tools_executed, 0)
                self.assertEqual(
                    bridge.snapshot()["pending_write_approvals"], 1,
                )

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
